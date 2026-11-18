#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define IOEXT_REQ_OP_MASK ((1ULL << 8) - 1)
#define Q_KYBER_MAX_HCTX 64U

const volatile __u64 kyber_read_target_ns = IOSCHED_KYBER_READ_TARGET_NS;
const volatile __u64 kyber_write_target_ns = IOSCHED_KYBER_WRITE_TARGET_NS;
const volatile __u64 kyber_discard_target_ns = IOSCHED_KYBER_DISCARD_TARGET_NS;
const volatile __u64 kyber_other_target_ns = IOSCHED_KYBER_OTHER_TARGET_NS;

struct q_kyber_state_entry {
    struct bpf_spin_lock lock;
    struct iosched_kyber_state state;
};

struct q_kyber_queue_cfg {
    __u32 async_depth;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_KYBER_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct q_kyber_state_entry);
} state_map SEC(".maps");

#if IOEXT_DEBUG
enum q_kyber_debug_event {
    Q_KYBER_DBG_INSERT_ACCEPT = 0,
    Q_KYBER_DBG_INSERT_REJECT,
    Q_KYBER_DBG_DISPATCH_HIT,
    Q_KYBER_DBG_DISPATCH_EMPTY,
    Q_KYBER_DBG_DISPATCH_BLOCKED,
    Q_KYBER_DBG_DISPATCH_STATE_MISS,
    Q_KYBER_DBG_EVACUATE_HIT,
    Q_KYBER_DBG_EVACUATE_EMPTY,
    Q_KYBER_DBG_EVACUATE_BLOCKED,
    Q_KYBER_DBG_EVACUATE_STATE_MISS,
    Q_KYBER_DBG_COMPLETE,
    Q_KYBER_DBG_FINISH,
    Q_KYBER_DBG_REQUEUE,
    Q_KYBER_DBG_LIMIT_DEPTH,
    Q_KYBER_DBG_DEPTH_UPDATED,
};

struct q_kyber_debug_counters {
    __u64 insert_accept;
    __u64 insert_reject;
    __u64 dispatch_hit;
    __u64 dispatch_empty;
    __u64 dispatch_blocked;
    __u64 dispatch_state_miss;
    __u64 evacuate_hit;
    __u64 evacuate_empty;
    __u64 evacuate_blocked;
    __u64 evacuate_state_miss;
    __u64 complete;
    __u64 finish;
    __u64 requeue;
    __u64 limit_depth;
    __u64 depth_updated;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_kyber_debug_counters);
} q_kyber_debug_map SEC(".maps");

static __always_inline __u64 q_kyber_debug_inc(__u32 event)
{
    __u32 key = 0;
    struct q_kyber_debug_counters *counters;

    counters = bpf_map_lookup_elem(&q_kyber_debug_map, &key);
    if (!counters)
        return 0;

    switch (event) {
    case Q_KYBER_DBG_INSERT_ACCEPT:
        counters->insert_accept++;
        return counters->insert_accept;
    case Q_KYBER_DBG_INSERT_REJECT:
        counters->insert_reject++;
        return counters->insert_reject;
    case Q_KYBER_DBG_DISPATCH_HIT:
        counters->dispatch_hit++;
        return counters->dispatch_hit;
    case Q_KYBER_DBG_DISPATCH_EMPTY:
        counters->dispatch_empty++;
        return counters->dispatch_empty;
    case Q_KYBER_DBG_DISPATCH_BLOCKED:
        counters->dispatch_blocked++;
        return counters->dispatch_blocked;
    case Q_KYBER_DBG_DISPATCH_STATE_MISS:
        counters->dispatch_state_miss++;
        return counters->dispatch_state_miss;
    case Q_KYBER_DBG_EVACUATE_HIT:
        counters->evacuate_hit++;
        return counters->evacuate_hit;
    case Q_KYBER_DBG_EVACUATE_EMPTY:
        counters->evacuate_empty++;
        return counters->evacuate_empty;
    case Q_KYBER_DBG_EVACUATE_BLOCKED:
        counters->evacuate_blocked++;
        return counters->evacuate_blocked;
    case Q_KYBER_DBG_EVACUATE_STATE_MISS:
        counters->evacuate_state_miss++;
        return counters->evacuate_state_miss;
    case Q_KYBER_DBG_COMPLETE:
        counters->complete++;
        return counters->complete;
    case Q_KYBER_DBG_FINISH:
        counters->finish++;
        return counters->finish;
    case Q_KYBER_DBG_REQUEUE:
        counters->requeue++;
        return counters->requeue;
    case Q_KYBER_DBG_LIMIT_DEPTH:
        counters->limit_depth++;
        return counters->limit_depth;
    case Q_KYBER_DBG_DEPTH_UPDATED:
        counters->depth_updated++;
        return counters->depth_updated;
    default:
        return 0;
    }
}

