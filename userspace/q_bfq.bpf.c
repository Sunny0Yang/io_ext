#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_BFQ_FLOW_F_ON_QUEUE    (1U << 0)
#define Q_BFQ_FLOW_F_INTERACTIVE (1U << 1)
#define Q_BFQ_FLOW_F_SOFT_RT     (1U << 2)
#define Q_BFQ_FLOW_PENDING       8U
#define Q_BFQ_FLOW_OVERFLOW      8U
#define Q_BFQ_STALE_SCAN_LIMIT   32U
#define Q_BFQ_BUDGET_SAMPLE_NS   100000000ULL
#define Q_BFQ_BUDGET_SLICE_NS    20000000ULL
#define Q_BFQ_DYNAMIC_MIN_BUDGET 16U
#define Q_BFQ_DYNAMIC_MAX_BUDGET 8192U
#define Q_BFQ_SAMPLE_SECTOR_CAP  16777216ULL

/*
 * Full request-container treeification is intentionally staged separately:
 * bpf_rbtree roots need co-located locks, __contains annotations,
 * bpf_obj_new/drop ownership paths, and callback-safe comparators.
 */

struct q_bfq_flow_key {
    __u64 entity_id;
    __u32 flow_class;
    __u32 hctx_idx;
};

struct q_bfq_flow {
    struct iosched_request_desc pending[Q_BFQ_FLOW_PENDING];
    struct iosched_request_desc overflow[Q_BFQ_FLOW_OVERFLOW];
    __u64 last_sector;
    __u32 weight;
    __u32 pending_nr;
    __u32 overflow_head;
    __u32 overflow_tail;
    __u32 overflow_nr;
    __u32 budget_left;
    __u32 flags;
};

struct q_bfq_active_queue {
    struct q_bfq_flow_key entries[IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH];
    __u32 head;
    __u32 tail;
    __u32 nr;
    __u32 reserved;
};

struct q_bfq_overflow_entry {
    __u64 rq_ptr;
    __u32 cost;
    __u32 reserved;
};

struct q_bfq_overflow_queue {
    struct q_bfq_overflow_entry entries[IOSCHED_BFQ_OVERFLOW_DEPTH];
    __u32 head;
    __u32 tail;
    __u32 nr;
    __u32 reserved;
};

struct q_bfq_runtime_state {
    struct q_bfq_flow_key current_key;
    struct q_bfq_active_queue active_queue;
    struct q_bfq_overflow_queue overflow_queue;
};

struct q_bfq_state_entry {
    struct bpf_spin_lock lock;
    struct q_bfq_runtime_state state;
    struct iosched_bfq_burst_state burst;
};

struct q_bfq_icq_state {
    __u64 last_completion_ns;
    __u64 last_insert_ns;
    __u64 think_avg_ns;
    __u64 boost_expires_ns;
    __u32 requests;
    __u32 boost_budget;
    __u32 flow_class;
    __u32 base_weight;
    __u32 flags;
};

struct q_bfq_budget_state {
    struct bpf_spin_lock lock;
    __u64 window_start_ns;
    __u64 window_sectors;
    __u32 max_budget;
    __u32 samples;
    __u32 clamp_events;
    __u32 fallback_uses;
};

#define Q_BFQ_ICQ_F_IN_LARGE_BURST (1U << 0)
#define Q_BFQ_ICQ_F_INTERACTIVE    (1U << 1)
#define Q_BFQ_ICQ_F_SOFT_RT        (1U << 2)

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_bfq_state_entry);
} state_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, IOSCHED_BFQ_ACTIVE_FLOW_LIMIT);
    __type(key, struct q_bfq_flow_key);
    __type(value, struct q_bfq_flow);
} flow_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_bfq_flow);
} flow_zero_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);
    __type(value, struct q_bfq_icq_state);
} icq_state_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_bfq_budget_state);
} budget_state_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, IOSCHED_BFQ_ACTIVE_FLOW_LIMIT);
    __type(key, struct iosched_bfq_tenant_key);
    __type(value, struct iosched_bfq_cgroup_state);
} bfq_cgroup_state SEC(".maps");

