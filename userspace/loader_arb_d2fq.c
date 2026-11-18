/* loader_arb_d2fq.c - userspace loader for the arb_d2fq arbiter
 * (D2FQ FAST'21 homo reproduction: device-cost weighted virtual-time fair
 * queueing with D2FQ fairness classes).
 *
 * Arbiter options (the arbiter CLI has no cgroup/weight args -- per-tenant
 * weights come from each queue loader's --arb-weight in policy_meta):
 *   --thrsh U64          vtime gap threshold of the LOW band, in scaled
 *                        vtime units (default 64 * 840 << 10; 0 disables
 *                        banding -> pure min-vtime WFQ)
 *   --md-ratio U32       MEDIUM band lower edge: gap > thrsh*(100-md)/100
 *                        (default 70)
 *   --lc-prio-max U32    arb_prio below which a tenant is LC and rides the
 *                        D2FQ fast share (default 3)
 *   --fast-share U32     LC vtime charge divisor (default 6 =
 *                        sysctl_d2fq_cls_base enabled value, the device
 *                        WRR HIGH:LOW class weight ratio; 1 disables)
 *   --type-weights R:W   read/write vtime charge divisor in percent, i.e.
 *                        per-type service-share weight (default 100:100)
 *   --cost-table R,...:W,...  per-IO-type device cost table, two rows
 *                        (read:write) of five size-bucket costs
 *                        4K/16K/64K/128K/>128K (default the arb_cost x64
 *                        table: 64,256,1024,2048,4096:256,1024,4096,8192,16384)
 *   --di-period-ms U64   deceptive-idleness reset window (default 200)
 *
 * The cfg and cost-table maps are loader-written before attach and
 * read-only afterwards (offline device calibration).
 */
#define _GNU_SOURCE

#include <stdio.h>

#include "arb_d2fq.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

/* Mirror of struct d2fq_cfg / struct d2fq_cost_row in arb_d2fq.bpf.c. */
#define D2FQ_VT_NUM (840ULL << 10)

struct d2fq_cfg {
    __u64 thrsh;
    __u32 md_ratio;
    __u32 lc_prio_max;
    __u32 fast_share;
    __u32 type_weight[2];
    __u64 di_period_ns;
    __u32 reserved;
};

struct d2fq_cost_row {
    __u64 bucket[5];
};

/* Clamp: the BPF advance = cost * VT_NUM * 100 / (tw*weight[*fast_share])
 * must stay inside u64 (see d2fq_advance). */
#define D2FQ_COST_MAX 1000000000ULL

static struct d2fq_cfg g_cfg = {
    .thrsh = 64ULL * D2FQ_VT_NUM,
    .md_ratio = 70,
    .lc_prio_max = 3,
    .fast_share = 6,
    .type_weight = {100, 100},
    .di_period_ns = 200ULL * 1000000ULL,
};

static struct d2fq_cost_row g_cost[2] = {
    { .bucket = {64, 256, 1024, 2048, 4096} },    /* READ */
    { .bucket = {256, 1024, 4096, 8192, 16384} }, /* WRITE */
};

#define D2FQ_OPT_THRSH        256
#define D2FQ_OPT_MD_RATIO     257
#define D2FQ_OPT_LC_PRIO_MAX  258
#define D2FQ_OPT_TYPE_WEIGHTS 259
#define D2FQ_OPT_COST_TABLE   260
#define D2FQ_OPT_DI_PERIOD    261
#define D2FQ_OPT_FAST_SHARE   262

static void d2fq_usage(const char *prog)
{
    printf("Usage: %s [OPTIONS]\n\nArbiter: arb_d2fq "
           "(D2FQ FAST'21 homo: cost-weighted vtime WFQ + fairness classes)\n\n"
           "Options:\n"
           "  --thrsh U64          LOW-band vtime gap threshold (default %llu; 0 = no banding)\n"
           "  --md-ratio U32       MEDIUM band lower edge %% (default 70)\n"
           "  --lc-prio-max U32    arb_prio below which a tenant is LC (default 3)\n"
           "  --fast-share U32     LC vtime charge divisor (default 6; 1 disables)\n"
           "  --type-weights R:W   read/write service-share weight %% (default 100:100)\n"
           "  --cost-table R0,..,R4:W0,..,W4  per-type cost table\n"
           "                        (default 64,256,1024,2048,4096:256,1024,4096,8192,16384)\n"
           "  --di-period-ms U64   deceptive-idlessness reset window (default 200)\n"
           "  -h, --help           print this help and exit\n"
           "  -v, --verbose        enable libbpf debug output\n",
           prog, 64ULL * D2FQ_VT_NUM);
}

