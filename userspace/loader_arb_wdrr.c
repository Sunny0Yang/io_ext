/* loader_arb_wdrr.c - loader for the WDRR arbiter (STRICT by default)
 *
 * Wave-B patch 0004 (STALL_FORENSICS.md §6 / §7 L2, proposal
 * patches/0004-userspace-arb-wdrr-allow-fallback-build-mode.patch):
 * --allow-fallback 0|1.
 *
 * Default 0 (and the flag absent) = the exact current binary semantics:
 * the arbiter attaches with the compiled-in IOSCHED_ARB_STRICT
 * (arb_wdrr.bpf.c .arb_mode), i.e. the kernel's generic active-pd
 * fallback is unreachable whenever pick returns 0 while BPF-owed
 * requests exist (block/iosched_ext.c strict branch).
 *
 * 1 = attach with arb_mode=IOSCHED_ARB_ALLOW_FALLBACK: the same
 * field-proven BPF object, but the kernel may drain BPF-owed requests
 * through scx_io_pick_active_pd_locked round-robin during pick-0
 * windows — the ioext2-era semantics that kept every non-strict arm
 * (qonly / arb_noop) alive across 81+ ioext4 runs.  Fairness degenerates
 * to work-conserving round-robin ONLY during pick-0 windows; the wdrr
 * ledger (pending/deficit/outstanding) keeps charging normally.
 *
 * Implementation form (a) — runtime CLI, not a build mode or a second
 * struct_ops variant — because arb_mode is a per-attach DATA member of
 * the struct_ops value, not program logic: the attach path
 * (iosched_loader_arbiter_attach_and_run) already copies it from the
 * libbpf struct_ops value image into the kernel-visible value right
 * before link_create.  Patching that one field keeps the BPF object
 * (arb_wdrr.bpf.o + skeleton) byte-identical to the field-proven _fix/
 * _tune builds, lets ONE binary run both the L2 (ALLOW_FALLBACK) and
 * L4 (STRICT) ladder arms, and makes the mode an auditable loader-arg
 * in the scene YAML instead of a build flag that forks binaries.
 */
#define _GNU_SOURCE

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "arb_wdrr.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

#ifndef IOSCHED_ARB_ALLOW_FALLBACK
#define IOSCHED_ARB_ALLOW_FALLBACK 0ULL
#endif

enum {
    WDRR_OPT_ALLOW_FALLBACK = 1000,
    WDRR_OPT_DIAG_INTERVAL_MS = 1001,
};

static int wdrr_allow_fallback;
static __u32 wdrr_diag_interval_ms;

static int wdrr_parse_extra(__u32 opt, const char *arg)
{
    if (opt == WDRR_OPT_DIAG_INTERVAL_MS)
        return ioext_cli_parse_u32_strict(arg, &wdrr_diag_interval_ms);
    if (opt != WDRR_OPT_ALLOW_FALLBACK)
        return -1;
    /* Literal 0/1 only: a mistyped value must fail-stop, never silently
     * fall back to STRICT (the wedge shape) under an L2 banner. */
    if (arg && !strcmp(arg, "0")) {
        wdrr_allow_fallback = 0;
        return 0;
    }
    if (arg && !strcmp(arg, "1")) {
        wdrr_allow_fallback = 1;
        return 0;
    }
    return -1;
}

