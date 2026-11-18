#define _GNU_SOURCE

#include <stdio.h>

#include <bpf/bpf.h>

#include "q_heimdall_v2.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

static int heimdall_v2_init_default_maps(struct q_heimdall_v2_bpf *skel)
{
    static struct iosched_heimdall_v2_fc1_weight fc1_w;
    static struct iosched_heimdall_v2_fc1_bias fc1_b;
    static struct iosched_heimdall_v2_fc2_weight fc2_w;
    static struct iosched_heimdall_v2_fc2_bias fc2_b;
    static struct iosched_heimdall_v2_out_weight out_w;
    static struct iosched_heimdall_v2_out_bias out_b;
    struct iosched_heimdall_v2_config cfg = {
        .flags = IOSCHED_HEIMDALL_V2_F_ENABLE |
                 IOSCHED_HEIMDALL_V2_F_TEST_RULE,
        .feature_count = IOSCHED_HEIMDALL_V2_FEATURES,
        .h1_count = IOSCHED_HEIMDALL_V2_H1,
        .h2_count = IOSCHED_HEIMDALL_V2_H2,
        .q_shift = IOSCHED_HEIMDALL_V2_Q_SHIFT,
        .threshold = 0,
        .max_hold_us = IOSCHED_HEIMDALL_V2_MAX_HOLD_US,
        .test_hold_every = IOSCHED_HEIMDALL_V2_DEFAULT_TEST_HOLD_EVERY,
        .schema_version = IOSCHED_HEIMDALL_V2_SCHEMA_VERSION,
    };
    struct iosched_heimdall_v2_norm_vector norm_min = {};
    struct iosched_heimdall_v2_norm_vector norm_scale = {};
    const char *test_rule;
    const char *hold_every;
    __u32 key = 0;
    __u32 i;

    test_rule = getenv("HEIMDALL_V2_TEST_RULE");
    if (test_rule && !iosched_loader_parse_u32(test_rule, 1))
        cfg.flags &= ~IOSCHED_HEIMDALL_V2_F_TEST_RULE;

    hold_every = getenv("HEIMDALL_V2_TEST_HOLD_EVERY");
    cfg.test_hold_every = iosched_loader_parse_u32(hold_every,
                                                   cfg.test_hold_every);
    if (!cfg.test_hold_every)
        cfg.test_hold_every = IOSCHED_HEIMDALL_V2_DEFAULT_TEST_HOLD_EVERY;

    for (i = 0; i < IOSCHED_HEIMDALL_V2_FEATURES; i++)
        norm_scale.values[i] = 1LL << IOSCHED_HEIMDALL_V2_Q_SHIFT;

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_cfg),
                            &key, &cfg, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_min),
                            &key, &norm_min, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_scale),
                            &key, &norm_scale, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_fc1_w),
                            &key, &fc1_w, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_fc1_b),
                            &key, &fc1_b, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_fc2_w),
                            &key, &fc2_w, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_fc2_b),
                            &key, &fc2_b, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_out_w),
                            &key, &out_w, BPF_ANY))
        return -1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.hm_v2_out_b),
                            &key, &out_b, BPF_ANY))
        return -1;

    IOSCHED_LOADER_DBG("q_heimdall_v2 cfg flags=0x%x hold_every=%u\n",
                       cfg.flags,
                       cfg.test_hold_every);
    return 0;
}

int main(int argc, char **argv)
{
    struct q_heimdall_v2_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage("q_heimdall_v2", NULL);
    if (ioext_cli_parse_policy_args(argc, argv, &args, NULL, NULL))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_heimdall_v2_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_heimdall_v2_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_hmd_v2, BPF_F_LINK);
    if (q_heimdall_v2_bpf__load(skel)) {
        q_heimdall_v2_bpf__destroy(skel);
        return 1;
    }

    if (heimdall_v2_init_default_maps(skel)) {
        q_heimdall_v2_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_heimdall_v2_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_hmd_v2,
                                               "q_heimdall_v2",
                                               args.cgroup,
                                               owner_token);
    q_heimdall_v2_bpf__destroy(skel);
    return ret;
}
