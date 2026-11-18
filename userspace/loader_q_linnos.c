/* loader_q_linnos.c - userspace loader for the q_linnos offline-table
 * interception policy (LinnOS OSDI'20 homo reproduction: offline calibrated
 * (device-busy band x class) -> interception ratio lookup table).
 *
 * Policy-specific options (ioext_cli_args.h extra_opts mechanism):
 *   --ir-low  LC:BE   band0 (<50% busy) interception percent (default 0:0)
 *   --ir-mid  LC:BE   band1 (50-80%)     interception percent (default 0:20)
 *   --ir-high LC:BE   band2 (>80%)        interception percent (default 5:60)
 *   --busy-ref U64    in-flight count treated as 100% busy (default 64)
 *   --max-hold-us U64 held-request hold deadline (default 2000)
 *   --lc-prio-max U32 arb_prio below which a cgroup is LC (default 3)
 * Each ir value must be 0..100.  The table is loader-pushed before attach
 * and read-only afterwards (offline calibration, no online learning).
 */
#define _GNU_SOURCE

#include <stdio.h>

#include "q_linnos.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirrors of the BPF-side structs (q_linnos.bpf.c). */
struct q_linnos_cfg {
    __u64 busy_ref;
    __u64 max_hold_ns;
    __u32 ir[3][2];
    __u32 lc_prio_max;
    __u32 flags;
};

struct q_linnos_stats {
    __u64 inserts;
    __u64 inserts_by_band[3];
    __u64 inserts_by_class[2];
    __u64 intercepted[3][2];
    __u64 dispatch_fast;
    __u64 dispatch_held_timeout;
    __u64 dispatch_held_soft;
    __u64 evac_fast;
    __u64 evac_held;
    __u64 null_returns;
    __u64 push_fails;
    __u64 completions;
    __u64 inflight;
    __u64 enqueued_fast;
    __u64 enqueued_held;
    __u64 lost_fast;
    __u64 lost_held;
    __u32 seq[3][2];
};

struct q_linnos_state_entry {
    __u32 lock;   /* struct bpf_spin_lock mirror */
    __u32 pad;
    struct q_linnos_stats stats;
};

#define Q_LINNOS_OPT_IR_LOW   256
#define Q_LINNOS_OPT_IR_MID   257
#define Q_LINNOS_OPT_IR_HIGH  258
#define Q_LINNOS_OPT_BUSY_REF 259
#define Q_LINNOS_OPT_MAX_HOLD 260
#define Q_LINNOS_OPT_LC_PRIO  261

static struct q_linnos_cfg g_cfg = {
    .busy_ref = 64,
    .max_hold_ns = 2000ULL * 1000ULL,
    .ir = { {0, 0}, {0, 20}, {5, 60} },
    .lc_prio_max = 3,
};

/* Parse "LC:BE" percent pair, e.g. "5:60". */
static int q_linnos_parse_ir(const char *arg, __u32 ir[2])
{
    char *end = NULL;
    unsigned long lc, be;

    if (!arg)
        return -1;
    lc = strtoul(arg, &end, 10);
    if (end == arg || *end != ':' || lc > 100)
        return -1;
    arg = end + 1;
    be = strtoul(arg, &end, 10);
    if (end == arg || *end != '\0' || be > 100)
        return -1;
    ir[0] = (__u32)lc;
    ir[1] = (__u32)be;
    return 0;
}

static int q_linnos_extra_parse(__u32 opt, const char *arg)
{
    switch (opt) {
    case Q_LINNOS_OPT_IR_LOW:
        return q_linnos_parse_ir(arg, g_cfg.ir[0]);
    case Q_LINNOS_OPT_IR_MID:
        return q_linnos_parse_ir(arg, g_cfg.ir[1]);
    case Q_LINNOS_OPT_IR_HIGH:
        return q_linnos_parse_ir(arg, g_cfg.ir[2]);
    case Q_LINNOS_OPT_BUSY_REF:
        g_cfg.busy_ref = iosched_loader_parse_u64(arg, 64);
        return g_cfg.busy_ref ? 0 : -1;
    case Q_LINNOS_OPT_MAX_HOLD:
        g_cfg.max_hold_ns = iosched_loader_parse_u64(arg, 2000) * 1000ULL;
        return g_cfg.max_hold_ns ? 0 : -1;
    case Q_LINNOS_OPT_LC_PRIO:
        g_cfg.lc_prio_max = iosched_loader_parse_u32(arg, 3);
        return g_cfg.lc_prio_max ? 0 : -1;
    default:
        return -1;
    }
}

