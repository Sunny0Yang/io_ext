// q_inject_null.bpf.c — S-CHAOS F-NULL fault policy ("null-pointer / abnormal return").
//
// SAFETY-CHAOS experiment (experiments/safety_chaos_kvmhost/EXPERIMENT_DESIGN.md §4.1).
// The policy NEVER accepts a request into its (absent) queue: every insert either
// defers to the parent or is rejected, so no request can ever be stranded by the
// fault modes below.  It only exercises the kernel's null/abnormal-return guards:
//   mode 0: insert_request -> IOSCHED_INSERT_DEFER_TO_PARENT   (parent/root fallback)
//   mode 1: insert_request -> -1                               (fallback_enqueue, policy_rejected)
//   mode 2: cgroup_dispatch -> 0  (NULL dispatch result; guarded at iosched_ext.c:3164)
//   mode 3: evacuate       -> 0  (NULL evacuate result; guarded at iosched_ext.c:1551/3447)
// NOTE: the kernel rejects attach when ops->insert_request or ops->evacuate is a
// NULL pointer (iosched_ext.c:1850) — that attach-time validation itself is one of
// the static safety properties this experiment reports; therefore a NULL *ops
// member can only be simulated at BPF-return-value level (modes 2/3).
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

IOEXT_POLICY_META_MAP(policy_meta);

/* control word, written by loader_q_inject_null before attach */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} inject_ctl SEC(".maps");

static __always_inline __u64 fault_mode(void)
{
    __u32 key = 0;
    __u64 *v;

    v = bpf_map_lookup_elem(&inject_ctl, &key);
    return v ? *v : 0;
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
    __u64 mode;

    if (!ictx || !ictx->rq)
        return -1;
    if ((__u64)ictx->rq < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    mode = fault_mode();

    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    /* INVARIANT: never ACCEPT — this policy owns no queue, no request is ever
     * held, so the fault returns below can never strand an in-flight request. */
    if (mode == 0) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    return -1;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    /* mode 2: always hand back "NULL" (no request) even if the kernel asks;
     * other modes: queue is guaranteed empty, plain pop returns 0 anyway. */
    if (fault_mode() == 2)
        return 0;
    return 0;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    /* mode 3: always return "NULL" during teardown; queue is empty by the
     * insert invariant, so the NULL evacuate can never strand a request. */
    if (fault_mode() == 3)
        return 0;
    return 0;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_inject_null = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_inject_null",
};
