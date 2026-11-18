/* arb_d2fq.bpf.c - D2FQ (FAST'21) homomorphic arbiter: device-cost weighted
 * virtual-time fair queueing with D2FQ fairness classes and deceptive-idleness
 * reset.
 *
 * Homo reproduction of "D2FQ: Device-Direct Fair Queueing for NVMe SSDs"
 * (Woo, Ahn, Lee, Jeong; FAST'21) on the iosched_ext arbiter interface,
 * cross-checked against the open-source reference kernel
 * skkucsl/d2fq reference tree (block/d2fq.c, drivers/nvme/host/pci.c):
 *
 *   reference mechanism                    this arbiter
 *   ------------------------------------  ------------------------------------
 *   per-flow lvt (local virtual time),    per-cgroup state with one vtime
 *   charge at completion:                 lane per IO type (read/write); all
 *                                         hctx share the lane pair (the q
 *                                         policy holds a per-CGROUP queue and
 *                                         any hctx may pop it, so lane state
 *                                         split per hctx would fragment the
 *                                         eligible set and destroy the global
 *                                         weight semantics -- see the
 *                                         framework mapping below)
 *     vt_unit = 840 / weight;               vtime[lane] += cost(op,size)
 *     tmp = (vt_unit * dlen) >> 10;                 * VT_NUM * type_wt[lane]
 *   weight = 8 - (ioc->ioprio & 0xF)         / (100 * arb_weight)
 *                                          (same 840*2^10 scale, one exact
 *                                          division instead of the reference's
 *                                          truncated vt_unit)
 *   device-calibrated per-IO-type cost     d2fq_cost_table[op][size_bucket]
 *   (sysctl read/write weight, dlen):      (default = arb_cost x64 table,
 *                                          loader --cost-table override) +
 *                                          --type-weights percent multiplier
 *   gvt = min lvt over active flows        per-hctx cached gvt = min vtime
 *   (union global_holder, gh_update_min)  over eligible (pending>0, not
 *                                          no-progress-blacklisted) lanes,
 *                                          refreshed every pick scan
 *   fairness class at request start:      per-lane band at pick:
 *     gap = lvt - gvt;                       gap = vtime - gvt;
 *     gap >  thrsh        -> LOW  (WRR l)    gap > md_range -> MEDIUM
 *     gap >  md_range     -> MEDIUM(WRR m)   gap >  thrsh   -> LOW
 *     else                -> HIGH (WRR h)    else          -> HIGH
 *     md_range = thrsh*(100-md_ratio)/100    (thrsh==0 disables banding:
 *                                             pure min-vtime WFQ)
 *   device WRR queues arbitrate classes;  the arbiter picks the best-band
 *   with per-class WRR WEIGHTS (the h:m:l lane first, min vtime inside the
 *   device does not see flows: class  band (see the sort-key note in
 *   weights are the actual service      d2fq_pick: with a common gvt the
 *   differentiator)                     band order equals min-vtime order,
 *                                          so the band is carried in the
 *                                          sort key and telemetry, and the
 *                                          CLASS WEIGHTS act through the
 *                                          charge below)
 *   LC fast share: latency-critical flows arb_prio < --lc-prio-max (default 3,
 *   stay near gvt and ride the HIGH WRR    arb_mix convention) lanes are
 *   queue -> low latency                   charged 1/fast_share of the
 *                                          vtime their weight implies
 *                                          (--fast-share, default 6 =
 *                                          sysctl_d2fq_cls_base enabled
 *                                          value: HIGH:LOW WRR weight
 *                                          ratio cls_base:1).  This is the
 *                                          stable host-direct analogue of
 *                                          the device WRR class weights:
 *                                          a bounded, weighted fast share
 *                                          for LC tenants, work-conserving
 *                                          and starvation-free (WFQ bounds
 *                                          the lead).
 *   deceptive idleness (di_timer,          inserted_request after both lanes
 *   lvt reset to gvt on reactivation):     were empty for > di_period_ns
 *                                          resets both vtimes to the cached
 *                                          gvt (no vtime banking across
 *                                          idleness)
 *   dynamic WRR weight re-tuning           NOT implemented (offline policy;
 *   (d2fq_timer_fn slowdown scan)          see deviations in the model)
 *   single active flow -> MEDIUM queue     skipped (host arbiter has no
 *                                          device WRR queue to spare)
 *
 * Framework mapping / charge point:
 *   - charge happens at dispatched_request (the framework's charge hook; the
 *     reference charges at completion), because the completion hook's cgroup
 *     attribution is known-broken in this kernel (ARB_POLICY_DEV_GUIDE.md
 *     pitfall 10) while dispatched_request carries selected_cgroup_id +
 *     home_hctx_idx.  requeue_refund refunds the exact same advance (the
 *     integer expression is recomputed identically), pairing charge/refund
 *     like every strict arbiter.
 *   - ALL scheduling state (vtime lanes, pending, telemetry totals) is keyed
 *     by cgroup ALONE (hctx_idx 0).  The q policy holds one per-cgroup queue
 *     and scx_io_validate_dispatch_rq() only requires the request to belong
 *     to the same QUEUE, not the same hctx -- any hctx's pick can pop any
 *     queued request.  Keying the WFQ lanes per (cgroup,hctx) (the
 *     pre-2026-11-14 form) fragmented the eligible set into lane-local
 *     views: with two tenants spread over the device's hctx, each lane's
 *     picks served whoever had lane-local pending and drained that
 *     tenant's WHOLE global queue in ownership episodes -- aggregate
 *     shares collapsed to ~1:1 regardless of the weights (d2fq campaign
 *     2026-11-14: per-hctx dispatch skews 14.5:1 / 1:6.7 / 1:46 that sum
 *     to 0.500 at weight 100:400).  This mirrors arb_wdrr's S12 move to
 *     cgroup-level DRR state for the same reason; per-hctx state remains
 *     only as pick telemetry (picks/band_picks/last_lane) and the global
 *     gvt cache lives in hctx_state[0].
 *   - strict mode (IOSCHED_ARB_STRICT): liveness contract requires that
 *     pick_cgroup returns a cgid whenever any tenant has pending > 0.  The
 *     F2-style no-progress guard below only skips a tenant that repeatedly
 *     wins picks without dispatching, and only for a bounded retry window
 *     (otherwise a holding queue policy, e.g. q_linnos, could never release
 *     its held requests: releases only happen inside cgroup_dispatch, which
 *     only runs for the picked cgroup).
 *
 * The behavioral spec is mirrored by the reference model in
 * tests/io/arb_d2fq_model.py; keep the two in sync.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_common.h"

char LICENSE[] SEC("license") = "GPL";

/*
 * vtime scale: the reference computes tmp = (840/weight * dlen) >> 10;
 * folding the >>10 into the numerator keeps one exact division per charge
 * (truncation error <= 1 unit on an 860160-unit scale, vs up to 17% for the
 * reference's truncated 840/weight at loader weights like 100..1600).
 */
