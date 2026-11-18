#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 4096);
    __type(value, __u64);
} io_queue SEC(".maps");

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
    __u64 rq_ptr;

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

    if (bpf_map_push_elem(&io_queue, &rq_ptr, 0) != 0) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                             "[q_fifo] push rq=%llx eff_cg=%llu orig_cg=%llu\n",
                             rq_ptr,
                             ictx->cgroup_id,
                             ictx->original_cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

IOEXT_DEFINE_SIMPLE_CGROUP_DISPATCH(io_queue, policy_meta)
IOEXT_DEFINE_SIMPLE_EVACUATE(io_queue, policy_meta)

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_fifo = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_fifo",
};
