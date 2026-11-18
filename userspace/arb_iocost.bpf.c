/* arb_iocost.bpf.c - io.cost homomorphic reproduction: cost model + qos
 * vrate feedback (dispatch-time charged DRR)
 *
 * *** io.cost 同态复现 (HOMOMORPHIC REPRODUCTION): linear COST model +
 * *** qos VRATE FEEDBACK.  This arbiter reproduces blk-iocost's two load
 * *** bearing mechanisms on the iosched_ext arbiter framework:
 * ***   1. the per-request COST model (cost = f(bytes), byte-linear by
 * ***      default -- loader-configurable, see --cost-table), and
 * ***   2. the qos VRATE FEEDBACK loop: cgroups holding a latency target
 * ***      (LC, prio <= ARB_IOCOST_PRIO_LC_MAX per the arb_iolatency role
 * ***      convention) feed their completion latencies into a global
 * ***      window FSM; sustained violations cut the GLOBAL vrate, which
 * ***      scales every cgroup's round-budget issuance together (io.cost's
 * ***      global throttle), while the LC inuse margin (io.cost's
 * ***      per-target debt/margin layer, see the margin comment below)
 * ***      rebates the target holders so the cut lands on the surplus
 * ***      consumers (BE) -- the native io.cost observable: LC keeps ~its
 * ***      uncapped throughput (homo-v2 rep1: lc_iops 90905 ~= none
 * ***      90858) and BE is throttled below its weight share (be_share
 * ***      0.20 vs byte-fair 0.60), trading global throughput for LC
 * ***      latency (lc_read_p99 3883 vs none 4620).
 * ***
 * *** Residual deviations from native blk-iocost (the honest list):
 * ***   a. the linear model is a FIXED loader-pushed table, not io.cost's
 * ***      on-line model calibration (rbps/wbps/iops coefficients);
 * ***   b. the latency signal is a windowed approximation: the DOWN gate
 * ***      uses the exact percentile condition "fraction of LC samples
 * ***      over rlat > (100 - qos_rpct)%" == p(100-rpct) > rlat (no
 * ***      histogram needed), the UP gate uses the window MEAN < rlat/2
 * ***      (io.cost uses per-period latency percentiles for both);
 * ***   c. vrate scales the ROUND-BUDGET ISSUANCE (event-driven DRR
 * ***      rounds), not a time-based period budget: an order-based DRR
 * ***      has no global issue-rate to throttle, so the uniform vrate
 * ***      component alone is share-preserving; the io.cost inuse/debt
 * ***      margin (budget multiplier 1/vrate^2 for LC, clamped) is what
 * ***      makes the cut land on BE as it does natively;
 * ***   d. window 100ms / >= 20 samples / 2-window hysteresis both ways
 * ***      (iolatency's window conventions; io.cost adapts every ~period
 * ***      with margin low/high steps);
 * ***   e. BE floor: the S11 uniform lift (ioext_arb_common.h) re-floors
 * ***      every active cgroup's round budget at >= one of its own
 * ***      max-size requests, so BE throttles in SHARE (LC eras grow),
 * ***      never in absolute starvation -- io.cost's vrate_min analog.
 * ***
 * *** vrate == 1.0 (the default; also whenever qos is disabled via
 * *** --qos-rlat-ns 0) is BIT-IDENTICAL to the previous byte-fair
 * *** arb_iocost: margin 1, eff_quantum == quantum, eff_min_budget ==
 * *** min_budget (the fixed-point ceil is exact at fp 256).
 *
 * What it adds over arb_cost (its closest sibling):
 *   - the per-request cost table is RUNTIME CONFIGURABLE: the loader
 *     writes one struct (quantum, min_budget, 2 x 5 cost rows) into the
 *     iocost_cfg ARRAY map before attaching (loader_arb_iocost.c
 *     --cost-quantum / --cost-min-budget / --cost-table).  arb_cost's
 *     table is compiled-in; arb_iocost never modifies arb_cost so
 *     existing users and the arb_cost accept loop stay valid.
 *
 * Semantics (identical DRR core contract, see ioext_arb_common.h):
 *   - charge unit = COST (arb_note_dispatched_cost / arb_note_requeue pair)
 *   - round budget = eff_quantum * arb_weight * margin * ew_mult cost units,
 *     where eff_quantum = ceil(quantum * vrate) is the GLOBAL uniform vrate
 *     application (every cgroup's nominal budget scales by vrate together,
 *     io.cost's global throttle; precomputed by the qos roller into
 *     cfg->runtime_eff_quantum — 0 == vrate 1.0 == byte-fair, exact) and
 *     margin is the LC inuse margin below (1 for non-LC)
 *   - round-budget floor = max(min_budget, uniform ew_mult lift) — the
 *     absolute min_budget stays UNSCALED by vrate (a per-visit liveness
 *     floor, not a share knob); the S11 uniform lift keeps cost fairness
 *     for large-request streams without compressing the weight ratio
 *     (and re-floors BE under qos pressure: the BE floor, deviation (e))
 *   - equal weights -> equal COST service (NOT equal bytes / requests
 *     unless the table is byte-linear, which the default is)
 *   - IOSCHED_ARB_STRICT + all seven hooks wired like arb_cost
 *
 * A missing/zero cfg entry falls back to the compiled-in defaults
 * (arb_cost_estimate + the constants below), so a bare load without
 * loader configuration is equivalent to arb_cost.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_common.h"

char LICENSE[] SEC("license") = "GPL";

/*
 * Cost-domain defaults (S11 recalibration, 20261107 homo-v2 evidence).
 *
 * The default table is BYTE-LINEAR (cost = bytes/64, read == write).
 * Rationale: io.cost's linear model on SSD-class devices charges ~equal
 * cost per byte (the campaign device's calibrated io.cost.model measured
 * rbps=488MB/s vs wbps=427MB/s -- a 1.14x per-byte write factor, not
 * 4x), so byte-linear is the faithful FIXED approximation of io.cost on
 * this device class, and cost-fair service == byte-fair service: weight
 * ratios then reproduce io.weight's byte-share contract (homo-v2 coloc,
 * weights 100:200 -> LC:BE byte share 1:2, be_share 0.667 -- the
 * campaign's acceptance target).  The old default (read row x64-scaled
 * + 4x flat write penalty, mirroring arb_cost's HDD-ish table) made
 * cost-fair service byte-UNfair for this workload (2:1 LC-ward bytes)
 * with no device calibration behind the 4x.  Op/size awareness stays
 * fully available via --cost-table for cost-aware experiments.
 *
 * ARB_IOCOST_QUANTUM=8: budget(w) = 8*w cost units.  With the byte-linear
 * table (4K read = 64) that is w/8 dispatches of a 4K read -- w=100
 * gives 12.5 requests/round, the same per-visit shape as arb_wdrr's
 * 512B*100 budget, and every weight >= 8 keeps its round budget above
 * one 64K request's cost (w=200: 1600 >= 1024) so the uniform lift stays
 * at 1 and the quantum binds per visit (the S6/F1 lesson).
 */