static __always_inline __u32 q_kyber_debug_pending_total(struct iosched_kyber_state *state)
{
    return state->urgent_nr[0] + state->urgent_nr[1] +
           state->urgent_nr[2] + state->urgent_nr[3] +
           state->domain_nr[0] + state->domain_nr[1] +
           state->domain_nr[2] + state->domain_nr[3];
}
#endif

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_kyber_queue_cfg);
} cfg_map SEC(".maps");

struct kyber_merge_cfg {
    __u32 enable;
    __u32 reserved;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct kyber_merge_cfg);
} merge_cfg_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_kyber_state_entry *state_lookup(__u64 hctx_idx)
{
    __u32 key;

    if (hctx_idx >= Q_KYBER_MAX_HCTX)
        return 0;

    key = hctx_idx;
    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline struct q_kyber_queue_cfg *cfg_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&cfg_map, &key);
}

static __always_inline void kyber_configure_entry(struct q_kyber_state_entry *entry)
{
    iosched_kyber_set_targets(&entry->state,
                              kyber_read_target_ns,
                              kyber_write_target_ns,
                              kyber_discard_target_ns,
                              kyber_other_target_ns);
}

static __always_inline void kyber_refresh_entry(struct q_kyber_state_entry *entry,
                                                __u32 async_depth)
{
    iosched_kyber_depth_updated(&entry->state, async_depth);
}

static __always_inline __u32 kyber_limit_depth_switch(struct iosched_kyber_state *state,
                                                      __u32 domain,
                                                      __u32 async_depth)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        return iosched_kyber_limit_depth(state, IOSCHED_KYBER_DOMAIN_READ, async_depth);
    case IOSCHED_KYBER_DOMAIN_WRITE:
        return iosched_kyber_limit_depth(state, IOSCHED_KYBER_DOMAIN_WRITE, async_depth);
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        return iosched_kyber_limit_depth(state, IOSCHED_KYBER_DOMAIN_DISCARD, async_depth);
    case IOSCHED_KYBER_DOMAIN_OTHER:
        return iosched_kyber_limit_depth(state, IOSCHED_KYBER_DOMAIN_OTHER, async_depth);
    default:
        return 0;
    }
}

static __always_inline void kyber_prepare_switch(struct iosched_kyber_state *state,
                                                 __u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        iosched_kyber_prepare(state, IOSCHED_KYBER_DOMAIN_READ);
        break;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        iosched_kyber_prepare(state, IOSCHED_KYBER_DOMAIN_WRITE);
        break;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        iosched_kyber_prepare(state, IOSCHED_KYBER_DOMAIN_DISCARD);
        break;
    case IOSCHED_KYBER_DOMAIN_OTHER:
        iosched_kyber_prepare(state, IOSCHED_KYBER_DOMAIN_OTHER);
        break;
    }
}

static __always_inline void kyber_complete_switch(struct iosched_kyber_state *state,
                                                  __u32 domain,
                                                  __u32 async_depth,
                                                  __u64 now_ns,
                                                  __u64 total_lat_ns,
                                                  __u64 io_lat_ns)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        iosched_kyber_complete_feedback(state, IOSCHED_KYBER_DOMAIN_READ,
                                        async_depth, now_ns,
                                        total_lat_ns, io_lat_ns);
        break;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        iosched_kyber_complete_feedback(state, IOSCHED_KYBER_DOMAIN_WRITE,
                                        async_depth, now_ns,
                                        total_lat_ns, io_lat_ns);
        break;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        iosched_kyber_complete_feedback(state, IOSCHED_KYBER_DOMAIN_DISCARD,
                                        async_depth, now_ns,
                                        total_lat_ns, io_lat_ns);
        break;
    case IOSCHED_KYBER_DOMAIN_OTHER:
        iosched_kyber_complete_feedback(state, IOSCHED_KYBER_DOMAIN_OTHER,
                                        async_depth, now_ns,
                                        total_lat_ns, io_lat_ns);
        break;
    }
}

