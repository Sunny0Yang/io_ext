#ifndef IOSCHED_BPF_COMMON_H
#define IOSCHED_BPF_COMMON_H

#include "ioext_policy_shared.h"

extern __u64 bpf_scx_io_rq_total_latency_ns(struct request *rq, __u64 now) __ksym;
extern __u64 bpf_scx_io_rq_io_latency_ns(struct request *rq, __u64 now) __ksym;
extern void bpf_scx_io_set_queue_async_depth(struct request_queue *q, __u32 depth) __ksym;
extern bool bpf_scx_io_rescue_active(__u64 queue_ptr) __ksym;

#define IOSCHED_REQ_OP_MASK 0xffU
#define IOSCHED_REQ_SYNC_BIT (1ULL << __REQ_SYNC)
#define IOSCHED_STATE_MAP_MAX_QUEUES 128
#define IOSCHED_BPF_EEXIST 17
#define IOSCHED_DEFAULT_ARB_PRIO 7U
#define IOSCHED_DEFAULT_ARB_WEIGHT 100U
#define IOSCHED_BYPASS_MODE_NONE   0ULL
#define IOSCHED_BYPASS_MODE_DIRECT 1ULL

#define IOSCHED_INSERT_ACCEPT 0
#define IOSCHED_INSERT_DEFER_TO_PARENT 1

#define IOSCHED_PACK_REQUEST_MERGE(type, rq_ptr) \
    ((((unsigned long long)(rq_ptr)) & ~0x7ULL) | ((type) & 0x7ULL))
#define IOSCHED_REQUEST_MERGE_TYPE(val) ((int)((val) & 0x7ULL))
#define IOSCHED_REQUEST_MERGE_RQ_PTR(val) ((val) & ~0x7ULL)

#define IOEXT_POLICY_META_MAP(map_name) \
struct { \
    __uint(type, BPF_MAP_TYPE_HASH); \
    __uint(max_entries, 64); \
    __type(key, __u64); \
    __type(value, struct iosched_policy_meta); \
} map_name SEC(".maps")

static __always_inline void iosched_atomic_dec_u32(__u32 *p)
{
    __sync_fetch_and_add(p, (__u32)-1);
}

static __always_inline void iosched_atomic_dec_u64(__u64 *p)
{
    __sync_fetch_and_add(p, (__u64)-1);
}

static __always_inline void iosched_policy_meta_note_insert(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return;

    __sync_fetch_and_add(&meta->queue_depth, 1);
}

static __always_inline void iosched_policy_meta_cancel_insert(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return;

    if (meta->queue_depth > 0)
        iosched_atomic_dec_u64(&meta->queue_depth);
}

static __always_inline void iosched_policy_meta_begin_insert(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return;

    __sync_fetch_and_add(&meta->active_inserts, 1);
}

static __always_inline void iosched_policy_meta_end_insert(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return;

    if (meta->active_inserts > 0)
        iosched_atomic_dec_u64(&meta->active_inserts);
}

static __always_inline bool iosched_policy_meta_is_draining(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return false;

    return !!(meta->flags & IOSCHED_POLICY_META_F_DRAINING);
}

static __always_inline void iosched_policy_meta_note_dispatch(void *map, __u64 cgroup_id)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(map, &cgroup_id);
    if (!meta)
        return;

    if (meta->queue_depth > 0)
        iosched_atomic_dec_u64(&meta->queue_depth);
    __sync_fetch_and_add(&meta->dispatches, 1);
}

#define IOEXT_DEFINE_SIMPLE_CGROUP_DISPATCH(map_name, meta_map_name) \
SEC("struct_ops/cgroup_dispatch") \
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx) \
{ \
    __u64 rq_ptr = 0; \
    if (!dctx) \
        return 0; \
    if (bpf_map_pop_elem(&map_name, &rq_ptr) != 0 || rq_ptr == 0) \
        return 0; \
    iosched_policy_meta_note_dispatch(&meta_map_name, dctx->cgroup_id); \
    return rq_ptr; \
}

#define IOEXT_DEFINE_SIMPLE_EVACUATE(map_name, meta_map_name) \
SEC("struct_ops/evacuate") \
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx) \
{ \
    __u64 rq_ptr = 0; \
    if (!dctx) \
        return 0; \
    if (bpf_map_pop_elem(&map_name, &rq_ptr) != 0 || rq_ptr == 0) \
        return 0; \
    iosched_policy_meta_note_dispatch(&meta_map_name, dctx->cgroup_id); \
    return rq_ptr; \
}

struct iosched_kyber_state_key {
    __u64 q_ptr;
    __u64 hctx_idx;
};

static __always_inline __u32 iosched_rq_flags_from_opf(__u64 opf)
{
    __u32 flags = 0;
    __u32 op = opf & IOSCHED_REQ_OP_MASK;

    if (opf & IOSCHED_REQ_SYNC_BIT)
        flags |= IOSCHED_REQF_SYNC;

    switch (op) {
    case REQ_OP_WRITE:
    case REQ_OP_WRITE_ZEROES:
    case REQ_OP_ZONE_APPEND:
        flags |= IOSCHED_REQF_WRITE;
        break;
    case REQ_OP_DISCARD:
    case REQ_OP_SECURE_ERASE:
        flags |= IOSCHED_REQF_DISCARD;
        break;
    case REQ_OP_READ:
        break;
    default:
        flags |= IOSCHED_REQF_OTHER;
        break;
    }

    return flags;
}

static __always_inline __u32 iosched_kyber_domain_from_opf(__u64 opf)
{
    __u32 op = opf & IOSCHED_REQ_OP_MASK;

    switch (op) {
    case REQ_OP_READ:
        return IOSCHED_KYBER_DOMAIN_READ;
    case REQ_OP_WRITE:
    case REQ_OP_WRITE_ZEROES:
    case REQ_OP_ZONE_APPEND:
        return IOSCHED_KYBER_DOMAIN_WRITE;
    case REQ_OP_DISCARD:
    case REQ_OP_SECURE_ERASE:
        return IOSCHED_KYBER_DOMAIN_DISCARD;
    default:
        return IOSCHED_KYBER_DOMAIN_OTHER;
    }
}

static __always_inline struct iosched_request_desc iosched_build_desc(struct iosched_insert_ctx *ctx)
{
    struct iosched_request_desc desc = {};

    desc.rq_ptr = (__u64)ctx->rq;
    desc.issuer_id = ctx->icq_ptr;
    desc.insert_flags = ctx->insert_flags;
    desc.sector = ctx->sector;
    desc.arrival_ns = bpf_ktime_get_ns();
    desc.cgroup_id = ctx->cgroup_id;
    desc.original_cgroup_id = ctx->original_cgroup_id;
    desc.hctx_idx = ctx->hctx_idx;
    desc.ioprio = ctx->ioprio;
    desc.opf = ctx->opf;
    desc.flags = iosched_rq_flags_from_opf(ctx->opf);
    desc.domain = iosched_kyber_domain_from_opf(ctx->opf);
    desc.cost = ctx->nr_sectors ? (__u32)ctx->nr_sectors : 1;
    desc.owner_id = desc.original_cgroup_id ? (__u32)desc.original_cgroup_id : 1;
    return desc;
}

static __always_inline struct iosched_kyber_state_key iosched_kyber_state_key_from_insert(struct iosched_insert_ctx *ctx)
{
    struct iosched_kyber_state_key key = {
        .q_ptr = (__u64)ctx->q,
        .hctx_idx = ctx->hctx_idx,
    };

    return key;
}

#endif