#define ARB_IOCOST_QUANTUM      8ULL
#define ARB_IOCOST_MIN_BUDGET   64ULL
#define ARB_IOCOST_COST_4K      64ULL
#define ARB_IOCOST_COST_16K     256ULL
#define ARB_IOCOST_COST_64K     1024ULL
#define ARB_IOCOST_COST_128K    2048ULL
#define ARB_IOCOST_COST_BIG     4096ULL

/*
 * QoS vrate feedback defaults (io.cost qos conventions, campaign-aligned).
 *
 * ARB_IOCOST_QOS_RLAT_NS = 2ms: the LC read-latency target, aligned with
 * the homo-v2 campaign's native io.cost qos vertex (IOCOST_QOS rlat=2000us,
 * rpct=99 in gen_homo_v2_yamls.py; the iteration#3 recalibration note: the
 * former 20ms rlat never triggered).  0 (loader --qos-rlat-ns 0) DISABLES
 * qos: no window is ever fed, vrate stays pinned at 1.0 and the arbiter
 * is exactly the previous byte-fair arb_iocost.
 *
 * ARB_IOCOST_PRIO_LC_MAX = 2: the LC role band, the arb_iolatency口径
 * (ARB_IOLAT_PRIO_LC_MAX): prio 0..2 = LC (holds the latency target),
 * prio 3..7 = BE (no target, takes the vrate cut).
 *
 * vrate is kept in 256ths (fp): 1.0 = 256, default clamp [0.25 .. 4.0]
 * = [64 .. 1024] (io.cost's qos min/max percentages 50/150 are the native
 * grid; the wider default range here gives the DRR issuance translation
 * room to express the same observable).  Ladders (io.cost's ~20% down /
 * ~5% up adaptation):
 *   DOWN: vrate *= 205/256   (~0.80): 256,205,164,131,104,83,66,64 (7
 *         cuts to the 0.25 floor under sustained violation; the last cut
 *         66 -> 52 clamps back up to the 64 floor)
 *   UP:   vrate *= 269/256   (~1.05): slow rise, clamped at vrate_max.
 *
 * ARB_IOCOST_QOS_HYST = 2: consecutive violating windows required per
 * DOWN step and consecutive comfortable windows per UP step (迟滞防抖; the
 * window itself already requires >= ARB_SLO_MIN_SAMPLES LC completions).
 *
 * ARB_IOCOST_MARGIN_MAX = 16: the LC inuse-margin ceiling.  Margin =
 * clamp(ceil(256^2 / vrate_fp^2), 1, 16): 1.0->1, 0.80->2, 0.64->3,
 * 0.51->4, 0.41->7, 0.32->10, 0.26->16.  See the margin comment at
 * iocost_get_boost for the io.cost mapping.
 *
 * Derived issuance ladder (weights 100:200, quantum 8, 64K write = 1024;
 * lift = the S11 uniform ew_mult re-floor computed by the shared core):
 *   fp     eff_q  lift  margin  budget_LC       budget_BE
 *   256    8      1     1       800  (12.5 rd)  1600 (2 wr)   <- byte-fair
 *   205    7      1     2       1400 (21 rd)    1400 (2 wr)
 *   164    6      1     3       1800 (28 rd)    1200 (2 wr)
 *   131    5      2     4       4000 (62 rd)    2000 (2 wr)
 *   104    4      2     7       5600 (87 rd)    1600 (2 wr)
 *   83     3      2     10      6000 (93 rd)    1200 (2 wr)
 *   66     3      2     16      9600 (150 rd)   1200 (2 wr)
 *   64     2      3     16      9600 (150 rd)   1200 (2 wr)
 * (BE never drops below one own-max-request of per-turn service, so BE
 * throttles in share, not in absolute starvation -- deviation (e).)
 */
