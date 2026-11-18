/* arb_iolatency.bpf.c - io.latency-style LC/BE latency-target arbiter
 *
 * Same-semantics reduction of cgroup v2 io.latency (blk-iolatency) onto
 * the iosched_ext arbiter framework:
 *
 *   - per-cgroup ROLE via the queue loader's -p (--arb-prio) convention:
 *       prio 0..2 = LC (latency-critical: has a latency target)
 *       prio 3..7 = BE (best-effort: no target)
 *     per-cgroup SHARE via -W (--arb-weight) as usual.
 *   - the latency TARGET itself is a global arbiter parameter
 *     (--lat-target-ns on loader_arb_iolatency, default 100us): the
 *     framework has no per-cgroup config channel into the arbiter
 *     (policy_meta carries only prio/weight), so every LC cgroup shares
 *     the one target.  This is the one deviation from native io.latency
 *     (which configures io.latency.target per cgroup).
 *
 * Feedback loop (completed_request): every LC completion feeds the
 * shared window FSM (arb_slo_update_window, 100ms windows, >=20 samples,
 * violation when >1% of completions land over target, NORMAL -> PRESSURE
 * -> RECOVERY -> NORMAL ladder with 3-good-window exit).  Sustained LC
 * violations RAISE the LC tenant's boost, which multiplies its DRR round
 * budget: effective_weight = weight * boost.
 *
 * How that "compresses BE service" (io.latency semantics): the DRR core
 * (ioext_arb_common.h) exposes exactly ONE weight-injection point — the
 * per-tenant integer boost multiplier consumed at refill time — and the
 * shared arb_drr_pick must not be copied or modified (guide §7).
 * Under deficit round-robin, boosting every LC by k and leaving BE
 * untouched is RATIO-EQUIVALENT to scaling BE by 1/k: the long-run
 * service ratio LC:BE moves by k in both cases, and idle tenants are
 * work-conserving in both.  So BE backpressure is implemented as the LC
 * boost ladder — the observable io.latency behavior (LC misses target ->
 * BE share shrinks until LC recovers -> BE share returns) is preserved.
 *
 * BE floor ("io.latency default"): the ladder is capped at
 * --boost-max (default 4, clamp [1..16]) — BE never loses more than the
 * cap-to-one ratio against LC (>= 1/(1+cap) of contended service at
 * equal weights), and BE-vs-BE service stays strictly weight-proportional
 * (every BE keeps boost 1: the FSM only updates LC tenants).
 *
 * hold_dispatch: the hard LC protection brake (arb_slo's second-level
 * gate): while an LC tenant is in PRESSURE and the per-hctx inflight count
 * has reached --protect-inflight (default 32), the hctx holds dispatch
 * (returns NULL) instead of queueing more BE-behind-LC depth.  Each hold
 * decision bumps iolat_hold_cnt[hctx] (acceptance-assertable WITHOUT
 * touching the shared arb_tenant_state/arb_hctx_state structs).
 *
 * Backpressure jitter smoothing is structural, not tuned: pressure
 * engages only on a window roll with >= MIN_SAMPLES completions and >1%
 * violations (single bad completions cannot trip it) and releases only
 * through RECOVERY + 3 consecutive good windows (no oscillation), see
 * ARB_SLO_WINDOW_NS / ARB_SLO_RECOVERY_WINDOWS in ioext_arb_common.h.
 *
 * Safety pattern (arb_slo template, ARB_POLICY_DEV_GUIDE §2/§6/§7):
 *   - IOSCHED_ARB_STRICT: liveness contract — while anyone is pending,
 *     pick_cgroup returns non-0 (arb_drr_pick guarantees it)
 *   - charge unit = BYTES (dispatch-time), refund mirrors it exactly
 *   - requeue_refund NEVER touches pending (kernel re-insert re-fires
 *     inserted_request); dispatch_failed retires pending + advances
 *   - inflight lifecycle: dispatched_request ++ / completed_request --
 *     / requeue_refund -- (known kernel caveat #10: the completion
 *     hook's cgroup_id/hctx can differ from the insert path's, so
 *     inflight can drift — inherited from arb_slo; the hold gate reads
 *     the per-hctx aggregate, not per-tenant inflight)
 *   - per-hctx state stays in small ARRAY maps (the mother's pattern)
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_common.h"

char LICENSE[] SEC("license") = "GPL";

#define ARB_IOLAT_PRIO_LC_MAX             2
#define ARB_IOLAT_DEFAULT_LAT_TARGET_NS   (100ULL * 1000ULL)
#define ARB_IOLAT_DEFAULT_PROTECT_INFLIGHT 32
#define ARB_IOLAT_BOOST_MAX               4

/* Loader-pushed configuration (loader_arb_iolatency.c).  Mirror struct;
 * zero fields fall back to the compiled-in defaults above. */
