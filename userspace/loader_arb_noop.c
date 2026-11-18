/* loader_arb_noop.c - ioext3 loader for the arb_noop no-op arbiter.
 *
 * The arbiter itself has no tunables and no ledger (all hooks are
 * no-ops), so the only option is observability: --diag-interval-ms U32
 * (default 0 = off) dumps the q-policy side truth
 * (active_cgroup_ids + policy_meta.queue_depth per cgroup) to stderr on
 * the shared cadence -- useful for the L1 sanity arm of the ladder,
 * where the q-side depth is the whole story (see ioext_loader_common.h,
 * iosched_loader_arb_diag_dump).
 *
 * The arbiter struct_ops stays pinned via BPF_F_LINK for the lifetime
 * of the loader process, matching loader_arb_wdrr's lifecycle.
 */
#define _GNU_SOURCE

#include <stdio.h>
#include <string.h>

#include "arb_noop.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

enum {
    NOOP_OPT_DIAG_INTERVAL_MS = 1000,
};

static __u32 noop_diag_interval_ms;

static int noop_parse_extra(__u32 opt, const char *arg)
{
    if (opt != NOOP_OPT_DIAG_INTERVAL_MS)
        return -1;
    return ioext_cli_parse_u32_strict(arg, &noop_diag_interval_ms);
}

int main(int argc, char **argv)
{
    const struct option extra_opts[] = {
        { "diag-interval-ms", required_argument, NULL, NOOP_OPT_DIAG_INTERVAL_MS },
        { NULL, 0, NULL, 0 },
    };
    struct iosched_arbiter_diag_maps diag;
    struct arb_noop_bpf *skel;
    int ret;

    if (ioext_cli_parse_arbiter_args_ex(argc, argv, NULL, extra_opts,
            "      --diag-interval-ms U32   every N ms dump the per-cgroup q-policy\n"
            "                             queue_depth summary to stderr (plus one line\n"
            "                             at attach and one at detach). Default 0 =\n"
            "                             off.\n",
            noop_parse_extra)) {
        fprintf(stderr, "loader_arb_noop: failed to parse args\n");
        return 1;
    }

    skel = arb_noop_bpf__open();
    if (!skel) {
        fprintf(stderr, "loader_arb_noop: failed to open skeleton\n");
        return 1;
    }

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        fprintf(stderr, "loader_arb_noop: setup_arbiter_maps failed: %d\n", ret);
        arb_noop_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.noop_arbiter, BPF_F_LINK);
    if (arb_noop_bpf__load(skel)) {
        fprintf(stderr, "loader_arb_noop: failed to load BPF skeleton\n");
        arb_noop_bpf__destroy(skel);
        return 1;
    }

    memset(&diag, 0, sizeof(diag));
    diag.active_cgroup_ids = skel->maps.active_cgroup_ids;
    diag.policy_meta = skel->maps.policy_meta;
    diag.interval_ms = noop_diag_interval_ms;

    ret = iosched_loader_arbiter_attach_and_run_ex(skel->obj,
                                                   skel->maps.noop_arbiter,
                                                   &diag);
    if (ret)
        fprintf(stderr, "loader_arb_noop: attach_and_run returned %d\n", ret);
    arb_noop_bpf__destroy(skel);
    return ret;
}