#define ARB_IOCOST_QOS_RLAT_NS_DEFAULT   2000000ULL   /* 2 ms */
#define ARB_IOCOST_QOS_RPCT_DEFAULT       99
#define ARB_IOCOST_PRIO_LC_MAX             2
#define ARB_IOCOST_VRATE_FP_ONE          256
#define ARB_IOCOST_VRATE_MIN_FP_DEFAULT   64    /* 0.25 */
#define ARB_IOCOST_VRATE_MAX_FP_DEFAULT 1024    /* 4.0 */
#define ARB_IOCOST_VRATE_DOWN_NUM        205    /* ~x0.80 per late step */
#define ARB_IOCOST_VRATE_DOWN_DEN        256
#define ARB_IOCOST_VRATE_UP_NUM          269    /* ~x1.05 per good step */
#define ARB_IOCOST_VRATE_UP_DEN          256
#define ARB_IOCOST_QOS_HYST                 2    /* consecutive windows */
#define ARB_IOCOST_MARGIN_MAX             16

/* Loader-configurable cost model + qos feedback.  Rows mirror
 * ioext_arb_common.h's arb_cost_estimate() table; op/bucket index helpers
 * are shared (arb_cost_op_type / arb_cost_size_bucket).  The qos tail is
 * zero-fallback like the rest: qos_rlat_ns==0 disables qos (vrate pinned
 * at 1.0 == pure byte-fair), zero rpct/vrate bounds fall back to the
 * compiled-in defaults above. */
struct arb_iocost_cfg {
    __u64 quantum;                          /* cost units per unit eff. weight */
    __u64 min_budget;                       /* absolute round-budget floor */
    __u64 cost_read[ARB_COST_BUCKETS];      /* 4K/16K/64K/128K/>128K */
    __u64 cost_write[ARB_COST_BUCKETS];
    /* --- qos vrate feedback (loader_arb_iocost.c --qos-* / --vrate-*) --- */
    __u64 qos_rlat_ns;      /* LC latency target (io.cost qos rlat); 0 = off */
    __u32 qos_rpct;         /* percentile that must meet rlat (io.cost rpct) */
    __u32 reserved;
    __u32 vrate_min_fp;     /* vrate floor in 256ths (25% -> 64) */
    __u32 vrate_max_fp;     /* vrate ceiling in 256ths (400% -> 1024) */
    /* --- runtime (NOT loader-written; the qos roller owns this field) --- */
    __u64 runtime_eff_quantum;  /* ceil(quantum * vrate); 0 = never stepped
                                 * (vrate 1.0) == byte-fair */
};

