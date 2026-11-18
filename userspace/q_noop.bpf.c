/* q_noop.bpf.c - ioext3 minimal no-op queue policy (D1' framework ladder)
 *
 * Ladder position: scx_io-bare < q_noop < q_fifo.  The framework is
 * attached and the BPF insert hook IS invoked (BYPASS_MODE_NONE keeps the
 * BPF insert path active, unlike the DIRECT bypass of q_none), but the
 * hook immediately declines every request: no queue, no map access, no
 * bookkeeping.  The kernel then serves the request through its own
 * fallback elevator.  This isolates "framework attached + one BPF insert
 * crossing" from queueing, policy_meta bookkeeping and arbiter work.
 *
 * Deliberately NOT derived from the pre-ioext3 stub: that version probed
 * policy_meta for draining on every insert (one hash lookup per IO) and
 * could therefore no longer measure a pure entry cost.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

/* Contract map: declared so the loader's setup_policy_maps can reuse the
 * globally pinned /sys/fs/bpf/ioext_policy_meta (no per-attach allocation),
 * but the hooks below never read or write it (zero map access by design,
 * D1' E3 ladder). */
IOEXT_POLICY_META_MAP(policy_meta);

SEC("struct_ops/init_queue")
int BPF_PROG(q_noop_init_queue, struct iosched_queue_ctx *qctx)
{
	return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/exit_queue")
int BPF_PROG(q_noop_exit_queue, struct iosched_queue_ctx *qctx)
{
	return qctx && qctx->q ? 0 : -1;
}

SEC("struct_ops/insert_request")
int BPF_PROG(q_noop_insert_request, struct iosched_insert_ctx *ictx)
{
	/* Decline unconditionally: the request falls back to the kernel
	 * elevator.  No map lookups, no counters, no state. */
	return -1;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(q_noop_cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
	/* Nothing is ever accepted, so nothing is ever dispatched here. */
	return 0;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(q_noop_evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
	return 0;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_noop = {
	.init_queue = (void *)q_noop_init_queue,
	.exit_queue = (void *)q_noop_exit_queue,
	.insert_request = (void *)q_noop_insert_request,
	.cgroup_dispatch = (void *)q_noop_cgroup_dispatch,
	.evacuate = (void *)q_noop_evacuate,
	.bypass_mode = IOSCHED_BYPASS_MODE_NONE,
	.name = "q_noop",
};