static int wdrr_apply_allow_fallback(struct arb_wdrr_bpf *skel)
{
    struct iosched_arbiter_ops *init_ops;
    size_t init_sz = 0;
    size_t need;

    if (!wdrr_allow_fallback)
        return 0;

    /*
     * iosched_loader_arbiter_attach_and_run() copies arb_mode from
     * exactly this image (bpf_map__initial_value() returns libbpf's
     * st_ops->data shadow — a writable heap buffer that exists from
     * object open until destroy) into the value it pushes with
     * bpf_map_update_elem() before link_create.  Patching it here is
     * the single decision point for the register-time arb_mode.
     */
    init_ops = bpf_map__initial_value(skel->maps.wdrr_arbiter, &init_sz);
    need = offsetof(struct iosched_arbiter_ops, arb_mode) +
           sizeof(init_ops->arb_mode);
    if (!init_ops || init_sz < need) {
        fprintf(stderr,
                "loader_arb_wdrr: --allow-fallback 1: struct_ops value image has "
                "no arb_mode member (got %zu bytes, need %zu) — refusing to attach\n",
                init_sz, need);
        return -1;
    }
    init_ops->arb_mode = IOSCHED_ARB_ALLOW_FALLBACK;
    fprintf(stderr,
            "loader_arb_wdrr: arb_mode=ALLOW_FALLBACK: kernel active-pd fallback "
            "reachable on pick-0\n");
    return 0;
}

int main(int argc, char **argv)
{
    const struct option extra_opts[] = {
        { "allow-fallback", required_argument, NULL, WDRR_OPT_ALLOW_FALLBACK },
        { "diag-interval-ms", required_argument, NULL, WDRR_OPT_DIAG_INTERVAL_MS },
        { NULL, 0, NULL, 0 },
    };
    struct iosched_arbiter_diag_maps diag;
    struct arb_wdrr_bpf *skel;
    int ret;

    if (ioext_cli_parse_arbiter_args_ex(argc, argv, NULL, extra_opts,
            "      --allow-fallback 0|1     1 = attach with arb_mode=ALLOW_FALLBACK: the\n"
            "                             kernel active-pd fallback stays reachable on\n"
            "                             pick-0 (wedge-repro ladder L2 arm). Default 0 =\n"
            "                             compiled-in STRICT.\n"
            "      --diag-interval-ms U32   every N ms dump the cgroup_drr /\n"
            "                             pending_total / outstanding ledger summary to\n"
            "                             stderr (plus one line at attach and one at\n"
            "                             detach). Default 0 = off. Ledger-drift\n"
            "                             observability for the S-decisive NULL-streak\n"
            "                             forensics (STALL_FORENSICS.md §3.4).\n",
            wdrr_parse_extra)) {
        fprintf(stderr, "loader_arb_wdrr: failed to parse args\n");
        return 1;
    }

    skel = arb_wdrr_bpf__open();
    if (!skel) {
        fprintf(stderr, "loader_arb_wdrr: failed to open skeleton\n");
        return 1;
    }

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        fprintf(stderr, "loader_arb_wdrr: setup_arbiter_maps failed: %d\n", ret);
        arb_wdrr_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.wdrr_arbiter, BPF_F_LINK);
    if (arb_wdrr_bpf__load(skel)) {
        fprintf(stderr, "loader_arb_wdrr: failed to load BPF skeleton\n");
        arb_wdrr_bpf__destroy(skel);
        return 1;
    }

    ret = wdrr_apply_allow_fallback(skel);
    if (ret) {
        fprintf(stderr, "loader_arb_wdrr: refusing to attach\n");
        arb_wdrr_bpf__destroy(skel);
        return 1;
    }

    memset(&diag, 0, sizeof(diag));
    diag.active_cgroup_ids = skel->maps.active_cgroup_ids;
    diag.policy_meta = skel->maps.policy_meta;
    diag.cgroup_drr = skel->maps.cgroup_drr;
    diag.cgroup_outstanding = skel->maps.cgroup_outstanding;
    diag.global_state = skel->maps.global_state;
    diag.hctx_state = skel->maps.hctx_state;
    diag.requeue_replay = skel->maps.requeue_replay;
    diag.interval_ms = wdrr_diag_interval_ms;

    fprintf(stderr, "loader_arb_wdrr: BPF loaded, attaching...\n");
    ret = iosched_loader_arbiter_attach_and_run_ex(skel->obj,
                                                   skel->maps.wdrr_arbiter,
                                                   &diag);
    fprintf(stderr, "loader_arb_wdrr: attach_and_run returned %d\n", ret);
    arb_wdrr_bpf__destroy(skel);
    return ret;
}
