#define _GNU_SOURCE

#include <stdio.h>

#include "q_fifo.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

int main(int argc, char **argv)
{
    struct q_fifo_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage("q_fifo", NULL);
    if (ioext_cli_parse_policy_args(argc, argv, &args, NULL, NULL))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_fifo_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_fifo_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_fifo, BPF_F_LINK);
    if (q_fifo_bpf__load(skel)) {
        q_fifo_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_fifo_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_fifo,
                                               "q_fifo",
                                               args.cgroup,
                                               owner_token);
    q_fifo_bpf__destroy(skel);
    return ret;
}