/* Single global qos FSM state (one per arbiter; dump with
 * `bpftool map dump name iocost_qos` for the acceptance evidence -- the
 * vrate state machine's actions are all visible here: vrate_fp,
 * window counters, step counters).  Zero-init is the NEVER-FED state:
 * vrate_fp==0 is read back as 1.0 (fp 256) everywhere. */
struct arb_iocost_qos {
    __u64 window_start_ns;  /* current window's first-sample timestamp */
    __u64 window_sum_ns;    /* sum of LC total latencies in this window */
    __u32 window_total;     /* LC completions in this window */
    __u32 window_over;      /* completions with total_latency_ns > rlat */
    __u32 late_windows;     /* consecutive violating windows (DOWN hyst) */
    __u32 good_windows;     /* consecutive comfortable windows (UP hyst) */
    __u32 vrate_fp;         /* global vrate, 256ths; 0 = never fed (==1.0) */
    __u32 down_steps;       /* telemetry: vrate cuts applied */
    __u32 up_steps;         /* telemetry: vrate raises applied */
    __u32 last_violation;   /* telemetry: last rolled window's DOWN gate */
    __u32 last_comfortable; /* telemetry: last rolled window's UP gate */
    __u64 last_sample_ns;   /* telemetry: last LC completion timestamp */
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct arb_iocost_cfg);
} iocost_cfg SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct arb_iocost_qos);
} iocost_qos SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_TENANTS);
    __type(key, __u32);
    __type(value, __u64);
} active_cgroup_ids SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, ARB_MAX_TENANTS);
    __type(key, __u64);
    __type(value, struct iosched_policy_meta);
} policy_meta SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, ARB_MAX_TENANTS * ARB_MAX_HCTX);
    __type(key, struct arb_tenant_key);
    __type(value, struct arb_tenant_state);
} tenant_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct arb_hctx_state);
} hctx_state SEC(".maps");

/* S11.1: cgroup-level demand signal (pending + inflight summed over the
 * cgroup's HOME keys + last-activity stamp).  Maintained by the
 * arb_note_* hooks via arb_outstanding_add/sub; read by arb_drr_pick's
 * dry-arm and forfeit paths.  Per-(cg,hctx) tenant keys cannot serve:
 * the pick runs on the DECISION hctx while the requests live on the
 * HOME keys. */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, ARB_MAX_TENANTS);
    __type(key, __u64);
    __type(value, struct arb_cgroup_outstanding);
} cgroup_outstanding SEC(".maps");

/* S12 cgroup-level DRR decision state (one entry per cgroup; see
 * ioext_arb_common.h struct arb_cgroup_drr). */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, ARB_MAX_TENANTS);
    __type(key, __u64);
    __type(value, struct arb_cgroup_drr);
} cgroup_drr SEC(".maps");

/* S12 global round counter + uniform lift, shared by every hctx. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct arb_global_state);
} global_state SEC(".maps");

/*
 * Per-request cost from the loader-configured table (io.cost's linear
 * model as a FIXED size-bucket table -- the calibrated-model deviation
 * (a) in the header; the ADAPTIVE part of io.cost is the vrate machine
 * above, not the cost table).  Zero entries and a missing cfg map
 * fall back to the compiled-in BYTE-LINEAR defaults (see the cost-domain
 * comment above) so the arbiter is never left without a cost model --
 * and so a bare load reproduces io.weight-style byte shares on
 * SSD-class devices instead of arb_cost's HDD-ish 4x write penalty.
 */
static __always_inline __u64 arb_iocost_estimate(__u64 opf, __u64 nr_sectors)
{
    struct arb_iocost_cfg *cfg;
    __u32 zero = 0;
    __u32 op = arb_cost_op_type(opf);
    __u32 bucket = arb_cost_size_bucket(nr_sectors);
    __u64 cost;

    /* Defensive clamps: the helpers above already return bounded values,
     * but make the map index range obvious to the verifier. */
    if (op >= ARB_COST_OPS)
        op = 0;
    if (bucket >= ARB_COST_BUCKETS)
        bucket = ARB_COST_BUCKETS - 1;

    cfg = bpf_map_lookup_elem(&iocost_cfg, &zero);
    if (cfg) {
        cost = (op == ARB_COST_OP_WRITE) ? cfg->cost_write[bucket]
                                         : cfg->cost_read[bucket];
        if (cost)
            return cost;
    }
    /* Compiled-in byte-linear default (cost = bytes/64, read == write);
     * do NOT fall back to arb_cost_estimate() -- its 4x flat write
     * penalty is the HDD-ish calibration arb_iocost deliberately does
     * not share (see the cost-domain defaults comment). */
    switch (bucket) {
    case 0:  return ARB_IOCOST_COST_4K;
    case 1:  return ARB_IOCOST_COST_16K;
    case 2:  return ARB_IOCOST_COST_64K;
    case 3:  return ARB_IOCOST_COST_128K;
    default: return ARB_IOCOST_COST_BIG;
    }
}

