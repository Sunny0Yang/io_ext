/* loader_q_iomax.c - userspace loader for the q_iomax 4-channel
 * token-bucket policy (P1-7, io.max-faithful reimplementation).
 *
 * Policy-specific options (ioext_cli_args.h extra_opts mechanism):
 *   --riops U64   max read IOs/s    (0 or omitted = unlimited)
 *   --wiops U64   max write IOs/s   (0 or omitted = unlimited)
 *   --rbps  U64   max read bytes/s  (0 or omitted = unlimited)
 *   --wbps  U64   max write bytes/s (0 or omitted = unlimited)
 * At least one channel must be limited (an all-unlimited policy is a
 * plain FIFO — use q_fifo).  Per-channel burst capacity defaults to
 * rate * 0.5s (simplified io.max burst window, q_ratelimit convention).
 */
#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

#include "q_iomax.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirror of struct q_iomax_cfg in q_iomax.bpf.c (loader-written before
 * attach, read-only afterwards). */
struct q_iomax_cfg {
    __u64 riops;
    __u64 wiops;
    __u64 rbps;
    __u64 wbps;
    __u64 riops_cap;
    __u64 wiops_cap;
    __u64 rbps_cap;
    __u64 wbps_cap;
};

/* Mirror of struct q_iomax_bucket in q_iomax.bpf.c for the stats dump
 * (see q_iomax_dump_stats). */
struct q_iomax_bucket_dump {
    __u32 lock;
    __u32 pad;
    __u64 riops_tokens;
    __u64 riops_carry;
    __u64 wiops_tokens;
    __u64 wiops_carry;
    __u64 rbps_tokens;
    __u64 rbps_carry;
    __u64 wbps_tokens;
    __u64 wbps_carry;
    __u64 last_ns;
    __u64 enqueued;
    __u64 dispatches;
    __u64 evac_pops;
    __u64 null_returns;
    __u64 push_fails;
    __u64 lost;
    __u64 read_dispatches;
    __u64 write_dispatches;
    __u8 rr_turn;
};

#define Q_IOMAX_OPT_RIOPS 256
#define Q_IOMAX_OPT_WIOPS 257
#define Q_IOMAX_OPT_RBPS  258
#define Q_IOMAX_OPT_WBPS  259

/* dt is clamped at 1s in the BPF refill (dt * rate must fit u64; carry
 * can hold up to ~1e9 more), so the per-channel rate must stay below
 * (2^64 - 1e9) / 1e9 = 18446744072 (~17.2 GiB/s) — far beyond any
 * physical device, same class of bound as q_ratelimit. */
#define Q_IOMAX_MAX_RATE 18446744072ULL

/* io.max burst window convention: cap = rate * 0.5s, floored so a
 * limited channel always keeps a non-zero cap: 1 IO for iops rates
 * below 2/s, one 512B sector for bps rates below 1 KiB/s. */
#define Q_IOMAX_MIN_BURST_BYTES 512ULL

static __u64 g_riops;
static __u64 g_wiops;
static __u64 g_rbps;
static __u64 g_wbps;

/* Strict U64 parse: distinguishes an explicit "0" (= unlimited) from
 * garbage (iosched_loader_parse_u64 folds both into the fallback). */
static int q_iomax_parse_u64_strict(const char *arg, __u64 *out)
{
    unsigned long long v;
    char *end = NULL;

    if (!arg || !*arg)
        return -1;
    errno = 0;
    v = strtoull(arg, &end, 10);
    if (errno || !end || *end != '\0')
        return -1;
    *out = (__u64)v;
    return 0;
}

static int q_iomax_extra_parse(__u32 opt, const char *arg)
{
    __u64 v;

    if (q_iomax_parse_u64_strict(arg, &v))
        return -1;
    if (v > Q_IOMAX_MAX_RATE)
        return -1;

    switch (opt) {
    case Q_IOMAX_OPT_RIOPS:
        g_riops = v;
        return 0;
    case Q_IOMAX_OPT_WIOPS:
        g_wiops = v;
        return 0;
    case Q_IOMAX_OPT_RBPS:
        g_rbps = v;
        return 0;
    case Q_IOMAX_OPT_WBPS:
        g_wbps = v;
        return 0;
    default:
        return -1;
    }
}

static int q_iomax_init_cfg_map(struct q_iomax_bpf *skel)
{
    struct q_iomax_cfg cfg = {};
    __u32 key = 0;

    cfg.riops = g_riops;
    cfg.wiops = g_wiops;
    cfg.rbps = g_rbps;
    cfg.wbps = g_wbps;
    cfg.riops_cap = g_riops ? (g_riops / 2 ? g_riops / 2 : 1) : 0;
    cfg.wiops_cap = g_wiops ? (g_wiops / 2 ? g_wiops / 2 : 1) : 0;
    cfg.rbps_cap = g_rbps ? (g_rbps / 2 >= Q_IOMAX_MIN_BURST_BYTES
                             ? g_rbps / 2 : Q_IOMAX_MIN_BURST_BYTES) : 0;
    cfg.wbps_cap = g_wbps ? (g_wbps / 2 >= Q_IOMAX_MIN_BURST_BYTES
                             ? g_wbps / 2 : Q_IOMAX_MIN_BURST_BYTES) : 0;

    if (!cfg.riops && !cfg.wiops && !cfg.rbps && !cfg.wbps)
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.q_iomax_cfg_map),
                            &key, &cfg, BPF_ANY))
        return -1;
    IOSCHED_LOADER_DBG("q_iomax riops=%llu wiops=%llu rbps=%llu wbps=%llu "
                       "caps=%llu/%llu/%llu/%llu\n",
                       cfg.riops, cfg.wiops, cfg.rbps, cfg.wbps,
                       cfg.riops_cap, cfg.wiops_cap,
                       cfg.rbps_cap, cfg.wbps_cap);
    return 0;
}

