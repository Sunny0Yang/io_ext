#define _GNU_SOURCE

#include <stdio.h>

#include "q_wedf.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/*
 * d2d4_bm tune (2026-11-10): q_wedf policy constants exposed as CLI knobs.
 * The knob names are a hard contract (the yml generator references them
 * verbatim): --read-deadline-ns, --write-deadline-ns, --window-ns,
 * --expire-guard-ns, --weights-enable.
 *
 * The defaults below MUST mirror the .rodata initializers in
 * q_wedf.bpf.c (read 50000000 / write 500000000 / window 5000000 /
 * expire-guard 1000000 / weights-enable 0). The loader always writes
 * the parsed value into skel->rodata before load, so an unknobbed start
 * reproduces the original compile-time behavior exactly.
 *
 * --weights-enable 0/1 (default 0): 1 turns on the weighted-EDF law —
 * the tenant weight registered via -W/--arb-weight into the pinned
 * policy_meta map is then read by q_wedf.bpf.c at insert time and
 * scales each request's deadline (D = base * 100 / w). With 0 (or with
 * all weights equal to the default 100) the scheduling behavior is
 * identical to the historical unweighted q_wedf.
 */
enum {
    Q_WEDF_OPT_READ_DEADLINE_NS = 1000,
    Q_WEDF_OPT_WRITE_DEADLINE_NS,
    Q_WEDF_OPT_WINDOW_NS,
    Q_WEDF_OPT_EXPIRE_GUARD_NS,
    Q_WEDF_OPT_WEIGHTS_ENABLE,
};

#define Q_WEDF_DEFAULT_READ_DEADLINE_NS   50000000ULL
#define Q_WEDF_DEFAULT_WRITE_DEADLINE_NS 500000000ULL
#define Q_WEDF_DEFAULT_WINDOW_NS           5000000ULL
#define Q_WEDF_DEFAULT_EXPIRE_GUARD_NS     1000000ULL
#define Q_WEDF_DEFAULT_WEIGHTS_ENABLE              0U

static __u64 wedf_read_deadline_ns = Q_WEDF_DEFAULT_READ_DEADLINE_NS;
static __u64 wedf_write_deadline_ns = Q_WEDF_DEFAULT_WRITE_DEADLINE_NS;
static __u64 wedf_window_ns = Q_WEDF_DEFAULT_WINDOW_NS;
static __u64 wedf_expire_guard_ns = Q_WEDF_DEFAULT_EXPIRE_GUARD_NS;
static __u32 wedf_weights_enable = Q_WEDF_DEFAULT_WEIGHTS_ENABLE;

static int wedf_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case Q_WEDF_OPT_READ_DEADLINE_NS:
        wedf_read_deadline_ns = iosched_loader_parse_u64(arg,
                                              Q_WEDF_DEFAULT_READ_DEADLINE_NS);
        return 0;
    case Q_WEDF_OPT_WRITE_DEADLINE_NS:
        wedf_write_deadline_ns = iosched_loader_parse_u64(arg,
                                             Q_WEDF_DEFAULT_WRITE_DEADLINE_NS);
        return 0;
    case Q_WEDF_OPT_WINDOW_NS:
        wedf_window_ns = iosched_loader_parse_u64(arg, Q_WEDF_DEFAULT_WINDOW_NS);
        return 0;
    case Q_WEDF_OPT_EXPIRE_GUARD_NS:
        wedf_expire_guard_ns = iosched_loader_parse_u64(arg,
                                              Q_WEDF_DEFAULT_EXPIRE_GUARD_NS);
        return 0;
    case Q_WEDF_OPT_WEIGHTS_ENABLE:
        wedf_weights_enable = iosched_loader_parse_u32(arg,
                                              Q_WEDF_DEFAULT_WEIGHTS_ENABLE);
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct q_wedf_bpf *skel;
    const struct option extra_opts[] = {
        { "read-deadline-ns", required_argument, NULL, Q_WEDF_OPT_READ_DEADLINE_NS },
        { "write-deadline-ns", required_argument, NULL, Q_WEDF_OPT_WRITE_DEADLINE_NS },
        { "window-ns", required_argument, NULL, Q_WEDF_OPT_WINDOW_NS },
        { "expire-guard-ns", required_argument, NULL, Q_WEDF_OPT_EXPIRE_GUARD_NS },
        { "weights-enable", required_argument, NULL, Q_WEDF_OPT_WEIGHTS_ENABLE },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage("q_wedf",
                        "      --read-deadline-ns U64    read deadline ns (default: 50000000)\n"
                        "      --write-deadline-ns U64   write deadline ns (default: 500000000)\n"
                        "      --window-ns U64           strict-EDF window ns (default: 5000000)\n"
                        "      --expire-guard-ns U64     expire telemetry guard ns (default: 1000000)\n"
                        "      --weights-enable U32       weighted-EDF from -W weights, 0/1 (default: 0)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, wedf_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_wedf_bpf__open();
    if (!skel)
        return 1;

    skel->rodata->wedf_read_deadline_ns = wedf_read_deadline_ns;
    skel->rodata->wedf_write_deadline_ns = wedf_write_deadline_ns;
    skel->rodata->wedf_window_ns = wedf_window_ns;
    skel->rodata->wedf_expire_guard_ns = wedf_expire_guard_ns;
    skel->rodata->wedf_weights_enable = (__u8)!!wedf_weights_enable;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_wedf_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_wedf, BPF_F_LINK);
    if (q_wedf_bpf__load(skel)) {
        q_wedf_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_wedf_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_wedf,
                                               "q_wedf",
                                               args.cgroup,
                                               owner_token);
    q_wedf_bpf__destroy(skel);
    return ret;
}
