/* arb_wdrr.bpf.c - Weighted Deficit Round Robin arbiter (dispatch-time charged)
 *
 * Keeps selecting the same tenant while deficit > 0 and pending > 0.
 * Charge happens at dispatch time via the dispatched_request hook.
 * Runs in IOSCHED_ARB_STRICT: the kernel's generic active-list fallback
 * must never bypass this arbiter while BPF-managed work is pending.
 *
 * io.weight (cgroup v2) fidelity: this arbiter IS the io.weight arm of
 * the controller family — weight ratio == long-run byte-share ratio,
 * charge = bytes exactly (no cost model, no latency feedback).  The
 * native-value -> -W mapping table, the low-end floor caveat (weights
 * 1..7 floor-compress to ew 8) and the x8 scaling recipe live in
 * ARB_IO_WEIGHT_MAP.md, guarded by tests/io/arb_io_weight_model.py.
 *
 * The behavioral spec is mirrored by the reference model in
 * tests/io/arb_wdrr_model.py; keep the two in sync.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"
#include "ioext_arb_common.h"

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

/* B2 requeue-replay pairing (see the leak-B2 block in
 * ioext_arb_common.h): rq_ptr -> {ts, refund, requeues}, LRU-bounded.
 * Retired eagerly by the elevator re-insert (inserted_request) or by
 * the bypass retirement (completed_request). */
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, ARB_REQUEUE_REPLAY_MAX);
    __type(key, __u64);
    __type(value, struct arb_requeue_replay);
} requeue_replay SEC(".maps");

static __u32 wdrr_get_boost(void *states_map, __u64 cgid, __u32 hctx_idx)
{
    return 1;
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
                              wdrr_get_boost,
                            0);

    if (cgid) {
        IOEXT_BPF_DBG_SAMPLED(128,
            "[arb_wdrr] pick cg=%llu hctx=%u cursor=%u\n",
            cgid, hctx_key, hs->cursor);
    }

    return cgid;
}

SEC("struct_ops/inserted_request")
void BPF_PROG(inserted_request, struct iosched_inserted_ctx *ictx)
{
    if (!ictx)
        return;
    IOEXT_BPF_DBG_SAMPLED(1,
        "[arb_wdrr] inserted cg=%llu hctx=%u\n",
        ictx->policy_cgroup_id, (__u32)ictx->hctx_idx);
    arb_requeue_replay_pair_insert(&requeue_replay, ictx->rq_ptr);
    arb_note_inserted(&tenant_state, &cgroup_outstanding,
                      &cgroup_drr, &global_state, ictx->policy_cgroup_id,
                      (__u32)ictx->hctx_idx, bpf_ktime_get_ns());
}

SEC("struct_ops/dispatched_request")
void BPF_PROG(dispatched_request, struct iosched_dispatched_ctx *dctx)
{
    __u64 bytes;

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
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    if (!cctx)
        return;
    arb_requeue_replay_pair_complete(&requeue_replay, &cgroup_outstanding,
                                     &cgroup_drr, (__u64)cctx->rq,
                                     cctx->cgroup_id, bpf_ktime_get_ns());
    arb_note_completed(&tenant_state, &cgroup_outstanding,
                       cctx->cgroup_id, (__u32)cctx->hctx_idx, bpf_ktime_get_ns());
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
                             &cgroup_drr, &global_state,
                             fctx->selected_cgroup_id,
                             hkey, bpf_ktime_get_ns());
    arb_drr_advance_cursor(hs);
}

SEC("struct_ops/requeue_refund")
void BPF_PROG(requeue_refund, struct iosched_dispatched_ctx *rctx)
{
    __u64 bytes;

    if (!rctx)
        return;
    bytes = rctx->nr_sectors * 512;
    arb_requeue_replay_stash(&requeue_replay, rctx->rq_ptr, bytes,
                             bpf_ktime_get_ns());
    arb_note_requeue(&tenant_state, &cgroup_outstanding,
                          &cgroup_drr, rctx->selected_cgroup_id,
                          (__u32)rctx->hctx_idx,
                          (__u32)rctx->home_hctx_idx,
                          bytes, bpf_ktime_get_ns());
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops wdrr_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .completed_request = (void *)completed_request,
    .inserted_request = (void *)inserted_request,
    .dispatched_request = (void *)dispatched_request,
    .dispatch_failed = (void *)dispatch_failed,
    .requeue_refund = (void *)requeue_refund,
    .arb_mode = IOSCHED_ARB_STRICT,
    .name = "arb_wdrr",
};