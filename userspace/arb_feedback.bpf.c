/* arb_feedback.bpf.c - Feedback-pressure arbiter (WDRR skeleton)
 *
 * Dynamic multi-tenant main experiment arbiter.  Three stacked controls
 * on top of the proven arb_drr_pick core (see
 * ioext_arb_feedback_common.h for the full design):
 *
 *   1. feedback pressure: per (cgroup,hctx) EWMA completion latency +
 *      AIMD-adjusted in-flight budget; hold_dispatch() gates competing
 *      low-prio tenants whose inflight exceeds their budget while a
 *      latency-critical tenant (arb_prio <= FB_HIGH_PRIO_MAX) is in
 *      FB_PRESSURE.
 *   2. service preference: expressed entirely via the existing loader
 *      CLI (--arb-prio / --arb-weight) plus a BOUNDED starvation credit
 *      that lifts low-prio effective_weight by FB_CREDIT_BOOST_MULT
 *      while their backlog goes unserved (credit capped at
 *      FB_CREDIT_MAX, decayed FB_CREDIT_DECAY per dispatch).
 *   3. load awareness: dispatch charge = cost(op, size bucket, 8
 *      buckets) from a config-overridable table (fb_cost_tbl; 0 = the
 *      built-in placeholder defaults, 待标定).
 *
 * Strict mode (IOSCHED_ARB_STRICT, ARB_POLICY_DEV_GUIDE.md §2/§7): the
 * liveness contract "pick_cgroup returns non-0 while anyone is pending"
 * is guaranteed by arb_drr_pick; hold_dispatch only delays dispatch
 * attempts (blk-mq re-kicks on completion), it never consumes requests.
 *
 * Request-identity accounting contract (ioext_arb_common.h header):
 * inserted -> pending++ (identity key), dispatched -> pending--/
 * inflight++ (identity key) + deficit charge (decision key),
 * completed/requeue -> inflight--, requeue refunds the SAME charge it
 * was dispatched with and never touches pending.  All five hooks are
 * wired; verified by tools/feedback/accept_static.sh.
 *
 * Observability (all map names <= 15 chars):
 *   tenant_state : arb_tenant_state (deficit/pending/inflight/EWMA...)
 *   fb_state     : aimd_budget / fb_state / starve_credit / counters
 *   fb_hold_cnt  : per-hctx hold_dispatch=true decisions
 *   fb_cost_tbl  : effective cost table (0 entry = built-in default)
 *   fb_cfg       : runtime config (0 field = compile-time default)
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_feedback_common.h"

char LICENSE[] SEC("license") = "GPL";

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

/* Per-hctx inflight (identity domain), observability + drain check. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} inflight_count SEC(".maps");

/* Feedback state: AIMD budget, pressure FSM, starvation credit. */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, ARB_MAX_TENANTS * ARB_MAX_HCTX);
    __type(key, struct arb_tenant_key);
    __type(value, struct fb_tenant_fb);
} fb_state SEC(".maps");

/* Per-hctx hold_dispatch decisions (acceptance gate, arb_slo pattern). */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} fb_hold_cnt SEC(".maps");

/* Cost table override: index op*FB_COST_BUCKETS+bucket, 0 = default. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 2 * FB_COST_BUCKETS);
    __type(key, __u32);
    __type(value, __u64);
} fb_cost_tbl SEC(".maps");

/* Runtime config, single entry; 0 fields = compile-time defaults. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct fb_config);
} fb_cfg SEC(".maps");

/* arb_drr_pick expects the 3-arg boost callback signature; adapt over
 * the always_inline fb_get_boost (plain function = arb_slo pattern). */
static __u32 fb_pick_boost(void *states_map, __u64 cgid, __u32 hctx_idx)
{
    return fb_get_boost(&fb_cfg, &fb_state, cgid, hctx_idx);
}

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 hctx_key;
    struct arb_hctx_state *hs;
    __u64 now_ns;
    __u64 cgid;

    if (!dctx)
        return 0;

    hctx_key = (__u32)dctx->hctx_idx;
    hs = bpf_map_lookup_elem(&hctx_state, &hctx_key);
    if (!hs)
        return 0;

    now_ns = bpf_ktime_get_ns();

    /* D1 v2: sole_gate = 1 -- the fast path is governed purely by the
     * pick-side census (exactly one FRESH queued demander).  On bm the
     * f1 harness keeps all tenant loaders resident for the whole
     * sequence, so a registration-count gate could never open; the
     * multi-tenant collapse that motivated it was later A/B-traced to
     * the ioext3 kernel build (see D1_FASTPATH_V2.md), not to the
     * fast path, so the census-only gate ships. */
    {
        __u32 solo_gate = 1;
        cgid = arb_drr_pick(&tenant_state, &cgroup_outstanding,
                              &cgroup_drr, &global_state, hs,
                            &active_cgroup_ids,
                            &policy_meta,
                            hctx_key,
                            now_ns,
                            FB_COST_QUANTUM,
                            FB_COST_MIN_BUDGET,
                            fb_pick_boost,
                            solo_gate);
    }

    if (cgid) {
        IOEXT_BPF_DBG_SAMPLED(128,
            "[arb_feedback] pick cg=%llu hctx=%u\n",
            cgid, hctx_key);
    }

    return cgid;
}

