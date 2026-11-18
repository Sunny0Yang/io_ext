/* loader_arb_iocost.c - loader for the io.cost homomorphic arbiter
 * (cost model + qos vrate feedback)
 *
 * io.cost 同态复现: the cost table pushed here is the FIXED linear-model
 * table (per-request cost = f(op, size bucket), byte-linear default);
 * the qos vrate feedback is configured via --qos-rlat-ns / --qos-rpct
 * (the LC latency target and percentile, io.cost's qos rlat/rpct) and
 * --vrate-min / --vrate-max (the vrate clamp, in PERCENT).  See
 * arb_iocost.bpf.c for the feedback loop design and the residual
 * deviations from native blk-iocost.
 *
 * The cfg struct (quantum, min_budget, 2 x 5 cost rows, qos tail) is
 * written into the BPF iocost_cfg ARRAY map after load and before
 * attach; the defaults mirror arb_iocost.bpf.c's compiled-in byte-linear
 * table (S11) and qos constants (2ms rlat, rpct 99, clamp [25%, 400%]).
 */
#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "arb_iocost.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirror of struct arb_iocost_cfg in arb_iocost.bpf.c (kept in sync; the
 * skeleton cannot export the BPF-side struct to the loader). */
struct arb_iocost_cfg_user {
    __u64 quantum;
    __u64 min_budget;
    __u64 cost_read[5];
    __u64 cost_write[5];
    __u64 qos_rlat_ns;
    __u32 qos_rpct;
    __u32 reserved;
    __u32 vrate_min_fp;
    __u32 vrate_max_fp;
    __u64 runtime_eff_quantum;   /* BPF roller-owned runtime cache; the
                                 * loader zero-inits it (vrate 1.0) */
};

/* Defaults: mirror arb_iocost.bpf.c's S11 recalibrated BYTE-LINEAR table
 * (cost = bytes/64, read == write -- io.cost's linear model on SSD-class
 * devices charges ~equal per byte; see arb_iocost.bpf.c's cost-domain
 * comment), its quantum 8 (budget(w) = w/8 4K-read dispatches, the
 * same per-visit shape as arb_wdrr's 512B*weight), and its qos
 * constants (rlat 2ms campaign-aligned, rpct 99, vrate clamp
 * [0.25, 4.0] as 256ths fixed point: pct * 256 / 100). */
#define IOCOST_DEFAULT_QUANTUM     8ULL
#define IOCOST_DEFAULT_MIN_BUDGET  64ULL
#define IOCOST_DEFAULT_QOS_RLAT_NS  2000000ULL   /* 2 ms; 0 disables qos */
#define IOCOST_DEFAULT_QOS_RPCT    99
#define IOCOST_DEFAULT_VRATE_MIN_PCT  25         /* 0.25 */
#define IOCOST_DEFAULT_VRATE_MAX_PCT 400         /* 4.0  */

enum {
    IOCOST_OPT_COST_TABLE = 1000,
    IOCOST_OPT_COST_QUANTUM,
    IOCOST_OPT_COST_MIN_BUDGET,
    IOCOST_OPT_QOS_RLAT_NS,
    IOCOST_OPT_QOS_RPCT,
    IOCOST_OPT_VRATE_MIN,
    IOCOST_OPT_VRATE_MAX,
};

static __u32 iocost_vrate_pct_to_fp(__u32 pct)
{
    /* percent -> 256ths (truncated): 25 -> 64, 400 -> 1024 */
    return (__u32)(((unsigned long long)pct * 256ULL) / 100ULL);
}

static struct arb_iocost_cfg_user iocost_cfg = {
    .quantum = IOCOST_DEFAULT_QUANTUM,
    .min_budget = IOCOST_DEFAULT_MIN_BUDGET,
    .cost_read = { 64, 256, 1024, 2048, 4096 },       /* 4K/16K/64K/128K/>128K */
    .cost_write = { 64, 256, 1024, 2048, 4096 },      /* byte-linear: read == write */
    .qos_rlat_ns = IOCOST_DEFAULT_QOS_RLAT_NS,
    .qos_rpct = IOCOST_DEFAULT_QOS_RPCT,
    .reserved = 0,
    .vrate_min_fp = IOCOST_DEFAULT_VRATE_MIN_PCT * 256 / 100,
    .vrate_max_fp = IOCOST_DEFAULT_VRATE_MAX_PCT * 256 / 100,
    .runtime_eff_quantum = 0,   /* vrate 1.0 == byte-fair until the BPF
                                 * qos roller steps it */
};