#define D2FQ_VT_NUM             (840ULL << 10)
/* Default class threshold: 64 cost units at weight 1 (the default cost table
 * is scaled x64, so this is "one max-budget of a weight-100 tenant" or
 * ~100 4K-read charges at weight 100).  0 disables banding entirely. */
#define D2FQ_DEFAULT_THRSH      (64ULL * D2FQ_VT_NUM)
#define D2FQ_DEFAULT_MD_RATIO   70U     /* md_range = thrsh*(100-70)/100 */
#define D2FQ_DEFAULT_LC_PRIO_MAX 3U     /* arb_prio < 3 -> LC (arb_mix line) */
/* LC fast share = the class-weight ratio analogue (reference
 * sysctl_d2fq_cls_base = 6 when enabled: the device WRR serves the HIGH
 * class with 6x the LOW class weight).  LC lanes are charged 1/6 of the
 * vtime their weight implies.  1 disables. */
#define D2FQ_DEFAULT_FAST_SHARE 6U
/* Reference: D2FQ_DI_SLICE * sysctl_d2fq_di_period = 50 jiffies = 0.2s. */
#define D2FQ_DEFAULT_DI_PERIOD_NS (200ULL * 1000000ULL)
/* F2-style empty-pop guard: consecutive winning picks that dispatched
 * nothing; beyond this the tenant is skipped for NOPROGRESS_RETRY_NS so the
 * scan reaches the other tenants. */
#define D2FQ_PICK_NO_PROGRESS_MAX 3
#define D2FQ_NOPROGRESS_RETRY_NS  (1ULL * 1000000ULL)
#define D2FQ_DISPATCH_FAIL_GHOST_RESET 4

#define D2FQ_LANE_READ   0
#define D2FQ_LANE_WRITE  1
#define D2FQ_LANES       2

#define D2FQ_BAND_HIGH   0
#define D2FQ_BAND_MEDIUM 1
#define D2FQ_BAND_LOW    2

