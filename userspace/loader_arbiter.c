#define _GNU_SOURCE

#include <stdio.h>

#include "arbiter.skel.h"
#include "ioext_loader_common.h"

int main(void)
{
    struct arbiter_bpf *skel;
    int ret;

    skel = arbiter_bpf__open();
    if (!skel)
        return 1;

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        arbiter_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.dummy_arbiter, BPF_F_LINK);
    if (arbiter_bpf__load(skel)) {
        arbiter_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_arbiter_attach_and_run(skel->obj, skel->maps.dummy_arbiter);
    arbiter_bpf__destroy(skel);
    return ret;
}