/* "--cost-table R4K,R16K,R64K,R128K,RMAX,W4K,...,WMAX" (10 u64, comma
 * separated; zeros rejected — a zero entry would silently fall back to
 * the compiled-in default inside the BPF). */
static int iocost_parse_cost_table(const char *arg)
{
    char buf[512];
    char *tok;
    __u64 vals[10];
    int i;

    if (!arg || !*arg)
        return -1;

    snprintf(buf, sizeof(buf), "%s", arg);
    tok = strtok(buf, ",");
    for (i = 0; i < 10; i++) {
        char *end = NULL;
        unsigned long long v;

        if (!tok)
            return -1;
        errno = 0;
        v = strtoull(tok, &end, 10);
        if (errno || !end || *end != '\0' || v == 0)
            return -1;
        vals[i] = (__u64)v;
        tok = strtok(NULL, ",");
    }
    if (tok)
        return -1;

    memcpy(iocost_cfg.cost_read, vals, sizeof(vals[0]) * 5);
    memcpy(iocost_cfg.cost_write, vals + 5, sizeof(vals[0]) * 5);
    return 0;
}

static int iocost_parse_extra(__u32 opt, const char *arg)
{
    switch (opt) {
    case IOCOST_OPT_COST_TABLE:
        return iocost_parse_cost_table(arg);
    case IOCOST_OPT_COST_QUANTUM:
        iocost_cfg.quantum = iosched_loader_parse_u64(arg,
                                                      IOCOST_DEFAULT_QUANTUM);
        return iocost_cfg.quantum ? 0 : -1;
    case IOCOST_OPT_COST_MIN_BUDGET:
        iocost_cfg.min_budget = iosched_loader_parse_u64(arg,
                                                         IOCOST_DEFAULT_MIN_BUDGET);
        return iocost_cfg.min_budget ? 0 : -1;
    case IOCOST_OPT_QOS_RLAT_NS:
        iocost_cfg.qos_rlat_ns = iosched_loader_parse_u64(
            arg, IOCOST_DEFAULT_QOS_RLAT_NS);
        return 0;    /* 0 = disable: legal, pins vrate at 1.0 (byte-fair) */
    case IOCOST_OPT_QOS_RPCT:
        iocost_cfg.qos_rpct = iosched_loader_parse_u32(
            arg, IOCOST_DEFAULT_QOS_RPCT);
        if (iocost_cfg.qos_rpct < 1)
            iocost_cfg.qos_rpct = 1;
        if (iocost_cfg.qos_rpct > 100)
            iocost_cfg.qos_rpct = 100;
        return 0;
    case IOCOST_OPT_VRATE_MIN: {
        __u32 pct = iosched_loader_parse_u32(
            arg, IOCOST_DEFAULT_VRATE_MIN_PCT);
        if (pct < 1 || pct > 10000)
            return -1;
        iocost_cfg.vrate_min_fp = iocost_vrate_pct_to_fp(pct);
        if (iocost_cfg.vrate_min_fp < 1)
            iocost_cfg.vrate_min_fp = 1;
        return 0;
    }
    case IOCOST_OPT_VRATE_MAX: {
        __u32 pct = iosched_loader_parse_u32(
            arg, IOCOST_DEFAULT_VRATE_MAX_PCT);
        if (pct < 1 || pct > 10000)
            return -1;
        iocost_cfg.vrate_max_fp = iocost_vrate_pct_to_fp(pct);
        if (iocost_cfg.vrate_max_fp < iocost_cfg.vrate_min_fp)
            iocost_cfg.vrate_max_fp = iocost_cfg.vrate_min_fp;
        return 0;
    }
    default:
        return -1;
    }
}