/*
 * vrate application cache: the qos ROLLER (single writer, once per vrate
 * step) precomputes eff_quantum = ceil(quantum * vrate) into
 * cfg->runtime_eff_quantum so pick_cgroup reads the vrate application
 * with ONE branch and NO qos-map lookup (verifier fit: pick_cgroup runs
 * against the 1M-processed-insn complexity limit — a qos lookup plus the
 * ceil arithmetic inline in the pick pushed it over, -E2BIG 20261107
 * smoke; the old byte-fair pick was already at the edge).  The cache
 * field lives in the cfg map (not the qos state) purely so the pick pays
 * zero extra lookups: it already reads cfg for quantum/min_budget.
 * 0 == never stepped == vrate 1.0 == byte-fair, exact.
 */
static __always_inline void iocost_update_eff_quantum(
    struct arb_iocost_cfg *cfg, __u32 vrate_fp)
{
    __u64 quantum;

    if (!cfg)
        return;
    quantum = cfg->quantum ? cfg->quantum : ARB_IOCOST_QUANTUM;
    if (vrate_fp == ARB_IOCOST_VRATE_FP_ONE) {
        cfg->runtime_eff_quantum = 0;   /* exact byte-fair fast path */
        return;
    }
    cfg->runtime_eff_quantum = (quantum * vrate_fp +
                                ARB_IOCOST_VRATE_FP_ONE - 1) /
                               ARB_IOCOST_VRATE_FP_ONE;
}

/*
 * LC inuse margin (io.cost's per-target debt/margin layer, the homomorph
 * of ioc_timer_fn's weight adaptation for late cgroups).
 *
 * Native io.cost cuts the GLOBAL vrate -- every cgroup's period budget
 * shrinks together -- but the cgroup that MISSES its target keeps
 * consuming at its demand side: it runs in vtime debt and its effective
 * share (hweight_in) is protected by the inuse/margin mechanism, so the
 * throttle lands on the surplus consumers (BE).  The homo-v2 campaign
 * measured exactly that: native io.cost qos left LC at ~its uncapped rate
 * (lc_iops 90905 ~= none 90858) while BE dropped below its weight share
 * (be_share 0.20).
 *
 * This DRR core is ORDER-based (event-driven rounds, no time-rate), so
 * the uniform vrate component alone is share-preserving: it shrinks every
 * nominal round budget by the same factor and the LC:BE service ratio
 * would not move.  The margin restores io.cost's split: applied on top
 * of the uniform cut, margin_LC = clamp(ceil((256/vrate_fp)^2), 1, 16)
 * gives budget_LC = quantum*vrate*w_LC*margin = byte-fair*(1/vrate)*lift
 * (the target holder's effective issuance RISES by 1/vrate as vrate
 * falls, exactly like running in debt) while budget_BE keeps the bare
 * quantum*vrate*w_BE cut.  Net: the LC:BE issuance ratio moves by
 * 1/vrate^2 LC-ward under qos pressure and BE absorbs the cut -- the
 * native observable.  At vrate==1.0 the margin is exactly 1 (byte-fair
 * bit-identical); vrate>1 (deep comfort) clamps to 1 (no LC penalty).
 *
 * DELIVERY (verifier fit): both vrate applications are PRECOMPUTED and
 * CACHED outside the pick — the margin in the per-tenant
 * arb_tenant_state.boost field (the framework's own weight-injection
 * channel, exactly arb_iolatency's iolat_get_boost pattern; written by
 * the qos feed, one u64 division per LC completion) and eff_quantum in
 * cfg->runtime_eff_quantum (written by the qos roller on every vrate
 * step).  Computing either inline in the pick pushed pick_cgroup past
 * the verifier's 1M-processed-insn complexity limit (-E2BIG, 20261107
 * smoke: the byte-fair pick already sat at the edge); the cached form
 * keeps the pick-side additions to one map lookup (get_boost) plus one
 * branch (the eff_quantum cache read).  BE tenants are never written by
 * the feed (their boost stays the zero-init ARB_SLO_BOOST_NORMAL == 1):
 * the BE floor.
 *
 * The margin ladder (fp -> margin): 256->1, 205->2, 164->3, 131->4,
 * 104->7, 83->10, 66->16, 64->16 -- geometric with the vrate cuts,
 * capped at ARB_IOCOST_MARGIN_MAX (the ratio headroom the acceptance
 * runs need; io.cost's hweight adaptation is likewise bounded by its
 * margin steps).
 */
