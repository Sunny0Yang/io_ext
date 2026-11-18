/* loader_q_ratelimit.c - userspace loader for the q_ratelimit token-bucket
 * policy (P1-6 controller-lineage faithful reimplementation).
 *
 * Policy-specific options (ioext_cli_args.h extra_opts mechanism):
 *   --rate-mib-s U64   rate cap in MiB/s (>= 1)
 *   --rate-kib-s U64   rate cap in KiB/s (>= 1) -- sub-MiB granularity for
 *                      slow devices (HDD batches); exactly ONE of the two
 *                      must be given.
 *   --burst-ms U64     bucket capacity in ms worth of rate (default 500 =
 *                      rate * 0.5s, the frozen NVMe contract).  A slow seek-
 *                      bound device wants a SMALL burst: a 0.5s bucket turns
 *                      the capped stream into sharp dispatch spikes that add
 *                      head round-trips; a ~20ms bucket smooths the stream.
 */
#define _GNU_SOURCE

#include <stdio.h>

#include "q_ratelimit.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirror of struct q_rl_cfg in q_ratelimit.bpf.c (loader-written before
 * attach, read-only afterwards). */
struct q_rl_cfg {
    __u64 rate_bytes_per_sec;
    __u64 burst_cap_bytes;
};

#define Q_RL_OPT_RATE_MIB_S 256
#define Q_RL_OPT_RATE_KIB_S 257
#define Q_RL_OPT_BURST_MS 258

static __u64 g_rate_bytes_per_sec;
static __u64 g_burst_ms = 500;   /* frozen NVMe default: rate * 0.5s */
static int g_rate_seen;

static int q_rl_extra_parse(__u32 opt, const char *arg)
{
    __u64 v;
    switch (opt) {
    case Q_RL_OPT_RATE_MIB_S:
        v = iosched_loader_parse_u64(arg, 0);
        if (!v)
            return -1;
        g_rate_bytes_per_sec = v * 1048576ULL;
        g_rate_seen++;
        return 0;
    case Q_RL_OPT_RATE_KIB_S:
        v = iosched_loader_parse_u64(arg, 0);
        if (!v)
            return -1;
        g_rate_bytes_per_sec = v * 1024ULL;
        g_rate_seen++;
        return 0;
    case Q_RL_OPT_BURST_MS:
        v = iosched_loader_parse_u64(arg, 0);
        if (!v || v > 1000000ULL)
            return -1;
        g_burst_ms = v;
        return 0;
    default:
        return -1;
    }
}

static int q_ratelimit_init_cfg_map(struct q_ratelimit_bpf *skel)
{
    struct q_rl_cfg cfg;
    __u32 key = 0;

    if (!g_rate_bytes_per_sec)
        return -1;
    if (g_rate_bytes_per_sec > (~0ULL) / 1000ULL / g_burst_ms)
        return -1;
    cfg.rate_bytes_per_sec = g_rate_bytes_per_sec;
    cfg.burst_cap_bytes = g_rate_bytes_per_sec * g_burst_ms / 1000ULL;

    if (!cfg.rate_bytes_per_sec)
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.q_rl_cfg_map),
                            &key, &cfg, BPF_ANY))
        return -1;
    IOSCHED_LOADER_DBG("q_ratelimit rate=%llu B/s cap=%llu B\n",
                       cfg.rate_bytes_per_sec,
                       cfg.burst_cap_bytes);
    return 0;
}