static void iocost_print_cfg(void)
{
    int i;

    fprintf(stderr, "loader_arb_iocost: io.cost homomorph (cost model + "
            "qos vrate feedback):\n");
    fprintf(stderr, "  quantum=%llu min_budget=%llu\n",
            iocost_cfg.quantum, iocost_cfg.min_budget);
    fprintf(stderr, "  read :");
    for (i = 0; i < 5; i++)
        fprintf(stderr, " %llu", iocost_cfg.cost_read[i]);
    fprintf(stderr, "\n  write:");
    for (i = 0; i < 5; i++)
        fprintf(stderr, " %llu", iocost_cfg.cost_write[i]);
    fprintf(stderr, "\n  qos: rlat=%lluns rpct=%u%s\n",
            iocost_cfg.qos_rlat_ns, iocost_cfg.qos_rpct,
            iocost_cfg.qos_rlat_ns ? "" : " (DISABLED: vrate pinned 1.0, byte-fair)");
    fprintf(stderr, "  vrate clamp: [min %u/256, max %u/256] "
            "(x0.80 late / x1.05 good, 2-window hysteresis)\n",
            iocost_cfg.vrate_min_fp, iocost_cfg.vrate_max_fp);
}

int main(int argc, char **argv)
{
    struct arb_iocost_bpf *skel;
    const struct option extra_opts[] = {
        { "cost-table", required_argument, NULL, IOCOST_OPT_COST_TABLE },
        { "cost-quantum", required_argument, NULL, IOCOST_OPT_COST_QUANTUM },
        { "cost-min-budget", required_argument, NULL, IOCOST_OPT_COST_MIN_BUDGET },
        { "qos-rlat-ns", required_argument, NULL, IOCOST_OPT_QOS_RLAT_NS },
        { "qos-rpct", required_argument, NULL, IOCOST_OPT_QOS_RPCT },
        { "vrate-min", required_argument, NULL, IOCOST_OPT_VRATE_MIN },
        { "vrate-max", required_argument, NULL, IOCOST_OPT_VRATE_MAX },
        { NULL, 0, NULL, 0 },
    };
    __u32 zero = 0;
    int ret;

    if (ioext_cli_parse_arbiter_args_ex(argc, argv, NULL, extra_opts,
            "      --cost-table R4K,R16K,R64K,R128K,RMAX,W4K,W16K,W64K,W128K,WMAX\n"
            "                             fixed per-request cost table (default: 64,256,1024,2048,4096,64,256,1024,2048,4096)\n"
            "      --cost-quantum U64      cost units per unit of weight (default: 8)\n"
            "      --cost-min-budget U64  round budget floor in cost units (default: 64)\n"
            "      --qos-rlat-ns U64       io.cost qos rlat: LC latency target in ns (default: 2000000 = 2ms; 0 disables the vrate feedback)\n"
            "      --qos-rpct U32          io.cost qos rpct: percentile that must meet rlat (default: 99, clamp [1,100])\n"
            "      --vrate-min PCT         vrate floor in PERCENT (default: 25 = 0.25x)\n"
            "      --vrate-max PCT         vrate ceiling in PERCENT (default: 400 = 4.0x)\n",
            iocost_parse_extra)) {
        fprintf(stderr, "loader_arb_iocost: failed to parse args\n");
        return 1;
    }

    skel = arb_iocost_bpf__open();
    if (!skel) {
        fprintf(stderr, "loader_arb_iocost: failed to open skeleton\n");
        return 1;
    }

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        fprintf(stderr, "loader_arb_iocost: setup_arbiter_maps failed: %d\n", ret);
        arb_iocost_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.iocost_arbiter, BPF_F_LINK);
    if (arb_iocost_bpf__load(skel)) {
        fprintf(stderr, "loader_arb_iocost: failed to load BPF skeleton\n");
        arb_iocost_bpf__destroy(skel);
        return 1;
    }

    if (bpf_map_update_elem(bpf_map__fd(skel->maps.iocost_cfg), &zero,
                            &iocost_cfg, BPF_ANY)) {
        fprintf(stderr, "loader_arb_iocost: failed to update iocost_cfg: %s\n",
                strerror(errno));
        arb_iocost_bpf__destroy(skel);
        return 1;
    }

    iocost_print_cfg();

    fprintf(stderr, "loader_arb_iocost: BPF loaded, attaching...\n");
    ret = iosched_loader_arbiter_attach_and_run(skel->obj, skel->maps.iocost_arbiter);
    fprintf(stderr, "loader_arb_iocost: attach_and_run returned %d\n", ret);
    arb_iocost_bpf__destroy(skel);
    return ret;
}