/* Context for the bpf_loop-based hold scan. */
struct fb_hold_ctx {
    __u64 hctx_key;
    __u32 phase;   /* 1: LC pressure exists?  2: low-prio budget breach? */
    __u32 found;
};

/* bpf_loop callback: one active-list slot per call.  Return 1 stops the
 * scan (predicate satisfied); 0 continues.  Iterations are bounded by
 * the caller (ARB_SCAN_LIMIT), so no open-coded loop needs verifying --
 * this is the conservative fallback after two open-coded-scan
 * rejections ("infinite loop detected", attempts
 * 20261103T161048Z-7fa6a2 / 20261103T161422Z-4cb705 /
 * 20261103T161754Z-37051b in the arb_feedback_smoke ledger campaign;
 * state pruning of the spilled loop counter never converged for this
 * body shape, while the structurally identical arb_slo loop did). */
static __u64 fb_hold_scan(__u32 idx, struct fb_hold_ctx *ctx)
{
    __u64 *cgid_ptr = bpf_map_lookup_elem(&active_cgroup_ids, &idx);
    struct iosched_policy_meta *meta;
    struct fb_tenant_fb *fb;
    struct arb_tenant_state *ts;

    if (!cgid_ptr || *cgid_ptr == 0)
        return 0;

    meta = bpf_map_lookup_elem(&policy_meta, cgid_ptr);
    if (!meta)
        return 0;

    {
        struct arb_tenant_key key = {
            .cgroup_id = *cgid_ptr,
            .hctx_idx = (__u32)ctx->hctx_key,
            .reserved = 0,
        };
        if (ctx->phase == 1) {
            if (meta->arb_prio > FB_HIGH_PRIO_MAX)
                return 0;
            fb = bpf_map_lookup_elem(&fb_state, &key);
            if (fb && fb->fb_state == FB_PRESSURE) {
                ctx->found = 1;
                return 1;
            }
            return 0;
        }

        if (meta->arb_prio <= FB_HIGH_PRIO_MAX)
            return 0;
        ts = bpf_map_lookup_elem(&tenant_state, &key);
        fb = bpf_map_lookup_elem(&fb_state, &key);
    }
    if (ts && fb && ts->inflight >= fb->aimd_budget) {
        ctx->found = 1;
        return 1;
    }
    return 0;
}

SEC("struct_ops/hold_dispatch")
bool BPF_PROG(hold_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 hctx_key;
    __u64 *hold_cnt;
    struct fb_hold_ctx hctx = {
        .phase = 1,
        .found = 0,
    };

    if (!dctx)
        return false;

    hctx_key = (__u32)dctx->hctx_idx;
    hctx.hctx_key = hctx_key;

    /* Two bounded scans (bpf_loop, max ARB_SCAN_LIMIT iterations each):
     * hold only when an LC tenant is in FB_PRESSURE AND some competing
     * low-prio tenant is at/over its AIMD in-flight budget. */
    bpf_loop(ARB_SCAN_LIMIT, fb_hold_scan, &hctx, 0);
    if (!hctx.found)
        return false;

    hctx.phase = 2;
    hctx.found = 0;
    bpf_loop(ARB_SCAN_LIMIT, fb_hold_scan, &hctx, 0);
    if (!hctx.found)
        return false;

    hold_cnt = bpf_map_lookup_elem(&fb_hold_cnt, &hctx_key);
    if (hold_cnt)
        __sync_fetch_and_add(hold_cnt, 1);

    IOEXT_BPF_DBG_SAMPLED(64,
        "[arb_feedback] hold hctx=%u\n", hctx_key);
    return true;
}