static int q_ratelimit_dump_stats(struct q_ratelimit_bpf *skel)
{
    /* Dump every q_rl_bucket entry to stderr (captured into the loader
     * log by the rig) before graceful detach — the P1-6 fidelity report
     * needs null_returns (self-kick frequency) and accounting counters
     * that only live in the BPF map while the loader runs. */
    int fd = bpf_map__fd(skel->maps.q_rl_bucket);
    __u64 key = 0, next_key = 0;
    bool first = true;
    int err;

    if (fd < 0)
        return -1;
    /* hash-map iteration: get_next_key(NULL) yields the first key */
    err = bpf_map_get_next_key(fd, NULL, &next_key);
    while (!err) {
        struct {
            __u32 lock;        /* struct q_rl_bucket mirror (see bpf.c) */
            __u32 pad;
            __u64 tokens;
            __u64 last_ns;
            __u64 enqueued;
            __u64 dispatches;
            __u64 evac_pops;
            __u64 null_returns;
            __u64 push_fails;
            __u64 lost;
        } v = {};
        struct q_rl_cfg cfg = {};
        __u32 cfg_key = 0;
        int cfg_fd = bpf_map__fd(skel->maps.q_rl_cfg_map);

        key = next_key;
        if (bpf_map_lookup_elem(fd, &key, &v))
            break;
        if (cfg_fd >= 0)
            bpf_map_lookup_elem(cfg_fd, &cfg_key, &cfg);
        fprintf(stderr,
                "q_ratelimit_stats cgid=%llu rate=%llu cap=%llu tokens=%llu last_ns=%llu "
                "enqueued=%llu dispatches=%llu evac_pops=%llu null_returns=%llu "
                "push_fails=%llu lost=%llu\n",
                (unsigned long long)key,
                (unsigned long long)cfg.rate_bytes_per_sec,
                (unsigned long long)cfg.burst_cap_bytes,
                (unsigned long long)v.tokens,
                (unsigned long long)v.last_ns,
                (unsigned long long)v.enqueued,
                (unsigned long long)v.dispatches,
                (unsigned long long)v.evac_pops,
                (unsigned long long)v.null_returns,
                (unsigned long long)v.push_fails,
                (unsigned long long)v.lost);
        first = false;
        err = bpf_map_get_next_key(fd, &key, &next_key);
    }
    if (first)
        fprintf(stderr, "q_ratelimit_stats none (no bucket entries)\n");
    return 0;
}

int main(int argc, char **argv)
{
    static const struct option q_rl_opts[] = {
        { "rate-mib-s", required_argument, NULL, Q_RL_OPT_RATE_MIB_S },
        { "rate-kib-s", required_argument, NULL, Q_RL_OPT_RATE_KIB_S },
        { "burst-ms", required_argument, NULL, Q_RL_OPT_BURST_MS },
        { NULL, 0, NULL, 0 },
    };
    struct q_ratelimit_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage(
        "q_ratelimit",
        "  --rate-mib-s U64         rate cap in MiB/s (>= 1)\n"
        "  --rate-kib-s U64         rate cap in KiB/s (>= 1); exactly one of "
        "the two is required\n"
        "  --burst-ms U64           bucket capacity in ms of rate "
        "(default 500)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args,
                                    q_rl_opts, q_rl_extra_parse))
        return 1;
    if (g_rate_seen != 1) {
        fprintf(stderr, "exactly one of --rate-mib-s / --rate-kib-s is "
                        "required (got %d)\n", g_rate_seen);
        return 1;
    }
    if (!g_rate_bytes_per_sec) {
        fprintf(stderr, "rate cap must be >= 1\n");
        return 1;
    }
    owner_token = iosched_loader_owner_token();

    skel = q_ratelimit_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_ratelimit_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_ratelimit, BPF_F_LINK);
    if (q_ratelimit_bpf__load(skel)) {
        q_ratelimit_bpf__destroy(skel);
        return 1;
    }

    if (q_ratelimit_init_cfg_map(skel)) {
        fprintf(stderr, "failed to write q_rl_cfg_map\n");
        q_ratelimit_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_ratelimit_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_ratelimit,
                                               "q_ratelimit",
                                               args.cgroup,
                                               owner_token);
    /* attach_and_run returned: graceful drain done, policy detached.
     * Maps still live in this skeleton — dump final bucket stats. */
    q_ratelimit_dump_stats(skel);
    q_ratelimit_bpf__destroy(skel);
    return ret;
}