/* Loader-written cfg (bpf_map_update_elem before BPF_LINK_CREATE; read-only
 * afterwards).  Zero value = "use compiled defaults" for every field, so a
 * missing/zero map entry behaves exactly like the reference defaults. */
struct d2fq_cfg {
    __u64 thrsh;              /* vtime gap threshold for the LOW band */
    __u32 md_ratio;           /* percent; MEDIUM band lower edge */
    __u32 lc_prio_max;        /* arb_prio < lc_prio_max -> LC fast share */
    __u32 fast_share;         /* LC vtime charge divisor (cls_base analogue) */
    __u32 type_weight[D2FQ_LANES]; /* read/write vtime multiplier, percent */
    __u64 di_period_ns;       /* deceptive-idleness reset window */
    __u32 reserved;
};

/* Device-calibrated per-IO-type cost table: one row per type (row 0 = READ,
 * row 1 = WRITE; ARB_COST_OP_* indices), one column per size bucket
 * (ARB_COST_BUCKETS, arb_cost_size_bucket boundaries).  Default = the arb_cost
 * x64-scaled table (4K read = 64); loader --cost-table overrides both rows. */
struct d2fq_cost_row {
    __u64 bucket[ARB_COST_BUCKETS];
};

struct d2fq_lane {
    __u64 vtime;              /* local virtual time (scaled units) */
    __u64 pending;            /* insert-path request counter (home hctx) */
    __u64 charges;            /* cost units charged (telemetry) */
    __u64 dispatches;
    __u64 picks;              /* times this lane won the pick */
};

struct d2fq_tenant_state {
    struct d2fq_lane lane[D2FQ_LANES];
    __u64 inflight;           /* telemetry only (completion cgid attribution
                               * is unreliable - ARB guide pitfall 10) */
    __u64 inserts;
    __u64 requeues;
    __u64 refunded_cost;
    __u64 di_resets;
    __u64 last_active_ns;     /* last insert or dispatch */
    __u64 last_pick_ns;       /* last winning pick (no-progress windowing) */
    __u64 last_pick_dispatches; /* dispatch snapshot at previous winning pick */
    __u32 pick_no_progress;   /* winning picks with no dispatch since */
    __u32 dispatch_fails;
};

struct d2fq_hctx_state {
    __u64 gvt;                /* slot 0: GLOBAL cached min eligible lane
                               * vtime (consumed by DI resets); slots >0
                               * unused -- per-hctx picks/band_picks/
                               * last_lane below stay per-hctx telemetry */
    __u64 picks;
    __u64 band_picks[3];
    __u32 last_lane;          /* lane of the most recent winning pick */
};

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
    __type(value, struct d2fq_tenant_state);
} tenant_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct d2fq_hctx_state);
} hctx_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct d2fq_cfg);
} d2fq_cfg_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, D2FQ_LANES);
    __type(key, __u32);
    __type(value, struct d2fq_cost_row);
} d2fq_cost_table SEC(".maps");

static __always_inline void d2fq_cfg_read(struct d2fq_cfg *out)
{
    struct d2fq_cfg *cfg;
    __u32 key = 0;

    out->thrsh = D2FQ_DEFAULT_THRSH;
    out->md_ratio = D2FQ_DEFAULT_MD_RATIO;
    out->lc_prio_max = D2FQ_DEFAULT_LC_PRIO_MAX;
    out->fast_share = D2FQ_DEFAULT_FAST_SHARE;
    out->type_weight[D2FQ_LANE_READ] = 100;
    out->type_weight[D2FQ_LANE_WRITE] = 100;
    out->di_period_ns = D2FQ_DEFAULT_DI_PERIOD_NS;

    cfg = bpf_map_lookup_elem(&d2fq_cfg_map, &key);
    if (!cfg)
        return;
    if (cfg->thrsh)
        out->thrsh = cfg->thrsh;
    if (cfg->md_ratio <= 100)
        out->md_ratio = cfg->md_ratio;
    if (cfg->lc_prio_max)
        out->lc_prio_max = cfg->lc_prio_max;
    if (cfg->fast_share)
        out->fast_share = cfg->fast_share;
    if (cfg->type_weight[D2FQ_LANE_READ])
        out->type_weight[D2FQ_LANE_READ] = cfg->type_weight[D2FQ_LANE_READ];
    if (cfg->type_weight[D2FQ_LANE_WRITE])
        out->type_weight[D2FQ_LANE_WRITE] = cfg->type_weight[D2FQ_LANE_WRITE];
    if (cfg->di_period_ns)
        out->di_period_ns = cfg->di_period_ns;
}