static int q_iomax_dump_stats(struct q_iomax_bpf *skel)
{
    /* Dump every q_iomax_bucket entry to stderr (captured into the
     * loader log by the rig) before graceful detach — the P1-7 fidelity
     * report needs null_returns (self-kick frequency), per-channel
     * token/carry state and accounting counters that only live in the
     * BPF map while the loader runs. */
    int fd = bpf_map__fd(skel->maps.q_iomax_bucket);
    __u64 key = 0, next_key = 0;
    bool first = true;
    int err;

    if (fd < 0)
        return -1;
    /* hash-map iteration: get_next_key(NULL) yields the first key */
    err = bpf_map_get_next_key(fd, NULL, &next_key);
    while (!err) {
        struct q_iomax_bucket_dump v = {};
        struct q_iomax_cfg cfg = {};
        __u32 cfg_key = 0;
        int cfg_fd = bpf_map__fd(skel->maps.q_iomax_cfg_map);

        key = next_key;
        if (bpf_map_lookup_elem(fd, &key, &v))
            break;
        if (cfg_fd >= 0)
            bpf_map_lookup_elem(cfg_fd, &cfg_key, &cfg);
        fprintf(stderr,
                "q_iomax_stats cgid=%llu cfg=[riops=%llu wiops=%llu rbps=%llu wbps=%llu] "
                "caps=[%llu %llu %llu %llu] "
                "ri=[tok=%llu carry=%llu] wi=[tok=%llu carry=%llu] "
                "rb=[tok=%llu carry=%llu] wb=[tok=%llu carry=%llu] "
                "last_ns=%llu enqueued=%llu dispatches=%llu(rd=%llu wr=%llu) "
                "evac_pops=%llu null_returns=%llu push_fails=%llu lost=%llu rr=%u\n",
                (unsigned long long)key,
                (unsigned long long)cfg.riops,
                (unsigned long long)cfg.wiops,
                (unsigned long long)cfg.rbps,
                (unsigned long long)cfg.wbps,
                (unsigned long long)cfg.riops_cap,
                (unsigned long long)cfg.wiops_cap,
                (unsigned long long)cfg.rbps_cap,
                (unsigned long long)cfg.wbps_cap,
                (unsigned long long)v.riops_tokens,
                (unsigned long long)v.riops_carry,
                (unsigned long long)v.wiops_tokens,
                (unsigned long long)v.wiops_carry,
                (unsigned long long)v.rbps_tokens,
                (unsigned long long)v.rbps_carry,
                (unsigned long long)v.wbps_tokens,
                (unsigned long long)v.wbps_carry,
                (unsigned long long)v.last_ns,
                (unsigned long long)v.enqueued,
                (unsigned long long)v.dispatches,
                (unsigned long long)v.read_dispatches,
                (unsigned long long)v.write_dispatches,
                (unsigned long long)v.evac_pops,
                (unsigned long long)v.null_returns,
                (unsigned long long)v.push_fails,
                (unsigned long long)v.lost,
                (unsigned)v.rr_turn);
        first = false;
        err = bpf_map_get_next_key(fd, &key, &next_key);
    }
    if (first)
        fprintf(stderr, "q_iomax_stats none (no bucket entries)\n");
    return 0;
}

int main(int argc, char **argv)
{
    static const struct option q_iomax_opts[] = {
        { "riops", required_argument, NULL, Q_IOMAX_OPT_RIOPS },
        { "wiops", required_argument, NULL, Q_IOMAX_OPT_WIOPS },
        { "rbps",  required_argument, NULL, Q_IOMAX_OPT_RBPS },
        { "wbps",  required_argument, NULL, Q_IOMAX_OPT_WBPS },
        { NULL, 0, NULL, 0 },
    };
    struct q_iomax_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage(
        "q_iomax",
        "  --riops U64    max read IOs/s    (0 or omitted = unlimited)\n"
        "  --wiops U64    max write IOs/s   (0 or omitted = unlimited)\n"
        "  --rbps  U64    max read bytes/s  (0 or omitted = unlimited)\n"
        "  --wbps  U64    max write bytes/s (0 or omitted = unlimited)\n"
        "At least one channel must be limited; an all-unlimited run is q_fifo.\n"
        "Per-channel burst cap = rate * 0.5s (io.max burst convention).\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args,
                                    q_iomax_opts, q_iomax_extra_parse))
        return 1;
    if (!g_riops && !g_wiops && !g_rbps && !g_wbps) {
        fprintf(stderr,
                "at least one of --riops/--wiops/--rbps/--wbps must be > 0\n");
        return 1;
    }
    owner_token = iosched_loader_owner_token();

    skel = q_iomax_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_iomax_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_iomax, BPF_F_LINK);
    if (q_iomax_bpf__load(skel)) {
        q_iomax_bpf__destroy(skel);
        return 1;
    }

    if (q_iomax_init_cfg_map(skel)) {
        fprintf(stderr, "failed to write q_iomax_cfg_map\n");
        q_iomax_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_iomax_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_iomax,
                                               "q_iomax",
                                               args.cgroup,
                                               owner_token);
    /* attach_and_run returned: graceful drain done, policy detached.
     * Maps still live in this skeleton — dump final bucket stats. */
    q_iomax_dump_stats(skel);
    q_iomax_bpf__destroy(skel);
    return ret;
}
