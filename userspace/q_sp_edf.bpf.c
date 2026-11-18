#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

/*
 * Tunable policy constants (d2d4_bm tune, 2026-11-10). Each knob is a
 * libbpf .rodata global set by loader_q_sp_edf's CLI before bpf load;
 * the initializers below are the historical compile-time defaults, so a
 * loader started WITHOUT the corresponding --knob keeps the exact
 * original scheduling behavior. loader_q_sp_edf.c re-declares the same
 * default values for its parse fallbacks — the two sets MUST stay in
 * sync (verified by loader --help output vs here).
 */
const volatile __u64 sp_edf_small_read_deadline_ns  = 1000000ULL;
const volatile __u64 sp_edf_small_write_deadline_ns = 2000000ULL;
const volatile __u64 sp_edf_large_deadline_ns       = 20000000ULL;
const volatile __u64 sp_edf_large_max_wait_ns       = 8000000ULL;

/* Not knob-ized (outside the d2d4_bm knob contract): small/lane split
 * threshold and the small-burst quantum stay compile-time constants. */
#define Q_SP_EDF_SMALL_SECTORS 8U
#define Q_SP_EDF_SMALL_BURST 8U

enum q_sp_edf_lane_id {
    Q_SP_EDF_SMALL_READ = 0,
    Q_SP_EDF_SMALL_WRITE = 1,
    Q_SP_EDF_LARGE = 2,
    Q_SP_EDF_NR_LANES = 3,
};

struct q_sp_edf_lane {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
};

struct q_sp_edf_local_state {
    struct q_sp_edf_lane lanes[Q_SP_EDF_NR_LANES];
    __u64 last_sector;
    __u32 small_burst_left;
    __u64 last_large_dispatch_ns;
    __u64 enqueued[Q_SP_EDF_NR_LANES];
    __u64 dispatched[Q_SP_EDF_NR_LANES];
};

struct q_sp_edf_state_entry {
    struct bpf_spin_lock lock;
    struct q_sp_edf_local_state state;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_sp_edf_state_entry);
} state_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_sp_edf_state_entry *state_lookup(__u64 hctx_idx)
{
    __u32 key = (__u32)hctx_idx;

    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline __u32 sp_edf_lane_id(const struct iosched_request_desc *desc)
{
    if (desc->cost > Q_SP_EDF_SMALL_SECTORS)
        return Q_SP_EDF_LARGE;
    if (iosched_req_is_write(desc))
        return Q_SP_EDF_SMALL_WRITE;
    return Q_SP_EDF_SMALL_READ;
}

static __always_inline __u64 sp_edf_deadline_ns(__u32 lane)
{
    if (lane == Q_SP_EDF_SMALL_READ)
        return sp_edf_small_read_deadline_ns;
    if (lane == Q_SP_EDF_SMALL_WRITE)
        return sp_edf_small_write_deadline_ns;
    return sp_edf_large_deadline_ns;
}

static __always_inline int sp_edf_insert_lane(struct q_sp_edf_lane *lane,
                                              const struct iosched_request_desc *req)
{
    __u32 pos = lane->pending_nr;
    __u32 i;

    if (lane->pending_nr >= IOSCHED_MAX_PENDING)
        return -1;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= lane->pending_nr)
            break;
        if (req->deadline_ns < lane->pending[i].deadline_ns ||
            (req->deadline_ns == lane->pending[i].deadline_ns &&
             req->sector < lane->pending[i].sector)) {
            pos = i;
            break;
        }
    }

    for (i = IOSCHED_MAX_PENDING - 1; i > 0; i--) {
        if (i <= pos || i > lane->pending_nr)
            continue;
        lane->pending[i] = lane->pending[i - 1];
    }

    lane->pending[pos] = *req;
    lane->pending_nr++;
    return 0;
}

static __always_inline bool sp_edf_large_should_run(struct q_sp_edf_local_state *state,
                                                    __u64 now_ns)
{
    struct q_sp_edf_lane *large = &state->lanes[Q_SP_EDF_LARGE];

    if (!large->pending_nr)
        return false;
    if (!state->lanes[Q_SP_EDF_SMALL_READ].pending_nr &&
        !state->lanes[Q_SP_EDF_SMALL_WRITE].pending_nr)
        return true;
    if (!state->small_burst_left)
        return true;
    if (large->pending[0].deadline_ns <= now_ns)
        return true;
    if (state->last_large_dispatch_ns &&
        now_ns - state->last_large_dispatch_ns >= sp_edf_large_max_wait_ns)
        return true;
    return false;
}

static __always_inline __u32 sp_edf_choose_lane(struct q_sp_edf_local_state *state,
                                                __u64 now_ns)
{
    if (sp_edf_large_should_run(state, now_ns))
        return Q_SP_EDF_LARGE;
    if (state->lanes[Q_SP_EDF_SMALL_READ].pending_nr)
        return Q_SP_EDF_SMALL_READ;
    if (state->lanes[Q_SP_EDF_SMALL_WRITE].pending_nr)
        return Q_SP_EDF_SMALL_WRITE;
    if (state->lanes[Q_SP_EDF_LARGE].pending_nr)
        return Q_SP_EDF_LARGE;
    return Q_SP_EDF_NR_LANES;
}

static __always_inline __u64 sp_edf_dispatch(struct q_sp_edf_local_state *state,
                                             __u64 now_ns)
{
    struct iosched_request_desc picked;
    struct q_sp_edf_lane *lane;
    __u32 lane_id;

    lane_id = sp_edf_choose_lane(state, now_ns);
    if (lane_id >= Q_SP_EDF_NR_LANES)
        return 0;

    lane = &state->lanes[lane_id];
    if (!lane->pending_nr)
        return 0;

    picked = lane->pending[0];
    if (!iosched_pop_head_32(lane->pending, &lane->pending_nr))
        return 0;

    state->last_sector = picked.sector + picked.cost;
    state->dispatched[lane_id]++;
    if (lane_id == Q_SP_EDF_LARGE) {
        state->last_large_dispatch_ns = now_ns;
        state->small_burst_left = Q_SP_EDF_SMALL_BURST;
    } else if (state->small_burst_left > 0) {
        state->small_burst_left--;
    }

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
    struct q_sp_edf_state_entry *entry;
    struct iosched_request_desc desc;
    __u32 lane;
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
    lane = sp_edf_lane_id(&desc);
    desc.deadline_ns = desc.arrival_ns + sp_edf_deadline_ns(lane);

    bpf_spin_lock(&entry->lock);
    if (!entry->state.small_burst_left)
        entry->state.small_burst_left = Q_SP_EDF_SMALL_BURST;
    ret = sp_edf_insert_lane(&entry->state.lanes[lane], &desc);
    if (!ret)
        entry->state.enqueued[lane]++;
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                          "[q_sp_edf] insert rq=%llx lane=%u cost=%u deadline=%llu\n",
                          desc.rq_ptr, lane, desc.cost, desc.deadline_ns);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_sp_edf_state_entry *entry;
    __u64 now_ns;
    __u64 rq_ptr;

    if (!dctx)
        return 0;
    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = sp_edf_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_sp_edf_state_entry *entry;
    __u64 now_ns;
    __u64 rq_ptr;

    if (!dctx)
        return 0;
    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = sp_edf_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_sp_edf = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_sp_edf",
};
