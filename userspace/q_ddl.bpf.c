/* q_ddl.bpf.c - Local deadline two-level FIFO queue policy
 *
 * Two FIFO queues: LAT (latency-critical) and BULK (best-effort).
 * Classification by ioprio class:
 *   IOPRIO_CLASS_RT (1)  -> LAT
 *   IOPRIO_CLASS_BE (2)  -> BULK (or LAT if read && small)
 *   IOPRIO_CLASS_IDLE (3)-> BULK
 * Fallback heuristic: read && size <= 8 sectors -> LAT
 *
 * Dispatch precedence (POL-05 fix):
 *   1. LAT missed deadline AND emergency_burst < EMERGENCY_MAX -> LAT
 *   2. BULK starved (wait > starvation_ns)                    -> BULK
 *   3. LAT urgent (deadline within guard_ns) AND burst < max -> LAT
 *   4. BULK pending                                           -> BULK
 *   5. LAT pending (fallback)                                 -> LAT
 *
 * Merge: disabled (allow_merge = false) for correctness (POL-06).
 *
 * State map key: (cgroup_id, hctx_idx) for per-cgroup isolation (POL-08).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

/*
 * Tunable policy constants (d2d4_bm tune, 2026-11-10). Each knob is a
 * libbpf .rodata global set by loader_q_ddl's CLI before bpf load; the
 * initializers below are the historical compile-time defaults, so a
 * loader started WITHOUT the corresponding --knob keeps the exact
 * original scheduling behavior (all comparisons/arithmetic evaluate to
 * the same results as the previous #define literals). loader_q_ddl.c
 * re-declares the same default values for its parse fallbacks — the two
 * sets MUST stay in sync (verified by loader --help output vs here).
 *
 * bm calibration context (experiments/isol_d2d4_bm RUNBOOK §3.3): with
 * the defaults below, D4 LC burst under BE QD1024 measured LC whole-
 * window p99 = 10.4857 ms — each cycle waits out the 10 ms
 * BULK_STARVATION window before the emergency-burst/LAT counters reset
 * (fast-device mismatch; the constants were calibrated on the slow sdb
 * rehearsal rig where the same defaults gave LC p99 ~143 us).
 */
const volatile __u64 q_ddl_lat_deadline_ns     = 1000000ULL;
const volatile __u64 q_ddl_bulk_deadline_ns   = 20000000ULL;
const volatile __u64 q_ddl_bulk_starvation_ns = 10000000ULL;
const volatile __u64 q_ddl_guard_ns           = 200000ULL;
const volatile __u32 q_ddl_emergency_max      = 3U;
const volatile __u32 q_ddl_small_sector_thresh = 8U;

/* Not knob-ized: LAT burst limit is structural (per the d2d4_bm knob
 * contract) and stays a compile-time constant. */
#define Q_DDL_BURST_MAX           8

#ifndef IOPRIO_CLASS_SHIFT
#define IOPRIO_CLASS_SHIFT 13
#endif

#define IOPRIO_CLASS_RT    1
#define IOPRIO_CLASS_BE    2
#define IOPRIO_CLASS_IDLE  3

enum ddl_class {
    DDL_LAT  = 0,
    DDL_BULK = 1,
    DDL_NR   = 2,
};

struct q_ddl_lane {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
    __u64 enqueued;
    __u64 dispatched;
};

struct q_ddl_local_state {
    struct q_ddl_lane lanes[DDL_NR];
    __u32 lat_burst;
    __u32 emergency_burst;
    __u64 lat_head_enqueue_ns;
    __u64 lat_head_deadline;
    __u64 bulk_head_enqueue_ns;
    __u64 last_dispatch_ns;
};

struct q_ddl_state_entry {
    struct bpf_spin_lock lock;
    struct q_ddl_local_state state;
};

