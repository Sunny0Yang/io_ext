// loader_q_inject_null.c — loader for the F-NULL fault policy (S-CHAOS).
// CLI: -c/--cgroup PATH  -p/--arb-prio U32  -W/--arb-weight U32  --mode U32
// --mode selects the fault vector (see q_inject_null.bpf.c header comment).
// The inject_ctl map is written BEFORE attach so the very first insert already
// goes through the fault path.
#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "q_inject_null.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* pre-scan --mode (not part of the shared policy CLI) and strip it from argv */
static long parse_mode_arg(int argc, char **argv, int *new_argc, char **new_argv)
{
    long mode = 0;
    int i, j = 0;

    *new_argc = 0;
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            mode = strtol(argv[i + 1], NULL, 10);
            if (mode < 0 || mode > 3)
                return -1;
            i++;                        /* skip the value too */
            continue;
        }
        new_argv[j++] = argv[i];
    }
    *new_argc = j;
    return mode;
}

int main(int argc, char **argv)
{
    struct q_inject_null_bpf *skel;
    struct ioext_policy_args args;
    __u64 owner_token;
    long mode;
    int ctl_fd;
    int ret;
    __u32 key = 0;
    __u64 val;

    char *fargv[64];
    int fargc = 0;

    mode = parse_mode_arg(argc, argv, &fargc, fargv);
    if (mode < 0) {
        fprintf(stderr, "ioext: --mode must be 0..3\n");
        return 1;
    }

    ioext_cli_set_usage("q_inject_null", NULL);
    if (ioext_cli_parse_policy_args(fargc, fargv, &args, NULL, NULL))
        return 1;

    skel = q_inject_null_bpf__open();
    if (!skel)
        return 1;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_inject_null_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_inject_null, BPF_F_LINK);
    if (q_inject_null_bpf__load(skel)) {
        q_inject_null_bpf__destroy(skel);
        return 1;
    }

    /* arm the fault vector before the policy can see a single request */
    ctl_fd = bpf_map__fd(skel->maps.inject_ctl);
    val = (__u64)mode;
    if (bpf_map_update_elem(ctl_fd, &key, &val, BPF_ANY)) {
        fprintf(stderr, "ioext: inject_ctl update failed: %s\n", strerror(errno));
        q_inject_null_bpf__destroy(skel);
        return 1;
    }

    owner_token = iosched_loader_owner_token();
    ret = iosched_loader_register_policy(args.cgroup, args.arb_prio,
                                         args.arb_weight, owner_token);
    if (ret) {
        q_inject_null_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_inject_null,
                                               "q_inject_null",
                                               args.cgroup,
                                               owner_token);
    q_inject_null_bpf__destroy(skel);
    return ret;
}
