/* d1_instr.bpf.c - D1 experiment E6: single-instrumentation decomposition
 *
 * Pure measurement probe for the ioext3 (7.3.1-ioext3) core scheduler:
 * four fentry/fexit programs around the two blk_mq_ops callbacks that
 * constitute the ioext3 fast path,
 *
 *   static void           scx_io_insert_requests(hctx, list, flags)   (~2793)
 *   static struct request *scx_io_dispatch_request(hctx)               (~3008)
 *
 * Both are address-taken (assigned into the ioext3 blk_mq_ops in
 * kernel/iosched_ext.c), therefore guaranteed present in BTF and never
 * inlined, so fentry/fexit attach is safe.  Segments accounted:
 *
 *   insert_span    fentry->fexit of scx_io_insert_requests
 *   dispatch_span  fentry->fexit of scx_io_dispatch_request (any retval)
 *   residence      d0(dispatch entry) - t0(insert entry) of the last
 *                  inserted request, only when dispatch returned non-NULL
 *                  (QD1 solo: that request is the inflight one being
 *                  dispatched, so this is enqueue->dispatch residence)
 *
 * Concurrency model: t0/d0/inflight flags live in a CPU-indexed ARRAY
 * (key = bpf_get_smp_processor_id()); fexit reads the slot of the CPU it
 * runs on.  Known approximation: if the instrumented task migrated
 * between fentry and fexit the pairing is lost and the span is dropped
 * (counted).  Under QD1 solo block paths the callbacks run in the submitter
 * context largely under hctx locks, so migration inside one callback is
 * negligible, and the E6 on/off comparison only looks at differences
 * between identical pairs of runs anyway.  A per-CPU in-flight flag skips
 * recording t0/d0 when an unpaired fentry is already outstanding on that
 * CPU (re-entry), so the outer fexit closes the outer span; the nested
 * fexit may close it early, in which case both halves land in the drop
 * counter rather than corrupting the histogram with a bogus span.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

/* ------------------------------------------------------------------ */
/* Segment ids for the segs map (key = segment, value = d1_seg).       */
/* ------------------------------------------------------------------ */
#define D1_SEG_INSERT_SPAN     0
#define D1_SEG_DISPATCH_SPAN   1
#define D1_SEG_RESIDENCE       2
#define D1_SEG_INSERT_DROP     3	/* unpaired fentry/fexit, >1s span, clock regression */
#define D1_SEG_DISPATCH_SPURIOUS 4	/* dispatch returned NULL (cnt only) */
#define D1_SEG_RESIDENCE_DROP  5	/* no valid insert t0, or d0 < t0 (cnt only) */
#define D1_SEG_NR              6

#define D1_HIST_BUCKETS        40
#define D1_HIST_MAX_BUCKET     39	/* min(39, log2(span_ns)) */
#define D1_SPAN_DROP_NS        1000000000ULL	/* 1 s: interrupted/preempted */

#define D1_MAX_CPUS            512
#define D1_PIN_SEGS            "/sys/fs/bpf/d1_instr_segs"

/* Aggregated (system-wide) statistics for one segment. */
struct d1_seg {
	__u64 cnt;
	__u64 sum_ns;
	__u64 hist[D1_HIST_BUCKETS];
};

/* Per-CPU scratch state, keyed by bpf_get_smp_processor_id(). */
struct d1_pcpu_state {
	__u64 insert_t0_ns;	/* insert fentry timestamp */
	__u64 dispatch_d0_ns;	/* dispatch fentry timestamp */
	__u32 insert_inflight;	/* insert fentry seen, fexit not yet */
	__u32 dispatch_inflight;
	__u32 insert_t0_valid;	/* t0 belongs to a request not yet dispatched */
	__u32 pad;
};

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, D1_SEG_NR);
	__type(key, __u32);
	__type(value, struct d1_seg);
} segs SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, D1_MAX_CPUS);
	__type(key, __u32);
	__type(value, struct d1_pcpu_state);
} pcpu_state SEC(".maps");

/* floor(log2(ns)) clamped to D1_HIST_MAX_BUCKET; unrolled so the
 * verifier sees straight-line code (this libbpf ships no bpf_log2l). */
static __always_inline __u32 d1_hist_bucket(__u64 ns)
{
	__u64 v = ns;
	__u32 b = 0;
	int i;

#pragma clang loop unroll(full)
	for (i = 0; i < D1_HIST_MAX_BUCKET; i++) {
		if (v >= 2) {
			v >>= 1;
			b++;
		}
	}
	if (v >= 2)
		b = D1_HIST_MAX_BUCKET;	/* span >= 2^40 ns */
	return b;
}

static __always_inline void d1_record_span(__u32 id, __u64 span_ns)
{
	struct d1_seg *seg;
	__u32 key = id;
	__u32 b;

	seg = bpf_map_lookup_elem(&segs, &key);
	if (!seg)
		return;
	__sync_fetch_and_add(&seg->cnt, 1);
	__sync_fetch_and_add(&seg->sum_ns, span_ns);
	b = d1_hist_bucket(span_ns);
	__sync_fetch_and_add(&seg->hist[b], 1);
}

