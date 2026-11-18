#ifndef IOEXT_CLI_ARGS_H
#define IOEXT_CLI_ARGS_H

#include <errno.h>
#include <getopt.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <linux/types.h>

#include <bpf/libbpf.h>

#include "ioext_loader_common.h"

#define IOEXT_CLI_DEFAULT_CGROUP "/sys/fs/cgroup"

struct ioext_policy_args {
    const char *cgroup;
    __u32 arb_prio;
    __u32 arb_weight;
};

typedef int (*ioext_cli_extra_parse_fn)(__u32 opt, const char *arg);

static const char *ioext_cli_usage_policy_name;
static const char *ioext_cli_usage_extra_help;

static inline const char *ioext_cli_prog_name(const char *prog)
{
    const char *slash;

    if (!prog)
        return "loader";

    slash = strrchr(prog, '/');
    return slash ? slash + 1 : prog;
}

static inline void ioext_cli_set_usage(const char *policy_name,
                                       const char *extra_help)
{
    ioext_cli_usage_policy_name = policy_name;
    ioext_cli_usage_extra_help = extra_help;
}

static inline int ioext_cli_verbose_print(enum libbpf_print_level level,
                                          const char *format,
                                          va_list args)
{
    (void)level;
    return vfprintf(stderr, format, args);
}

static inline void ioext_cli_enable_verbose(void)
{
    libbpf_set_print(ioext_cli_verbose_print);
}

static inline void ioext_cli_policy_args_init(struct ioext_policy_args *out)
{
    out->cgroup = IOEXT_CLI_DEFAULT_CGROUP;
    out->arb_prio = IOSCHED_DEFAULT_ARB_PRIO;
    out->arb_weight = IOSCHED_DEFAULT_ARB_WEIGHT;
}

static inline void ioext_cli_print_usage(const char *prog,
                                         const char *policy_name,
                                         const struct option *extra,
                                         const char *extra_help)
{
    const struct option *opt;

    printf("Usage: %s [OPTIONS]\n", ioext_cli_prog_name(prog));
    if (policy_name)
        printf("\nPolicy: %s\n", policy_name);

    printf("\nCommon options:\n");
    if (policy_name) {
        printf("  -c, --cgroup PATH        target cgroup path (default: %s)\n",
               IOEXT_CLI_DEFAULT_CGROUP);
        printf("  -p, --arb-prio U32       arbiter priority (default: %u)\n",
               IOSCHED_DEFAULT_ARB_PRIO);
        printf("  -W, --arb-weight U32     arbiter weight (default: %u)\n",
               IOSCHED_DEFAULT_ARB_WEIGHT);
    }
    printf("  -h, --help               print this help and exit\n");
    printf("  -v, --verbose            enable libbpf debug output\n");

    if (extra_help && *extra_help) {
        printf("\nPolicy-specific options:\n%s", extra_help);
        if (extra_help[strlen(extra_help) - 1] != '\n')
            printf("\n");
    } else if (extra) {
        printf("\nPolicy-specific options:\n");
        for (opt = extra; opt->name; opt++)
            printf("      --%s%s\n", opt->name,
                   opt->has_arg == no_argument ? "" : " VALUE");
    }
}

static inline int ioext_cli_extra_count(const struct option *extra_opts)
{
    int count = 0;

    if (!extra_opts)
        return 0;

    while (extra_opts[count].name)
        count++;

    return count;
}

static inline struct option *ioext_cli_build_policy_options(const struct option *extra_opts)
{
    static const struct option common_opts[] = {
        { "cgroup", required_argument, NULL, 'c' },
        { "arb-prio", required_argument, NULL, 'p' },
        { "arb-weight", required_argument, NULL, 'W' },
        { "help", no_argument, NULL, 'h' },
        { "verbose", no_argument, NULL, 'v' },
    };
    const size_t common_count = sizeof(common_opts) / sizeof(common_opts[0]);
    int extra_count = ioext_cli_extra_count(extra_opts);
    struct option *opts;
    int i;

    opts = (struct option *)calloc(common_count + extra_count + 1, sizeof(*opts));
    if (!opts)
        return NULL;

    memcpy(opts, common_opts, sizeof(common_opts));
    for (i = 0; i < extra_count; i++)
        opts[common_count + i] = extra_opts[i];

    return opts;
}