static __always_inline struct q_bfq_state_entry *state_lookup(__u64 hctx_idx)
{
    __u32 key = hctx_idx;

    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline bool bfq_flow_key_empty(const struct q_bfq_flow_key *key)
{
    return !key->entity_id;
}

static __always_inline void bfq_flow_key_clear(struct q_bfq_flow_key *key)
{
    key->entity_id = 0;
    key->flow_class = IOSCHED_BFQ_CLASS_SYNC;
    key->hctx_idx = 0;
}

static __always_inline bool bfq_flow_key_equal(const struct q_bfq_flow_key *lhs,
                                               const struct q_bfq_flow_key *rhs)
{
    return lhs->entity_id == rhs->entity_id &&
           lhs->flow_class == rhs->flow_class &&
           lhs->hctx_idx == rhs->hctx_idx;
}

static __always_inline struct q_bfq_flow_key bfq_flow_key_from_desc(const struct iosched_request_desc *req,
                                                                    __u64 hctx_idx)
{
    struct q_bfq_flow_key key = {
        .entity_id = iosched_bfq_entity_id(req),
        .flow_class = iosched_bfq_class(req),
        .hctx_idx = hctx_idx,
    };

    return key;
}

static __always_inline struct q_bfq_flow *flow_lookup(const struct q_bfq_flow_key *key)
{
    return bpf_map_lookup_elem(&flow_map, key);
}

static __always_inline struct q_bfq_budget_state *budget_state_lookup(__u64 hctx_idx)
{
    __u32 key = hctx_idx;

    return bpf_map_lookup_elem(&budget_state_map, &key);
}

static __always_inline __u32 bfq_scale_budget(__u32 base,
                                              __u32 weight)
{
    __u64 scaled;

    if (!base)
        base = 1;
    if (!weight)
        weight = IOSCHED_BFQ_WEIGHT_DEFAULT;

    scaled = ((__u64)base * weight) / IOSCHED_BFQ_WEIGHT_DEFAULT;
    if (!scaled)
        return 1;
    if (scaled > Q_BFQ_DYNAMIC_MAX_BUDGET)
        return Q_BFQ_DYNAMIC_MAX_BUDGET;
    return (__u32)scaled;
}

static __always_inline __u32 bfq_dynamic_base_budget(__u64 hctx_idx,
                                                     __u32 flow_class,
                                                     __u32 weight)
{
    struct q_bfq_budget_state *state = budget_state_lookup(hctx_idx);
    __u32 max_budget = 0;
    __u32 base;

    if (state) {
        bpf_spin_lock(&state->lock);
        if (state->samples && state->max_budget)
            max_budget = state->max_budget;
        else if (state->fallback_uses < ~0U)
            state->fallback_uses++;
        bpf_spin_unlock(&state->lock);
    }

    if (!max_budget)
        return iosched_bfq_flow_base_budget(flow_class, weight);

    base = flow_class == IOSCHED_BFQ_CLASS_SYNC ? max_budget : max_budget / 2;
    if (!base)
        base = 1;
    return bfq_scale_budget(base, weight);
}

static __always_inline __u32 bfq_clamp_dynamic_budget(__u64 budget,
                                                      bool *clamped)
{
    if (budget < Q_BFQ_DYNAMIC_MIN_BUDGET) {
        *clamped = true;
        return Q_BFQ_DYNAMIC_MIN_BUDGET;
    }
    if (budget > Q_BFQ_DYNAMIC_MAX_BUDGET) {
        *clamped = true;
        return Q_BFQ_DYNAMIC_MAX_BUDGET;
    }
    return (__u32)budget;
}

static __always_inline void bfq_budget_note_completion(const struct iosched_completed_request_ctx *cctx)
{
    struct q_bfq_budget_state *state;
    __u64 now;
    __u64 sectors;
    __u64 delta;
    __u64 raw_budget;
    __u32 new_budget = 0;
    bool clamped = false;
    bool updated = false;

    if (!cctx)
        return;

    state = budget_state_lookup(cctx->hctx_idx);
    if (!state)
        return;

    now = cctx->now ? cctx->now : bpf_ktime_get_ns();
    sectors = cctx->nr_sectors ? cctx->nr_sectors : 1;
    if (sectors > Q_BFQ_SAMPLE_SECTOR_CAP)
        sectors = Q_BFQ_SAMPLE_SECTOR_CAP;

    bpf_spin_lock(&state->lock);
    if (!state->window_start_ns || now <= state->window_start_ns) {
        state->window_start_ns = now;
        if (state->window_sectors + sectors < state->window_sectors ||
            state->window_sectors + sectors > Q_BFQ_SAMPLE_SECTOR_CAP)
            state->window_sectors = Q_BFQ_SAMPLE_SECTOR_CAP;
        else
            state->window_sectors += sectors;
        bpf_spin_unlock(&state->lock);
        return;
    }

    if (state->window_sectors + sectors < state->window_sectors ||
        state->window_sectors + sectors > Q_BFQ_SAMPLE_SECTOR_CAP)
        state->window_sectors = Q_BFQ_SAMPLE_SECTOR_CAP;
    else
        state->window_sectors += sectors;

    delta = now - state->window_start_ns;
    if (delta >= Q_BFQ_BUDGET_SAMPLE_NS) {
        raw_budget = (state->window_sectors * Q_BFQ_BUDGET_SLICE_NS) / delta;
        new_budget = bfq_clamp_dynamic_budget(raw_budget, &clamped);
        if (state->max_budget)
            state->max_budget = (state->max_budget * 3 + new_budget) / 4;
        else
            state->max_budget = new_budget;
        if (state->max_budget < Q_BFQ_DYNAMIC_MIN_BUDGET)
            state->max_budget = Q_BFQ_DYNAMIC_MIN_BUDGET;
        if (state->samples < ~0U)
            state->samples++;
        if (clamped && state->clamp_events < ~0U)
            state->clamp_events++;
        state->window_start_ns = now;
        state->window_sectors = 0;
        updated = true;
        new_budget = state->max_budget;
    }
    bpf_spin_unlock(&state->lock);

    if (updated)
        IOEXT_BPF_DBG_SAMPLED(256,
                              "[q_bfq] dynamic budget hctx=%llu budget=%u\n",
                              cctx->hctx_idx,
                              new_budget);
}

static __always_inline int bfq_insert_sorted_pending(struct q_bfq_flow *flow,
                                                     const struct iosched_request_desc *req)
{
    __u32 pos = flow->pending_nr;
    __u32 i;

    if (flow->pending_nr >= Q_BFQ_FLOW_PENDING)
        return -1;

    for (i = 0; i < Q_BFQ_FLOW_PENDING; i++) {
        if (i >= flow->pending_nr)
            break;
        if (req->sector < flow->pending[i].sector) {
            pos = i;
            break;
        }
    }

    if (flow->pending_nr > 6 && pos <= 6)
        flow->pending[7] = flow->pending[6];
    if (flow->pending_nr > 5 && pos <= 5)
        flow->pending[6] = flow->pending[5];
    if (flow->pending_nr > 4 && pos <= 4)
        flow->pending[5] = flow->pending[4];
    if (flow->pending_nr > 3 && pos <= 3)
        flow->pending[4] = flow->pending[3];
    if (flow->pending_nr > 2 && pos <= 2)
        flow->pending[3] = flow->pending[2];
    if (flow->pending_nr > 1 && pos <= 1)
        flow->pending[2] = flow->pending[1];
    if (flow->pending_nr > 0 && pos == 0)
        flow->pending[1] = flow->pending[0];

    flow->pending[pos] = *req;
    flow->pending_nr++;
    return 0;
}

static __always_inline __u32 bfq_pending_c_look_index(struct q_bfq_flow *flow)
{
    __u32 i;
    __u32 best = 0;

    if (!flow->pending_nr)
        return IOSCHED_INVALID_INDEX;

    for (i = 0; i < Q_BFQ_FLOW_PENDING; i++) {
        if (i >= flow->pending_nr)
            break;
        if (flow->pending[i].sector >= flow->last_sector)
            return i;
    }

    for (i = 0; i < Q_BFQ_FLOW_PENDING; i++) {
        if (i >= flow->pending_nr)
            break;
        if (flow->pending[i].sector < flow->pending[best].sector)
            best = i;
    }

    return best;
}

static __always_inline struct iosched_request_desc bfq_take_pending(struct q_bfq_flow *flow,
                                                                     __u32 idx)
{
    struct iosched_request_desc picked = {};

    if (!flow->pending_nr || idx >= flow->pending_nr)
        return picked;

    picked = flow->pending[idx];
    if (idx < 1 && flow->pending_nr > 1)
        flow->pending[0] = flow->pending[1];
    if (idx < 2 && flow->pending_nr > 2)
        flow->pending[1] = flow->pending[2];
    if (idx < 3 && flow->pending_nr > 3)
        flow->pending[2] = flow->pending[3];
    if (idx < 4 && flow->pending_nr > 4)
        flow->pending[3] = flow->pending[4];
    if (idx < 5 && flow->pending_nr > 5)
        flow->pending[4] = flow->pending[5];
    if (idx < 6 && flow->pending_nr > 6)
        flow->pending[5] = flow->pending[6];
    if (idx < 7 && flow->pending_nr > 7)
        flow->pending[6] = flow->pending[7];
    flow->pending_nr--;
    return picked;
}

static __always_inline bool bfq_flow_overflow_push(struct q_bfq_flow *flow,
                                                   const struct iosched_request_desc *req)
{
    __u32 tail;

    if (flow->overflow_nr >= Q_BFQ_FLOW_OVERFLOW)
        return false;

    tail = flow->overflow_tail;
    if (tail >= Q_BFQ_FLOW_OVERFLOW)
        tail = 0;

    flow->overflow[tail] = *req;
    tail++;
    if (tail >= Q_BFQ_FLOW_OVERFLOW)
        tail = 0;
    flow->overflow_tail = tail;
    flow->overflow_nr++;
    return true;
}

static __always_inline void bfq_flow_overflow_undo_tail(struct q_bfq_flow *flow)
{
    __u32 tail;

    if (!flow->overflow_nr)
        return;

    tail = flow->overflow_tail;
    if (tail >= Q_BFQ_FLOW_OVERFLOW)
        tail = 0;
    if (!tail)
        tail = Q_BFQ_FLOW_OVERFLOW;
    tail--;
    flow->overflow_tail = tail;
    flow->overflow_nr--;
}

static __always_inline bool bfq_rehydrate_from_overflow(struct q_bfq_flow *flow)
{
    struct iosched_request_desc req;
    __u32 head;

    if (!flow->overflow_nr || flow->pending_nr >= Q_BFQ_FLOW_PENDING)
        return false;

    head = flow->overflow_head;
    if (head >= Q_BFQ_FLOW_OVERFLOW)
        head = 0;

    req = flow->overflow[head];
    if (bfq_insert_sorted_pending(flow, &req))
        return false;

    head++;
    if (head >= Q_BFQ_FLOW_OVERFLOW)
        head = 0;
    flow->overflow_head = head;
    flow->overflow_nr--;
    return true;
}

static __always_inline int bfq_find_pending_index(struct q_bfq_flow *flow,
                                                  __u64 rq_ptr)
{
    __u32 i;

    for (i = 0; i < Q_BFQ_FLOW_PENDING; i++) {
        if (i >= flow->pending_nr)
            break;
        if (flow->pending[i].rq_ptr == rq_ptr)
            return i;
    }

    return -1;
}

static __always_inline __u32 bfq_flow_flags_from_icq(const struct q_bfq_icq_state *icq_state)
{
    __u32 flags = 0;

    if (!icq_state)
        return 0;
    if (icq_state->flags & Q_BFQ_ICQ_F_INTERACTIVE)
        flags |= Q_BFQ_FLOW_F_INTERACTIVE;
    if (icq_state->flags & Q_BFQ_ICQ_F_SOFT_RT)
        flags |= Q_BFQ_FLOW_F_SOFT_RT;
    return flags;
}

static __always_inline void bfq_reset_flow(struct q_bfq_flow *flow)
{
    flow->last_sector = 0;
    flow->weight = 0;
    flow->pending_nr = 0;
    flow->overflow_head = 0;
    flow->overflow_tail = 0;
    flow->overflow_nr = 0;
    flow->budget_left = 0;
    flow->flags = 0;
}

static __always_inline bool bfq_active_push(struct q_bfq_runtime_state *state,
                                            const struct q_bfq_flow_key *key)
{
    __u32 tail;

    if (state->active_queue.nr >= IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH)
        return false;

    tail = state->active_queue.tail;
    if (tail >= IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH)
        tail = 0;

    state->active_queue.entries[tail] = *key;
    tail++;
    if (tail >= IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH)
        tail = 0;
    state->active_queue.tail = tail;
    state->active_queue.nr++;
    return true;
}

static __always_inline bool bfq_active_pop(struct q_bfq_runtime_state *state,
                                           struct q_bfq_flow_key *key)
{
    __u32 head;

    if (!state->active_queue.nr)
        return false;

    head = state->active_queue.head;
    if (head >= IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH)
        head = 0;

    *key = state->active_queue.entries[head];
    head++;
    if (head >= IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH)
        head = 0;
    state->active_queue.head = head;
    state->active_queue.nr--;
    return true;
}

static __always_inline bool bfq_overflow_push(struct q_bfq_runtime_state *state,
                                              __u64 rq_ptr,
                                              __u32 cost)
{
    __u32 tail;

    if (state->overflow_queue.nr >= IOSCHED_BFQ_OVERFLOW_DEPTH)
        return false;

    tail = state->overflow_queue.tail;
    if (tail >= IOSCHED_BFQ_OVERFLOW_DEPTH)
        tail = 0;

    state->overflow_queue.entries[tail].rq_ptr = rq_ptr;
    state->overflow_queue.entries[tail].cost = cost;
    tail++;
    if (tail >= IOSCHED_BFQ_OVERFLOW_DEPTH)
        tail = 0;
    state->overflow_queue.tail = tail;
    state->overflow_queue.nr++;
    return true;
}

static __always_inline bool bfq_overflow_pop(struct q_bfq_runtime_state *state,
                                             struct q_bfq_overflow_entry *entry)
{
    __u32 head;

    if (!state->overflow_queue.nr)
        return false;

    head = state->overflow_queue.head;
    if (head >= IOSCHED_BFQ_OVERFLOW_DEPTH)
        head = 0;

    *entry = state->overflow_queue.entries[head];
    head++;
    if (head >= IOSCHED_BFQ_OVERFLOW_DEPTH)
        head = 0;
    state->overflow_queue.head = head;
    state->overflow_queue.nr--;
    return true;
}

static __always_inline struct iosched_bfq_tenant_key bfq_tenant_key(__u64 cgroup_id,
                                                                     __u64 hctx_idx)
{
    struct iosched_bfq_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
    };

    return key;
}

