#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

/*
 * Tunable policy constants (d2d4_bm tune, 2026-11-10). Each knob is a
 * libbpf .rodata global set by loader_q_wedf's CLI before bpf load; the
 * initializers below are the historical compile-time defaults, so a
 * loader started WITHOUT the corresponding --knob keeps the exact
 * original scheduling behavior. loader_q_wedf.c re-declares the same
 * default values for its parse fallbacks — the two sets MUST stay in
 * sync (verified by loader --help output vs here).
 */
const volatile __u64 wedf_read_deadline_ns   = 50000000ULL;
const volatile __u64 wedf_write_deadline_ns = 500000000ULL;
const volatile __u64 wedf_window_ns          = 5000000ULL;
const volatile __u64 wedf_expire_guard_ns    = 1000000ULL;

/*
 * Weighted-EDF feature gate (d2d4_bm tune, 2026-11-10), default OFF.
 *
 * OFF (default): the policy is bit-identical to the historical q_wedf —
 *   policy_meta is consumed only by the queue-depth/drain bookkeeping
 *   helpers; per-tenant weights are never read (the KNOWN gap recorded
 *   in POLICIES_INVENTORY.md / q_wedf_accept.sh wedf_weight_probe
 *   BLOCKED-by-design).
 *
 * ON: insert_request reads the tenant's weight from the same pinned
 *   policy_meta map the arbiters use (key = cgroup id, field
 *   arb_weight, written by this loader's -W/--arb-weight) and applies
 *   the weighted-deadline law below. -W then shapes the queue policy
 *   itself, not just the arbiter.
 */
const volatile bool wedf_weights_enable = false;

#define Q_WEDF_W_REF 100ULL /* IOSCHED_DEFAULT_ARB_WEIGHT: neutral weight */
#define Q_WEDF_SECTOR_WRAP_SLACK 0xffffffffffffffffULL

struct q_wedf_local_state {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
    __u64 last_sector;
    __u64 enqueued;
    __u64 dispatched;
    __u64 expired_dispatches;
    __u64 locality_dispatches;
};