static __always_inline __u64 d2fq_cost(__u64 opf, __u64 nr_sectors)
{
    struct d2fq_cost_row default_row;
    struct d2fq_cost_row *row;
    __u32 op = arb_cost_op_type(opf);
    __u32 bkt = arb_cost_size_bucket(nr_sectors);

    row = bpf_map_lookup_elem(&d2fq_cost_table, &op);
    if (!row) {
        /* Loader never wrote the table: compiled defaults (arb_cost x64
         * table) so a bare BPF load without the loader still works. */
        if (op == ARB_COST_OP_WRITE) {
            default_row.bucket[0] = 256;
            default_row.bucket[1] = 1024;
            default_row.bucket[2] = 4096;
            default_row.bucket[3] = 8192;
            default_row.bucket[4] = 16384;
        } else {
            default_row.bucket[0] = 64;
            default_row.bucket[1] = 256;
            default_row.bucket[2] = 1024;
            default_row.bucket[3] = 2048;
            default_row.bucket[4] = 4096;
        }
        row = &default_row;
    }
    if (bkt >= ARB_COST_BUCKETS)
        bkt = ARB_COST_BUCKETS - 1;
    return row->bucket[bkt];
}

/*
 * vtime advance for one dispatched request: the reference's
 * (840/weight * dlen) >> 10, folded into one exact division and extended
 * with the two weight knobs:
 *   type_weight[lane]  per-IO-type service-share weight (percent, 100 =
 *                      cost-table proportional; the reference's
 *                      sysctl_d2fq_read/write_weight analogue),
 *   fast_share         LC fast share: an LC lane's charge is divided by
 *                      fast_share (default 6 = sysctl_d2fq_cls_base
 *                      enabled value, the device WRR HIGH:LOW weight
 *                      ratio), so LC tenants ride a bounded, weighted
 *                      fast share of the device.
 * Loader-side cost entries are clamped so this cannot overflow u64
 * (cost <= 1e9: 1e9 * 860160 * 100 / (1 * 1 * 1) < 2^63).
 */
static __always_inline __u64 d2fq_advance(const struct d2fq_cfg *cfg,
                                          __u64 cost, __u32 lane, bool lc,
                                          __u64 weight)
{
    __u64 tw, div;

    if (weight == 0)
        weight = 1;
    /* Constant-offset select, not type_weight[lane]: a runtime index into
     * this stack array lowers to a bitwise-OR address on the frame
     * pointer, which the verifier rejects ("Do bitwise operations on
     * scalar values, not on pointer-valued registers"); lane is 0/1. */
    tw = lane ? cfg->type_weight[D2FQ_LANE_WRITE]
              : cfg->type_weight[D2FQ_LANE_READ];
    if (!tw)
        tw = 100;
    div = tw * weight;
    if (lc && cfg->fast_share > 1)
        div *= cfg->fast_share;
    return cost * D2FQ_VT_NUM * 100ULL / div;
}

static __always_inline void d2fq_ensure_tenant(__u64 cgroup_id)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = 0,
        .reserved = 0,
    };
    struct d2fq_tenant_state *ts;

    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (ts)
        return;
    bpf_map_update_elem(&tenant_state, &key,
                        &(struct d2fq_tenant_state){}, BPF_ANY);
}

static __always_inline void d2fq_note_inserted(__u64 cgroup_id,
                                               __u64 opf, __u64 now_ns)
{
    struct d2fq_hctx_state *hs;
    struct d2fq_cfg cfg;
    struct d2fq_tenant_state *ts;
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = 0,
        .reserved = 0,
    };
    __u32 hkey = 0;
    __u32 lane = arb_cost_op_type(opf);

    d2fq_ensure_tenant(cgroup_id);
    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return;

    if (ts->lane[D2FQ_LANE_READ].pending == 0 &&
        ts->lane[D2FQ_LANE_WRITE].pending == 0 &&
        ts->inflight == 0) {
        /*
         * Deceptive idleness: the tenant has NOTHING queued AND nothing
         * in flight.  If it has been in that state beyond di_period, reset
         * its vtimes to the cached gvt (reference: di_timer fires when the
         * flow has no requests in the device, lvt := gvt on reactivation)
         * so an idled tenant cannot bank vtime credit and harvest a
         * catch-up burst on wake.  A short idle (< di_period) keeps the
         * credit, matching the reference.
         *
         * The inflight==0 guard is load-bearing, not cosmetic: without it
         * a THROTTLED tenant whose queue happens to be fully dispatched
         * (pending==0 while its requests are still completing) reads as
         * "idle" at its next insert, and the reset erases the very vtime
         * deficit that WFQ throttling relies on -- measured 2026-11-14 as
         * a 0.72 share instead of 0.80 with ~1 DI reset/s on the starved
         * tenant (smoke_d2fq_fix, clat p99.9 918ms = the stuck-queue
         * phase between resets).
         */
        d2fq_cfg_read(&cfg);
        if (ts->last_active_ns &&
            now_ns - ts->last_active_ns > cfg.di_period_ns) {
            hs = bpf_map_lookup_elem(&hctx_state, &hkey);
            if (hs) {
                ts->lane[D2FQ_LANE_READ].vtime = hs->gvt;
                ts->lane[D2FQ_LANE_WRITE].vtime = hs->gvt;
                ts->di_resets++;
            }
        }
    }

    /* Fresh arrivals un-blacklist the no-progress guard: new work gets a
     * fresh chance to be picked and dispatched. */
    ts->pick_no_progress = 0;
    ts->last_active_ns = now_ns;
    __sync_fetch_and_add(&ts->lane[lane].pending, 1);
    __sync_fetch_and_add(&ts->inserts, 1);
}

