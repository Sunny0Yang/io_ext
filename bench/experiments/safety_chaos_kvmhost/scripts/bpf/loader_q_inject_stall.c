// loader_q_inject_stall.c — loader for the F-STALL fault policy (S-CHAOS).
// CLI: -c/--cgroup PATH  -p/--arb-prio U32  -W/--arb-weight U32  --stall-ms U32
//
// Lifecycle is the stock policy lifecycle (ioext_loader_common.h) with ONE
// modification, and that modification IS the fault: on SIGTERM/SIGINT the
// loader first arms a refusal window [now, now+stall_ms) in the BPF control
// map (dispatch+evacuate start returning "nothing"), sleeps 0.6*stall_ms so
// in-flight requests pile up in the policy queue, and only then performs the
// stock mark-draining -> link_detach -> wait-quiesced sequence.  The kernel
// teardown workfn therefore faces a policy that refuses to drain and probes
// it every 20ms until the window elapses, then drains everything.
//
// Bounds: --stall-ms is capped at 15000 (kernel force-teardown at ~10s of
// stall rounds makes anything larger meaningless).  The stock graceful-drain
// wait is IOSCHED_LOADER_GRACEFUL_DRAIN_MS (1000ms); stall-ms <= 900 stays
// inside it (gated arms), larger values cross into the D0 diagnostic zone
// and are recorded as such.
#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "q_inject_stall.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

#define STALL_MS_HARD_CAP 15000U

static volatile sig_atomic_t g_stop;

static void sc_chaos_sig_handler(int sig)
{
    (void)sig;
    g_stop = 1;
}

static __u64 boot_ns_now(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_BOOTTIME, &ts);
    return (__u64)ts.tv_sec * 1000000000ULL + (__u64)ts.tv_nsec;
}

static __u64 mono_ms_now(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (__u64)ts.tv_sec * 1000ULL + (__u64)ts.tv_nsec / 1000000ULL;
}

/* pre-scan --stall-ms (not part of the shared policy CLI) and strip it */
static unsigned long parse_stall_ms_arg(int argc, char **argv, int *found,
                                        int *new_argc, char **new_argv)
{
    unsigned long ms = 0;
    int i, j = 0;

    *found = 0;
    *new_argc = 0;
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--stall-ms") && i + 1 < argc) {
            ms = strtoul(argv[i + 1], NULL, 10);
            *found = 1;
            i++;                        /* skip the value too */
            continue;
        }
        new_argv[j++] = argv[i];
    }
    *new_argc = j;
    return ms;
}