static int d2fq_parse_row(const char *arg, struct d2fq_cost_row *row)
{
    char *end = NULL;
    unsigned long long v;
    int i;

    if (!arg)
        return -1;
    for (i = 0; i < 5; i++) {
        v = strtoull(arg, &end, 10);
        if (end == arg || v == 0 || v > D2FQ_COST_MAX)
            return -1;
        row->bucket[i] = v;
        arg = end;
        if (*arg == ',') {
            arg++;
            continue;
        }
        break;
    }
    if (i != 4 || (*arg != '\0'))
        return -1;
    return 0;
}

static int d2fq_parse_cost_table(const char *arg)
{
    const char *colon;
    char rowbuf[128];

    if (!arg)
        return -1;
    colon = strchr(arg, ':');
    if (!colon || colon == arg || strlen(colon + 1) == 0)
        return -1;
    if (colon - arg >= (long)sizeof(rowbuf))
        return -1;
    memcpy(rowbuf, arg, colon - arg);
    rowbuf[colon - arg] = '\0';
    if (d2fq_parse_row(rowbuf, &g_cost[0]))
        return -1;
    return d2fq_parse_row(colon + 1, &g_cost[1]);
}

static int d2fq_parse_type_weights(const char *arg)
{
    char *end = NULL;
    unsigned long r, w;

    if (!arg)
        return -1;
    r = strtoul(arg, &end, 10);
    if (end == arg || *end != ':' || r == 0 || r > 1000000)
        return -1;
    arg = end + 1;
    w = strtoul(arg, &end, 10);
    if (end == arg || *end != '\0' || w == 0 || w > 1000000)
        return -1;
    g_cfg.type_weight[0] = (__u32)r;
    g_cfg.type_weight[1] = (__u32)w;
    return 0;
}

static int d2fq_parse_args(int argc, char **argv)
{
    static const struct option long_opts[] = {
        { "thrsh", required_argument, NULL, D2FQ_OPT_THRSH },
        { "md-ratio", required_argument, NULL, D2FQ_OPT_MD_RATIO },
        { "lc-prio-max", required_argument, NULL, D2FQ_OPT_LC_PRIO_MAX },
        { "fast-share", required_argument, NULL, D2FQ_OPT_FAST_SHARE },
        { "type-weights", required_argument, NULL, D2FQ_OPT_TYPE_WEIGHTS },
        { "cost-table", required_argument, NULL, D2FQ_OPT_COST_TABLE },
        { "di-period-ms", required_argument, NULL, D2FQ_OPT_DI_PERIOD },
        { "help", no_argument, NULL, 'h' },
        { "verbose", no_argument, NULL, 'v' },
        { NULL, 0, NULL, 0 },
    };
    int opt;

    opterr = 0;
    optind = 1;
    while ((opt = getopt_long(argc, argv, ":hv", long_opts, NULL)) != -1) {
        switch (opt) {
        case D2FQ_OPT_THRSH:
            g_cfg.thrsh = iosched_loader_parse_u64(optarg, g_cfg.thrsh);
            break;
        case D2FQ_OPT_MD_RATIO:
            g_cfg.md_ratio = iosched_loader_parse_u32(optarg, 70);
            if (g_cfg.md_ratio > 100)
                return -1;
            break;
        case D2FQ_OPT_LC_PRIO_MAX:
            g_cfg.lc_prio_max = iosched_loader_parse_u32(optarg, 3);
            break;
        case D2FQ_OPT_FAST_SHARE:
            g_cfg.fast_share = iosched_loader_parse_u32(optarg, 6);
            if (g_cfg.fast_share == 0)
                return -1;
            break;
        case D2FQ_OPT_TYPE_WEIGHTS:
            if (d2fq_parse_type_weights(optarg))
                return -1;
            break;
        case D2FQ_OPT_COST_TABLE:
            if (d2fq_parse_cost_table(optarg))
                return -1;
            break;
        case D2FQ_OPT_DI_PERIOD:
            g_cfg.di_period_ns =
                iosched_loader_parse_u64(optarg, 200) * 1000000ULL;
            break;
        case 'h':
            d2fq_usage(argv[0]);
            exit(0);
        case 'v':
            ioext_cli_enable_verbose();
            break;
        case ':':
            fprintf(stderr, "option requires an argument\n");
            d2fq_usage(argv[0]);
            return -1;
        case '?':
        default:
            fprintf(stderr, "unknown option: %s\n", argv[optind - 1]);
            d2fq_usage(argv[0]);
            return -1;
        }
    }
    if (optind < argc) {
        fprintf(stderr, "unexpected positional argument: %s\n", argv[optind]);
        d2fq_usage(argv[0]);
        return -1;
    }
    return 0;
}