struct arb_iolat_cfg {
    __u64 lat_target_ns;      /* LC latency target (io.latency.target) */
    __u32 protect_inflight;   /* per-hctx inflight threshold for the hold gate */
    __u32 boost_max;          /* LC boost ceiling == BE floor knob (>=1) */
    __u32 reserved;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct arb_iolat_cfg);
} iolat_cfg SEC(".maps");

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

/* Per-hctx dispatched-not-completed count across ALL tenants (the hold
 * gate reads this aggregate — arb_slo's inflight_count pattern). */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} inflight_count SEC(".maps");

/* Per-hctx count of hold_dispatch=true decisions (name <= 15 chars:
 * BPF map name cap).  Acceptance-only observability, arb_slo's
 * slo_hold_cnt pattern. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} iolat_hold_cnt SEC(".maps");

static __always_inline bool iolat_is_lc(__u32 arb_prio)
{
    return arb_prio <= ARB_IOLAT_PRIO_LC_MAX;
}

/*
 * Boost injection (arb_drr_pick's get_boost_fn): LC tenants carry the
 * window-FSM boost (1 -> 2 -> 4, capped by cfg->boost_max); BE tenants
 * are never updated by the FSM (latency target 0 for them — see
 * completed_request) so their boost stays the zero-init ARB_SLO_BOOST_NORMAL,
 * i.e. BE service is strictly weight-proportional (the BE floor).
 */
static __u32 iolat_get_boost(void *states_map, __u64 cgid, __u32 hctx_idx)
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

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 hctx_key;
    struct arb_hctx_state *hs;
    __u64 now_ns;

    if (!dctx)
        return 0;

    hctx_key = (__u32)dctx->hctx_idx;
    hs = bpf_map_lookup_elem(&hctx_state, &hctx_key);
    if (!hs)
        return 0;

    now_ns = bpf_ktime_get_ns();

    __u64 cgid = arb_drr_pick(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, &global_state, hs,
                              &active_cgroup_ids,
                              &policy_meta,
                              hctx_key,
                              now_ns,
                              ARB_WDRR_QUANTUM_BYTES,
                              ARB_WDRR_MIN_BUDGET_BYTES,
                              iolat_get_boost,
                            0);

    if (cgid) {
        IOEXT_BPF_DBG_SAMPLED(128,
            "[arb_iolatency] pick cg=%llu hctx=%u\n",
            cgid, hctx_key);
    }

    return cgid;
}

