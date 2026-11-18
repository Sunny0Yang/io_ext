/* arb_slo.bpf.c - SLO-aware arbiter with per-tenant dynamic urgency boost
 *
 * WDRR base + per-cgroup latency SLO enforcement via completion feedback.
 * SLO class derived from arb_prio:
 *   prio 0-2: LC (latency-critical), target = ARB_SLO_DEFAULT_LATENCY_TARGET
 *   prio 3-5: BW (bandwidth), no latency target
 *   prio 6-7: BE (best-effort), no latency target
 *
 * Boost is per-tenant: effective_weight = base_weight * tenant_boost.
 * hold_dispatch() as second-level protection when LC inflight is high.
 * Inflight tracked via dispatched_request (++), completed_request (--),
 * and requeue_refund (--).
 *
 * Strict mode (IOSCHED_ARB_STRICT): weighted arbiters MUST be strict, or
 * the generic active-list fallback bypasses the fairness/boost decisions
 * (ARB_POLICY_DEV_GUIDE.md §2/§7).  liveness contract: while anyone is
 * pending, pick_cgroup must return non-0 (arb_drr_pick guarantees it).
 *
 * slo_hold_cnt[hctx] counts hold_dispatch=true decisions: the VM
 * acceptance asserts on it to PROVE the hold gate engaged (observability
 * without touching the shared arb_tenant_state/arb_hctx_state structs —
 * wdrr/cost accept scripts decode those and must stay valid).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_common.h"

char LICENSE[] SEC("license") = "GPL";

#define ARB_SLO_DEFAULT_LATENCY_TARGET_NS  (100ULL * 1000ULL)
#define ARB_SLO_DEFAULT_PROTECT_INFLIGHT   32

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

/* B2 requeue-replay pairing (see the leak-B2 block in
 * ioext_arb_common.h): rq_ptr -> {ts, refund, requeues}, LRU-bounded. */
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, ARB_REQUEUE_REPLAY_MAX);
    __type(key, __u64);
    __type(value, struct arb_requeue_replay);
} requeue_replay SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} inflight_count SEC(".maps");

/* Per-hctx count of hold_dispatch=true decisions (name must stay <= 15
 * chars: BPF map name cap).  Acceptance-only observability: proves the
 * hold gate fired WITHOUT touching the shared arb_tenant_state /
 * arb_hctx_state structs (wdrr/cost accept scripts decode those). */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, ARB_MAX_HCTX);
    __type(key, __u32);
    __type(value, __u64);
} slo_hold_cnt SEC(".maps");

static __always_inline __u64 slo_latency_target(__u32 arb_prio)
{
    if (arb_prio <= 2)
        return ARB_SLO_DEFAULT_LATENCY_TARGET_NS;
    return 0;
}

static __always_inline bool slo_is_lc(__u32 arb_prio)
{
    return arb_prio <= 2;
}

static __u32 slo_get_boost(void *states_map, __u64 cgid, __u32 hctx_idx)
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
                              slo_get_boost,
                            0);

    if (cgid) {
        IOEXT_BPF_DBG_SAMPLED(128,
            "[arb_slo] pick cg=%llu hctx=%u\n",
            cgid, hctx_key);
    }

    return cgid;
}

SEC("struct_ops/hold_dispatch")
bool BPF_PROG(hold_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 hctx_key;
    __u64 *inflight;
    __u64 *hold_cnt;
    __u64 held_cg = 0;
    bool hold = false;
    __u32 step;

    if (!dctx)
        return false;

    hctx_key = (__u32)dctx->hctx_idx;
    inflight = bpf_map_lookup_elem(&inflight_count, &hctx_key);
    if (!inflight)
        return false;

    for (step = 0; step < ARB_SCAN_LIMIT; step++) {
        __u64 *cgid_ptr = bpf_map_lookup_elem(&active_cgroup_ids, &step);
        if (!cgid_ptr || *cgid_ptr == 0)
            continue;

        struct iosched_policy_meta *meta = bpf_map_lookup_elem(&policy_meta, cgid_ptr);
        if (!meta)
            continue;

        if (!slo_is_lc(meta->arb_prio))
            continue;

        struct arb_tenant_key tkey = {
            .cgroup_id = *cgid_ptr,
            .hctx_idx = hctx_key,
            .reserved = 0,
        };
        struct arb_tenant_state *ts = bpf_map_lookup_elem(&tenant_state, &tkey);
        if (!ts)
            continue;

        if (ts->slo_state == SLO_PRESSURE &&
            *inflight >= ARB_SLO_DEFAULT_PROTECT_INFLIGHT) {
            hold = true;
            held_cg = *cgid_ptr;
            break;
        }
    }

    if (!hold)
        return false;

    hold_cnt = bpf_map_lookup_elem(&slo_hold_cnt, &hctx_key);
    if (hold_cnt)
        __sync_fetch_and_add(hold_cnt, 1);

    IOEXT_BPF_DBG_SAMPLED(64,
        "[arb_slo] hold cg=%llu inflight=%llu\n",
        held_cg, *inflight);
    return true;
}

SEC("struct_ops/inserted_request")
void BPF_PROG(inserted_request, struct iosched_inserted_ctx *ictx)
{
    if (!ictx)
        return;
    arb_requeue_replay_pair_insert(&requeue_replay, ictx->rq_ptr);
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
    __u64 cgid;
    __u32 hctx_idx, hkey;
    struct arb_tenant_key tkey;
    struct arb_tenant_state *ts;
    __u64 *inflight;

    if (!cctx)
        return;

    cgid = cctx->cgroup_id;
    hctx_idx = (__u32)cctx->hctx_idx;

    arb_requeue_replay_pair_complete(&requeue_replay, &cgroup_outstanding,
                                     &cgroup_drr, (__u64)cctx->rq,
                                     cgid, bpf_ktime_get_ns());
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

    {
        struct iosched_policy_meta *meta = bpf_map_lookup_elem(&policy_meta, &cgid);
        if (meta) {
            __u64 lat_target = slo_latency_target(meta->arb_prio);
            if (lat_target > 0) {
                arb_slo_update_window(ts,
                                      cctx->now,
                                      lat_target,
                                      ARB_SLO_BOOST_MAX,
                                      cctx->total_latency_ns);

                IOEXT_BPF_DBG_SAMPLED(256,
                    "[arb_slo] complete cg=%llu lat=%llu state=%u boost=%u\n",
                    cgid, cctx->total_latency_ns, ts->slo_state, ts->boost);
            }
        }
    }
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

    arb_requeue_replay_stash(&requeue_replay, rctx->rq_ptr,
                             rctx->nr_sectors * 512, bpf_ktime_get_ns());
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
struct iosched_arbiter_ops slo_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .hold_dispatch = (void *)hold_dispatch,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_STRICT,
    .name = "arb_slo",
};