static __u32 iocost_get_boost(void *states_map, __u64 cgid, __u32 hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgid,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;

    ts = bpf_map_lookup_elem(states_map, &key);
    if (!ts)
        return 1;
    return ts->boost;
}

/*
 * QoS window feed: ONE global sliding window over LC-role completions
 * (prio <= ARB_IOCOST_PRIO_LC_MAX, the arb_iolatency口径), window width
 * and min-sample gate follow the iolatency conventions
 * (ARB_SLO_WINDOW_NS = 100ms, ARB_SLO_MIN_SAMPLES = 20).
 *
 * DOWN gate: fraction of samples over rlat > (100 - qos_rpct)% -- the
 * EXACT p(100-rpct) > rlat percentile condition (p99 <= rlat <=> at most
 * 1% of samples over rlat), no mean approximation.  UP gate: window
 * mean < rlat/2 (deep comfort).
 *
 * The feed also refreshes the LC tenant's margin cache (ts->boost, see
 * iocost_get_boost) on its completion-hctx key — the known framework
 * caveat #10 (completion cgid/hctx can differ from the insert path)
 * applies to the boost cache exactly as it does to arb_iolatency's boost
 * ladder; a miss reads as boost 1 (byte-fair) and the next completion on
 * the decision hctx re-arms it.
 *
 * Concurrency: completions fire from multiple CPUs.  Counters are XADD;
 * the window roll (start_ns CAS old->now) picks exactly one roller per
 * window who evaluates the gates, steps vrate and resets the counters.
 * A sample XADDed between the roller's counter read and its reset is
 * lost from both windows -- bounded to one structural undercount, self
 * corrects next window (same race shape the shared core documents for
 * the own_max_charge window).
 */