/*
 * Dispatch-time charge on the cgroup-level key.  pending-- pairs the
 * insert-path counter (cgroup-wide: the q policy queue is per-cgroup and
 * any hctx may pop it), and the vtime advance lands on the same cgroup
 * lane so the WFQ feedback loop is global across hctx.
 */
static __always_inline void d2fq_note_dispatched(__u64 cgroup_id,
                                                 __u64 opf, __u64 nr_sectors,
                                                 __u64 weight, __u32 arb_prio,
                                                 __u64 now_ns)
{
    struct d2fq_cfg cfg;
    struct d2fq_tenant_state *ts;
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = 0,
        .reserved = 0,
    };
    __u64 cost, adv;
    bool lc;
    __u32 lane = arb_cost_op_type(opf);

    d2fq_cfg_read(&cfg);
    lc = arb_prio && arb_prio < cfg.lc_prio_max;
    cost = d2fq_cost(opf, nr_sectors);
    adv = d2fq_advance(&cfg, cost, lane, lc, weight);

    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return;
    if (lane < D2FQ_LANES) {
        if (ts->lane[lane].pending)
            __sync_fetch_and_add(&ts->lane[lane].pending, -1);
    }
    __sync_fetch_and_add(&ts->inflight, 1);
    ts->pick_no_progress = 0;
    ts->dispatch_fails = 0;
    ts->last_active_ns = now_ns;
    __sync_fetch_and_add(&ts->lane[lane].vtime, adv);
    __sync_fetch_and_add(&ts->lane[lane].charges, cost);
    __sync_fetch_and_add(&ts->lane[lane].dispatches, 1);
}

/* Requeue refund: exact inverse of the charge (same integer expression),
 * on the same cgroup-level key.  Never touches pending (guide pitfall
 * 4): the kernel re-inserts the request and re-fires inserted_request. */
static __always_inline void d2fq_note_requeue(__u64 cgroup_id,
                                              __u64 opf, __u64 nr_sectors,
                                              __u64 weight, __u32 arb_prio)
{
    struct d2fq_cfg cfg;
    struct d2fq_tenant_state *ts;
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = 0,
        .reserved = 0,
    };
    __u64 cost, adv;
    bool lc;
    __u32 lane = arb_cost_op_type(opf);

    d2fq_cfg_read(&cfg);
    lc = arb_prio && arb_prio < cfg.lc_prio_max;
    cost = d2fq_cost(opf, nr_sectors);
    adv = d2fq_advance(&cfg, cost, lane, lc, weight);

    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return;
    if (ts->inflight)
        __sync_fetch_and_add(&ts->inflight, -1);
    if (adv <= ts->lane[lane].vtime)
        __sync_fetch_and_add(&ts->lane[lane].vtime, -adv);
    else
        ts->lane[lane].vtime = 0;
    __sync_fetch_and_add(&ts->refunded_cost, cost);
    __sync_fetch_and_add(&ts->requeues, 1);
}

/*
 * D2FQ band for one lane (reference __d2fq_start_request):
 *   HIGH   while the lane's vtime is within md_range of gvt,
 *   MEDIUM up to thrsh,
 *   LOW    beyond.
 * Band order is monotone in vtime for a common gvt, so as a SORT KEY it is
 * order-equivalent to min-vtime; it is carried in the pick key and the
 * telemetry (band_picks) because it is the D2FQ fairness-class vocabulary,
 * and the class WEIGHTS act through d2fq_advance (LC fast share) instead.
 * thrsh == 0 disables banding (pure WFQ).  md_range = thrsh*(100-md_ratio)/100
 * is precomputed once per pick by the caller (same integer expression).
 */
