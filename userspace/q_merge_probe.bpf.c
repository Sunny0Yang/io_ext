/*
 * q_merge_probe.bpf.c - minimal merge-probing queueing policy.
 *
 * Modes (probe_cfg map key 0), mirrored in loader / userspace tools:
 *   0 DENY_ALL          allow_merge=false (negative control)
 *   1 ALLOW_ALL         allow_merge=true, request_merged counts (reachability)
 *   2 ADJACENT_SAME_OP  allow iff same opf and bio_sector==rq_end_sector
 *   3 COUNT_ONLY        record then deny (pure probe, no merge)
 * merge_stats map (key 0) is the L1 primary observation surface.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

/* mode enum mirrored in loader / userspace tools */
#define MERGE_PROBE_DENY_ALL          0
#define MERGE_PROBE_ALLOW_ALL         1
#define MERGE_PROBE_ADJACENT_SAME_OP  2
#define MERGE_PROBE_COUNT_ONLY        3

struct merge_stats {
    __u64 allow_merge_calls;
    __u64 allow_merge_true;
    __u64 allow_merge_false;
    __u64 former_request_calls;
    __u64 former_request_hit;
    __u64 request_merged_calls;
    __u64 request_merge_calls;
    __u64 bio_merge_calls;
    __u64 bio_merge_true;
    __u64 insert_accept;
    __u64 insert_reject;
};

struct probe_cfg {
    __u32 mode;
    __u32 reserved;
};

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 4096);
    __type(value, __u64);
} io_queue SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct merge_stats);
} merge_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct probe_cfg);
} probe_cfg SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct merge_stats *stats_lookup(void)
{
    __u32 key = 0;
    return bpf_map_lookup_elem(&merge_stats, &key);
}

static __always_inline __u32 probe_mode(void)
{
    __u32 key = 0;
    struct probe_cfg *cfg = bpf_map_lookup_elem(&probe_cfg, &key);
    return cfg ? cfg->mode : MERGE_PROBE_DENY_ALL;
}

static __always_inline void stats_inc(__u64 *field)
{
    if (field)
        __sync_fetch_and_add(field, 1);
}

static __always_inline __u64 rq_end_sector_val(__u64 sector, __u64 nr_sectors)
{
    return sector + nr_sectors;
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
    __u64 rq_ptr;
    struct merge_stats *st;

    if (!ictx || !ictx->rq)
        return -1;

    rq_ptr = (__u64)ictx->rq;
    if (rq_ptr < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);

    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        st = stats_lookup();
        stats_inc(&st->insert_reject);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    if (bpf_map_push_elem(&io_queue, &rq_ptr, 0) != 0) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        st = stats_lookup();
        stats_inc(&st->insert_reject);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);

    st = stats_lookup();
    stats_inc(&st->insert_accept);
    return IOSCHED_INSERT_ACCEPT;
}

static __always_inline bool probe_allow_merge(struct iosched_allow_merge_ctx *mctx)
{
    __u32 mode = probe_mode();
    struct merge_stats *st;
    bool allow = false;

    st = stats_lookup();
    stats_inc(&st->allow_merge_calls);

    switch (mode) {
    case MERGE_PROBE_DENY_ALL:
        allow = false;
        break;
    case MERGE_PROBE_ALLOW_ALL:
        allow = true;
        break;
    case MERGE_PROBE_ADJACENT_SAME_OP:
        if (!mctx || !mctx->rq || !mctx->bio) {
            allow = false;
            break;
        }
        allow = mctx->bio_sector ==
               rq_end_sector_val(mctx->rq_sector, mctx->rq_nr_sectors);
        break;
    case MERGE_PROBE_COUNT_ONLY:
        allow = false;
        break;
    default:
        allow = false;
        break;
    }

    if (allow)
        stats_inc(&st->allow_merge_true);
    else
        stats_inc(&st->allow_merge_false);

    return allow;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    return probe_allow_merge(mctx);
}

SEC("struct_ops/bio_merge")
bool BPF_PROG(bio_merge, struct iosched_bio_merge_ctx *bctx)
{
    struct merge_stats *st;
    __u32 mode;

    st = stats_lookup();
    stats_inc(&st->bio_merge_calls);

    mode = probe_mode();
    if (mode == MERGE_PROBE_ALLOW_ALL || mode == MERGE_PROBE_ADJACENT_SAME_OP) {
        stats_inc(&st->bio_merge_true);
        return true;
    }
    return false;
}

SEC("struct_ops/former_request")
__u64 BPF_PROG(former_request, struct iosched_rq_ctx *rctx)
{
    struct merge_stats *st;
    __u64 rq_ptr = 0;
    __u64 peeked = 0;

    st = stats_lookup();
    stats_inc(&st->former_request_calls);

    if (!rctx || !rctx->rq)
        return 0;

    if (probe_mode() == MERGE_PROBE_ADJACENT_SAME_OP) {
        if (bpf_map_peek_elem(&io_queue, &peeked) == 0) {
            rq_ptr = peeked;
            if (rq_ptr)
                stats_inc(&st->former_request_hit);
        }
    }
    return rq_ptr;
}

SEC("struct_ops/request_merge")
__u64 BPF_PROG(request_merge, struct iosched_request_merge_ctx *mctx)
{
    struct merge_stats *st;

    st = stats_lookup();
    stats_inc(&st->request_merge_calls);
    return 0;
}

SEC("struct_ops/request_merged")
void BPF_PROG(request_merged, struct iosched_request_merged_ctx *mctx)
{
    struct merge_stats *st;

    st = stats_lookup();
    stats_inc(&st->request_merged_calls);
}

IOEXT_DEFINE_SIMPLE_CGROUP_DISPATCH(io_queue, policy_meta)
IOEXT_DEFINE_SIMPLE_EVACUATE(io_queue, policy_meta)

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_merge_probe = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .allow_merge = (void *)allow_merge,
    .bio_merge = (void *)bio_merge,
    .former_request = (void *)former_request,
    .request_merge = (void *)request_merge,
    .request_merged = (void *)request_merged,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_merge_probe",
};