#define _GNU_SOURCE

#include <stdio.h>

#include "q_ddl.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/*
 * d2d4_bm tune (2026-11-10): q_ddl policy constants exposed as CLI knobs.
 * The knob names are a hard contract (the yml generator references them
 * verbatim): --lat-deadline-ns, --bulk-deadline-ns, --bulk-starvation-ns,
 * --guard-ns, --emergency-max, --small-sector-thresh.
 *
 * The defaults below MUST mirror the .rodata initializers in q_ddl.bpf.c
 * (lat 1000000 / bulk 20000000 / starvation 10000000 / guard 200000 /
 * emergency 3 / small-sector 8). The loader always writes the parsed
 * value into skel->rodata before load, so an unknobbed start reproduces
 * the original compile-time behavior exactly.
 */
enum {
    Q_DDL_OPT_LAT_DEADLINE_NS = 1000,
    Q_DDL_OPT_BULK_DEADLINE_NS,
    Q_DDL_OPT_BULK_STARVATION_NS,
    Q_DDL_OPT_GUARD_NS,
    Q_DDL_OPT_EMERGENCY_MAX,
    Q_DDL_OPT_SMALL_SECTOR_THRESH,
};

#define Q_DDL_DEFAULT_LAT_DEADLINE_NS     1000000ULL
#define Q_DDL_DEFAULT_BULK_DEADLINE_NS   20000000ULL
#define Q_DDL_DEFAULT_BULK_STARVATION_NS 10000000ULL
#define Q_DDL_DEFAULT_GUARD_NS            200000ULL
#define Q_DDL_DEFAULT_EMERGENCY_MAX              3U
#define Q_DDL_DEFAULT_SMALL_SECTOR_THRESH        8U

static __u64 q_ddl_lat_deadline_ns = Q_DDL_DEFAULT_LAT_DEADLINE_NS;
static __u64 q_ddl_bulk_deadline_ns = Q_DDL_DEFAULT_BULK_DEADLINE_NS;
static __u64 q_ddl_bulk_starvation_ns = Q_DDL_DEFAULT_BULK_STARVATION_NS;
static __u64 q_ddl_guard_ns = Q_DDL_DEFAULT_GUARD_NS;
static __u32 q_ddl_emergency_max = Q_DDL_DEFAULT_EMERGENCY_MAX;
static __u32 q_ddl_small_sector_thresh = Q_DDL_DEFAULT_SMALL_SECTOR_THRESH;

static int q_ddl_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case Q_DDL_OPT_LAT_DEADLINE_NS:
        q_ddl_lat_deadline_ns = iosched_loader_parse_u64(arg,
                                                Q_DDL_DEFAULT_LAT_DEADLINE_NS);
        return 0;
    case Q_DDL_OPT_BULK_DEADLINE_NS:
        q_ddl_bulk_deadline_ns = iosched_loader_parse_u64(arg,
                                               Q_DDL_DEFAULT_BULK_DEADLINE_NS);
        return 0;
    case Q_DDL_OPT_BULK_STARVATION_NS:
        q_ddl_bulk_starvation_ns = iosched_loader_parse_u64(arg,
                                         Q_DDL_DEFAULT_BULK_STARVATION_NS);
        return 0;
    case Q_DDL_OPT_GUARD_NS:
        q_ddl_guard_ns = iosched_loader_parse_u64(arg, Q_DDL_DEFAULT_GUARD_NS);
        return 0;
    case Q_DDL_OPT_EMERGENCY_MAX:
        q_ddl_emergency_max = iosched_loader_parse_u32(arg,
                                               Q_DDL_DEFAULT_EMERGENCY_MAX);
        return 0;
    case Q_DDL_OPT_SMALL_SECTOR_THRESH:
        q_ddl_small_sector_thresh = iosched_loader_parse_u32(arg,
                                        Q_DDL_DEFAULT_SMALL_SECTOR_THRESH);
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct q_ddl_bpf *skel;
    const struct option extra_opts[] = {
        { "lat-deadline-ns", required_argument, NULL, Q_DDL_OPT_LAT_DEADLINE_NS },
        { "bulk-deadline-ns", required_argument, NULL, Q_DDL_OPT_BULK_DEADLINE_NS },
        { "bulk-starvation-ns", required_argument, NULL, Q_DDL_OPT_BULK_STARVATION_NS },
        { "guard-ns", required_argument, NULL, Q_DDL_OPT_GUARD_NS },
        { "emergency-max", required_argument, NULL, Q_DDL_OPT_EMERGENCY_MAX },
        { "small-sector-thresh", required_argument, NULL, Q_DDL_OPT_SMALL_SECTOR_THRESH },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage("q_ddl",
                        "      --lat-deadline-ns U64      LAT lane deadline ns (default: 1000000)\n"
                        "      --bulk-deadline-ns U64     BULK lane deadline ns (default: 20000000)\n"
                        "      --bulk-starvation-ns U64   BULK head starvation gate ns (default: 10000000)\n"
                        "      --guard-ns U64             LAT urgency guard ns (default: 200000)\n"
                        "      --emergency-max U32       LAT emergency burst cap (default: 3)\n"
                        "      --small-sector-thresh U32  read<=sectors classified LAT (default: 8)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, q_ddl_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_ddl_bpf__open();
    if (!skel)
        return 1;

    skel->rodata->q_ddl_lat_deadline_ns = q_ddl_lat_deadline_ns;
    skel->rodata->q_ddl_bulk_deadline_ns = q_ddl_bulk_deadline_ns;
    skel->rodata->q_ddl_bulk_starvation_ns = q_ddl_bulk_starvation_ns;
    skel->rodata->q_ddl_guard_ns = q_ddl_guard_ns;
    skel->rodata->q_ddl_emergency_max = q_ddl_emergency_max;
    skel->rodata->q_ddl_small_sector_thresh = q_ddl_small_sector_thresh;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_ddl_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_ddl, BPF_F_LINK);
    if (q_ddl_bpf__load(skel)) {
        q_ddl_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_ddl_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_ddl,
                                               "q_ddl",
                                               args.cgroup,
                                               owner_token);
    q_ddl_bpf__destroy(skel);
    return ret;
}