static inline int ioext_cli_parse_policy_args(int argc,
                                              char **argv,
                                              struct ioext_policy_args *out,
                                              const struct option *extra_opts,
                                              ioext_cli_extra_parse_fn extra_parse)
{
    struct option *long_opts;
    int opt;
    int ret = 0;

    if (!out)
        return -1;

    ioext_cli_policy_args_init(out);
#if IOEXT_DEBUG
    ioext_cli_enable_verbose();
#endif

    long_opts = ioext_cli_build_policy_options(extra_opts);
    if (!long_opts) {
        fprintf(stderr, "failed to allocate CLI option table\n");
        return -1;
    }

    opterr = 0;
    optind = 1;
    while ((opt = getopt_long(argc, argv, ":c:p:W:hv", long_opts, NULL)) != -1) {
        switch (opt) {
        case 'c':
            out->cgroup = optarg;
            break;
        case 'p':
            out->arb_prio = iosched_loader_parse_u32(optarg,
                                                     IOSCHED_DEFAULT_ARB_PRIO);
            break;
        case 'W':
            out->arb_weight = iosched_loader_parse_u32(optarg,
                                                       IOSCHED_DEFAULT_ARB_WEIGHT);
            break;
        case 'h':
            ioext_cli_print_usage(argv[0],
                                  ioext_cli_usage_policy_name,
                                  extra_opts,
                                  ioext_cli_usage_extra_help);
            free(long_opts);
            exit(0);
        case 'v':
            ioext_cli_enable_verbose();
            break;
        case ':':
            fprintf(stderr, "option requires an argument\n");
            ret = -1;
            goto out;
        case '?':
            fprintf(stderr, "unknown option: %s\n", argv[optind - 1]);
            ret = -1;
            goto out;
        default:
            if (!extra_parse || extra_parse((__u32)opt, optarg)) {
                fprintf(stderr, "invalid option: %s\n", argv[optind - 1]);
                ret = -1;
                goto out;
            }
            break;
        }
    }

    if (optind < argc) {
        fprintf(stderr, "unexpected positional argument: %s\n", argv[optind]);
        ret = -1;
    }

out:
    if (ret)
        ioext_cli_print_usage(argv[0],
                              ioext_cli_usage_policy_name,
                              extra_opts,
                              ioext_cli_usage_extra_help);
    free(long_opts);
    return ret;
}

/*
 * Arbiter-loader argument parsing with policy-specific extra options
 * (backward-compatible extension of ioext_cli_parse_arbiter_args).
 *
 * Arbiters are global singletons: they take NO -c/-p/-W (those belong to
 * the per-cgroup queue loaders).  Extra options are appended to the plain
 * help/verbose table (ioext_cli_build_policy_options would wrongly add
 * -c/-p/-W), and unknown options are routed to extra_parse exactly like
 * the policy path.  extra_help is printed under "Policy-specific
 * options:" via ioext_cli_print_usage(prog, NULL, ...).
 */
static inline struct option *ioext_cli_build_arbiter_options(const struct option *extra_opts)
{
    static const struct option common_opts[] = {
        { "help", no_argument, NULL, 'h' },
        { "verbose", no_argument, NULL, 'v' },
    };
    const size_t common_count = sizeof(common_opts) / sizeof(common_opts[0]);
    int extra_count = ioext_cli_extra_count(extra_opts);
    struct option *opts;
    int i;

    opts = (struct option *)calloc(common_count + extra_count + 1, sizeof(*opts));
    if (!opts)
        return NULL;

    memcpy(opts, common_opts, sizeof(common_opts));
    for (i = 0; i < extra_count; i++)
        opts[common_count + i] = extra_opts[i];

    return opts;
}

static inline int ioext_cli_parse_arbiter_args_ex(int argc,
                                                  char **argv,
                                                  int *verbose_out,
                                                  const struct option *extra_opts,
                                                  const char *extra_help,
                                                  ioext_cli_extra_parse_fn extra_parse)
{
    struct option *long_opts;
    int opt;
    int ret = 0;

    if (verbose_out)
        *verbose_out = 0;
#if IOEXT_DEBUG
    ioext_cli_enable_verbose();
    if (verbose_out)
        *verbose_out = 1;
#endif

    long_opts = ioext_cli_build_arbiter_options(extra_opts);
    if (!long_opts) {
        fprintf(stderr, "failed to allocate CLI option table\n");
        return -1;
    }

    opterr = 0;
    optind = 1;
    while ((opt = getopt_long(argc, argv, ":hv", long_opts, NULL)) != -1) {
        switch (opt) {
        case 'h':
            ioext_cli_print_usage(argv[0], NULL, extra_opts, extra_help);
            free(long_opts);
            exit(0);
        case 'v':
            ioext_cli_enable_verbose();
            if (verbose_out)
                *verbose_out = 1;
            break;
        case ':':
            fprintf(stderr, "option requires an argument\n");
            ret = -1;
            goto out;
        case '?':
        default:
            if (extra_parse && !extra_parse((__u32)opt, optarg)) {
                /* dispatched extra option */
                break;
            }
            fprintf(stderr, "unknown option: %s\n", argv[optind - 1]);
            ret = -1;
            goto out;
        }
    }

    if (optind < argc) {
        fprintf(stderr, "unexpected positional argument: %s\n", argv[optind]);
        ret = -1;
    }

out:
    if (ret)
        ioext_cli_print_usage(argv[0], NULL, extra_opts, extra_help);
    free(long_opts);
    return ret;
}

static inline int ioext_cli_parse_arbiter_args(int argc,
                                               char **argv,
                                               int *verbose_out)
{
    return ioext_cli_parse_arbiter_args_ex(argc, argv, verbose_out,
                                           NULL, NULL, NULL);
}

/*
 * Strict U32 parse for extra options (arbiter --diag-interval-ms and
 * kin): empty / non-numeric / overflow all return -1.  Fail-stop on
 * typos -- an observability knob mistyped must error visibly, never
 * silently take a default that changes what gets recorded.
 */
static inline int ioext_cli_parse_u32_strict(const char *arg, __u32 *out)
{
    char *end = NULL;
    unsigned long long v;

    if (!arg || !*arg)
        return -1;
    errno = 0;
    v = strtoull(arg, &end, 10);
    if (errno || !end || *end != '\0' || v > 0xffffffffULL)
        return -1;
    *out = (__u32)v;
    return 0;
}

#endif
