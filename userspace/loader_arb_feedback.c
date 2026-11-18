#define _GNU_SOURCE

#include <stdio.h>

#include "arb_feedback.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

int main(int argc, char **argv)
{
    struct arb_feedback_bpf *skel;
    int ret;

    if (ioext_cli_parse_arbiter_args(argc, argv, NULL))
        return 1;

    skel = arb_feedback_bpf__open();
    if (!skel)
        return 1;

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        arb_feedback_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.fb_arbiter, BPF_F_LINK);
    if (arb_feedback_bpf__load(skel)) {
        arb_feedback_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_arbiter_attach_and_run(skel->obj, skel->maps.fb_arbiter);
    arb_feedback_bpf__destroy(skel);
    return ret;
}