SEC("struct_ops/inserted_request")
void BPF_PROG(inserted_request, struct iosched_inserted_ctx *ictx)
{
    struct iosched_policy_meta *meta;
    __u64 cgid;
    __u32 hctx_idx;

    if (!ictx)
        return;

    /* Copy the ctx field out first: a ctx-derived POINTER must not be
     * passed to a helper as a map key (-EACCES, attempt
     * 20261103T162234Z-adf017); only stack/map-value pointers are valid
     * key arguments. */
    cgid = ictx->policy_cgroup_id;
    hctx_idx = (__u32)ictx->hctx_idx;

    /* Identity accounting: pending++ on the hctx the request was
     * allocated on (ioext_arb_common.h contract). */
    arb_note_inserted(&tenant_state, &cgroup_outstanding,
                      &cgroup_drr, &global_state, cgid, hctx_idx,
                      bpf_ktime_get_ns());
    fb_ensure_tenant(&fb_state, cgid, hctx_idx);

    /* Starvation credit only accumulates for the non-preferred class. */
    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (meta)
        fb_note_insert_credit(&fb_cfg, &fb_state, cgid, hctx_idx,
                              meta->arb_prio);
}

SEC("struct_ops/dispatched_request")
void BPF_PROG(dispatched_request, struct iosched_dispatched_ctx *dctx)
{
    __u64 cost;

    if (!dctx)
        return;

    cost = fb_cost_estimate(&fb_cost_tbl, dctx->opf, dctx->nr_sectors);
    /* S11: per-key own-max-charge tracking moved into
     * arb_note_dispatched_* (decision-key ts, no extra lookup). */
    /* Identity key (home_hctx): pending--/inflight++; decision key
     * (hctx_idx): deficit charge. */
    arb_note_dispatched_cost(&tenant_state, &cgroup_drr, &global_state,
                             dctx->selected_cgroup_id,
                             (__u32)dctx->hctx_idx,
                             (__u32)dctx->home_hctx_idx,
                             cost);

    /* Serve-side starvation credit decay (quantum, floor 0). */
    fb_note_serve_credit(&fb_state, dctx->selected_cgroup_id,
                         (__u32)dctx->home_hctx_idx);

    IOEXT_BPF_DBG_SAMPLED(256,
        "[arb_feedback] charge cg=%llu cost=%llu opf=%llu sectors=%llu\n",
        dctx->selected_cgroup_id, cost, dctx->opf, dctx->nr_sectors);
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    __u64 cgid;
    __u32 hctx_idx, hkey;
    struct arb_tenant_key tkey;
    struct arb_tenant_state *ts;
    struct fb_tenant_fb *fb;
    struct iosched_policy_meta *meta;
    __u64 *inflight;

    if (!cctx)
        return;

    cgid = cctx->cgroup_id;
    hctx_idx = (__u32)cctx->hctx_idx;

    arb_note_completed(&tenant_state, &cgroup_outstanding,
                       cgid, hctx_idx, bpf_ktime_get_ns());

    hkey = hctx_idx;
    inflight = bpf_map_lookup_elem(&inflight_count, &hkey);
    if (inflight)
        arb_atomic_dec_guarded(inflight);

    tkey.cgroup_id = cgid;
    tkey.hctx_idx = hctx_idx;
    tkey.reserved = 0;
    ts = bpf_map_lookup_elem(&tenant_state, &tkey);
    fb = bpf_map_lookup_elem(&fb_state, &tkey);
    if (!ts || !fb)
        return;

    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (meta)
        fb_aimd_update(&fb_cfg, fb, ts, meta->arb_prio,
                       cctx->now, cctx->total_latency_ns);

    IOEXT_BPF_DBG_SAMPLED(256,
        "[arb_feedback] complete cg=%llu lat=%llu ewma=%llu budget=%u state=%u credit=%llu\n",
        cgid, cctx->total_latency_ns, ts->ewma_latency_ns,
        fb->aimd_budget, fb->fb_state, fb->starve_credit);
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

    /* Refund ONLY (never pending++): the kernel re-inserts through
     * scx_io_insert_requests and re-fires inserted_request.  The refund
     * mirrors the exact charge the request was dispatched with. */
    arb_note_requeue(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, rctx->selected_cgroup_id,
                          (__u32)rctx->hctx_idx,
                          (__u32)rctx->home_hctx_idx,
                          fb_cost_estimate(&fb_cost_tbl, rctx->opf, rctx->nr_sectors), bpf_ktime_get_ns());
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops fb_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .hold_dispatch = (void *)hold_dispatch,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_STRICT,
    .name = "arb_feedback",
};