/* Count-only segments (drops, spurious dispatches). */
static __always_inline void d1_count(__u32 id)
{
	struct d1_seg *seg;
	__u32 key = id;

	seg = bpf_map_lookup_elem(&segs, &key);
	if (seg)
		__sync_fetch_and_add(&seg->cnt, 1);
}

static __always_inline struct d1_pcpu_state *d1_state_this_cpu(void)
{
	__u32 cpu = bpf_get_smp_processor_id();

	return bpf_map_lookup_elem(&pcpu_state, &cpu);
}

/* ------------------------------------------------------------------ */
/* scx_io_insert_requests: void f(hctx, list, flags)                   */
/* ------------------------------------------------------------------ */

SEC("fentry/scx_io_insert_requests")
int BPF_PROG(d1_insert_entry, struct blk_mq_hw_ctx *hctx,
	     struct list_head *list, blk_insert_t flags)
{
	struct d1_pcpu_state *st = d1_state_this_cpu();

	if (!st)
		return 0;

	if (st->insert_inflight) {
		/* Re-entry with an unpaired fexit outstanding on this CPU:
		 * keep the outer t0 (its fexit will close the outer span);
		 * the nested invocation is not double-counted here. */
		return 0;
	}

	st->insert_t0_ns = bpf_ktime_get_ns();
	st->insert_inflight = 1;
	return 0;
}

SEC("fexit/scx_io_insert_requests")
int BPF_PROG(d1_insert_exit, struct blk_mq_hw_ctx *hctx,
	     struct list_head *list, blk_insert_t flags, int ret)
{
	struct d1_pcpu_state *st = d1_state_this_cpu();
	__u64 now, span;

	if (!st)
		return 0;

	if (!st->insert_inflight) {
		d1_count(D1_SEG_INSERT_DROP);
		return 0;
	}
	st->insert_inflight = 0;

	now = bpf_ktime_get_ns();
	if (now < st->insert_t0_ns) {
		d1_count(D1_SEG_INSERT_DROP);
		return 0;
	}
	span = now - st->insert_t0_ns;
	if (span > D1_SPAN_DROP_NS) {
		/* Interrupted/preempted for seconds: not representative of
		 * the insert path, drop it. */
		d1_count(D1_SEG_INSERT_DROP);
		return 0;
	}

	d1_record_span(D1_SEG_INSERT_SPAN, span);

	/* Keep t0 valid for the residence segment: under QD1 solo the
	 * request inserted in this call is the one later dispatched, so
	 * dispatch fexit computes d0 - insert_t0 as enqueue->dispatch
	 * residence.  Cleared after consumption. */
	st->insert_t0_valid = 1;
	return 0;
}

/* ------------------------------------------------------------------ */
/* scx_io_dispatch_request: struct request *f(hctx)                    */
/* ------------------------------------------------------------------ */

SEC("fentry/scx_io_dispatch_request")
int BPF_PROG(d1_dispatch_entry, struct blk_mq_hw_ctx *hctx)
{
	struct d1_pcpu_state *st = d1_state_this_cpu();

	if (!st)
		return 0;

	if (st->dispatch_inflight) {
		/* Same re-entry policy as the insert pair. */
		return 0;
	}

	st->dispatch_d0_ns = bpf_ktime_get_ns();
	st->dispatch_inflight = 1;
	return 0;
}

SEC("fexit/scx_io_dispatch_request")
int BPF_PROG(d1_dispatch_exit, struct blk_mq_hw_ctx *hctx,
	     struct request *retval)
{
	struct d1_pcpu_state *st = d1_state_this_cpu();
	__u64 now, d0, span;

	if (!st)
		return 0;

	if (!st->dispatch_inflight) {
		d1_count(D1_SEG_RESIDENCE_DROP);
		return 0;
	}
	st->dispatch_inflight = 0;

	now = bpf_ktime_get_ns();
	d0 = st->dispatch_d0_ns;
	if (now < d0) {
		d1_count(D1_SEG_RESIDENCE_DROP);
		return 0;
	}
	span = now - d0;

	/* The dispatch call itself happened regardless of the outcome. */
	d1_record_span(D1_SEG_DISPATCH_SPAN, span);

	if (!retval) {
		/* Spurious dispatch (queue empty): no request to account
		 * residence for; keep insert_t0_valid for the next one. */
		d1_count(D1_SEG_DISPATCH_SPURIOUS);
		return 0;
	}

	if (!st->insert_t0_valid) {
		d1_count(D1_SEG_RESIDENCE_DROP);
		return 0;
	}
	st->insert_t0_valid = 0;

	if (d0 < st->insert_t0_ns) {
		/* Clock regression / stale t0: not a valid residence. */
		d1_count(D1_SEG_RESIDENCE_DROP);
		return 0;
	}

	d1_record_span(D1_SEG_RESIDENCE, d0 - st->insert_t0_ns);
	return 0;
}
