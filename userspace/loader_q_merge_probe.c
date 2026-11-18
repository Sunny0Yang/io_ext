#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "q_merge_probe.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

#define MERGE_PROBE_DENY_ALL          0
#define MERGE_PROBE_ALLOW_ALL         1
#define MERGE_PROBE_ADJACENT_SAME_OP  2
#define MERGE_PROBE_COUNT_ONLY        3

struct probe_cfg_user {
    __u32 mode;
    __u32 reserved;
};

enum {
    MERGE_PROBE_OPT_MODE = 1000,
};

static __u32 merge_probe_mode = MERGE_PROBE_DENY_ALL;

static int merge_probe_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case MERGE_PROBE_OPT_MODE:
        merge_probe_mode = iosched_loader_parse_u32(arg, MERGE_PROBE_DENY_ALL);
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct q_merge_probe_bpf *skel;
    const struct option extra_opts[] = {
        { "mode", required_argument, NULL, MERGE_PROBE_OPT_MODE },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    __u32 cfg_key = 0;
    struct probe_cfg_user cfg;
    int ret;

    ioext_cli_set_usage("q_merge_probe",
                        "      --mode U32            DENY_ALL/ALLOW_ALL/ADJACENT_SAME_OP/COUNT_ONLY (default: 0)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, merge_probe_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();
    cfg.mode = merge_probe_mode;
    cfg.reserved = 0;

    skel = q_merge_probe_bpf__open();
    if (!skel) {
        IOSCHED_LOADER_DBG("failed to open q_merge_probe skeleton\n");
        return 1;
    }

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        IOSCHED_LOADER_DBG("failed to set up q_merge_probe policy maps\n");
        q_merge_probe_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_merge_probe, BPF_F_LINK);
    if (q_merge_probe_bpf__load(skel)) {
        IOSCHED_LOADER_DBG("failed to load q_merge_probe BPF object\n");
        q_merge_probe_bpf__destroy(skel);
        return 1;
    }

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.probe_cfg), &cfg_key, &cfg, BPF_ANY)) {
        IOSCHED_LOADER_DBG("failed to update probe_cfg mode=%u\n", merge_probe_mode);
        q_merge_probe_bpf__destroy(skel);
        return 1;
    }

    unlink("/sys/fs/bpf/merge_stats");
    unlink("/sys/fs/bpf/probe_cfg");
    if (bpf_map__pin(skel->maps.merge_stats, "/sys/fs/bpf/merge_stats"))
        IOSCHED_LOADER_DBG("warn: pin merge_stats failed: %s\n", strerror(errno));
    if (bpf_map__pin(skel->maps.probe_cfg, "/sys/fs/bpf/probe_cfg"))
        IOSCHED_LOADER_DBG("warn: pin probe_cfg failed: %s\n", strerror(errno));

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        IOSCHED_LOADER_DBG("failed to register q_merge_probe policy: %d\n", ret);
        q_merge_probe_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_merge_probe,
                                               "q_merge_probe",
                                               args.cgroup,
                                               owner_token);
    if (ret)
        IOSCHED_LOADER_DBG("q_merge_probe attach/run failed: %d\n", ret);
    q_merge_probe_bpf__destroy(skel);
    return ret;
}