static int d2fq_init_maps(struct arb_d2fq_bpf *skel)
{
    __u32 key;

    key = 0;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.d2fq_cfg_map),
                            &key, &g_cfg, BPF_ANY))
        return -1;
    key = 0;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.d2fq_cost_table),
                            &key, &g_cost[0], BPF_ANY))
        return -1;
    key = 1;
    if (bpf_map_update_elem(bpf_map__fd(skel->maps.d2fq_cost_table),
                            &key, &g_cost[1], BPF_ANY))
        return -1;
    IOSCHED_LOADER_DBG("arb_d2fq thrsh=%llu md_ratio=%u lc_prio<%u fast_share=%u "
                       "tw=%u:%u di=%lluns cost=read{%llu,%llu,%llu,%llu,%llu} "
                       "write{%llu,%llu,%llu,%llu,%llu}\n",
                       g_cfg.thrsh, g_cfg.md_ratio, g_cfg.lc_prio_max,
                       g_cfg.fast_share,
                       g_cfg.type_weight[0], g_cfg.type_weight[1],
                       g_cfg.di_period_ns,
                       g_cost[0].bucket[0], g_cost[0].bucket[1],
                       g_cost[0].bucket[2], g_cost[0].bucket[3],
                       g_cost[0].bucket[4],
                       g_cost[1].bucket[0], g_cost[1].bucket[1],
                       g_cost[1].bucket[2], g_cost[1].bucket[3],
                       g_cost[1].bucket[4]);
    return 0;
}

int main(int argc, char **argv)
{
    struct arb_d2fq_bpf *skel;
    int ret;

    if (d2fq_parse_args(argc, argv))
        return 1;

    skel = arb_d2fq_bpf__open();
    if (!skel) {
        fprintf(stderr, "loader_arb_d2fq: failed to open skeleton\n");
        return 1;
    }

    ret = iosched_loader_setup_arbiter_maps(skel->obj);
    if (ret) {
        fprintf(stderr, "loader_arb_d2fq: setup_arbiter_maps failed: %d\n", ret);
        arb_d2fq_bpf__destroy(skel);
        return 1;
    }

    bpf_map__set_map_flags(skel->maps.d2fq_arbiter, BPF_F_LINK);
    if (arb_d2fq_bpf__load(skel)) {
        fprintf(stderr, "loader_arb_d2fq: failed to load BPF skeleton\n");
        arb_d2fq_bpf__destroy(skel);
        return 1;
    }

    if (d2fq_init_maps(skel)) {
        fprintf(stderr, "loader_arb_d2fq: failed to write cfg/cost maps\n");
        arb_d2fq_bpf__destroy(skel);
        return 1;
    }

    fprintf(stderr, "loader_arb_d2fq: BPF loaded, attaching...\n");
    ret = iosched_loader_arbiter_attach_and_run(skel->obj,
                                                skel->maps.d2fq_arbiter);
    fprintf(stderr, "loader_arb_d2fq: attach_and_run returned %d\n", ret);
    arb_d2fq_bpf__destroy(skel);
    return ret;
}