static __always_inline __u32 d2fq_band(__u64 gap, __u64 thrsh, __u64 md_range)
{
    if (!thrsh)
        return D2FQ_BAND_HIGH;
    if (gap <= md_range)
        return D2FQ_BAND_HIGH;
    if (gap <= thrsh)
        return D2FQ_BAND_MEDIUM;
    return D2FQ_BAND_LOW;
}

/*
 * Pick working set carried across the two bpf_loop scans (one per active-
 * list slot i).  Iterations are caller-bounded at ARB_MAX_TENANTS, so the
 * scan body is verified ONCE as a bpf_loop callback instead of as an
 * open-coded 64-iteration x two-pass loop -- the open-coded form never had
 * its loop-back-edge states pruned (every iteration re-walked the body with
 * fresh scalar ids on the spilled key/now slots) and pick_cgroup blew the
 * verifier complexity limit ("BPF program is too large. Processed 1000001
 * insn (limit 1000000)", smoke 20261106, /tmp/smoke_d2fq_arb.log line 6481).
 * This is the same fallback arb_feedback.bpf.c took for its hold scan.
 */
struct d2fq_pick_ctx {
    __u64 now;
    __u64 gvt;           /* pass 1 out: min eligible lane vtime */
    __u64 thrsh;         /* band edges, precomputed from cfg */
    __u64 md_range;      /* = thrsh * (100 - md_ratio) / 100 */
    __u64 best_vtime;    /* pass 2 out: winning lane's vtime */
    __u64 best_cgid;     /* pass 2 out: winning tenant */
    __u32 phase;         /* 1: gvt pass, 2: best pass */
    __u32 best_lane;     /* pass 2 out: winning lane */
    __u32 best_band;     /* pass 2 in: 3 (past LOW), out: winner band */
};

/* Pass-2 candidate update: (band, vtime) lexicographic, lane order and slot
 * order preserved (READ before WRITE, ascending slot index) so ties break
 * exactly like the open-coded form. */
static __always_inline void d2fq_pick_consider(struct d2fq_pick_ctx *ctx,
                                               struct d2fq_tenant_state *ts,
                                               __u64 cgid, __u32 lane)
{
    __u64 vtime = ts->lane[lane].vtime;
    /* A concurrent insert or DI reset can drop a lane's vtime below the
     * pass-1 gvt snapshot: clamp so the u64 gap cannot wrap (a transiently
     * mis-banded lane self-corrects on the next pick, when its vtime
     * becomes the new gvt). */
    __u64 gap = vtime > ctx->gvt ? vtime - ctx->gvt : 0;
    __u32 band = d2fq_band(gap, ctx->thrsh, ctx->md_range);

    if (band < ctx->best_band ||
        (band == ctx->best_band && vtime < ctx->best_vtime)) {
        ctx->best_band = band;
        ctx->best_vtime = vtime;
        ctx->best_cgid = cgid;
        ctx->best_lane = lane;
    }
}

/*
 * One active-list slot per call.  Eligibility is identical to the
 * open-coded form: an eligible lane has pending > 0 and its tenant is not
 * inside a no-progress skip window; pass 1 additionally requires a
 * policy_meta entry (gvt considers registered tenants only -- the pass-2
 * pick keeps picking meta-less tenants, as before).
 */