static __always_inline struct iosched_bfq_cgroup_state *bfq_summary_lookup(__u64 cgroup_id,
                                                                            __u64 hctx_idx)
{
    struct iosched_bfq_cgroup_state zero = {};
    struct iosched_bfq_tenant_key key = bfq_tenant_key(cgroup_id, hctx_idx);
    struct iosched_bfq_cgroup_state *summary;

    if (!cgroup_id)
        return NULL;

    bpf_map_update_elem(&bfq_cgroup_state, &key, &zero, BPF_NOEXIST);
    summary = bpf_map_lookup_elem(&bfq_cgroup_state, &key);
    return summary;
}

static __always_inline void bfq_summary_refresh(struct iosched_bfq_cgroup_state *summary)
{
    summary->dispatch_class = iosched_bfq_summary_class(summary->active_flows,
                                                        summary->soft_rt_flows,
                                                        summary->interactive_flows,
                                                        summary->sync_flows);
    summary->dispatch_budget = summary->queued_requests ?
        iosched_bfq_summary_budget(summary->dispatch_class) : 0;
    if (!summary->queued_requests)
        summary->next_cost = 0;
}

static __always_inline void bfq_summary_note_new_flow(__u64 cgroup_id,
                                                      __u64 hctx_idx,
                                                      __u32 flow_class,
                                                      __u32 flow_flags)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    __sync_fetch_and_add(&summary->active_flows, 1);
    if (flow_class == IOSCHED_BFQ_CLASS_SYNC)
        __sync_fetch_and_add(&summary->sync_flows, 1);
    if (flow_flags & Q_BFQ_FLOW_F_INTERACTIVE)
        __sync_fetch_and_add(&summary->interactive_flows, 1);
    if (flow_flags & Q_BFQ_FLOW_F_SOFT_RT)
        __sync_fetch_and_add(&summary->soft_rt_flows, 1);
    bfq_summary_refresh(summary);
}