static __always_inline void iocost_qos_feed(__u64 cgid, __u32 hctx_idx,
                                            __u64 now_ns, __u64 lat_ns)
{
    struct arb_iocost_cfg *cfg;
    struct iosched_policy_meta *meta;
    struct arb_iocost_qos *qs;
    struct arb_tenant_key tkey;
    struct arb_tenant_state *ts;
    __u32 zero = 0;
    __u64 rlat;
    __u32 rpct;
    __u32 vmin, vmax;

    cfg = bpf_map_lookup_elem(&iocost_cfg, &zero);
    if (!cfg || cfg->qos_rlat_ns == 0)
        return;                        /* qos disabled: vrate pinned at 1.0 */
    rlat = cfg->qos_rlat_ns;

    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (!meta || meta->arb_prio > ARB_IOCOST_PRIO_LC_MAX)
        return;                       /* BE completions carry no target */

    qs = bpf_map_lookup_elem(&iocost_qos, &zero);
    if (!qs)
        return;

    /* First-ever sample opens the window (CAS so a racing first sample
     * does not re-open it). */
    if (qs->window_start_ns == 0)
        __sync_val_compare_and_swap(&qs->window_start_ns, 0, now_ns);

    if (now_ns - qs->window_start_ns >= ARB_SLO_WINDOW_NS) {
        __u64 old_start = qs->window_start_ns;

        /* Roll: exactly one roller wins the CAS and evaluates. */
        if (__sync_val_compare_and_swap(&qs->window_start_ns,
                                        old_start, now_ns) == old_start) {
            __u32 total = qs->window_total;
            __u32 over = qs->window_over;
            __u64 sum = qs->window_sum_ns;
            __u32 fp;
            int violation = 0;
            int comfortable = 0;

            rpct = cfg->qos_rpct;
            if (rpct == 0)
                rpct = ARB_IOCOST_QOS_RPCT_DEFAULT;
            if (rpct > 100)
                rpct = 100;
            vmin = cfg->vrate_min_fp;
            if (vmin == 0)
                vmin = ARB_IOCOST_VRATE_MIN_FP_DEFAULT;
            if (vmin < 1)
                vmin = 1;
            vmax = cfg->vrate_max_fp;
            if (vmax == 0)
                vmax = ARB_IOCOST_VRATE_MAX_FP_DEFAULT;
            if (vmax < vmin)
                vmax = vmin;

            if (total >= ARB_SLO_MIN_SAMPLES) {
                /* p(100-rpct) > rlat  <=>  over-fraction > (100-rpct)% */
                if ((__u64)over * 100 > (__u64)total * (100 - rpct))
                    violation = 1;
                else if (sum * 2 < (__u64)total * rlat)
                    comfortable = 1;    /* window mean < rlat/2 */
            }

            qs->last_violation = violation;
            qs->last_comfortable = comfortable;

            if (violation) {
                qs->late_windows++;
                qs->good_windows = 0;
                if (qs->late_windows >= ARB_IOCOST_QOS_HYST) {
                    /* vrate DOWN: geometric x0.8 step, clamped at
                     * vrate_min; re-arm the hysteresis for the next cut. */
                    fp = qs->vrate_fp ? qs->vrate_fp
                                      : ARB_IOCOST_VRATE_FP_ONE;
                    fp = (__u32)((__u64)fp *
                                 ARB_IOCOST_VRATE_DOWN_NUM /
                                 ARB_IOCOST_VRATE_DOWN_DEN);
                    if (fp < vmin)
                        fp = vmin;
                    qs->vrate_fp = fp;
                    iocost_update_eff_quantum(cfg, fp);
                    qs->late_windows = 0;
                    qs->down_steps++;
                }
            } else {
                qs->late_windows = 0;
                if (comfortable) {
                    qs->good_windows++;
                    if (qs->good_windows >= ARB_IOCOST_QOS_HYST) {
                        /* vrate UP: slow x1.05 rise, clamped at vrate_max
                         * (io.cost raises the same way: ~5% per good
                         * period, never above max). */
                        fp = qs->vrate_fp ? qs->vrate_fp
                                          : ARB_IOCOST_VRATE_FP_ONE;
                        fp = (__u32)((__u64)fp *
                                     ARB_IOCOST_VRATE_UP_NUM /
                                     ARB_IOCOST_VRATE_UP_DEN);
                        if (fp > vmax)
                            fp = vmax;
                        qs->vrate_fp = fp;
                        iocost_update_eff_quantum(cfg, fp);
                        qs->good_windows = 0;
                        qs->up_steps++;
                    }
                } else {
                    qs->good_windows = 0;
                }
            }

            /* window rolled: reset the accumulators (racing samples
             * between the read above and this reset are lost once --
             * bounded, see the concurrency note). */
            qs->window_total = 0;
            qs->window_over = 0;
            qs->window_sum_ns = 0;
        }
    }

    /* Count THIS completion into the (possibly just rolled) window. */
    __sync_fetch_and_add(&qs->window_total, 1);
    __sync_fetch_and_add(&qs->window_sum_ns, lat_ns);
    if (lat_ns > rlat)
        __sync_fetch_and_add(&qs->window_over, 1);
    qs->last_sample_ns = now_ns;

    /* Refresh the LC margin cache (see iocost_get_boost): the margin is
     * a pure function of the CURRENT vrate, recomputed per LC completion
     * so a stale boost can survive at most one completion interval. */
    {
        __u32 fp = qs->vrate_fp ? qs->vrate_fp : ARB_IOCOST_VRATE_FP_ONE;
        __u64 den = (__u64)fp * (__u64)fp;
        __u64 margin = (256ULL * 256ULL + den - 1) / den;

        if (margin > ARB_IOCOST_MARGIN_MAX)
            margin = ARB_IOCOST_MARGIN_MAX;
        if (margin < 1)
            margin = 1;

        tkey.cgroup_id = cgid;
        tkey.hctx_idx = hctx_idx;
        tkey.reserved = 0;
        ts = bpf_map_lookup_elem(&tenant_state, &tkey);
        if (ts && ts->boost != (__u32)margin)
            ts->boost = (__u32)margin;
    }
}

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct arb_iocost_cfg *cfg;
    __u32 zero = 0;
    __u32 hctx_key;
    struct arb_hctx_state *hs;
    __u64 now_ns;
    __u64 quantum = ARB_IOCOST_QUANTUM;
    __u64 min_budget = ARB_IOCOST_MIN_BUDGET;

    if (!dctx)
        return 0;

    hctx_key = (__u32)dctx->hctx_idx;
    hs = bpf_map_lookup_elem(&hctx_state, &hctx_key);
    if (!hs)
        return 0;

    cfg = bpf_map_lookup_elem(&iocost_cfg, &zero);
    if (cfg) {
        if (cfg->runtime_eff_quantum)
            /* io.cost global vrate application: EVERY cgroup's nominal
             * round budget = eff_quantum * weight, where eff_quantum =
             * ceil(quantum * vrate) is precomputed by the qos roller (see
             * iocost_update_eff_quantum; 0 == vrate 1.0 == byte-fair,
             * exact).  The per-role margin rides iocost_get_boost. */
            quantum = cfg->runtime_eff_quantum;
        else if (cfg->quantum)
            quantum = cfg->quantum;
        if (cfg->min_budget)
            min_budget = cfg->min_budget;   /* absolute floor: unscaled
                                              * (liveness floor, not a
                                              * share knob) */
    }

    now_ns = bpf_ktime_get_ns();

    __u64 cgid = arb_drr_pick(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, &global_state, hs,
                              &active_cgroup_ids,
                              &policy_meta,
                              hctx_key,
                              now_ns,
                              quantum,
                              min_budget,
                              iocost_get_boost,
                            0);

    if (cgid) {
        IOEXT_BPF_DBG_SAMPLED(128,
            "[arb_iocost] pick cg=%llu hctx=%u\n",
            cgid, hctx_key);
    }

    return cgid;
}