static __u64 d2fq_pick_scan(__u32 i, struct d2fq_pick_ctx *ctx)
{
    struct arb_tenant_key key = {
        .cgroup_id = 0,
        .hctx_idx = 0,    /* cgroup-level state: one lane pair per tenant */
        .reserved = 0,
    };
    struct d2fq_tenant_state *ts;
    struct iosched_policy_meta *meta;
    __u64 *cgid;

    cgid = bpf_map_lookup_elem(&active_cgroup_ids, &i);
    if (!cgid || *cgid == 0)
        return 0;
    if (ctx->phase == 1) {
        meta = bpf_map_lookup_elem(&policy_meta, cgid);
        if (!meta)
            return 0;
    }
    key.cgroup_id = *cgid;
    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return 0;
    if (ts->pick_no_progress > D2FQ_PICK_NO_PROGRESS_MAX &&
        ctx->now - ts->last_pick_ns < D2FQ_NOPROGRESS_RETRY_NS)
        return 0;

    if (ctx->phase == 1) {
        if (ts->lane[D2FQ_LANE_READ].pending &&
            ts->lane[D2FQ_LANE_READ].vtime < ctx->gvt)
            ctx->gvt = ts->lane[D2FQ_LANE_READ].vtime;
        if (ts->lane[D2FQ_LANE_WRITE].pending &&
            ts->lane[D2FQ_LANE_WRITE].vtime < ctx->gvt)
            ctx->gvt = ts->lane[D2FQ_LANE_WRITE].vtime;
        return 0;
    }

    if (ts->lane[D2FQ_LANE_READ].pending)
        d2fq_pick_consider(ctx, ts, *cgid, D2FQ_LANE_READ);
    if (ts->lane[D2FQ_LANE_WRITE].pending)
        d2fq_pick_consider(ctx, ts, *cgid, D2FQ_LANE_WRITE);
    return 0;
}

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct d2fq_pick_ctx pctx = {};
    struct d2fq_tenant_state *ts;
    struct d2fq_hctx_state *hs;
    struct d2fq_hctx_state *hg;   /* slot 0: global gvt cache */
    struct arb_tenant_key wkey;
    struct d2fq_cfg cfg;
    __u64 total_dispatches;
    __u32 hkey;
    __u32 gkey = 0;

    if (!dctx)
        return 0;
    hkey = (__u32)dctx->hctx_idx;
    if (hkey >= ARB_MAX_HCTX)
        return 0;
    hs = bpf_map_lookup_elem(&hctx_state, &hkey);
    if (!hs)
        return 0;
    hg = bpf_map_lookup_elem(&hctx_state, &gkey);
    if (!hg)
        return 0;

    d2fq_cfg_read(&cfg);
    pctx.now = bpf_ktime_get_ns();
    pctx.thrsh = cfg.thrsh;
    pctx.md_range = cfg.thrsh * (100ULL - cfg.md_ratio) / 100ULL;

    /* Pass 1: gvt = min vtime over eligible lanes (pending > 0, not inside
     * a no-progress skip window).  A stuck (no-progress) lane is excluded
     * so its stale vtime cannot depress gvt and inflate everyone's gap. */
    pctx.phase = 1;
    pctx.gvt = ~0ULL;
    bpf_loop(ARB_MAX_TENANTS, d2fq_pick_scan, &pctx, 0);

    if (pctx.gvt == ~0ULL)
        return 0;   /* nobody eligible: ordinary no-work NULL */

    /* Pass 2: best (band, vtime) among eligible lanes (meta lookups are
     * pass-1 only; eligibility and vtime come from the tenant state). */
    pctx.phase = 2;
    pctx.best_vtime = 0;
    pctx.best_cgid = 0;
    pctx.best_lane = 0;
    pctx.best_band = 3;
    bpf_loop(ARB_MAX_TENANTS, d2fq_pick_scan, &pctx, 0);

    if (!pctx.best_cgid)
        return 0;

    wkey.cgroup_id = pctx.best_cgid;
    wkey.hctx_idx = 0;   /* cgroup-level tenant state */
    wkey.reserved = 0;
    ts = bpf_map_lookup_elem(&tenant_state, &wkey);
    if (!ts)
        return pctx.best_cgid;

    /* F2-style empty-pop guard: count winning picks that dispatched nothing
     * (strict mode gives no empty-pop feedback).  Beyond the limit the
     * tenant is skipped for a bounded retry window so the scan reaches the
     * other tenants; the periodic retry keeps dispatching to the stuck
     * tenant so holding queue policies can release their held requests. */
    total_dispatches = ts->lane[D2FQ_LANE_READ].dispatches +
                       ts->lane[D2FQ_LANE_WRITE].dispatches;
    if (total_dispatches == ts->last_pick_dispatches)
        ts->pick_no_progress++;
    else
        ts->pick_no_progress = 0;
    ts->last_pick_dispatches = total_dispatches;
    ts->last_pick_ns = pctx.now;

    hg->gvt = pctx.gvt;   /* global gvt cache (DI resets read slot 0) */
    hs->picks++;
    hs->band_picks[pctx.best_band < 3 ? pctx.best_band : 2]++;
    hs->last_lane = pctx.best_lane;
    /* best_lane is always a D2FQ_LANE_* constant here (d2fq_pick_consider),
     * but it travels through the scan ctx, so the bound must be explicit
     * for the verifier (variable-offset map access otherwise). */
    if (pctx.best_lane < D2FQ_LANES)
        ts->lane[pctx.best_lane].picks++;

    IOEXT_BPF_DBG_SAMPLED(128,
        "[arb_d2fq] pick cg=%llu lane=%u band=%u vtime=%llu gvt=%llu\n",
        pctx.best_cgid, pctx.best_lane, pctx.best_band,
        pctx.best_vtime, pctx.gvt);
    return pctx.best_cgid;
}