static __always_inline void bfq_summary_note_flags(__u64 cgroup_id,
                                                   __u64 hctx_idx,
                                                   bool add_interactive,
                                                   bool add_soft_rt)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    if (add_interactive)
        __sync_fetch_and_add(&summary->interactive_flows, 1);
    if (add_soft_rt)
        __sync_fetch_and_add(&summary->soft_rt_flows, 1);
    bfq_summary_refresh(summary);
}

static __always_inline void bfq_summary_undo_flags(__u64 cgroup_id,
                                                   __u64 hctx_idx,
                                                   bool remove_interactive,
                                                   bool remove_soft_rt)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    if (remove_interactive && summary->interactive_flows)
        __sync_fetch_and_add(&summary->interactive_flows, (__u32)-1);
    if (remove_soft_rt && summary->soft_rt_flows)
        __sync_fetch_and_add(&summary->soft_rt_flows, (__u32)-1);
    bfq_summary_refresh(summary);
}

static __always_inline void bfq_summary_note_remove_flow(__u64 cgroup_id,
                                                         __u64 hctx_idx,
                                                         __u32 flow_class,
                                                         __u32 flow_flags)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    if (summary->active_flows)
        __sync_fetch_and_add(&summary->active_flows, (__u32)-1);
    if (flow_class == IOSCHED_BFQ_CLASS_SYNC && summary->sync_flows)
        __sync_fetch_and_add(&summary->sync_flows, (__u32)-1);
    if ((flow_flags & Q_BFQ_FLOW_F_INTERACTIVE) && summary->interactive_flows)
        __sync_fetch_and_add(&summary->interactive_flows, (__u32)-1);
    if ((flow_flags & Q_BFQ_FLOW_F_SOFT_RT) && summary->soft_rt_flows)
        __sync_fetch_and_add(&summary->soft_rt_flows, (__u32)-1);
    bfq_summary_refresh(summary);
}

static __always_inline void bfq_summary_note_enqueue(__u64 cgroup_id,
                                                     __u64 hctx_idx,
                                                     __u32 cost)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    __sync_fetch_and_add(&summary->queued_requests, 1);
    if (!summary->next_cost || cost < summary->next_cost)
        summary->next_cost = cost;
    bfq_summary_refresh(summary);
}

static __always_inline void bfq_summary_note_dispatch(__u64 cgroup_id,
                                                      __u64 hctx_idx,
                                                      __u64 now,
                                                      __u32 next_cost)
{
    struct iosched_bfq_cgroup_state *summary = bfq_summary_lookup(cgroup_id, hctx_idx);

    if (!summary)
        return;
    if (summary->queued_requests)
        __sync_fetch_and_add(&summary->queued_requests, (__u32)-1);
    summary->last_dispatch_ns = now;
    summary->next_cost = next_cost;
    bfq_summary_refresh(summary);
}

