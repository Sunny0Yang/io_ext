#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define QPRIO_SMALL_SECTORS 8

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 1024);
    __type(value, __u64);
} read_queue SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 1024);
    __type(value, __u64);
} write_queue SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

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
    __u32 op;
    __u64 rq_ptr;
    int is_high_prio;
    int ret;

    if (!ictx || !ictx->rq)
        return -1;

    rq_ptr = (__u64)ictx->rq;
    if (rq_ptr < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);

    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    op = (__u32)(ictx->opf & IOSCHED_REQ_OP_MASK);
    is_high_prio = (op == REQ_OP_READ) || (ictx->nr_sectors <= QPRIO_SMALL_SECTORS);
    if (is_high_prio)
        ret = bpf_map_push_elem(&read_queue, &rq_ptr, 0);
    else
        ret = bpf_map_push_elem(&write_queue, &rq_ptr, 0);

    if (ret != 0) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                             "[q_prio] push rq=%llx eff_cg=%llu orig_cg=%llu lane=%u\n",
                             rq_ptr,
                             ictx->cgroup_id,
                             ictx->original_cgroup_id,
                             is_high_prio ? 0 : 1);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq_ptr = 0;

    if (!dctx)
        return 0;

    if (bpf_map_pop_elem(&read_queue, &rq_ptr) == 0 && rq_ptr != 0) {
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq_ptr;
    }

    if (bpf_map_pop_elem(&write_queue, &rq_ptr) == 0 && rq_ptr != 0) {
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq_ptr;
    }

    return 0;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq_ptr = 0;

    if (!dctx)
        return 0;

    if (bpf_map_pop_elem(&read_queue, &rq_ptr) == 0 && rq_ptr != 0) {
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq_ptr;
    }

    if (bpf_map_pop_elem(&write_queue, &rq_ptr) == 0 && rq_ptr != 0) {
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq_ptr;
    }

    return 0;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_prio = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_prio",
};