SEC("struct_ops/inserted_request")
void BPF_PROG(inserted_request, struct iosched_inserted_ctx *ictx)
{
    if (!ictx)
        return;
    d2fq_note_inserted(ictx->policy_cgroup_id, ictx->opf,
                       bpf_ktime_get_ns());
}

SEC("struct_ops/dispatched_request")
void BPF_PROG(dispatched_request, struct iosched_dispatched_ctx *dctx)
{
    struct iosched_policy_meta *meta;
    __u64 cgid;
    __u64 weight = 1;
    __u32 prio = 0;

    if (!dctx)
        return;
    /* Copy the ctx field out first: a ctx-derived POINTER must not be
     * passed to a helper as a map key (-EACCES); only stack/map-value
     * pointers are valid key arguments (same pitfall note as
     * arb_feedback). */
    cgid = dctx->selected_cgroup_id;
    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (meta) {
        if (meta->arb_weight)
            weight = meta->arb_weight;
        prio = meta->arb_prio;
    }
    d2fq_note_dispatched(cgid, dctx->opf, dctx->nr_sectors, weight, prio,
                         bpf_ktime_get_ns());
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct d2fq_tenant_state *ts;
    struct arb_tenant_key key = {
        .cgroup_id = 0,
        .hctx_idx = 0,
        .reserved = 0,
    };

    if (!cctx)
        return;
    key.cgroup_id = cctx->cgroup_id;
    key.hctx_idx = 0;   /* cgroup-level tenant state */
    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return;
    /* Telemetry only (known completion-cgid attribution issue, guide
     * pitfall 10): the guarded decrement cannot underflow. */
    if (ts->inflight)
        __sync_fetch_and_add(&ts->inflight, -1);
}

SEC("struct_ops/dispatch_failed")
void BPF_PROG(dispatch_failed, struct iosched_dispatch_failed_ctx *fctx)
{
    struct d2fq_tenant_state *ts;
    struct d2fq_hctx_state *hs;
    struct arb_tenant_key key = {
        .cgroup_id = 0,
        .hctx_idx = 0,
        .reserved = 0,
    };
    __u32 hkey, lane;

    if (!fctx)
        return;
    hkey = (__u32)fctx->hctx_idx;
    hs = bpf_map_lookup_elem(&hctx_state, &hkey);
    lane = hs ? hs->last_lane : D2FQ_LANE_READ;

    key.cgroup_id = fctx->selected_cgroup_id;
    key.hctx_idx = 0;   /* cgroup-level tenant state */
    ts = bpf_map_lookup_elem(&tenant_state, &key);
    if (!ts)
        return;
    if (++ts->dispatch_fails >= D2FQ_DISPATCH_FAIL_GHOST_RESET) {
        /* Crystallized ghost pending (drain-phase races): hard reset so the
         * key self-heals on the next insert (F2 pattern). */
        ts->lane[D2FQ_LANE_READ].pending = 0;
        ts->lane[D2FQ_LANE_WRITE].pending = 0;
        ts->pick_no_progress = 0;
        ts->dispatch_fails = 0;
        return;
    }
    /* The framework popped the rq before validating it: retire one pending
     * entry on the lane that won the turn (guide pitfall 3). */
    if (lane < D2FQ_LANES && ts->lane[lane].pending)
        __sync_fetch_and_add(&ts->lane[lane].pending, -1);
}

SEC("struct_ops/requeue_refund")
void BPF_PROG(requeue_refund, struct iosched_dispatched_ctx *rctx)
{
    struct iosched_policy_meta *meta;
    __u64 cgid;
    __u64 weight = 1;
    __u32 prio = 0;

    if (!rctx)
        return;
    /* Same ctx-key pitfall as dispatched_request: copy the field out. */
    cgid = rctx->selected_cgroup_id;
    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (meta) {
        if (meta->arb_weight)
            weight = meta->arb_weight;
        prio = meta->arb_prio;
    }
    d2fq_note_requeue(cgid, rctx->opf, rctx->nr_sectors, weight, prio);
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops d2fq_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_ALLOW_FALLBACK,
    .name = "arb_d2fq",
};