static __always_inline void kyber_finish_switch(struct iosched_kyber_state *state,
                                                __u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        iosched_kyber_finish(state, IOSCHED_KYBER_DOMAIN_READ);
        break;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        iosched_kyber_finish(state, IOSCHED_KYBER_DOMAIN_WRITE);
        break;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        iosched_kyber_finish(state, IOSCHED_KYBER_DOMAIN_DISCARD);
        break;
    case IOSCHED_KYBER_DOMAIN_OTHER:
        iosched_kyber_finish(state, IOSCHED_KYBER_DOMAIN_OTHER);
        break;
    }
}

static __always_inline void kyber_requeue_switch(struct iosched_kyber_state *state,
                                                 __u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        iosched_kyber_requeue(state, IOSCHED_KYBER_DOMAIN_READ);
        break;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        iosched_kyber_requeue(state, IOSCHED_KYBER_DOMAIN_WRITE);
        break;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        iosched_kyber_requeue(state, IOSCHED_KYBER_DOMAIN_DISCARD);
        break;
    case IOSCHED_KYBER_DOMAIN_OTHER:
        iosched_kyber_requeue(state, IOSCHED_KYBER_DOMAIN_OTHER);
        break;
    }
}

static __always_inline __u64 state_dispatch(__u64 hctx_idx, __u64 cgroup_id)
{
    struct q_kyber_state_entry *entry;
    __u64 rq_ptr;
#if IOEXT_DEBUG
    __u32 pending = 0;
    __u32 inflight0 = 0, inflight1 = 0, inflight2 = 0, inflight3 = 0;
    __u32 limit0 = 0, limit1 = 0, limit2 = 0, limit3 = 0;
#endif

    entry = state_lookup(hctx_idx);
    if (!entry) {
#if IOEXT_DEBUG
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_DISPATCH_STATE_MISS);
        bpf_printk("q_kyber dispatch miss seq=%llu hctx=%llu cgid=%llu", seq, hctx_idx, cgroup_id);
#endif
        return 0;
    }

    bpf_spin_lock(&entry->lock);
    rq_ptr = iosched_kyber_dispatch(&entry->state);
#if IOEXT_DEBUG
    pending = q_kyber_debug_pending_total(&entry->state);
    inflight0 = entry->state.inflight[0];
    inflight1 = entry->state.inflight[1];
    inflight2 = entry->state.inflight[2];
    inflight3 = entry->state.inflight[3];
    limit0 = entry->state.depth_limit[0];
    limit1 = entry->state.depth_limit[1];
    limit2 = entry->state.depth_limit[2];
    limit3 = entry->state.depth_limit[3];
#endif
    bpf_spin_unlock(&entry->lock);
    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
#if IOEXT_DEBUG
    if (rq_ptr) {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_DISPATCH_HIT);
        bpf_printk("q_kyber dispatch hit seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_kyber dispatch rq=%llu cgid=%llu", rq_ptr, cgroup_id);
    } else {
        __u32 event = pending ? Q_KYBER_DBG_DISPATCH_BLOCKED : Q_KYBER_DBG_DISPATCH_EMPTY;
        __u64 seq = q_kyber_debug_inc(event);
        bpf_printk("q_kyber dispatch zero seq=%llu hctx=%llu pending=%u", seq, hctx_idx, pending);
        bpf_printk("q_kyber inflight rw=%u/%u", inflight0, inflight1);
        bpf_printk("q_kyber inflight do=%u/%u", inflight2, inflight3);
        bpf_printk("q_kyber limit rw=%u/%u", limit0, limit1);
        bpf_printk("q_kyber limit do=%u/%u", limit2, limit3);
    }
#endif
    return rq_ptr;
}

