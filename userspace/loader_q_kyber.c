#define _GNU_SOURCE

#include <stdio.h>
#include <unistd.h>

#include "q_kyber.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

struct kyber_merge_cfg_user {
    __u32 enable;
    __u32 reserved;
};

enum {
    KYBER_OPT_READ_TARGET_NS = 1000,
    KYBER_OPT_WRITE_TARGET_NS,
    KYBER_OPT_DISCARD_TARGET_NS,
    KYBER_OPT_OTHER_TARGET_NS,
    KYBER_OPT_MERGE_ENABLE,
};

static __u64 kyber_read_target_ns = IOSCHED_KYBER_READ_TARGET_NS;
static __u64 kyber_write_target_ns = IOSCHED_KYBER_WRITE_TARGET_NS;
static __u64 kyber_discard_target_ns = IOSCHED_KYBER_DISCARD_TARGET_NS;
static __u64 kyber_other_target_ns = IOSCHED_KYBER_OTHER_TARGET_NS;
static __u32 kyber_merge_enable = 1;

static int kyber_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case KYBER_OPT_READ_TARGET_NS:
        kyber_read_target_ns = iosched_loader_parse_u64(arg, IOSCHED_KYBER_READ_TARGET_NS);
        return 0;
    case KYBER_OPT_WRITE_TARGET_NS:
        kyber_write_target_ns = iosched_loader_parse_u64(arg, IOSCHED_KYBER_WRITE_TARGET_NS);
        return 0;
    case KYBER_OPT_DISCARD_TARGET_NS:
        kyber_discard_target_ns = iosched_loader_parse_u64(arg,
                                                           IOSCHED_KYBER_DISCARD_TARGET_NS);
        return 0;
    case KYBER_OPT_OTHER_TARGET_NS:
        kyber_other_target_ns = iosched_loader_parse_u64(arg, IOSCHED_KYBER_OTHER_TARGET_NS);
        return 0;
    case KYBER_OPT_MERGE_ENABLE:
        kyber_merge_enable = iosched_loader_parse_u32(arg, 1);
        return 0;
    default:
        return -1;
    }
}

static int pin_map_if_present(struct bpf_map *map, const char *path)
{
    if (!map)
        return 0;

    unlink(path);
    if (bpf_map__pin(map, path)) {
        IOSCHED_LOADER_DBG("pin map '%s' failed: %s\n", path, strerror(errno));
        return -errno;
    }
    return 0;
}

int main(int argc, char **argv)
{
    struct q_kyber_bpf *skel;
    const struct option extra_opts[] = {
        { "read-target-ns", required_argument, NULL, KYBER_OPT_READ_TARGET_NS },
        { "write-target-ns", required_argument, NULL, KYBER_OPT_WRITE_TARGET_NS },
        { "discard-target-ns", required_argument, NULL, KYBER_OPT_DISCARD_TARGET_NS },
        { "other-target-ns", required_argument, NULL, KYBER_OPT_OTHER_TARGET_NS },
        { "merge-enable", required_argument, NULL, KYBER_OPT_MERGE_ENABLE },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    __u32 cfg_key = 0;
    struct kyber_merge_cfg_user merge_cfg;
    int ret;

    ioext_cli_set_usage("q_kyber",
                        "      --read-target-ns U64      read latency target ns (default: 2000000)\n"
                        "      --write-target-ns U64     write latency target ns (default: 10000000)\n"
                        "      --discard-target-ns U64   discard latency target ns (default: 5000000000)\n"
                        "      --other-target-ns U64     other op latency target ns (default: 5000000)\n"
                        "      --merge-enable U32        enable merge (default: 1)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, kyber_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();
    merge_cfg.enable = kyber_merge_enable;
    merge_cfg.reserved = 0;

    skel = q_kyber_bpf__open();
    if (!skel)
        return 1;

    skel->rodata->kyber_read_target_ns = kyber_read_target_ns;
    skel->rodata->kyber_write_target_ns = kyber_write_target_ns;
    skel->rodata->kyber_discard_target_ns = kyber_discard_target_ns;
    skel->rodata->kyber_other_target_ns = kyber_other_target_ns;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_kyber_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_kyber, BPF_F_LINK);
    if (q_kyber_bpf__load(skel)) {
        q_kyber_bpf__destroy(skel);
        return 1;
    }

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.merge_cfg_map), &cfg_key, &merge_cfg, BPF_ANY)) {
        IOSCHED_LOADER_DBG("failed to update kyber merge_cfg enable=%u\n", kyber_merge_enable);
        q_kyber_bpf__destroy(skel);
        return 1;
    }

    pin_map_if_present(skel->maps.merge_cfg_map, "/sys/fs/bpf/kyber_merge_cfg");

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_kyber_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_kyber,
                                               "q_kyber",
                                               args.cgroup,
                                               owner_token);
    q_kyber_bpf__destroy(skel);
    return ret;
}