static __always_inline int bfq_insert_request_locked(struct q_bfq_runtime_state *state,
                                                     struct q_bfq_flow *flow,
                                                     const struct q_bfq_flow_key *key,
                                                     const struct iosched_request_desc *desc,
                                                     __u32 flow_flags,
                                                     __u32 base_budget,
                                                     bool *need_queue,
                                                     bool *need_overflow,
                                                     bool *new_flow,
                                                     bool *added_interactive,
                                                     bool *added_soft_rt,
                                                     bool *added_on_queue)
{
    *need_queue = false;
    *need_overflow = false;
    *new_flow = flow->pending_nr == 0 && flow->overflow_nr == 0;
    *added_interactive = false;
    *added_soft_rt = false;
    *added_on_queue = false;

    if (bfq_insert_sorted_pending(flow, desc)) {
        if (*new_flow)
            return -1;
        if (!(flow->flags & Q_BFQ_FLOW_F_ON_QUEUE) &&
            !bfq_flow_key_equal(&state->current_key, key)) {
            flow->flags |= Q_BFQ_FLOW_F_ON_QUEUE;
            *need_queue = true;
            *added_on_queue = true;
        }
        *need_overflow = true;
        return 0;
    }

    if (*new_flow) {
        flow->weight = desc->weight ? desc->weight : IOSCHED_BFQ_WEIGHT_DEFAULT;
        flow->budget_left = base_budget ? base_budget :
            iosched_bfq_flow_base_budget(key->flow_class, flow->weight);
        flow->last_sector = 0;
        flow->flags = flow_flags | Q_BFQ_FLOW_F_ON_QUEUE;
        *need_queue = true;
        return 0;
    }

    if ((flow_flags & Q_BFQ_FLOW_F_INTERACTIVE) &&
        !(flow->flags & Q_BFQ_FLOW_F_INTERACTIVE)) {
        flow->flags |= Q_BFQ_FLOW_F_INTERACTIVE;
        *added_interactive = true;
    }
    if ((flow_flags & Q_BFQ_FLOW_F_SOFT_RT) &&
        !(flow->flags & Q_BFQ_FLOW_F_SOFT_RT)) {
        flow->flags |= Q_BFQ_FLOW_F_SOFT_RT;
        *added_soft_rt = true;
    }
    if (desc->weight > flow->weight)
        flow->weight = desc->weight;
    if (!(flow->flags & Q_BFQ_FLOW_F_ON_QUEUE) &&
        !bfq_flow_key_equal(&state->current_key, key)) {
        flow->flags |= Q_BFQ_FLOW_F_ON_QUEUE;
        *need_queue = true;
        *added_on_queue = true;
    }

    return 0;
}

static __always_inline void bfq_rollback_insert_locked(struct q_bfq_runtime_state *state,
                                                       struct q_bfq_flow *flow,
                                                       const struct q_bfq_flow_key *key,
                                                       __u64 rq_ptr,
                                                       bool new_flow,
                                                       bool added_interactive,
                                                       bool added_soft_rt,
                                                       bool added_on_queue)
{
    int idx;

    idx = bfq_find_pending_index(flow, rq_ptr);
    if (idx >= 0)
        bfq_take_pending(flow, idx);

    if (new_flow) {
        bfq_reset_flow(flow);
        return;
    }

    if (added_interactive)
        flow->flags &= ~Q_BFQ_FLOW_F_INTERACTIVE;
    if (added_soft_rt)
        flow->flags &= ~Q_BFQ_FLOW_F_SOFT_RT;
    if (added_on_queue)
        flow->flags &= ~Q_BFQ_FLOW_F_ON_QUEUE;
    if (!flow->pending_nr && !flow->overflow_nr)
        flow->flags &= ~Q_BFQ_FLOW_F_ON_QUEUE;
    if (bfq_flow_key_equal(&state->current_key, key))
        flow->flags &= ~Q_BFQ_FLOW_F_ON_QUEUE;
}

static __always_inline __u64 bfq_dispatch_from_flow_locked(struct q_bfq_flow *flow,
                                                           __u32 flow_class,
                                                           __u32 base_budget,
                                                           __u32 *next_cost,
                                                           bool *needs_requeue)
{
    struct iosched_request_desc picked;
    __u32 idx;
    __u32 charge;

    *needs_requeue = false;
    *next_cost = 0;
    if (!flow->pending_nr)
        bfq_rehydrate_from_overflow(flow);
    if (!flow->pending_nr)
        return 0;

    idx = bfq_pending_c_look_index(flow);
    if (idx == IOSCHED_INVALID_INDEX)
        return 0;

    if (!base_budget)
        base_budget = iosched_bfq_flow_base_budget(flow_class,
                                                   flow->weight ? flow->weight : IOSCHED_BFQ_WEIGHT_DEFAULT);
    if (!flow->budget_left)
        flow->budget_left = base_budget;

    picked = bfq_take_pending(flow, idx);
    if (!picked.rq_ptr)
        return 0;

    charge = iosched_bfq_charge_cost(flow_class, picked.cost);
    if (charge > flow->budget_left)
        flow->budget_left = charge;

    if (charge >= flow->budget_left)
        flow->budget_left = 0;
    else
        flow->budget_left -= charge;

    flow->last_sector = picked.sector + picked.cost;
    bfq_rehydrate_from_overflow(flow);
    if (flow->pending_nr) {
        idx = bfq_pending_c_look_index(flow);
        if (idx != IOSCHED_INVALID_INDEX)
            *next_cost = flow->pending[idx].cost;
    }
    if (flow->pending_nr && !flow->budget_left) {
        *needs_requeue = true;
        flow->budget_left = base_budget;
    }

    return picked.rq_ptr;
}