static __always_inline __u64 state_evacuate(__u64 hctx_idx, __u64 cgroup_id)
{
    struct q_kyber_state_entry *entry;
    __u64 rq_ptr;
#if IOEXT_DEBUG
    __u32 pending = 0;
    __u32 inflight0 = 0, inflight1 = 0, inflight2 = 0, inflight3 = 0;
#endif

    entry = state_lookup(hctx_idx);
    if (!entry) {
#if IOEXT_DEBUG
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_EVACUATE_STATE_MISS);
        bpf_printk("q_kyber evacuate miss seq=%llu hctx=%llu cgid=%llu", seq, hctx_idx, cgroup_id);
#endif
        return 0;
    }

    bpf_spin_lock(&entry->lock);
    rq_ptr = iosched_kyber_evacuate(&entry->state);
#if IOEXT_DEBUG
    pending = q_kyber_debug_pending_total(&entry->state);
    inflight0 = entry->state.inflight[0];
    inflight1 = entry->state.inflight[1];
    inflight2 = entry->state.inflight[2];
    inflight3 = entry->state.inflight[3];
#endif
    bpf_spin_unlock(&entry->lock);
    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
#if IOEXT_DEBUG
    if (rq_ptr) {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_EVACUATE_HIT);
        bpf_printk("q_kyber evacuate hit seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_kyber evacuate rq=%llu cgid=%llu", rq_ptr, cgroup_id);
    } else {
        __u32 event = pending ? Q_KYBER_DBG_EVACUATE_BLOCKED : Q_KYBER_DBG_EVACUATE_EMPTY;
        __u64 seq = q_kyber_debug_inc(event);
        bpf_printk("q_kyber evacuate zero seq=%llu hctx=%llu pending=%u", seq, hctx_idx, pending);
        bpf_printk("q_kyber evacuate inflight rw=%u/%u", inflight0, inflight1);
        bpf_printk("q_kyber evacuate inflight do=%u/%u", inflight2, inflight3);
    }
#endif
    return rq_ptr;
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

SEC("struct_ops/init_hctx")
int BPF_PROG(init_hctx, struct iosched_hctx_ctx *hctx)
{
    struct q_kyber_state_entry *entry;
    __u32 domain;
    __u32 type;
    __u32 bucket;

    if (!hctx || !hctx->q || !hctx->hctx)
        return -1;

    entry = state_lookup(hctx->hctx_idx);
    if (!entry)
        return -1;

    /* Fresh scheduler instance (native kyber_init_hctx allocates a fresh
     * khd): reset the whole per-hctx state. CLI targets reach the state
     * only through this hook (elevator init path), so the accept rig must
     * re-switch the scheduler after loader attach for them to land. */
    bpf_spin_lock(&entry->lock);
    for (domain = 0; domain < IOSCHED_KYBER_DOMAIN_NR; domain++) {
        entry->state.urgent_nr[domain] = 0;
        entry->state.domain_nr[domain] = 0;
        entry->state.inflight[domain] = 0;
        entry->state.depth_limit[domain] = iosched_kyber_domain_depth_cap(domain);
        entry->state.avg_total_lat_ns[domain] = 0;
        entry->state.avg_io_lat_ns[domain] = 0;
        entry->state.target_lat_ns[domain] = 0;
    }
    for (domain = 0; domain < IOSCHED_KYBER_DOMAIN_NR - 1; domain++) {
        for (type = 0; type < IOSCHED_KYBER_LATENCY_TYPES; type++)
            for (bucket = 0; bucket < IOSCHED_KYBER_LATENCY_BUCKETS; bucket++)
                entry->state.lat_buckets[domain][type][bucket] = 0;
        entry->state.window_timeout_ns[domain] = 0;
        entry->state.domain_p99[domain] = IOSCHED_KYBER_P99_INVALID;
    }
    entry->state.cur_domain = IOSCHED_INVALID_INDEX;
    entry->state.batching = 0;
    entry->state.async_depth = 0;
    entry->state.queue_async_depth = 0;
    entry->state.last_depth_update_ns = 0;
    kyber_configure_entry(entry);
    bpf_spin_unlock(&entry->lock);
    return 0;
}