SEC("struct_ops/inserted_request")
void BPF_PROG(inserted_request, struct iosched_inserted_ctx *ictx)
{
    if (!ictx)
        return;
    arb_note_inserted(&tenant_state, &cgroup_outstanding,
                      &cgroup_drr, &global_state, ictx->policy_cgroup_id,
                      (__u32)ictx->hctx_idx, bpf_ktime_get_ns());
}

SEC("struct_ops/dispatched_request")
void BPF_PROG(dispatched_request, struct iosched_dispatched_ctx *dctx)
{
    __u64 cost;

    if (!dctx)
        return;
    cost = arb_iocost_estimate(dctx->opf, dctx->nr_sectors);
    /* S11: per-key own-max-charge tracking moved into
     * arb_note_dispatched_* (decision-key ts, no extra lookup). */
    arb_note_dispatched_cost(&tenant_state, &cgroup_drr, &global_state,
                             dctx->selected_cgroup_id,
                             (__u32)dctx->hctx_idx,
                             (__u32)dctx->home_hctx_idx,
                             cost);

    IOEXT_BPF_DBG_SAMPLED(256,
        "[arb_iocost] charge cg=%llu cost=%llu opf=%llu sectors=%llu\n",
        dctx->selected_cgroup_id, cost, dctx->opf, dctx->nr_sectors);
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    if (!cctx)
        return;
    arb_note_completed(&tenant_state, &cgroup_outstanding,
                       cctx->cgroup_id, (__u32)cctx->hctx_idx, bpf_ktime_get_ns());
    /* io.cost qos feedback: LC completions feed the global vrate window
     * and refresh the LC margin cache (total_latency_ns is the
     * scheduler+device latency, the same signal arb_iolatency's window
     * FSM uses and the same latency fio's p99 reports). */
    iocost_qos_feed(cctx->cgroup_id, (__u32)cctx->hctx_idx, cctx->now,
                    cctx->total_latency_ns);
}

SEC("struct_ops/dispatch_failed")
void BPF_PROG(dispatch_failed, struct iosched_dispatch_failed_ctx *fctx)
{
    __u32 hkey;
    struct arb_hctx_state *hs;

    if (!fctx)
        return;
    hkey = (__u32)fctx->hctx_idx;
    hs = bpf_map_lookup_elem(&hctx_state, &hkey);
    arb_note_dispatch_failed(&tenant_state, &cgroup_outstanding,
                              &cgroup_drr, &global_state, fctx->selected_cgroup_id,
                              hkey, bpf_ktime_get_ns());
    arb_drr_advance_cursor(hs);
}

SEC("struct_ops/requeue_refund")
void BPF_PROG(requeue_refund, struct iosched_dispatched_ctx *rctx)
{
    if (!rctx)
        return;
    /* Refund must mirror the dispatched charge exactly: same fixed-cost
     * estimate on the same request (guide §6 pitfall 4 / requeue contract). */
    arb_note_requeue(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, rctx->selected_cgroup_id,
                          (__u32)rctx->hctx_idx,
                          (__u32)rctx->home_hctx_idx,
                          arb_iocost_estimate(rctx->opf, rctx->nr_sectors), bpf_ktime_get_ns());
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops iocost_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_ALLOW_FALLBACK,
    .name = "arb_iocost",
};