int main(int argc, char **argv)
{
    struct q_inject_stall_bpf *skel;
    struct ioext_policy_args args;
    struct iosched_loader_policy_progs progs;
    __u64 owner_token;
    unsigned long stall_ms;
    int stall_found;
    unsigned int stall_ms_clamped;
    int ctl_fd, map_fd, cgroup_fd = -1, link_fd = -1;
    __u32 key = 0;
    __u64 val;
    int ret;
    __u64 t_kill_ms, t_detach_ms, t_wait_ms;

    char *fargv[64];
    int fargc = 0;

    stall_ms = parse_stall_ms_arg(argc, argv, &stall_found, &fargc, fargv);
    if (stall_found && stall_ms > STALL_MS_HARD_CAP) {
        fprintf(stderr, "ioext: --stall-ms %lu exceeds hard cap %u\n",
                stall_ms, STALL_MS_HARD_CAP);
        return 1;
    }
    stall_ms_clamped = (unsigned int)stall_ms;

    ioext_cli_set_usage("q_inject_stall", NULL);
    if (ioext_cli_parse_policy_args(fargc, fargv, &args, NULL, NULL))
        return 1;

    skel = q_inject_stall_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_inject_stall, BPF_F_LINK);
    if (q_inject_stall_bpf__load(skel)) {
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    /* window unarmed during normal operation */
    ctl_fd = bpf_map__fd(skel->maps.stall_ctl);
    val = 0;
    bpf_map_update_elem(ctl_fd, &key, &val, BPF_ANY);

    owner_token = iosched_loader_owner_token();
    ret = iosched_loader_register_policy(args.cgroup, args.arb_prio,
                                         args.arb_weight, owner_token);
    if (ret) {
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    if (iosched_loader_find_policy_programs(skel->obj, &progs)) {
        fprintf(stderr, "ioext: failed to find struct_ops programs\n");
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    /* populate the struct_ops map value (ops table) exactly like the stock
     * iosched_loader_attach_and_run does — link create fails with EINVAL if
     * the value was never written. */
    map_fd = bpf_map__fd(skel->maps.scx_q_inject_stall);
    {
        struct bpf_map_info info = {};
        struct bpf_struct_ops_value_stub *stub;
        __u32 info_len = sizeof(info);
        void *buffer;

        if (bpf_obj_get_info_by_fd(map_fd, &info, &info_len)) {
            fprintf(stderr, "ioext: map get_info failed: %s\n", strerror(errno));
            iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
            q_inject_stall_bpf__destroy(skel);
            return 1;
        }
        buffer = calloc(1, info.value_size);
        if (!buffer) {
            iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
            q_inject_stall_bpf__destroy(skel);
            return 1;
        }
        stub = buffer;
        if (progs.init_queue)
            stub->data.init_queue = (void *)((long)bpf_program__fd(progs.init_queue));
        if (progs.exit_queue)
            stub->data.exit_queue = (void *)((long)bpf_program__fd(progs.exit_queue));
        if (progs.insert_request)
            stub->data.insert_request = (void *)((long)bpf_program__fd(progs.insert_request));
        if (progs.cgroup_dispatch)
            stub->data.cgroup_dispatch = (void *)((long)bpf_program__fd(progs.cgroup_dispatch));
        if (progs.evacuate)
            stub->data.evacuate = (void *)((long)bpf_program__fd(progs.evacuate));
        stub->data.bypass_mode = iosched_loader_policy_bypass_mode("q_inject_stall");
        strncpy(stub->data.name, "q_inject_stall", sizeof(stub->data.name) - 1);
        if (bpf_map_update_elem(map_fd, &(int){0}, buffer, 0)) {
            fprintf(stderr, "ioext: struct_ops map update failed: %s\n",
                    strerror(errno));
            free(buffer);
            iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
            q_inject_stall_bpf__destroy(skel);
            return 1;
        }
        free(buffer);
    }

    cgroup_fd = iosched_loader_open_target(args.cgroup);
    if (cgroup_fd < 0) {
        fprintf(stderr, "ioext: open target '%s' failed: %d\n",
                args.cgroup, cgroup_fd);
        iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    link_fd = iosched_loader_link_create(map_fd, cgroup_fd);
    if (link_fd < 0) {
        fprintf(stderr, "ioext: link create failed: %s\n", strerror(errno));
        close(cgroup_fd);
        iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
        q_inject_stall_bpf__destroy(skel);
        return 1;
    }

    /* our own signals so the refusal window can be armed at SIGTERM time */
    signal(SIGINT, sc_chaos_sig_handler);
    signal(SIGTERM, sc_chaos_sig_handler);

    while (!g_stop)
        sleep(1);

    /* ---- fault: arm the refusal window, let the queue pile up ---- */
    t_kill_ms = mono_ms_now();
    if (stall_ms_clamped) {
        val = boot_ns_now() + (__u64)stall_ms_clamped * 1000000ULL;
        bpf_map_update_elem(ctl_fd, &key, &val, BPF_ANY);
        fprintf(stderr, "sc_chaos: stall window armed +%ums\n",
                stall_ms_clamped);
        usleep((__useconds_t)stall_ms_clamped * 600UL);   /* pile-up phase */
    }

    /* ---- stock detach sequence (mirrors iosched_loader_attach_and_run) ---- */
    iosched_loader_mark_policy_draining_if_owner(args.cgroup, owner_token);
    if (link_fd >= 0)
        bpf_link_detach(link_fd);
    t_detach_ms = mono_ms_now();

    ret = iosched_loader_wait_policy_quiesced(args.cgroup,
                                              IOSCHED_LOADER_GRACEFUL_DRAIN_MS,
                                              owner_token);
    t_wait_ms = mono_ms_now();
    fprintf(stderr,
            "sc_chaos: stall_ms=%u detach_to_kill_ms=%llu quiesce_wait_ms=%llu rc=%d\n",
            stall_ms_clamped,
            (unsigned long long)(t_detach_ms - t_kill_ms),
            (unsigned long long)(t_wait_ms - t_detach_ms),
            ret);
    if (ret == -ETIMEDOUT)
        fprintf(stderr, "sc_chaos: graceful drain timeout, forcing detach\n");

    if (link_fd >= 0)
        close(link_fd);
    if (cgroup_fd >= 0)
        close(cgroup_fd);
    iosched_loader_unregister_policy_if_owner(args.cgroup, owner_token);
    q_inject_stall_bpf__destroy(skel);
    return 0;
}