static int q_linnos_init_cfg_map(struct q_linnos_bpf *skel)
{
    __u32 key = 0;

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.q_linnos_cfg_map),
                            &key, &g_cfg, BPF_ANY))
        return -1;
    IOSCHED_LOADER_DBG("q_linnos table: ir[0]=%u:%u ir[1]=%u:%u ir[2]=%u:%u "
                       "busy_ref=%llu max_hold=%lluns lc_prio<%u\n",
                       g_cfg.ir[0][0], g_cfg.ir[0][1],
                       g_cfg.ir[1][0], g_cfg.ir[1][1],
                       g_cfg.ir[2][0], g_cfg.ir[2][1],
                       g_cfg.busy_ref, g_cfg.max_hold_ns, g_cfg.lc_prio_max);
    return 0;
}

static void q_linnos_dump_stats(struct q_linnos_bpf *skel)
{
    struct q_linnos_state_entry v = {};
    __u32 key = 0;
    int fd;

    fd = bpf_map__fd(skel->maps.q_linnos_state);
    if (fd < 0)
        return;
    if (bpf_map_lookup_elem(fd, &key, &v))
        return;
    fprintf(stderr,
            "q_linnos_stats inserts=%llu "
            "band0=%llu band1=%llu band2=%llu lc=%llu be=%llu "
            "ir0=%llu/%llu ir1=%llu/%llu ir2=%llu/%llu "
            "disp_fast=%llu disp_held_to=%llu disp_held_soft=%llu "
            "evac_fast=%llu evac_held=%llu nulls=%llu push_fails=%llu "
            "completions=%llu inflight=%llu "
            "enq_fast=%llu enq_held=%llu lost_fast=%llu lost_held=%llu\n",
            (unsigned long long)v.stats.inserts,
            (unsigned long long)v.stats.inserts_by_band[0],
            (unsigned long long)v.stats.inserts_by_band[1],
            (unsigned long long)v.stats.inserts_by_band[2],
            (unsigned long long)v.stats.inserts_by_class[0],
            (unsigned long long)v.stats.inserts_by_class[1],
            (unsigned long long)v.stats.intercepted[0][0],
            (unsigned long long)v.stats.intercepted[0][1],
            (unsigned long long)v.stats.intercepted[1][0],
            (unsigned long long)v.stats.intercepted[1][1],
            (unsigned long long)v.stats.intercepted[2][0],
            (unsigned long long)v.stats.intercepted[2][1],
            (unsigned long long)v.stats.dispatch_fast,
            (unsigned long long)v.stats.dispatch_held_timeout,
            (unsigned long long)v.stats.dispatch_held_soft,
            (unsigned long long)v.stats.evac_fast,
            (unsigned long long)v.stats.evac_held,
            (unsigned long long)v.stats.null_returns,
            (unsigned long long)v.stats.push_fails,
            (unsigned long long)v.stats.completions,
            (unsigned long long)v.stats.inflight,
            (unsigned long long)v.stats.enqueued_fast,
            (unsigned long long)v.stats.enqueued_held,
            (unsigned long long)v.stats.lost_fast,
            (unsigned long long)v.stats.lost_held);
}

int main(int argc, char **argv)
{
    static const struct option q_linnos_opts[] = {
        { "ir-low", required_argument, NULL, Q_LINNOS_OPT_IR_LOW },
        { "ir-mid", required_argument, NULL, Q_LINNOS_OPT_IR_MID },
        { "ir-high", required_argument, NULL, Q_LINNOS_OPT_IR_HIGH },
        { "busy-ref", required_argument, NULL, Q_LINNOS_OPT_BUSY_REF },
        { "max-hold-us", required_argument, NULL, Q_LINNOS_OPT_MAX_HOLD },
        { "lc-prio-max", required_argument, NULL, Q_LINNOS_OPT_LC_PRIO },
        { NULL, 0, NULL, 0 },
    };
    struct q_linnos_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage(
        "q_linnos",
        "  --ir-low LC:BE          band0 (<50% busy) interception %% (default 0:0)\n"
        "  --ir-mid LC:BE          band1 (50-80%%) interception %% (default 0:20)\n"
        "  --ir-high LC:BE         band2 (>80%%) interception %% (default 5:60)\n"
        "  --busy-ref U64          in-flight count == 100% busy (default 64)\n"
        "  --max-hold-us U64       held-request hold deadline (default 2000)\n"
        "  --lc-prio-max U32       arb_prio below which a cgroup is LC (default 3)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args,
                                    q_linnos_opts, q_linnos_extra_parse))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_linnos_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_linnos_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_linnos, BPF_F_LINK);
    if (q_linnos_bpf__load(skel)) {
        q_linnos_bpf__destroy(skel);
        return 1;
    }

    if (q_linnos_init_cfg_map(skel)) {
        fprintf(stderr, "failed to write q_linnos_cfg_map\n");
        q_linnos_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_linnos_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_linnos,
                                               "q_linnos",
                                               args.cgroup,
                                               owner_token);
    /* attach_and_run returned: graceful drain done, policy detached. */
    q_linnos_dump_stats(skel);
    q_linnos_bpf__destroy(skel);
    return ret;
}
