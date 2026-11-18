#define _GNU_SOURCE

#include <stdio.h>

#include "minimal.skel.h"
#include "ioext_loader_common.h"

int main(int argc, char **argv)
{
    struct minimal_bpf *skel;
    const char *cgroup_path = argc > 1 ? argv[1] : "/sys/fs/cgroup";
    __u64 owner_token = iosched_loader_owner_token();
    __u32 arb_prio = argc > 2 ? iosched_loader_parse_u32(argv[2], IOSCHED_DEFAULT_ARB_PRIO)
                             : IOSCHED_DEFAULT_ARB_PRIO;
    __u32 arb_weight = argc > 3 ? iosched_loader_parse_u32(argv[3], IOSCHED_DEFAULT_ARB_WEIGHT)
                               : IOSCHED_DEFAULT_ARB_WEIGHT;
    int ret;

    skel = minimal_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        minimal_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_template, BPF_F_LINK);
    if (minimal_bpf__load(skel)) {
        minimal_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(cgroup_path,
                                         arb_prio,
                                         arb_weight,
                                         owner_token);
    if (ret) {
        minimal_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_template,
                                               "minimal",
                                               cgroup_path,
                                               owner_token);
    minimal_bpf__destroy(skel);
    return ret;
}