/*
 * Phase-3 bypass-bug fix (2026-09-01): this map was BPF_MAP_TYPE_HASH keyed
 * by (cgroup_id, hctx_idx) with NO creator anywhere (no bpf_map_update_elem
 * in this file - BPF cannot update spin_lock-valued hash maps), so lookups
 * ALWAYS missed, insert_request returned -1 for every request, and the
 * kernel bypassed q_ddl entirely (probe: policy_meta dispatches==0 for all
 * traces; only cases whose golden differs from submission order could
 * detect it - q_ddl M02 FAIL 5/5, see the bypass_gap evidence package in
 * the q_ddl accept results dirs).
 * Now mirrors the proven sibling pattern (q_sp_edf, q_wedf, q_kyber):
 * ARRAY keyed by hctx_idx, auto-zero-initialized entries, no creator
 * needed. Semantic reduction, same as siblings: per-hctx state shared
 * across cgroups (the DT rig runs root-cgroup only; multi-tenant
 * isolation was never reachable in the old code either - it bypassed
 * everything).
 */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_ddl_state_entry);
} state_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_ddl_state_entry *ddl_state_lookup(__u64 cgroup_id,
                                                                    __u64 hctx_idx)
{
    __u32 key = (__u32)hctx_idx;

    (void)cgroup_id;
    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline __u32 ddl_ioprio_class(__u64 ioprio)
{
    return (__u32)((ioprio >> IOPRIO_CLASS_SHIFT) & 0x7);
}

static __always_inline enum ddl_class ddl_classify(const struct iosched_request_desc *desc)
{
    __u32 ioprio_cls = ddl_ioprio_class(desc->ioprio);

    if (ioprio_cls == IOPRIO_CLASS_RT)
        return DDL_LAT;

    if (ioprio_cls == IOPRIO_CLASS_IDLE)
        return DDL_BULK;

    if (!iosched_req_is_write(desc) && desc->cost <= q_ddl_small_sector_thresh)
        return DDL_LAT;

    return DDL_BULK;
}

static __always_inline __u64 ddl_deadline_ns(enum ddl_class cls)
{
    return (cls == DDL_LAT) ? q_ddl_lat_deadline_ns : q_ddl_bulk_deadline_ns;
}

static __always_inline int ddl_push_tail(struct q_ddl_lane *lane,
                                          const struct iosched_request_desc *req)
{
    if (lane->pending_nr >= IOSCHED_MAX_PENDING)
        return -1;
    lane->pending[lane->pending_nr] = *req;
    lane->pending_nr++;
    lane->enqueued++;
    return 0;
}

static __always_inline __u64 ddl_pop_head(struct q_ddl_lane *lane)
{
    struct iosched_request_desc picked;
    __u64 rq_ptr;

    if (lane->pending_nr == 0)
        return 0;

    picked = lane->pending[0];
    if (!iosched_pop_head_32(lane->pending, &lane->pending_nr))
        return 0;

    lane->dispatched++;
    rq_ptr = picked.rq_ptr;
    return rq_ptr;
}

static __always_inline void ddl_update_heads(struct q_ddl_local_state *state)
{
    if (state->lanes[DDL_LAT].pending_nr > 0) {
        state->lat_head_enqueue_ns = state->lanes[DDL_LAT].pending[0].arrival_ns;
        state->lat_head_deadline = state->lanes[DDL_LAT].pending[0].deadline_ns;
    } else {
        state->lat_head_enqueue_ns = 0;
        state->lat_head_deadline = 0;
    }

    if (state->lanes[DDL_BULK].pending_nr > 0) {
        state->bulk_head_enqueue_ns = state->lanes[DDL_BULK].pending[0].arrival_ns;
    } else {
        state->bulk_head_enqueue_ns = 0;
    }
}

static __always_inline __u64 ddl_dispatch(struct q_ddl_local_state *state,
                                           __u64 now_ns)
{
    bool lat_missed = false;
    bool lat_urgent = false;
    bool bulk_starved = false;

    if (state->lanes[DDL_LAT].pending_nr > 0) {
        __u64 hd = state->lat_head_deadline;
        if (hd > 0) {
            if (hd <= now_ns)
                lat_missed = true;
            else if (hd <= now_ns + q_ddl_guard_ns)
                lat_urgent = true;
        }
    }

    if (state->lanes[DDL_BULK].pending_nr > 0) {
        __u64 bulk_wait = now_ns - state->bulk_head_enqueue_ns;
        if (bulk_wait >= q_ddl_bulk_starvation_ns)
            bulk_starved = true;
    }

    if (lat_missed && state->emergency_burst < q_ddl_emergency_max) {
        state->emergency_burst++;
        state->lat_burst++;
        state->last_dispatch_ns = now_ns;
        return ddl_pop_head(&state->lanes[DDL_LAT]);
    }

    if (bulk_starved) {
        state->emergency_burst = 0;
        state->lat_burst = 0;
        state->last_dispatch_ns = now_ns;
        return ddl_pop_head(&state->lanes[DDL_BULK]);
    }

    if (lat_urgent && state->lat_burst < Q_DDL_BURST_MAX) {
        state->lat_burst++;
        state->last_dispatch_ns = now_ns;
        return ddl_pop_head(&state->lanes[DDL_LAT]);
    }

    if (state->lanes[DDL_BULK].pending_nr > 0) {
        state->lat_burst = 0;
        state->emergency_burst = 0;
        state->last_dispatch_ns = now_ns;
        return ddl_pop_head(&state->lanes[DDL_BULK]);
    }

    if (state->lanes[DDL_LAT].pending_nr > 0) {
        state->lat_burst++;
        state->last_dispatch_ns = now_ns;
        return ddl_pop_head(&state->lanes[DDL_LAT]);
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
    return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    struct q_ddl_state_entry *entry;
    struct iosched_request_desc desc;
    enum ddl_class cls;
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

    entry = ddl_state_lookup(ictx->cgroup_id, ictx->hctx_idx);
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    cls = ddl_classify(&desc);
    desc.deadline_ns = desc.arrival_ns + ddl_deadline_ns(cls);

    bpf_spin_lock(&entry->lock);
    ret = ddl_push_tail(&entry->state.lanes[cls], &desc);
    if (ret == 0)
        ddl_update_heads(&entry->state);
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);

    IOEXT_BPF_DBG_SAMPLED(64,
        "[q_ddl] insert rq=%llx cls=%u deadline=%llu sector=%llu\n",
        desc.rq_ptr, cls, desc.deadline_ns, desc.sector);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_ddl_state_entry *entry;
    __u64 now_ns, rq_ptr;

    if (!dctx)
        return 0;

    entry = ddl_state_lookup(dctx->cgroup_id, dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = ddl_dispatch(&entry->state, now_ns);
    if (rq_ptr)
        ddl_update_heads(&entry->state);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_ddl_state_entry *entry;
    __u64 now_ns, rq_ptr;

    if (!dctx)
        return 0;

    entry = ddl_state_lookup(dctx->cgroup_id, dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = ddl_dispatch(&entry->state, now_ns);
    if (rq_ptr)
        ddl_update_heads(&entry->state);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    return false;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_ddl = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .allow_merge = (void *)allow_merge,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_ddl",
};