SEC("struct_ops/hold_dispatch")
bool BPF_PROG(hold_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct arb_iolat_cfg *cfg;
    __u32 zero = 0;
    __u32 hctx_key;
    __u64 *inflight;
    __u64 *hold_cnt;
    __u32 protect = ARB_IOLAT_DEFAULT_PROTECT_INFLIGHT;
    bool hold = false;
    /* Verifier-safe induction variable (arb_drr_pick's scan pattern in
     * ioext_arb_common.h): the loop counter must never be address-taken.
     * A __u32 counter that doubles as the lookup key spills into an
     * untracked STACK_MISC slot whose value is invisible to the verifier's
     * state pruning (scalar_reg_for_stack turns it into unbound_reg), so
     * every loop iteration presents an identical checkpoint state and the
     * load is rejected with "infinite loop detected at insn 55" (-EINVAL).
     * A __u64 counter plus a separate recomputed idx key keeps the counter
     * in a register or a 64-bit tracked spill, so each iteration's state
     * provably advances and the bounded loop verifies. */
    __u64 step;

    if (!dctx)
        return false;

    cfg = bpf_map_lookup_elem(&iolat_cfg, &zero);
    if (cfg && cfg->protect_inflight)
        protect = cfg->protect_inflight;

    hctx_key = (__u32)dctx->hctx_idx;
    inflight = bpf_map_lookup_elem(&inflight_count, &hctx_key);
    if (!inflight)
        return false;

    for (step = 0; step < ARB_SCAN_LIMIT; step++) {
        __u32 idx = (__u32)step;   /* map key, decoupled from the counter */
        __u64 *cgid_ptr = bpf_map_lookup_elem(&active_cgroup_ids, &idx);
        if (!cgid_ptr || *cgid_ptr == 0)
            continue;

        struct iosched_policy_meta *meta = bpf_map_lookup_elem(&policy_meta, cgid_ptr);
        if (!meta)
            continue;

        if (!iolat_is_lc(meta->arb_prio))
            continue;

        struct arb_tenant_key tkey = {
            .cgroup_id = *cgid_ptr,
            .hctx_idx = hctx_key,
            .reserved = 0,
        };
        struct arb_tenant_state *ts = bpf_map_lookup_elem(&tenant_state, &tkey);
        if (!ts)
            continue;

        if (ts->slo_state == SLO_PRESSURE && *inflight >= protect) {
            hold = true;
            break;
        }
    }

    if (!hold)
        return false;

    hold_cnt = bpf_map_lookup_elem(&iolat_hold_cnt, &hctx_key);
    if (hold_cnt)
        __sync_fetch_and_add(hold_cnt, 1);

    IOEXT_BPF_DBG_SAMPLED(64,
        "[arb_iolatency] hold inflight=%llu protect=%u\n",
        *inflight, protect);
    return true;
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
    __u64 bytes;
    __u32 home_key;
    __u64 *inflight;

    if (!dctx)
        return;

    bytes = dctx->nr_sectors * 512;
    /* S11: per-key own-max-charge tracking moved into
     * arb_note_dispatched_* (decision-key ts, no extra lookup). */
    arb_note_dispatched_bytes(&tenant_state, &cgroup_drr, &global_state,
                              dctx->selected_cgroup_id,
                              (__u32)dctx->hctx_idx,
                              (__u32)dctx->home_hctx_idx,
                              bytes);

    home_key = (__u32)dctx->home_hctx_idx;
    inflight = bpf_map_lookup_elem(&inflight_count, &home_key);
    if (inflight)
        __sync_fetch_and_add(inflight, 1);
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct arb_iolat_cfg *cfg;
    __u32 zero = 0;
    __u64 cgid;
    __u32 hctx_idx, hkey;
    struct arb_tenant_key tkey;
    struct arb_tenant_state *ts;
    struct iosched_policy_meta *meta;
    __u64 *inflight;
    __u64 lat_target = ARB_IOLAT_DEFAULT_LAT_TARGET_NS;
    __u32 boost_max = ARB_IOLAT_BOOST_MAX;

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
    if (!ts)
        return;

    /* The latency feedback loop runs for LC tenants only: BE (prio >= 3)
     * has no target and its boost stays 1 (the BE floor). */
    meta = bpf_map_lookup_elem(&policy_meta, &cgid);
    if (!meta || !iolat_is_lc(meta->arb_prio))
        return;

    cfg = bpf_map_lookup_elem(&iolat_cfg, &zero);
    if (cfg) {
        if (cfg->lat_target_ns)
            lat_target = cfg->lat_target_ns;
        if (cfg->boost_max)
            boost_max = cfg->boost_max;
    }

    if (lat_target == 0)
        return;   /* target disabled: pure WDRR + hold gate */

    arb_slo_update_window(ts, cctx->now, lat_target, boost_max,
                          cctx->total_latency_ns);

    IOEXT_BPF_DBG_SAMPLED(256,
        "[arb_iolatency] complete cg=%llu lat=%llu state=%u boost=%u\n",
        cgid, cctx->total_latency_ns, ts->slo_state, ts->boost);
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
    __u32 home_key;
    __u64 *inflight;

    if (!rctx)
        return;

    arb_note_requeue(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, rctx->selected_cgroup_id,
                          (__u32)rctx->hctx_idx,
                          (__u32)rctx->home_hctx_idx,
                          rctx->nr_sectors * 512, bpf_ktime_get_ns());

    home_key = (__u32)rctx->home_hctx_idx;
    inflight = bpf_map_lookup_elem(&inflight_count, &home_key);
    if (inflight)
        arb_atomic_dec_guarded(inflight);
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops iolat_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .hold_dispatch = (void *)hold_dispatch,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_ALLOW_FALLBACK,
    .name = "arb_iolatency",
};