static __always_inline __u64 state_dispatch(__u64 cgroup_id,
                                            __u64 hctx_idx)
{
    struct q_bfq_state_entry *entry;
    struct q_bfq_flow *flow;
    struct q_bfq_flow_key key = {};
    struct q_bfq_overflow_entry overflow = {};
    __u64 rq_ptr = 0;
    __u64 now = bpf_ktime_get_ns();
    __u32 attempt;
    __u32 next_cost = 0;
    __u32 base_budget = 0;
    __u32 removed_flow_flags = 0;
    bool remove_flow = false;
    bool needs_requeue = false;
    bool from_overflow = false;
    
    entry = state_lookup(hctx_idx);
    if (!entry)
        return 0;

    for (attempt = 0; attempt < Q_BFQ_STALE_SCAN_LIMIT; attempt++) {
        bpf_spin_lock(&entry->lock);
        if (!bfq_flow_key_empty(&entry->state.current_key)) {
            key = entry->state.current_key;
            from_overflow = false;
            bpf_spin_unlock(&entry->lock);
        } else if (bfq_active_pop(&entry->state, &key)) {
            entry->state.current_key = key;
            from_overflow = false;
            bpf_spin_unlock(&entry->lock);
        } else if (bfq_overflow_pop(&entry->state, &overflow)) {
            __u32 head = entry->state.overflow_queue.head;

            if (head >= IOSCHED_BFQ_OVERFLOW_DEPTH)
                head = 0;
            next_cost = entry->state.overflow_queue.nr ?
                entry->state.overflow_queue.entries[head].cost : 0;
            from_overflow = true;
            bpf_spin_unlock(&entry->lock);
            bfq_summary_note_dispatch(cgroup_id, hctx_idx, now, next_cost);
            iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
            return overflow.rq_ptr;
        } else {
            bpf_spin_unlock(&entry->lock);
            return 0;
        }

        flow = flow_lookup(&key);
        if (!flow || (!flow->pending_nr && !flow->overflow_nr)) {
            bpf_spin_lock(&entry->lock);
            if (bfq_flow_key_equal(&entry->state.current_key, &key))
                bfq_flow_key_clear(&entry->state.current_key);
            bpf_spin_unlock(&entry->lock);
            continue;
        }

        base_budget = bfq_dynamic_base_budget(hctx_idx,
                                             key.flow_class,
                                             flow->weight ? flow->weight : IOSCHED_BFQ_WEIGHT_DEFAULT);

        bpf_spin_lock(&entry->lock);
        if (!bfq_flow_key_equal(&entry->state.current_key, &key) ||
            (!flow->pending_nr && !flow->overflow_nr)) {
            if (bfq_flow_key_equal(&entry->state.current_key, &key))
                bfq_flow_key_clear(&entry->state.current_key);
            bpf_spin_unlock(&entry->lock);
            continue;
        }

        next_cost = 0;
        needs_requeue = false;
        remove_flow = false;
        removed_flow_flags = 0;
        rq_ptr = bfq_dispatch_from_flow_locked(flow,
                                              key.flow_class,
                                              base_budget,
                                              &next_cost,
                                              &needs_requeue);
        if (!rq_ptr) {
            bfq_flow_key_clear(&entry->state.current_key);
            bpf_spin_unlock(&entry->lock);
            continue;
        }

        if (!flow->pending_nr && !flow->overflow_nr) {
            removed_flow_flags = flow->flags;
            bfq_reset_flow(flow);
            bfq_flow_key_clear(&entry->state.current_key);
            remove_flow = true;
        } else if (needs_requeue) {
            __u32 idx;

            flow->flags |= Q_BFQ_FLOW_F_ON_QUEUE;
            if (!bfq_active_push(&entry->state, &key)) {
                flow->flags &= ~Q_BFQ_FLOW_F_ON_QUEUE;
                entry->state.current_key = key;
                idx = bfq_pending_c_look_index(flow);
                next_cost = idx != IOSCHED_INVALID_INDEX ? flow->pending[idx].cost : 0;
            } else {
                bfq_flow_key_clear(&entry->state.current_key);
            }
        }
        bpf_spin_unlock(&entry->lock);

        if (remove_flow)
            bfq_summary_note_remove_flow(cgroup_id, hctx_idx, key.flow_class, removed_flow_flags);
        bfq_summary_note_dispatch(cgroup_id, hctx_idx, now, next_cost);
        iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
        return rq_ptr;
    }

    return 0;
}

