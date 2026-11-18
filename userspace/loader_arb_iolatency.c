/* loader_arb_iolatency.c - loader for the io.latency-style LC/BE
 * latency-target arbiter (arb_iolatency)
 *
 * Same-semantics reduction of cgroup v2 io.latency: per-cgroup LC/BE
 * roles come from the queue loaders' -p/--arb-prio (0-2 = LC), per-cgroup
 * shares from -W/--arb-weight; the latency target itself is a global
 * arbiter parameter here (--lat-target-ns) because the framework has no
 * per-cgroup config channel into the arbiter (policy_meta carries only
 * prio/weight).  See arb_iolatency.bpf.c for the backpressure design
 * (LC boost ladder, ratio-equivalent to BE compression; BE floor =
 * --boost-max cap) and the hold gate (--protect-inflight).
 */
#define _GNU_SOURCE

#include <stdio.h>
#include <string.h>

#include "arb_iolatency.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirror of struct arb_iolat_cfg in arb_iolatency.bpf.c (kept in sync). */
struct arb_iolat_cfg_user {
    __u64 lat_target_ns;
    __u32 protect_inflight;
    __u32 boost_max;
    __u32 reserved;
};

#define IOLAT_DEFAULT_LAT_TARGET_NS    (100ULL * 1000ULL)   /* 100 us */
#define IOLAT_DEFAULT_PROTECT_INFLIGHT 32
#define IOLAT_DEFAULT_BOOST_MAX        4

enum {
    IOLAT_OPT_LAT_TARGET_NS = 1000,
    IOLAT_OPT_PROTECT_INFLIGHT,
    IOLAT_OPT_BOOST_MAX,
};

static struct arb_iolat_cfg_user iolat_cfg = {
    .lat_target_ns = IOLAT_DEFAULT_LAT_TARGET_NS,
    .protect_inflight = IOLAT_DEFAULT_PROTECT_INFLIGHT,
    .boost_max = IOLAT_DEFAULT_BOOST_MAX,
    .reserved = 0,
};

static int iolat_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case IOLAT_OPT_LAT_TARGET_NS:
        iolat_cfg.lat_target_ns = iosched_loader_parse_u64(
            arg, IOLAT_DEFAULT_LAT_TARGET_NS);
        return 0;
    case IOLAT_OPT_PROTECT_INFLIGHT:
        iolat_cfg.protect_inflight = iosched_loader_parse_u32(
            arg, IOLAT_DEFAULT_PROTECT_INFLIGHT);
        return iolat_cfg.protect_inflight ? 0 : -1;
    case IOLAT_OPT_BOOST_MAX:
        iolat_cfg.boost_max = iosched_loader_parse_u32(
            arg, IOLAT_DEFAULT_BOOST_MAX);
        /* BE floor knob: clamp into the boost ladder's range */
        if (iolat_cfg.boost_max < 1)
            iolat_cfg.boost_max = 1;
        if (iolat_cfg.boost_max > 16)
            iolat_cfg.boost_max = 16;
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct arb_iolatency_bpf *skel;
    const struct option extra_opts[] = {
        { "lat-target-ns", required_argument, NULL, IOLAT_OPT_LAT_TARGET_NS },
        { "protect-inflight", required_argument, NULL, IOLAT_OPT_PROTECT_INFLIGHT },
        { "boost-max", required_argument, NULL, IOLAT_OPT_BOOST_MAX },
        { NULL, 0, NULL, 0 },
    };
    __u32 zero = 0;
    int ret;

    if (ioext_cli_parse_arbiter_args_ex(argc, argv, NULL, extra_opts,
            "      --lat-target-ns U64       LC latency target in ns (default: 100000 = 100us; 0 disables)\n"
            "      --protect-inflight U32   per-hctx inflight threshold for the LC hold gate (default: 32)\n"
            "      --boost-max U32           LC boost ceiling == BE floor knob, clamp [1,16] (default: 4)\n",
            iolat_parse_extra)) {
        fprintf(stderr, "loader_arb_iolatency: failed to parse args\n");
        return 1;
    }

    skel = arb_iolatency_bpf__open();
    if (!skel) {
        fprintf(stderr, "loader_arb_iolatency: failed to open skeleton\n");
        return 1;
    }

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        fprintf(stderr, "loader_arb_iolatency: setup_arbiter_maps failed: %d\n", ret);
        arb_iolatency_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.iolat_arbiter, BPF_F_LINK);
    if (arb_iolatency_bpf__load(skel)) {
        fprintf(stderr, "loader_arb_iolatency: failed to load BPF skeleton\n");
        arb_iolatency_bpf__destroy(skel);
        return 1;
    }

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.iolat_cfg), &zero,
                            &iolat_cfg, BPF_ANY)) {
        fprintf(stderr, "loader_arb_iolatency: failed to update iolat_cfg: %s\n",
                strerror(errno));
        arb_iolatency_bpf__destroy(skel);
        return 1;
    }

    fprintf(stderr, "loader_arb_iolatency: lat_target=%lluns "
            "protect_inflight=%u boost_max=%u\n",
            iolat_cfg.lat_target_ns, iolat_cfg.protect_inflight,
            iolat_cfg.boost_max);

    fprintf(stderr, "loader_arb_iolatency: BPF loaded, attaching...\n");
    ret = iosched_loader_arbiter_attach_and_run(skel->obj, skel->maps.iolat_arbiter);
    fprintf(stderr, "loader_arb_iolatency: attach_and_run returned %d\n", ret);
    arb_iolatency_bpf__destroy(skel);
    return ret;
}
