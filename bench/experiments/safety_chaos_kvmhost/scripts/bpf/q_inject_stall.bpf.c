// q_inject_stall.bpf.c — S-CHAOS F-STALL fault policy ("refuses to drain", bounded).
//
// SAFETY-CHAOS experiment (experiments/safety_chaos_kvmhost/EXPERIMENT_DESIGN.md §4.2).
// Behaves like q_fifo while no stall window is armed.  During an armed window
// [stall_until_boot_ns, ...): cgroup_dispatch and evacuate BOTH return 0, i.e. the
// policy keeps accepting requests but refuses to hand any back — the "deadlock
// policy that refuses to drain".  The kernel's teardown workfn probes evacuate
// every 20ms (stall_rounds++) until the window elapses, then drains everything.
//
// Boundedness (experiment validity): the window is armed by the loader at
// SIGTERM time and is hard-capped by the loader (<=15000ms).  Queue capacity is
// 4096; overflow inserts return -1 so the kernel fallback path completes them —
// no request can ever be stranded by the policy itself.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

IOEXT_POLICY_META_MAP(policy_meta);

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, 4096);
    __type(value, __u64);
} io_queue SEC(".maps");

/* control: [0] = stall_until_boot_ns (0 = normal operation) */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} stall_ctl SEC(".maps");

static __always_inline bool stalling(void)
{
    __u32 key = 0;
    __u64 *until;

    until = bpf_map_lookup_elem(&stall_ctl, &key);
    return until && *until && bpf_ktime_get_boot_ns() < *until;
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
        /* queue full while the stall window withholds dispatch: reject, the
         * kernel fallback path completes this request — never lose it here. */
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq_ptr = 0;

    if (!dctx)
        return 0;
    if (stalling())
        return 0;               /* refuse to dispatch while window armed */
    if (bpf_map_pop_elem(&io_queue, &rq_ptr) != 0 || rq_ptr == 0)
        return 0;
    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u64 rq_ptr = 0;

    if (!dctx)
        return 0;
    if (stalling())
        return 0;               /* REFUSE TO DRAIN: kernel probes every 20ms */
    if (bpf_map_pop_elem(&io_queue, &rq_ptr) != 0 || rq_ptr == 0)
        return 0;
    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_inject_stall = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_inject_stall",
};