SEC("struct_ops/init_queue")
int BPF_PROG(init_queue, struct iosched_queue_ctx *qctx)
{
    return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/exit_queue")
int BPF_PROG(exit_queue, struct iosched_queue_ctx *qctx)
{
    if (!qctx || !qctx->q)
        return -1;
    return 0;
}

SEC("struct_ops/init_hctx")
int BPF_PROG(init_hctx, struct iosched_hctx_ctx *hctx)
{
    struct q_bfq_state_entry *entry;

    if (!hctx || !hctx->q || !hctx->hctx)
        return -1;

    entry = state_lookup(hctx->hctx_idx);
    if (!entry)
        return -1;

    bpf_spin_lock(&entry->lock);
    bfq_flow_key_clear(&entry->state.current_key);
    entry->state.active_queue.head = 0;
    entry->state.active_queue.tail = 0;
    entry->state.active_queue.nr = 0;
    entry->state.overflow_queue.head = 0;
    entry->state.overflow_queue.tail = 0;
    entry->state.overflow_queue.nr = 0;
    bpf_spin_unlock(&entry->lock);
    return 0;
}

SEC("struct_ops/exit_hctx")
void BPF_PROG(exit_hctx, struct iosched_hctx_ctx *hctx)
{
    if (!hctx || !hctx->q || !hctx->hctx)
        return;
}

SEC("struct_ops/init_icq")
void BPF_PROG(init_icq, struct iosched_icq_ctx *ictx)
{
    struct q_bfq_icq_state zero = {};
    struct q_bfq_state_entry *entry;
    __u64 key;
    __u64 now;

    if (!ictx || !ictx->icq)
        return;

    entry = state_lookup(0);
    if (!entry)
        return;

    key = (__u64)ictx->icq;
    now = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    if (iosched_bfq_burst_event(&entry->burst, now))
        zero.flags |= Q_BFQ_ICQ_F_IN_LARGE_BURST;
    bpf_spin_unlock(&entry->lock);
    bpf_map_update_elem(&icq_state_map, &key, &zero, BPF_NOEXIST);
}

SEC("struct_ops/exit_icq")
void BPF_PROG(exit_icq, struct iosched_icq_ctx *ictx)
{
    __u64 key;

    if (!ictx || !ictx->icq)
        return;

    key = (__u64)ictx->icq;
    bpf_map_delete_elem(&icq_state_map, &key);
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    struct q_bfq_state_entry *entry;
    struct q_bfq_icq_state *icq_state;
    struct q_bfq_flow_key key;
    struct q_bfq_flow *flow;
    struct q_bfq_flow *zero_flow;
    struct iosched_request_desc desc;
    __u64 icq_key;
    __u32 flow_flags = 0;
    __u32 zero_key = 0;
    bool need_queue = false;
    bool need_overflow = false;
    bool new_flow = false;
    bool added_interactive = false;
    bool added_soft_rt = false;
    bool added_on_queue = false;
    __u32 base_budget;

    if (!ictx || !ictx->rq)
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
    desc.weight = iosched_bfq_weight_from_ioprio(desc.ioprio);
    icq_key = ictx->icq_ptr;
    icq_state = NULL;
    if (icq_key) {
        __u32 flow_class = iosched_bfq_class(&desc);
        __u64 last_completion_ns = 0;
        __u64 think_avg_ns = 0;
        __u32 requests = 0;
        __u32 boost_budget = 0;
        __u64 boost_expires_ns = 0;
        __u32 flags = 0;

        icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
        if (icq_state) {
            if (!icq_state->base_weight)
                icq_state->base_weight = iosched_bfq_weight_from_ioprio(desc.ioprio);
            icq_state->flow_class = flow_class;
            desc.weight = icq_state->base_weight;
            last_completion_ns = icq_state->last_completion_ns;
            think_avg_ns = iosched_bfq_update_think_time(desc.arrival_ns,
                                                         icq_state->last_insert_ns,
                                                         icq_state->think_avg_ns);
            requests = icq_state->requests;
            flags = icq_state->flags;
            if (iosched_bfq_interactive_state(!!(flags & Q_BFQ_ICQ_F_IN_LARGE_BURST),
                                              iosched_bfq_has_short_ttime(think_avg_ns),
                                              requests))
                flags |= Q_BFQ_ICQ_F_INTERACTIVE;
            else
                flags &= ~Q_BFQ_ICQ_F_INTERACTIVE;
            if (iosched_bfq_soft_rt_state(flow_class,
                                          !!(flags & Q_BFQ_ICQ_F_IN_LARGE_BURST),
                                          iosched_bfq_has_short_ttime(think_avg_ns),
                                          requests,
                                          desc.arrival_ns,
                                          last_completion_ns))
                flags |= Q_BFQ_ICQ_F_SOFT_RT;
            else
                flags &= ~Q_BFQ_ICQ_F_SOFT_RT;
            boost_budget = iosched_bfq_effective_boost_budget(desc.arrival_ns,
                                                              icq_state->boost_budget,
                                                              icq_state->boost_expires_ns);
            boost_budget = iosched_bfq_boost_budget(requests,
                                                    flow_class,
                                                    !!(flags & Q_BFQ_ICQ_F_IN_LARGE_BURST),
                                                    !!(flags & Q_BFQ_ICQ_F_INTERACTIVE),
                                                    desc.arrival_ns,
                                                    last_completion_ns,
                                                    boost_budget);
            if (boost_budget && !icq_state->boost_expires_ns)
                boost_expires_ns = desc.arrival_ns + IOSCHED_BFQ_INTERACTIVE_WINDOW_NS;
            else
                boost_expires_ns = icq_state->boost_expires_ns;
            desc.weight = iosched_bfq_boost_weight(desc.weight,
                                                   flow_class,
                                                   !!(flags & Q_BFQ_ICQ_F_IN_LARGE_BURST),
                                                   !!(flags & Q_BFQ_ICQ_F_INTERACTIVE),
                                                   requests,
                                                   desc.arrival_ns,
                                                   last_completion_ns,
                                                   boost_budget);
            if ((flags & Q_BFQ_ICQ_F_SOFT_RT) && desc.weight < IOSCHED_BFQ_WEIGHT_RT)
                desc.weight = IOSCHED_BFQ_WEIGHT_RT;
            icq_state->last_insert_ns = desc.arrival_ns;
            icq_state->think_avg_ns = think_avg_ns;
            icq_state->boost_budget = boost_budget;
            icq_state->boost_expires_ns = boost_expires_ns;
            icq_state->flags = flags;
            flow_flags = bfq_flow_flags_from_icq(icq_state);
        }
    }
    if (!desc.issuer_id)
        desc.owner_id = (__u32)(bpf_get_current_pid_tgid() >> 32);
    if (icq_key) {
        icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
        if (icq_state)
            icq_state->requests++;
    }

    key = bfq_flow_key_from_desc(&desc, ictx->hctx_idx);
    base_budget = bfq_dynamic_base_budget(ictx->hctx_idx, key.flow_class, desc.weight);
    flow = flow_lookup(&key);
    if (!flow) {
        zero_flow = bpf_map_lookup_elem(&flow_zero_map, &zero_key);
        if (!zero_flow) {
            if (icq_key) {
                icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
                if (icq_state && icq_state->requests)
                    icq_state->requests--;
            }
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }
        bpf_map_update_elem(&flow_map, &key, zero_flow, BPF_NOEXIST);
        flow = flow_lookup(&key);
        if (!flow) {
            if (icq_key) {
                icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
                if (icq_state && icq_state->requests)
                    icq_state->requests--;
            }
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }
    }

    bpf_spin_lock(&entry->lock);
    if (bfq_insert_request_locked(&entry->state,
                                  flow,
                                  &key,
                                  &desc,
                                  flow_flags,
                                  base_budget,
                                  &need_queue,
                                  &need_overflow,
                                  &new_flow,
                                  &added_interactive,
                                  &added_soft_rt,
                                  &added_on_queue)) {
        bpf_spin_unlock(&entry->lock);
        if (icq_key) {
            icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
            if (icq_state && icq_state->requests)
                icq_state->requests--;
        }
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    if (need_overflow && !bfq_flow_overflow_push(flow, &desc)) {
        bfq_rollback_insert_locked(&entry->state,
                                   flow,
                                   &key,
                                   desc.rq_ptr,
                                   new_flow,
                                   added_interactive,
                                   added_soft_rt,
                                   added_on_queue);
        bpf_spin_unlock(&entry->lock);
        if (icq_key) {
            icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
            if (icq_state && icq_state->requests)
                icq_state->requests--;
        }
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    if (need_queue && !bfq_active_push(&entry->state, &key)) {
        bfq_rollback_insert_locked(&entry->state,
                                   flow,
                                   &key,
                                   desc.rq_ptr,
                                   new_flow,
                                   added_interactive,
                                   added_soft_rt,
                                   added_on_queue);
        if (need_overflow)
            bfq_flow_overflow_undo_tail(flow);
        bpf_spin_unlock(&entry->lock);

        if (new_flow)
            bfq_summary_note_remove_flow(ictx->cgroup_id, ictx->hctx_idx, key.flow_class, flow_flags);
        else
            bfq_summary_undo_flags(ictx->cgroup_id, ictx->hctx_idx, added_interactive, added_soft_rt);

        if (icq_key) {
            icq_state = bpf_map_lookup_elem(&icq_state_map, &icq_key);
            if (icq_state && icq_state->requests)
                icq_state->requests--;
        }
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }
    bpf_spin_unlock(&entry->lock);

    if (new_flow)
        bfq_summary_note_new_flow(ictx->cgroup_id, ictx->hctx_idx, key.flow_class, flow->flags);
    else
        bfq_summary_note_flags(ictx->cgroup_id, ictx->hctx_idx, added_interactive, added_soft_rt);
    bfq_summary_note_enqueue(ictx->cgroup_id, ictx->hctx_idx, desc.cost);

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/limit_depth")
__u64 BPF_PROG(limit_depth, struct iosched_limit_depth_ctx *dctx)
{
    struct q_bfq_icq_state *icq_state = NULL;
    bool is_sync;
    bool is_sync_read;
    bool interactive = false;
    bool soft_rt = false;
    __u32 requests = 0;
    __u32 weight;

    if (!dctx || !dctx->data)
        return 0;

    if (dctx->icq_ptr) {
        __u64 key = dctx->icq_ptr;

        icq_state = bpf_map_lookup_elem(&icq_state_map, &key);
        if (icq_state) {
            requests = icq_state->requests;
            interactive = !!(icq_state->flags & Q_BFQ_ICQ_F_INTERACTIVE);
            soft_rt = !!(icq_state->flags & Q_BFQ_ICQ_F_SOFT_RT);
        }
    }

    weight = iosched_bfq_weight_from_ioprio(dctx->ioprio);
    is_sync = !!(dctx->opf & IOSCHED_REQ_SYNC_BIT);
    is_sync_read = (dctx->opf & IOSCHED_REQ_OP_MASK) == REQ_OP_READ && is_sync;
    return iosched_bfq_limit_depth_value(is_sync_read,
                                         is_sync,
                                         interactive,
                                         soft_rt,
                                         weight,
                                         requests,
                                         dctx->async_depth);
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct q_bfq_icq_state *icq_state;
    __u64 key;

    bfq_budget_note_completion(cctx);

    if (!cctx || !cctx->icq_ptr)
        return;

    key = cctx->icq_ptr;
    icq_state = bpf_map_lookup_elem(&icq_state_map, &key);
    if (!icq_state)
        return;

    if (icq_state->requests)
        icq_state->requests--;
    if (icq_state->boost_budget > cctx->nr_sectors)
        icq_state->boost_budget -= cctx->nr_sectors;
    else
        icq_state->boost_budget = 0;
    if (icq_state->boost_budget == 0 ||
        (icq_state->boost_expires_ns && cctx->now > icq_state->boost_expires_ns)) {
        icq_state->boost_budget = 0;
        icq_state->boost_expires_ns = 0;
    }
    icq_state->last_completion_ns = cctx->now;
}

SEC("struct_ops/requeue_request")
void BPF_PROG(requeue_request, struct iosched_rq_ctx *rctx)
{
    struct q_bfq_icq_state *icq_state;
    __u64 key;

    if (!rctx || !rctx->rq || !rctx->icq_ptr)
        return;

    key = rctx->icq_ptr;
    icq_state = bpf_map_lookup_elem(&icq_state_map, &key);
    if (icq_state) {
        if (icq_state->requests)
            icq_state->requests--;
        if (!icq_state->requests &&
            icq_state->boost_expires_ns &&
            icq_state->last_completion_ns > icq_state->boost_expires_ns) {
            icq_state->boost_budget = 0;
            icq_state->boost_expires_ns = 0;
        }
    }
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_dispatch(dctx->cgroup_id, dctx->hctx_idx);
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_dispatch(dctx->cgroup_id, dctx->hctx_idx);
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_bfq = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .init_hctx = (void *)init_hctx,
    .exit_hctx = (void *)exit_hctx,
    .init_icq = (void *)init_icq,
    .exit_icq = (void *)exit_icq,
    .insert_request = (void *)insert_request,
    .limit_depth = (void *)limit_depth,
    .completed_request = (void *)completed_request,
    .requeue_request = (void *)requeue_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_bfq",
};
