// q_rfirst: hard-coded read-first per-tenant policy (ycsb6_coloc_kvmhost)
// Workload rationale: co-located YCSB A/F tenants are ~50/50 read/update on a
// slow SSD; read dispatches pay the latency tail, write completions gate the
// app via WAL fsync batching (inherently batch-friendly).  Policy: two queues
// (reads, writes); dispatch reads as long as they keep coming, but after
// RFIRST_MAX_READ_BURST consecutive reads hand one write back so the write
// queue can never lock out; if no reads are queued, writes dispatch freely.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define RFIRST_MAX_READ_BURST 8

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 4096);
    __type(value, __u64);
} io_read_q SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 4096);
    __type(value, __u64);
} io_write_q SEC(".maps");

// consecutive read dispatches since the last write dispatch (single element)
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} burst_cnt SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline __u64 burst_get(void)
{
    __u32 zero = 0;
    __u64 *v = bpf_map_lookup_elem(&burst_cnt, &zero);
    return v ? *v : 0;
}

static __always_inline void burst_set(__u64 v)
{
    __u32 zero = 0;
    __u64 x = v;
    bpf_map_update_elem(&burst_cnt, &zero, &x, BPF_ANY);
}

static __always_inline int req_is_write(struct iosched_insert_ctx *ictx)
{
    struct iosched_request_desc desc = iosched_build_desc(ictx);
    return (desc.flags & IOSCHED_REQF_WRITE) != 0;
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
    int write;

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

    write = req_is_write(ictx);
    if (bpf_map_push_elem(write ? &io_write_q : &io_read_q, &rq_ptr, 0) != 0) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                          "[q_rfirst] push %s rq=%llx eff_cg=%llu\n",
                          write ? "write" : "read",
                          rq_ptr,
                          ictx->cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq = 0;

    if (!dctx)
        return 0;

    // hand one write back if reads have monopolized the burst window
    if (burst_get() >= RFIRST_MAX_READ_BURST) {
        if (bpf_map_pop_elem(&io_write_q, &rq) == 0 && rq) {
            burst_set(0);
            iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
            return rq;
        }
        burst_set(0);
    }

    if (bpf_map_pop_elem(&io_read_q, &rq) == 0 && rq) {
        burst_set(burst_get() + 1);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq;
    }

    if (bpf_map_pop_elem(&io_write_q, &rq) == 0 && rq) {
        burst_set(0);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return rq;
    }
    return 0;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq = 0;

    if (!dctx)
        return 0;
    if (bpf_map_pop_elem(&io_read_q, &rq) != 0 || !rq)
        bpf_map_pop_elem(&io_write_q, &rq);
    if (!rq)
        return 0;
    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_rfirst = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_rfirst",
};