SEC("struct_ops/exit_hctx")
void BPF_PROG(exit_hctx, struct iosched_hctx_ctx *hctx)
{
    if (!hctx || !hctx->q || !hctx->hctx)
        return;
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    struct q_kyber_state_entry *entry;
    struct iosched_request_desc desc;

    if (!ictx || !ictx->rq)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    entry = state_lookup(ictx->hctx_idx);
    if (!entry) {
#if IOEXT_DEBUG
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_INSERT_REJECT);
        bpf_printk("q_kyber insert miss seq=%llu hctx=%llu cgid=%llu",
                   seq, ictx->hctx_idx, ictx->cgroup_id);
#endif
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    bpf_spin_lock(&entry->lock);
    if (iosched_kyber_insert(&entry->state, &desc)) {
        bpf_spin_unlock(&entry->lock);
#if IOEXT_DEBUG
        {
            __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_INSERT_REJECT);
            bpf_printk("q_kyber insert reject seq=%llu hctx=%llu cgid=%llu",
                       seq, ictx->hctx_idx, ictx->cgroup_id);
        }
#endif
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }
    bpf_spin_unlock(&entry->lock);

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_INSERT_ACCEPT);
        bpf_printk("q_kyber insert accept seq=%llu hctx=%llu cgid=%llu",
                   seq, ictx->hctx_idx, ictx->cgroup_id);
    }
#endif
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    __u32 key = 0;
    struct kyber_merge_cfg *cfg;

    if (!mctx || !mctx->rq || !mctx->bio)
        return false;

    cfg = bpf_map_lookup_elem(&merge_cfg_map, &key);
    if (cfg && !cfg->enable)
        return false;

    return iosched_kyber_merge_compatible(iosched_kyber_domain_from_opf(mctx->rq->cmd_flags),
                                          iosched_kyber_domain_from_opf(mctx->bio_opf));
}

SEC("struct_ops/limit_depth")
__u64 BPF_PROG(limit_depth, struct iosched_limit_depth_ctx *dctx)
{
    struct q_kyber_state_entry *entry;
    struct q_kyber_queue_cfg *cfg;
    __u32 domain;
    __u32 async_depth;
    __u32 queue_async_depth;
    __u64 depth;

    if (!dctx || !dctx->data)
        return 0;

    entry = state_lookup(dctx->hctx_idx);
    cfg = cfg_lookup();
    if (!entry)
        return 0;

    domain = iosched_kyber_domain_from_opf(dctx->opf);
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return 0;

    bpf_spin_lock(&entry->lock);
    queue_async_depth = cfg && cfg->async_depth ? cfg->async_depth : dctx->async_depth;
    kyber_refresh_entry(entry, queue_async_depth);
    depth = kyber_limit_depth_switch(&entry->state,
                                     domain,
                                     entry->state.queue_async_depth);
    async_depth = entry->state.async_depth;
    bpf_spin_unlock(&entry->lock);

#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_LIMIT_DEPTH);
        bpf_printk("q_kyber limit_depth seq=%llu hctx=%llu dom=%u",
                   seq, dctx->hctx_idx, domain);
        bpf_printk("q_kyber limit_depth depth=%llu async=%u",
                   depth, async_depth);
    }
#endif

    return depth;
}

SEC("struct_ops/depth_updated")
void BPF_PROG(depth_updated, struct iosched_depth_updated_ctx *dctx)
{
    struct q_kyber_queue_cfg *cfg;

    if (!dctx || !dctx->q)
        return;

    cfg = cfg_lookup();
    if (!cfg)
        return;

    cfg->async_depth = dctx->async_depth;

#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_DEPTH_UPDATED);
        bpf_printk("q_kyber depth_updated seq=%llu async=%llu",
                   seq, dctx->async_depth);
    }
#endif
}

SEC("struct_ops/prepare_request")
void BPF_PROG(prepare_request, struct iosched_rq_ctx *rctx)
{
    struct q_kyber_state_entry *entry;
    __u32 domain;

    if (!rctx || !rctx->rq)
        return;

    entry = state_lookup(rctx->hctx_idx);
    if (!entry)
        return;

    domain = iosched_kyber_domain_from_opf(rctx->opf);
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return;
    bpf_spin_lock(&entry->lock);
    kyber_prepare_switch(&entry->state, domain);
    bpf_spin_unlock(&entry->lock);
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *rctx)
{
    struct q_kyber_state_entry *entry;
    struct q_kyber_queue_cfg *cfg;
    __u32 domain;
    __u32 queue_async_depth;

    if (!rctx || !rctx->rq)
        return;

    entry = state_lookup(rctx->hctx_idx);
    cfg = cfg_lookup();
    if (!entry)
        return;

    domain = iosched_kyber_domain_from_opf(rctx->opf);
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return;

    bpf_spin_lock(&entry->lock);
    queue_async_depth = cfg && cfg->async_depth ?
        cfg->async_depth : entry->state.queue_async_depth;
    kyber_complete_switch(&entry->state,
                          domain,
                          queue_async_depth,
                          rctx->now,
                          rctx->total_latency_ns,
                          rctx->io_latency_ns);
    bpf_spin_unlock(&entry->lock);

#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_COMPLETE);
        bpf_printk("q_kyber complete seq=%llu hctx=%llu dom=%u",
                   seq, rctx->hctx_idx, domain);
        bpf_printk("q_kyber complete io_lat=%llu total_lat=%llu",
                   rctx->io_latency_ns, rctx->total_latency_ns);
    }
