/* loader_arb_slo.c - loader for the SLO arbiter (STRICT)
 *
 * arb_recovery tune3: --diag-interval-ms U32 (default 0 = off) enables
 * the shared ledger-drift dump (see ioext_loader_common.h) so the next
 * S-decisive round can watch cgroup_drr/pending_total/outstanding
 * directly (STALL_FORENSICS.md §3.4 residual blind spot).
 */
#define _GNU_SOURCE

#include <stdio.h>
#include <string.h>

#include "arb_slo.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

enum {
    SLO_OPT_DIAG_INTERVAL_MS = 1000,
};

static __u32 slo_diag_interval_ms;

static int slo_parse_extra(__u32 opt, const char *arg)
{
    if (opt != SLO_OPT_DIAG_INTERVAL_MS)
        return -1;
    return ioext_cli_parse_u32_strict(arg, &slo_diag_interval_ms);
}

int main(int argc, char **argv)
{
    const struct option extra_opts[] = {
        { "diag-interval-ms", required_argument, NULL, SLO_OPT_DIAG_INTERVAL_MS },
        { NULL, 0, NULL, 0 },
    };
    struct iosched_arbiter_diag_maps diag;
    struct arb_slo_bpf *skel;
    int ret;

    if (ioext_cli_parse_arbiter_args_ex(argc, argv, NULL, extra_opts,
            "      --diag-interval-ms U32   every N ms dump the cgroup_drr /\n"
            "                             pending_total / outstanding ledger summary to\n"
            "                             stderr (plus one line at attach and one at\n"
            "                             detach). Default 0 = off.\n",
            slo_parse_extra))
        return 1;

    skel = arb_slo_bpf__open();
    if (!skel)
        return 1;

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        arb_slo_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.slo_arbiter, BPF_F_LINK);
    if (arb_slo_bpf__load(skel)) {
        arb_slo_bpf__destroy(skel);
        return 1;
    }

    memset(&diag, 0, sizeof(diag));
    diag.active_cgroup_ids = skel->maps.active_cgroup_ids;
    diag.policy_meta = skel->maps.policy_meta;
    diag.cgroup_drr = skel->maps.cgroup_drr;
    diag.cgroup_outstanding = skel->maps.cgroup_outstanding;
    diag.global_state = skel->maps.global_state;
    diag.hctx_state = skel->maps.hctx_state;
    diag.requeue_replay = skel->maps.requeue_replay;
    diag.interval_ms = slo_diag_interval_ms;

    ret = iosched_loader_arbiter_attach_and_run_ex(skel->obj,
                                                   skel->maps.slo_arbiter,
                                                   &diag);
    arb_slo_bpf__destroy(skel);
    return ret;
}
