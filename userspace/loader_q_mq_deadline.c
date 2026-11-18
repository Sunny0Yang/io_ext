#define _GNU_SOURCE

#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "q_mq_deadline.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

#define MQDD_MERGE_MODE_D0  0
#define MQDD_MERGE_MODE_D1  1
#define MQDD_MERGE_MODE_D2  2
#define MQDD_MERGE_MODE_D3  3

struct q_mqdd_merge_cfg_user {
    __u32 merge_mode;
    __u32 reserved;
};

enum {
    Q_MQDD_OPT_MERGE_MODE = 1000,
};

static __u32 q_mqdd_merge_mode = MQDD_MERGE_MODE_D0;

static int q_mqdd_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case Q_MQDD_OPT_MERGE_MODE:
        q_mqdd_merge_mode = iosched_loader_parse_u32(arg, MQDD_MERGE_MODE_D0);
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct q_mq_deadline_bpf *skel;
    const struct option extra_opts[] = {
        { "merge-mode", required_argument, NULL, Q_MQDD_OPT_MERGE_MODE },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    __u32 cfg_key = 0;
    struct q_mqdd_merge_cfg_user cfg;
    int ret;

    ioext_cli_set_usage("q_mq_deadline",
                        "      --merge-mode U32      D0..D3 merge mode (default: 0)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, q_mqdd_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();
    cfg.merge_mode = q_mqdd_merge_mode;
    cfg.reserved = 0;

    skel = q_mq_deadline_bpf__open();
    if (!skel) {
        IOSCHED_LOADER_DBG("failed to open q_mq_deadline skeleton\n");
        return 1;
    }

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        IOSCHED_LOADER_DBG("failed to set up q_mq_deadline policy maps\n");
        q_mq_deadline_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_mq_deadline, BPF_F_LINK);
    if (q_mq_deadline_bpf__load(skel)) {
        IOSCHED_LOADER_DBG("failed to load q_mq_deadline BPF object\n");
        q_mq_deadline_bpf__destroy(skel);
        return 1;
    }

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.merge_cfg_map), &cfg_key, &cfg, BPF_ANY)) {
        IOSCHED_LOADER_DBG("failed to update merge_cfg_map mode=%u\n", q_mqdd_merge_mode);
        q_mq_deadline_bpf__destroy(skel);
        return 1;
    }

    unlink("/sys/fs/bpf/merge_stats_map");
    unlink("/sys/fs/bpf/merge_cfg_map");
    if (bpf_map__pin(skel->maps.merge_stats_map, "/sys/fs/bpf/merge_stats_map"))
        IOSCHED_LOADER_DBG("warn: pin merge_stats_map failed: %s\n", strerror(errno));
    if (bpf_map__pin(skel->maps.merge_cfg_map, "/sys/fs/bpf/merge_cfg_map"))
        IOSCHED_LOADER_DBG("warn: pin merge_cfg_map failed: %s\n", strerror(errno));

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        IOSCHED_LOADER_DBG("failed to register q_mq_deadline policy: %d\n", ret);
        q_mq_deadline_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_mq_deadline,
                                               "q_mq_deadline",
                                               args.cgroup,
                                               owner_token);
    if (ret)
        IOSCHED_LOADER_DBG("q_mq_deadline attach/run failed: %d\n", ret);
    q_mq_deadline_bpf__destroy(skel);
    return ret;
}
