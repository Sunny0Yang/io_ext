/* arb_noop.bpf.c - ioext3 minimal no-op arbiter (D1' framework ladder)
 *
 * Ladder position: q_fifo < q_fifo+arb_noop.  All SEVEN ioext3 arbiter
 * hooks are implemented as immediate no-ops and the arbiter registers
 * with IOSCHED_ARB_ALLOW_FALLBACK: pick_cgroup returns 0 (no tenant
 * selection), hold_dispatch returns false, and the request-lifecycle
 * notification hooks do nothing.  Every dispatch of a BPF-managed
 * request therefore still pays one BPF arbiter entry, then takes the
 * kernel's default active-list path.
 *
 * Maps: the loader contract (iosched_loader_setup_arbiter_maps) requires
 * the skeleton to declare active_cgroup_ids + policy_meta; both are
 * REUSED from the pinned shared paths (no per-attach allocation) and no
 * hook ever reads or writes them, so this arbiter carries no private
 * state.  The pre-ioext3 stub additionally never implemented the
 * inserted/dispatched/dispatch_failed/requeue_refund hooks ioext3 added.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#ifndef IOSCHED_ARB_ALLOW_FALLBACK
#define IOSCHED_ARB_ALLOW_FALLBACK 0ULL
#endif

/* Loader-contract shared maps: pinned once globally and reused here;
 * never accessed by any hook below. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, __u64);
} active_cgroup_ids SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 64);
    __type(key, __u64);
    __type(value, struct iosched_policy_meta);
} policy_meta SEC(".maps");

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(arb_noop_pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
	/* 0 = no selection; with ALLOW_FALLBACK the kernel default path
	 * picks the next request. */
	return 0;
}

SEC("struct_ops/hold_dispatch")
bool BPF_PROG(arb_noop_hold_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
	return false;
}

SEC("struct_ops/completed_request")
void BPF_PROG(arb_noop_completed_request,
	      struct iosched_completed_request_ctx *cctx)
{
}

SEC("struct_ops/inserted_request")
void BPF_PROG(arb_noop_inserted_request, struct iosched_inserted_ctx *ictx)
{
}

SEC("struct_ops/dispatched_request")
void BPF_PROG(arb_noop_dispatched_request, struct iosched_dispatched_ctx *dctx)
{
}

SEC("struct_ops/dispatch_failed")
void BPF_PROG(arb_noop_dispatch_failed, struct iosched_dispatch_failed_ctx *fctx)
{
}

SEC("struct_ops/requeue_refund")
void BPF_PROG(arb_noop_requeue_refund, struct iosched_dispatched_ctx *rctx)
{
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops noop_arbiter = {
	.pick_cgroup = (void *)arb_noop_pick_cgroup,
	.hold_dispatch = (void *)arb_noop_hold_dispatch,
	.completed_request = (void *)arb_noop_completed_request,
	.inserted_request = (void *)arb_noop_inserted_request,
	.dispatched_request = (void *)arb_noop_dispatched_request,
	.dispatch_failed = (void *)arb_noop_dispatch_failed,
	.requeue_refund = (void *)arb_noop_requeue_refund,
	.arb_mode = IOSCHED_ARB_ALLOW_FALLBACK,
	.name = "arb_noop",
};
