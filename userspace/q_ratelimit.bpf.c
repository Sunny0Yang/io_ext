/* q_ratelimit.bpf.c - per-cgroup token-bucket rate limiter (io.max wbps
 * semantics), controller-lineage faithful reimplementation (P1-6).
 *
 * Semantics (mirror native io.max wbps = absolute rate cap):
 *   - one token bucket per effective cgroup (key = cgroup inode id)
 *   - cgroup_dispatch refills tokens by elapsed time
 *     (rate_bytes_per_sec * dt, dt clamped at 1s which is exact because
 *     the bucket refills to its cap within 0.5s), then peeks the FIFO
 *     head: tokens >= head bytes -> pop and deduct; otherwise return
 *     NULL and rely on the blk-mq BLK_MQ_BUDGET_DELAY (3ms) self-kick
 *     retry loop (blk-mq-sched.c:116-127,152-155) plus the insert-time
 *     immediate kick (iosched_ext.c:3027).
 *   - insert always accepts; queued entry carries (rq_ptr, bytes) so the
 *     head size is visible before popping (bpf_map_peek_elem works on
 *     BPF_MAP_TYPE_QUEUE). Queue depth 4096 >> nr_requests, so the
 *     fail-open bypass of a full array (q_db POL-06 note) cannot happen.
 *   - bucket capacity = rate * 0.5s (simplified io.max burst window).
 *
 * evacuate is DRAINING-gated (the critical non-strict fix):
 *   While the policy is active it returns 0, so the non-strict generic
 *   fallback path (iosched_ext.c:3307-3418, evacuate call at :3388-3415)
 *   cannot steal queued requests past the token bucket. Only after the
 *   loader marks IOSCHED_POLICY_META_F_DRAINING on graceful stop
 *   (ioext_loader_common.h iosched_loader_mark_policy_draining_if_owner,
 *   set before bpf_link_detach) does evacuate release requests for the
 *   teardown drain, keeping detach drain + accounting conservation
 *   intact. Known boundary (documented, not exercised in this rig):
 *   SIGKILL / REPLACE paths skip the DRAINING mark and fall back to the
 *   kernel 500-round stall force-teardown (iosched_ext.c:1130-1141).
 *
 * allow_merge = false: a merge can grow a queued request past the bytes
 * recorded at insert, under-charging the bucket (same rule as q_db).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_RL_QUEUE_MAX       4096
#define Q_RL_DT_CLAMP_NS     1000000000ULL
#define Q_RL_MAX_CGROUPS     64

struct q_rl_cfg {
    __u64 rate_bytes_per_sec;
    __u64 burst_cap_bytes;
};

struct q_rl_qentry {
    __u64 rq_ptr;
    __u64 bytes;
};

struct q_rl_bucket {
    struct bpf_spin_lock lock;
    __u64 tokens;
    __u64 last_ns;
    __u64 enqueued;
    __u64 dispatches;
    __u64 evac_pops;
    __u64 null_returns;
    __u64 push_fails;
    __u64 lost;   /* enqueued - dispatches - evac_pops; must be 0 at quiesce */
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_rl_cfg);
} q_rl_cfg_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, Q_RL_MAX_CGROUPS);
    __type(key, __u64);
    __type(value, struct q_rl_bucket);
} q_rl_bucket SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, Q_RL_QUEUE_MAX);
    __type(value, struct q_rl_qentry);
} q_rl_queue SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_rl_bucket *q_rl_bucket_get(__u64 cgroup_id)
{
    struct q_rl_bucket *b;
    struct q_rl_bucket zero = {};

    b = bpf_map_lookup_elem(&q_rl_bucket, &cgroup_id);
    if (b)
        return b;
    /* tokens=0, last_ns=0: lazily initialized on first dispatch touch
     * (no retroactive refill across the policy's dark age). */
    if (bpf_map_update_elem(&q_rl_bucket, &cgroup_id, &zero,
                            BPF_NOEXIST))
        return 0;
    return bpf_map_lookup_elem(&q_rl_bucket, &cgroup_id);
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
    struct iosched_request_desc desc;
    struct q_rl_qentry entry = {};
    struct q_rl_bucket *b;
    __u64 cgroup_id, rq_ptr;

    if (!ictx || !ictx->rq)
        return -1;
    rq_ptr = (__u64)ictx->rq;
    if (rq_ptr < 0xffff000000000000ULL)
        return -1;

    cgroup_id = ictx->cgroup_id;
    iosched_policy_meta_begin_insert(&policy_meta, cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    b = q_rl_bucket_get(cgroup_id);
    if (!b) {
        iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    entry.rq_ptr = rq_ptr;
    entry.bytes = (__u64)desc.cost * 512ULL;

    if (bpf_map_push_elem(&q_rl_queue, &entry, 0) != 0) {
        /* Queue full -> fail-open for this single request. With
         * Q_RL_QUEUE_MAX=4096 >> nr_requests this must not fire. */
        bpf_spin_lock(&b->lock);
        b->push_fails++;
        bpf_spin_unlock(&b->lock);
        iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
        return -1;
    }

    bpf_spin_lock(&b->lock);
    b->enqueued++;
    b->lost = b->enqueued - b->dispatches - b->evac_pops;
    bpf_spin_unlock(&b->lock);

    iosched_policy_meta_note_insert(&policy_meta, cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                          "[q_ratelimit] insert rq=%llx bytes=%llu eff_cg=%llu\n",
                          rq_ptr, entry.bytes, cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_rl_cfg *cfg;
    struct q_rl_bucket *b;
    struct q_rl_qentry head = {};
    struct q_rl_qentry popped = {};
    __u32 cfg_key = 0;
    __u64 now, dt, tokens_now, rate, cap;

    if (!dctx)
        return 0;

    /* cfg is loader-written before attach and constant afterwards; read
     * outside the bucket spin_lock critical section. */
    cfg = bpf_map_lookup_elem(&q_rl_cfg_map, &cfg_key);
    rate = cfg ? cfg->rate_bytes_per_sec : 0;
    cap = cfg ? cfg->burst_cap_bytes : 0;

    b = q_rl_bucket_get(dctx->cgroup_id);
    if (!b)
        return 0;

    now = bpf_ktime_get_ns();

    bpf_spin_lock(&b->lock);
    if (!b->last_ns)
        b->last_ns = now;
    /*
     * `now` is sampled BEFORE the lock: two CPUs can sample now in one
     * order and acquire the lock in the other, making now < last_ns here.
     * A plain u64 subtraction would underflow to ~2^64, get clamped to
     * the 1s dt clamp and award a phantom rate*1s refill on every
     * lock-order inversion (measured: 2-3 inversions/s at ~50K
     * dispatch calls/s inflated the effective refill to ~2.8x rate,
     * breaking the cap entirely — vng smoke 20261029).  Guard the
     * inversion to zero refill instead.
     */
    if (now > b->last_ns)
        dt = now - b->last_ns;
    else
        dt = 0;
    if (dt > Q_RL_DT_CLAMP_NS)
        dt = Q_RL_DT_CLAMP_NS;
    b->last_ns = now;
    /* dt is clamped to 1s so dt * rate cannot overflow u64 even at
     * multi-GB/s rates; refill beyond cap is clamped below anyway. */
    b->tokens += dt * rate / 1000000000ULL;
    if (cap && b->tokens > cap)
        b->tokens = cap;
    tokens_now = b->tokens;
    bpf_spin_unlock(&b->lock);

    if (!rate) {
        /* No configured rate: fail-open FIFO (loader enforces rate>=1). */
        if (bpf_map_pop_elem(&q_rl_queue, &popped) != 0 || !popped.rq_ptr)
            return 0;
        bpf_spin_lock(&b->lock);
        b->dispatches++;
        b->lost = b->enqueued - b->dispatches - b->evac_pops;
        bpf_spin_unlock(&b->lock);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
        return popped.rq_ptr;
    }

    if (bpf_map_peek_elem(&q_rl_queue, &head) != 0 || !head.rq_ptr)
        return 0;   /* empty: ordinary no-work NULL */

    if (head.bytes > tokens_now) {
        /* Rate-limited NULL: the 3ms blk-mq self-kick retries us. */
        bpf_spin_lock(&b->lock);
        b->null_returns++;
        bpf_spin_unlock(&b->lock);
        return 0;
    }

    if (bpf_map_pop_elem(&q_rl_queue, &popped) != 0 || !popped.rq_ptr)
        return 0;   /* raced with a concurrent popper: nothing to hand out */

    bpf_spin_lock(&b->lock);
    /* Deduct the ACTUAL popped bytes (peek/pop may straddle a concurrent
     * pop); floor at 0 instead of u64 wraparound - over-admit bounded by
     * one request per race. */
    b->tokens = popped.bytes <= b->tokens ? b->tokens - popped.bytes : 0;
    b->dispatches++;
    b->lost = b->enqueued - b->dispatches - b->evac_pops;
    bpf_spin_unlock(&b->lock);

    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return popped.rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_rl_qentry popped = {};
    struct q_rl_bucket *b;

    if (!dctx)
        return 0;

    /* DRAINING gate: while active, the non-strict generic-fallback path
     * (iosched_ext.c:3388) must not steal requests past the token
     * bucket; after the loader marks graceful drain, release requests
     * for the teardown evacuation loop (iosched_ext.c:1112-1127).
     *
     * ABI note (tune8): an earlier revision of this gate ALSO consulted
     * the bpf_scx_io_rescue_active() kfunc.  That kfunc does not exist
     * on the ioext4 kernel line (no definition, no BTF_KFUNCS export in
     * /tmp/ioext4_wt ioext4v3 nor in the worktree kernel), so the extern
     * made libbpf fail the whole skeleton load with
     * "extern (func ksym) 'bpf_scx_io_rescue_active': not found in
     * kernel or module BTFs" (-EINVAL) on 86.  Gate is DRAINING-only
     * now: there is no rescue mode on this kernel line to release for,
     * and the rate cap lives entirely in cgroup_dispatch (this path is
     * not on the dispatch hot path). */
    if (!iosched_policy_meta_is_draining(&policy_meta, dctx->cgroup_id))
        return 0;

    if (bpf_map_pop_elem(&q_rl_queue, &popped) != 0 || !popped.rq_ptr)
        return 0;

    b = q_rl_bucket_get(dctx->cgroup_id);
    if (b) {
        bpf_spin_lock(&b->lock);
        b->evac_pops++;
        b->lost = b->enqueued - b->dispatches - b->evac_pops;
        bpf_spin_unlock(&b->lock);
    }
    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return popped.rq_ptr;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    /* A merge would grow a queued request past the bytes recorded at
     * insert, under-charging the bucket (q_db POL-06 rule). */
    return false;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_ratelimit = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .allow_merge = (void *)allow_merge,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_ratelimit",
};