#endif
}

SEC("struct_ops/finish_request")
void BPF_PROG(finish_request, struct iosched_rq_ctx *rctx)
{
    struct q_kyber_state_entry *entry;
    __u32 domain;
#if IOEXT_DEBUG
    __u32 inflight0 = 0, inflight1 = 0, inflight2 = 0, inflight3 = 0;
#endif

    if (!rctx || !rctx->rq)
        return;

    entry = state_lookup(rctx->hctx_idx);
    if (!entry)
        return;

    domain = iosched_kyber_domain_from_opf(rctx->opf);
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return;

    bpf_spin_lock(&entry->lock);
    kyber_finish_switch(&entry->state, domain);
#if IOEXT_DEBUG
    inflight0 = entry->state.inflight[0];
    inflight1 = entry->state.inflight[1];
    inflight2 = entry->state.inflight[2];
    inflight3 = entry->state.inflight[3];
#endif
    bpf_spin_unlock(&entry->lock);
#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_FINISH);
        bpf_printk("q_kyber finish seq=%llu hctx=%llu domain=%u",
                   seq, rctx->hctx_idx, domain);
        bpf_printk("q_kyber finish inflight rw=%u/%u", inflight0, inflight1);
        bpf_printk("q_kyber finish inflight do=%u/%u", inflight2, inflight3);
    }
#endif
}

SEC("struct_ops/requeue_request")
void BPF_PROG(requeue_request, struct iosched_rq_ctx *rctx)
{
    struct q_kyber_state_entry *entry;
    __u32 domain;
#if IOEXT_DEBUG
    __u32 inflight0 = 0, inflight1 = 0, inflight2 = 0, inflight3 = 0;
#endif

    if (!rctx || !rctx->rq)
        return;

    entry = state_lookup(rctx->hctx_idx);
    if (!entry)
        return;

    domain = iosched_kyber_domain_from_opf(rctx->opf);
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return;
    bpf_spin_lock(&entry->lock);
    kyber_requeue_switch(&entry->state, domain);
#if IOEXT_DEBUG
    inflight0 = entry->state.inflight[0];
    inflight1 = entry->state.inflight[1];
    inflight2 = entry->state.inflight[2];
    inflight3 = entry->state.inflight[3];
#endif
    bpf_spin_unlock(&entry->lock);
#if IOEXT_DEBUG
    {
        __u64 seq = q_kyber_debug_inc(Q_KYBER_DBG_REQUEUE);
        bpf_printk("q_kyber requeue seq=%llu hctx=%llu domain=%u",
                   seq, rctx->hctx_idx, domain);
        bpf_printk("q_kyber requeue inflight rw=%u/%u", inflight0, inflight1);
        bpf_printk("q_kyber requeue inflight do=%u/%u", inflight2, inflight3);
    }
#endif
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_dispatch(dctx->hctx_idx, dctx->cgroup_id);
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_evacuate(dctx->hctx_idx, dctx->cgroup_id);
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_kyber = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .init_hctx = (void *)init_hctx,
    .exit_hctx = (void *)exit_hctx,
    .insert_request = (void *)insert_request,
    .allow_merge = (void *)allow_merge,
    .limit_depth = (void *)limit_depth,
    .depth_updated = (void *)depth_updated,
    .prepare_request = (void *)prepare_request,
    .finish_request = (void *)finish_request,
    .completed_request = (void *)completed_request,
    .requeue_request = (void *)requeue_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_kyber",
};
