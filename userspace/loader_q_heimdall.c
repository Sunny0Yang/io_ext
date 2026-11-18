#define _GNU_SOURCE

#include <stdio.h>

#include <bpf/bpf.h>

#include "q_heimdall.skel.h"
#include "ioext_loader_common.h"

static int heimdall_init_default_maps(struct q_heimdall_bpf *skel)
{
    struct iosched_heimdall_model_config cfg = {
        .flags = 0,
        .feature_count = IOSCHED_HEIMDALL_FEATURES,
        .hidden_count = IOSCHED_HEIMDALL_HIDDEN,
        .threshold = 0,
        .score_shift = IOSCHED_HEIMDALL_SCORE_SHIFT,
        .max_hold_us = IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS / 1000ULL,
    };
    struct iosched_heimdall_norm_vector norm_min = {};
    struct iosched_heimdall_norm_vector norm_scale = {};
    struct iosched_heimdall_w0_vector w0 = {};
    struct iosched_heimdall_b0_vector b0 = {};
    struct iosched_heimdall_w1_vector w1 = {};
    struct iosched_heimdall_b1_vector b1 = {};
    __u32 key = 0;
    __u32 i;

    for (i = 0; i < IOSCHED_HEIMDALL_FEATURES; i++)
        norm_scale.values[i] = 1;

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_model_cfg),
                            &key, &cfg, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_norm_min),
                            &key, &norm_min, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_norm_scale),
                            &key, &norm_scale, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_w0),
                            &key, &w0, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_b0),
                            &key, &b0, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_w1),
                            &key, &w1, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.heimdall_b1),
                            &key, &b1, BPF_ANY))
        return -1;

    return 0;
}

int main(int argc, char **argv)
{
    struct q_heimdall_bpf *skel;
    const char *cgroup_path = argc > 1 ? argv[1] : "/sys/fs/cgroup";
    __u64 owner_token = iosched_loader_owner_token();
    __u32 arb_prio = argc > 2 ? iosched_loader_parse_u32(argv[2], IOSCHED_DEFAULT_ARB_PRIO)
                             : IOSCHED_DEFAULT_ARB_PRIO;
    __u32 arb_weight = argc > 3 ? iosched_loader_parse_u32(argv[3], IOSCHED_DEFAULT_ARB_WEIGHT)
                               : IOSCHED_DEFAULT_ARB_WEIGHT;
    int ret;

    skel = q_heimdall_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_heimdall_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_heimdall, BPF_F_LINK);
    if (q_heimdall_bpf__load(skel)) {
        q_heimdall_bpf__destroy(skel);
        return 1;
    }

    if (heimdall_init_default_maps(skel)) {
        q_heimdall_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(cgroup_path,
                                         arb_prio,
                                         arb_weight,
                                         owner_token);
    if (ret) {
        q_heimdall_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_heimdall,
                                               "q_heimdall",
                                               cgroup_path,
                                               owner_token);
    q_heimdall_bpf__destroy(skel);
    return ret;
}
