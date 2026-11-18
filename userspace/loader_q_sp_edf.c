#define _GNU_SOURCE

#include <stdio.h>

#include "q_sp_edf.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/*
 * d2d4_bm tune (2026-11-10): q_sp_edf policy constants exposed as CLI
 * knobs. The knob names are a hard contract (the yml generator references
 * them verbatim): --small-read-deadline-ns, --small-write-deadline-ns,
 * --large-deadline-ns, --large-max-wait-ns.
 *
 * The defaults below MUST mirror the .rodata initializers in
 * q_sp_edf.bpf.c (small-read 1000000 / small-write 2000000 /
 * large 20000000 / large-max-wait 8000000). The loader always writes
 * the parsed value into skel->rodata before load, so an unknobbed start
 * reproduces the original compile-time behavior exactly.
 */
enum {
    Q_SP_EDF_OPT_SMALL_READ_DEADLINE_NS = 1000,
    Q_SP_EDF_OPT_SMALL_WRITE_DEADLINE_NS,
    Q_SP_EDF_OPT_LARGE_DEADLINE_NS,
    Q_SP_EDF_OPT_LARGE_MAX_WAIT_NS,
};

#define Q_SP_EDF_DEFAULT_SMALL_READ_DEADLINE_NS   1000000ULL
#define Q_SP_EDF_DEFAULT_SMALL_WRITE_DEADLINE_NS  2000000ULL
#define Q_SP_EDF_DEFAULT_LARGE_DEADLINE_NS       20000000ULL
#define Q_SP_EDF_DEFAULT_LARGE_MAX_WAIT_NS        8000000ULL

static __u64 sp_edf_small_read_deadline_ns = Q_SP_EDF_DEFAULT_SMALL_READ_DEADLINE_NS;
static __u64 sp_edf_small_write_deadline_ns = Q_SP_EDF_DEFAULT_SMALL_WRITE_DEADLINE_NS;
static __u64 sp_edf_large_deadline_ns = Q_SP_EDF_DEFAULT_LARGE_DEADLINE_NS;
static __u64 sp_edf_large_max_wait_ns = Q_SP_EDF_DEFAULT_LARGE_MAX_WAIT_NS;

static int sp_edf_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case Q_SP_EDF_OPT_SMALL_READ_DEADLINE_NS:
        sp_edf_small_read_deadline_ns = iosched_loader_parse_u64(arg,
                                        Q_SP_EDF_DEFAULT_SMALL_READ_DEADLINE_NS);
        return 0;
    case Q_SP_EDF_OPT_SMALL_WRITE_DEADLINE_NS:
        sp_edf_small_write_deadline_ns = iosched_loader_parse_u64(arg,
                                       Q_SP_EDF_DEFAULT_SMALL_WRITE_DEADLINE_NS);
        return 0;
    case Q_SP_EDF_OPT_LARGE_DEADLINE_NS:
        sp_edf_large_deadline_ns = iosched_loader_parse_u64(arg,
                                            Q_SP_EDF_DEFAULT_LARGE_DEADLINE_NS);
        return 0;
    case Q_SP_EDF_OPT_LARGE_MAX_WAIT_NS:
        sp_edf_large_max_wait_ns = iosched_loader_parse_u64(arg,
                                         Q_SP_EDF_DEFAULT_LARGE_MAX_WAIT_NS);
        return 0;
    default:
        return -1;
    }
}

int main(int argc, char **argv)
{
    struct q_sp_edf_bpf *skel;
    const struct option extra_opts[] = {
        { "small-read-deadline-ns", required_argument, NULL, Q_SP_EDF_OPT_SMALL_READ_DEADLINE_NS },
        { "small-write-deadline-ns", required_argument, NULL, Q_SP_EDF_OPT_SMALL_WRITE_DEADLINE_NS },
        { "large-deadline-ns", required_argument, NULL, Q_SP_EDF_OPT_LARGE_DEADLINE_NS },
        { "large-max-wait-ns", required_argument, NULL, Q_SP_EDF_OPT_LARGE_MAX_WAIT_NS },
        { NULL, 0, NULL, 0 },
    };
    struct ioext_policy_args args;
    __u64 owner_token;
    int ret;

    ioext_cli_set_usage("q_sp_edf",
                        "      --small-read-deadline-ns U64   small read lane deadline ns (default: 1000000)\n"
                        "      --small-write-deadline-ns U64  small write lane deadline ns (default: 2000000)\n"
                        "      --large-deadline-ns U64        large lane deadline ns (default: 20000000)\n"
                        "      --large-max-wait-ns U64        large lane max wait ns (default: 8000000)\n");
    if (ioext_cli_parse_policy_args(argc, argv, &args, extra_opts, sp_edf_parse_extra))
        return 1;
    owner_token = iosched_loader_owner_token();

    skel = q_sp_edf_bpf__open();
    if (!skel)
        return 1;

    skel->rodata->sp_edf_small_read_deadline_ns = sp_edf_small_read_deadline_ns;
    skel->rodata->sp_edf_small_write_deadline_ns = sp_edf_small_write_deadline_ns;
    skel->rodata->sp_edf_large_deadline_ns = sp_edf_large_deadline_ns;
    skel->rodata->sp_edf_large_max_wait_ns = sp_edf_large_max_wait_ns;

    if (iosched_loader_setup_policy_maps(skel->obj)) {
        q_sp_edf_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.scx_q_sp_edf, BPF_F_LINK);
    if (q_sp_edf_bpf__load(skel)) {
        q_sp_edf_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_register_policy(args.cgroup,
                                         args.arb_prio,
                                         args.arb_weight,
                                         owner_token);
    if (ret) {
        q_sp_edf_bpf__destroy(skel);
        return 1;
    }

    ret = iosched_loader_policy_attach_and_run(skel->obj,
                                               skel->maps.scx_q_sp_edf,
                                               "q_sp_edf",
                                               args.cgroup,
                                               owner_token);
    q_sp_edf_bpf__destroy(skel);
    return ret;
}