struct q_wedf_state_entry {
    struct bpf_spin_lock lock;
    struct q_wedf_local_state state;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_wedf_state_entry);
} state_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_wedf_state_entry *state_lookup(__u64 hctx_idx)
{
    __u32 key = (__u32)hctx_idx;

    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline __u64 wedf_base_deadline_ns(const struct iosched_request_desc *desc)
{
    return iosched_req_is_write(desc) ? wedf_write_deadline_ns :
        wedf_read_deadline_ns;
}

/*
 * Tenant weight for the weighted-EDF law, verifier-safe in the same
 * shape as the arbiter readers (ioext_arb_common.h:1037-1040):
 *   bpf_map_lookup_elem(policy_meta, cgroup_id)
 *   -> NULL-safe dereference, zero/missing weight falls back to neutral.
 * When the feature gate is OFF the map is never consulted at all, so
 * the disabled path performs exactly the historical lookups.
 */
static __always_inline __u32 wedf_tenant_weight(__u64 cgroup_id)
{
    const struct iosched_policy_meta *meta;

    if (!wedf_weights_enable)
        return (__u32)Q_WEDF_W_REF;

    meta = bpf_map_lookup_elem(&policy_meta, &cgroup_id);
    if (!meta || !meta->arb_weight)
        return (__u32)Q_WEDF_W_REF;

    return meta->arb_weight;
}

/*
 * Weighted-deadline law (WFQ-stretch / SCED form), the mathematical
 * core of --weights-enable:
 *
 *     D_i = base_ns * W_REF / w_i ,   deadline_ns = arrival_ns + D_i
 *
 * with W_REF = 100 = IOSCHED_DEFAULT_ARB_WEIGHT and w_i the tenant's
 * arb_weight (loader -W). Properties:
 *
 *   1. Neutral exactness: w_i = 100 => D_i = base_ns EXACTLY (integer
 *      (base*100)/100 == base), so default weights — and any missing /
 *      zero map entry, and the whole disabled path — reproduce the
 *      historical unweighted deadlines with no rounding residue.
 *   2. Weight ordering: higher weight => shorter deadline => the EDF
 *      insert sort places the tenant's requests nearer the queue head,
 *      so under contention tenant i wins dispatch roughly in proportion
 *      to w_i. This is the classical "deadline scaling == service-rate
 *      scaling" equivalence: in a GPS/WFQ server with rates r_i ∝ w_i,
 *      per-request virtual sojourn scales as 1/w_i; mapping that time
 *      constant onto the EDF deadline makes the sorted queue drain in
 *      weighted-fair order (tests/io/q_wedf_model.py MODE_INTENDED,
 *      "dl = arrival + base_ns / weight", same law normalized to the
 *      neutral point 100 instead of 1: the recorded intended golden's
 *      1:3 weight pair == arb_weight 100:300 here).
 *   3. Bounds: w_i ∈ [1, 2^32-1]; D_i ∈ [base*100/2^32, base*100].
 *      The division is a plain u64 div whose divisor is provably
 *      non-zero (weight==0 is folded to 100 above); the multiply is
 *      overflow-guarded for absurd base_ns (> ~1.8e17 ns = 58 days),
 *      where the unweighted base is used instead.
 *   4. Scope: weights shape only insert-time deadlines; wedf_before /
 *      wedf_dispatch and the expire-guard telemetry are untouched, so
 *      ordering inside the window stays sector-locality and dispatch
 *      stays pop-head.
 */
static __always_inline __u64 wedf_weighted_deadline_ns(__u64 base_ns, __u32 weight)
{
    if (!wedf_weights_enable)
        return base_ns;
    if (base_ns > ~0ULL / Q_WEDF_W_REF)
        return base_ns;
    return (base_ns * Q_WEDF_W_REF) / weight;
}

static __always_inline bool wedf_before(const struct iosched_request_desc *req,
                                        const struct iosched_request_desc *cur)
{
    __u64 req_late = req->deadline_ns + wedf_window_ns;
    __u64 cur_late = cur->deadline_ns + wedf_window_ns;

    if (req_late < cur->deadline_ns)
        return true;
    if (req->deadline_ns > cur_late)
        return false;
    return req->sector < cur->sector;
}

static __always_inline int wedf_insert(struct q_wedf_local_state *state,
                                       const struct iosched_request_desc *req)
{
    __u32 pos = state->pending_nr;
    __u32 i;

    if (state->pending_nr >= IOSCHED_MAX_PENDING)
        return -1;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= state->pending_nr)
            break;
        if (wedf_before(req, &state->pending[i])) {
            pos = i;
            break;
        }
    }

    for (i = IOSCHED_MAX_PENDING - 1; i > 0; i--) {
        if (i <= pos || i > state->pending_nr)
            continue;
        state->pending[i] = state->pending[i - 1];
    }

    state->pending[pos] = *req;
    state->pending_nr++;
    state->enqueued++;
    return 0;
}

static __always_inline __u64 wedf_dispatch(struct q_wedf_local_state *state,
                                           __u64 now_ns)
{
    struct iosched_request_desc picked;

    if (!state->pending_nr)
        return 0;

    picked = state->pending[0];
    if (!iosched_pop_head_32(state->pending, &state->pending_nr))
        return 0;

    state->last_sector = picked.sector + picked.cost;
    state->dispatched++;
    if (picked.deadline_ns <= now_ns + wedf_expire_guard_ns)
        state->expired_dispatches++;
    else
        state->locality_dispatches++;
    return picked.rq_ptr;
}

SEC("struct_ops/init_queue")
int BPF_PROG(init_queue, struct iosched_queue_ctx *qctx)
{
    return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/exit_queue")
int BPF_PROG(exit_queue, struct iosched_queue_ctx *qctx)
{
    return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    struct q_wedf_state_entry *entry;
    struct iosched_request_desc desc;
    int ret;

    if (!ictx || !ictx->rq)
        return -1;
    if ((__u64)ictx->rq < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    entry = state_lookup(ictx->hctx_idx);
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    desc.deadline_ns = desc.arrival_ns +
        wedf_weighted_deadline_ns(wedf_base_deadline_ns(&desc),
                                  wedf_tenant_weight(ictx->cgroup_id));

    bpf_spin_lock(&entry->lock);
    ret = wedf_insert(&entry->state, &desc);
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                          "[q_wedf] insert rq=%llx deadline=%llu sector=%llu hctx=%llu\n",
                          desc.rq_ptr, desc.deadline_ns, desc.sector,
                          desc.hctx_idx);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_wedf_state_entry *entry;
    __u64 now_ns;
    __u64 rq_ptr;

    if (!dctx)
        return 0;

    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = wedf_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_wedf_state_entry *entry;
    __u64 now_ns;
    __u64 rq_ptr;

    if (!dctx)
        return 0;

    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = wedf_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_wedf = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_wedf",
};
