/* q_iomax.bpf.c - per-cgroup 4-channel token-bucket rate limiter with
 * io.max semantics (riops/wiops/rbps/wbps), q_ratelimit-lineage faithful
 * reimplementation (P1-7).
 *
 * Semantics (mirror native io.max, block/blk-throttle.c):
 *   - four independent channels per effective cgroup (key = cgroup
 *     inode id): riops (read IOs/s), wiops (write IOs/s), rbps (read
 *     bytes/s), wbps (write bytes/s).  A channel value of 0 = unlimited
 *     (io.max "max"): no refill, no clamp, never gates.
 *   - direction parity with blk-throttle's bio_data_dir() ==
 *     op_is_write(bio_op(bio)): the opcode LSB encodes the data
 *     transfer direction (include/linux/blk_types.h), so REQ_OP_READ(0)
 *     -> read channels, REQ_OP_WRITE/DISCARD/SECURE_ERASE/ZONE_APPEND/
 *     WRITE_ZEROES (odd opcodes) -> write channels (REQ_OP_FLUSH(2) is
 *     even, i.e. read-direction, exactly like blk-throttle).  Read
 *     channels never gate writes and vice versa.
 *   - admission is the AND over the request's own two channels
 *     (tg_dispatch_time parity: the bps limit must pass first, charging
 *     bytes, then the iops limit, charging 1): bps needs
 *     tokens >= request bytes, iops needs tokens >= 1.
 *   - io.max keeps one FIFO per direction (throtl_service_queue.
 *     queued[READ]/queued[WRITE]; the tg_dispatch_time BUG_ON pins the
 *     per-direction state machine to the first queued bio), so a
 *     rate-limited read head does NOT block queued writes.  Mirror:
 *     two BPF_MAP_TYPE_QUEUE maps, one per direction; cgroup_dispatch
 *     peeks both heads, admits each head against its own channels and
 *     serves the preferred admissible direction round-robin
 *     (work-conserving: a blocked preferred direction falls through
 *     to the other direction).
 *   - one token bucket per channel: capacity = rate * 0.5s (simplified
 *     io.max burst window, q_ratelimit convention), refilled by time
 *     progression on every cgroup_dispatch call with the fractional
 *     part of dt*rate/1e9 carried across calls (analogue of
 *     blk-throttle's __tg_update_carryover).  The carry is what makes
 *     low-rate channels exact: at the ~3ms blk-mq BLK_MQ_BUDGET_DELAY
 *     retry cadence a naive tokens += dt*rate/1e9 floors every refill
 *     of a <333/s channel to zero and starves it forever.
 *   - a rate-limited NULL return relies on the blk-mq
 *     BLK_MQ_BUDGET_DELAY (3ms) self-kick retry loop
 *     (blk-mq-sched.c:116-127,152-155) plus the insert-time immediate
 *     kick (iosched_ext.c:3027).
 *   - known boundary of the cap = rate * 0.5s convention (inherited
 *     from q_ratelimit, unreachable there because of the MiB/s input
 *     granularity): a bps channel whose rate * 0.5s is smaller than
 *     the queued request size starves that request size (io.max's
 *     slice-accrual would admit one IO per size/rate seconds).  Keep
 *     rbps/wbps >= 2 * max request bytes for the workload in question.
 *   - insert always accepts; queued entries carry (rq_ptr, bytes) so
 *     the head size is visible before popping (bpf_map_peek_elem works
 *     on BPF_MAP_TYPE_QUEUE).  Queue depth 4096 per direction
 *     >> nr_requests, so the fail-open bypass of a full array (q_db
 *     POL-06 note) cannot happen.
 *
 * evacuate is DRAINING-gated (the critical non-strict fix, inherited
 *   from q_ratelimit):
 *   While the policy is active it returns 0, so the non-strict generic
 *   fallback path (iosched_ext.c:3307-3418, evacuate call at :3388-3415)
 *   cannot steal queued requests past the token buckets.  Only after
 *   the loader marks IOSCHED_POLICY_META_F_DRAINING on graceful stop
 *   (ioext_loader_common.h iosched_loader_mark_policy_draining_if_owner,
 *   set before bpf_link_detach) does evacuate release requests for the
 *   teardown drain, keeping detach drain + accounting conservation
 *   intact.  Known boundary (documented, not exercised in this rig):
 *   SIGKILL / REPLACE paths skip the DRAINING mark and fall back to the
 *   kernel 500-round stall force-teardown (iosched_ext.c:1130-1141).
 *
 * allow_merge = false: a merge can grow a queued request past the bytes
 * recorded at insert, under-charging the bps channel (same rule as
 * q_db/q_ratelimit).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_IOMAX_QUEUE_MAX       4096
#define Q_IOMAX_DT_CLAMP_NS     1000000000ULL
#define Q_IOMAX_MAX_CGROUPS     64

struct q_iomax_cfg {
    __u64 riops;      /* read IOs/s,     0 = unlimited */
    __u64 wiops;      /* write IOs/s,    0 = unlimited */
    __u64 rbps;       /* read bytes/s,   0 = unlimited */
    __u64 wbps;       /* write bytes/s,  0 = unlimited */
    __u64 riops_cap;  /* burst caps, loader-written (rate * 0.5s) */
    __u64 wiops_cap;
    __u64 rbps_cap;
    __u64 wbps_cap;
};

struct q_iomax_qentry {
    __u64 rq_ptr;
    __u64 bytes;
};

struct q_iomax_bucket {
    struct bpf_spin_lock lock;
    __u64 riops_tokens;
    __u64 riops_carry;
    __u64 wiops_tokens;
    __u64 wiops_carry;
    __u64 rbps_tokens;
    __u64 rbps_carry;
    __u64 wbps_tokens;
    __u64 wbps_carry;
    __u64 last_ns;
    __u64 enqueued;
    __u64 dispatches;
    __u64 evac_pops;
    __u64 null_returns;
    __u64 push_fails;
    __u64 lost;   /* enqueued - dispatches - evac_pops; must be 0 at quiesce */
    __u64 read_dispatches;
    __u64 write_dispatches;
    __u8 rr_turn;   /* direction to try first on the next dispatch */
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_iomax_cfg);
} q_iomax_cfg_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, Q_IOMAX_MAX_CGROUPS);
    __type(key, __u64);
    __type(value, struct q_iomax_bucket);
} q_iomax_bucket SEC(".maps");

/* io.max parity: one FIFO per direction
 * (throtl_service_queue.queued[READ]/.queued[WRITE]). */
struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, Q_IOMAX_QUEUE_MAX);
    __type(value, struct q_iomax_qentry);
} q_iomax_queue_r SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, Q_IOMAX_QUEUE_MAX);
    __type(value, struct q_iomax_qentry);
} q_iomax_queue_w SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_iomax_bucket *q_iomax_bucket_get(__u64 cgroup_id)
{
    struct q_iomax_bucket *b;
    struct q_iomax_bucket zero = {};

    b = bpf_map_lookup_elem(&q_iomax_bucket, &cgroup_id);
    if (b)
        return b;
    /* tokens=0, last_ns=0: lazily initialized on first dispatch touch
     * (no retroactive refill across the policy's dark age). */
    if (bpf_map_update_elem(&q_iomax_bucket, &cgroup_id, &zero,
                            BPF_NOEXIST))
        return 0;
    return bpf_map_lookup_elem(&q_iomax_bucket, &cgroup_id);
}

/* io.max direction parity: blk-throttle classifies by
 * bio_data_dir(bio) == op_is_write(bio_op(bio)) — the opcode LSB
 * encodes the data transfer direction (blk_types.h).  REQ_OP_READ(0)
 * is even (read channel); REQ_OP_WRITE(1)/DISCARD(3)/SECURE_ERASE(5)/
 * ZONE_APPEND(7)/WRITE_ZEROES(9) are odd (write channel). */
static __always_inline __u8 q_iomax_op_is_write(__u64 opf)
{
    return (__u8)((opf & IOSCHED_REQ_OP_MASK) & 1ULL);
}

/* Time-progression refill of one channel (q_ratelimit convention).
 * rate == 0 (unlimited) leaves the channel inert.  The fractional part
 * of dt*rate/1e9 is carried across calls so low-rate channels accrue
 * exactly (blk-throttle carryover analogue); without it every refill
 * of a <333/s channel at the ~3ms retry cadence floors to zero and
 * starves forever.  dt <= 1s keeps dt*rate inside u64 for the
 * loader-enforced rate bound (< 18.4G per s, same class of bound as
 * q_ratelimit). */
static __always_inline void q_iomax_refill(__u64 *tokens, __u64 *carry,
                                           __u64 rate, __u64 cap, __u64 dt)
{
    __u64 whole;

    if (!rate)
        return;
    *carry += dt * rate;
    whole = *carry / 1000000000ULL;
    *carry -= whole * 1000000000ULL;
    *tokens += whole;
    if (cap && *tokens > cap)
        *tokens = cap;
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
    struct q_iomax_qentry entry = {};
    struct q_iomax_bucket *b;
    __u64 cgroup_id, rq_ptr;
    __u8 is_write;

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

    b = q_iomax_bucket_get(cgroup_id);
    if (!b) {
        iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    is_write = q_iomax_op_is_write(desc.opf);
    entry.rq_ptr = rq_ptr;
    entry.bytes = (__u64)desc.cost * 512ULL;

    if (is_write) {
        if (bpf_map_push_elem(&q_iomax_queue_w, &entry, 0) != 0) {
            /* Queue full -> fail-open for this single request. With
             * Q_IOMAX_QUEUE_MAX=4096 >> nr_requests this must not fire. */
            bpf_spin_lock(&b->lock);
            b->push_fails++;
            bpf_spin_unlock(&b->lock);
            iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
            return -1;
        }
    } else {
        if (bpf_map_push_elem(&q_iomax_queue_r, &entry, 0) != 0) {
            bpf_spin_lock(&b->lock);
            b->push_fails++;
            bpf_spin_unlock(&b->lock);
            iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
            return -1;
        }
    }

    bpf_spin_lock(&b->lock);
    b->enqueued++;
    b->lost = b->enqueued - b->dispatches - b->evac_pops;
    bpf_spin_unlock(&b->lock);

    iosched_policy_meta_note_insert(&policy_meta, cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
                          "[q_iomax] insert rq=%llx bytes=%llu wr=%u eff_cg=%llu\n",
                          rq_ptr, entry.bytes, (__u32)is_write, cgroup_id);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_iomax_cfg *cfg;
    struct q_iomax_bucket *b;
    struct q_iomax_qentry head_r = {};
    struct q_iomax_qentry head_w = {};
    struct q_iomax_qentry popped = {};
    __u32 cfg_key = 0;
    __u64 now, dt;
    __u64 ri_rate, wi_rate, rb_rate, wb_rate;
    __u64 ri_cap, wi_cap, rb_cap, wb_cap;
    __u64 ri_tok, wi_tok, rb_tok, wb_tok;
    __u8 prefer_w, have_r, have_w, ok_r, ok_w, take_w;

    if (!dctx)
        return 0;

    /* cfg is loader-written before attach and constant afterwards; read
     * outside the bucket spin_lock critical section (q_ratelimit rule). */
    cfg = bpf_map_lookup_elem(&q_iomax_cfg_map, &cfg_key);
    ri_rate = cfg ? cfg->riops : 0;
    wi_rate = cfg ? cfg->wiops : 0;
    rb_rate = cfg ? cfg->rbps  : 0;
    wb_rate = cfg ? cfg->wbps  : 0;
    ri_cap = cfg ? cfg->riops_cap : 0;
    wi_cap = cfg ? cfg->wiops_cap : 0;
    rb_cap = cfg ? cfg->rbps_cap  : 0;
    wb_cap = cfg ? cfg->wbps_cap  : 0;

    b = q_iomax_bucket_get(dctx->cgroup_id);
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
     * lock-order inversion (q_ratelimit 20261029 vng smoke bug: 2-3
     * inversions/s inflated the effective refill to ~2.8x rate and broke
     * the cap).  Guard the inversion to refill zero instead.
     */
    if (now > b->last_ns) {
        dt = now - b->last_ns;
        if (dt > Q_IOMAX_DT_CLAMP_NS)
            dt = Q_IOMAX_DT_CLAMP_NS;
    } else {
        dt = 0;
    }
    b->last_ns = now;
    q_iomax_refill(&b->riops_tokens, &b->riops_carry, ri_rate, ri_cap, dt);
    q_iomax_refill(&b->wiops_tokens, &b->wiops_carry, wi_rate, wi_cap, dt);
    q_iomax_refill(&b->rbps_tokens,  &b->rbps_carry,  rb_rate, rb_cap, dt);
    q_iomax_refill(&b->wbps_tokens,  &b->wbps_carry,  wb_rate, wb_cap, dt);
    ri_tok = b->riops_tokens;
    wi_tok = b->wiops_tokens;
    rb_tok = b->rbps_tokens;
    wb_tok = b->wbps_tokens;
    prefer_w = b->rr_turn;
    bpf_spin_unlock(&b->lock);

    /* Peek both direction FIFO heads (io.max parity: the per-direction
     * state machine is pinned to the first queued request). */
    have_r = bpf_map_peek_elem(&q_iomax_queue_r, &head_r) == 0 &&
             head_r.rq_ptr;
    have_w = bpf_map_peek_elem(&q_iomax_queue_w, &head_w) == 0 &&
             head_w.rq_ptr;
    if (!have_r && !have_w)
        return 0;   /* empty: ordinary no-work NULL */

    /* Admission (tg_dispatch_time parity): the AND over the head's own
     * two channels; bps needs tokens >= head bytes, iops needs 1.
     * Unlimited channels (rate==0) never gate. */
    ok_r = have_r &&
           (!rb_rate || rb_tok >= head_r.bytes) &&
           (!ri_rate || ri_tok >= 1);
    ok_w = have_w &&
           (!wb_rate || wb_tok >= head_w.bytes) &&
           (!wi_rate || wi_tok >= 1);

    if (!ok_r && !ok_w) {
        /* Rate-limited NULL: the 3ms blk-mq self-kick retries us. */
        bpf_spin_lock(&b->lock);
        b->null_returns++;
        bpf_spin_unlock(&b->lock);
        return 0;
    }

    /* Round-robin between the admissible directions (Kyber-style
     * domain RR): serve the preferred one, fall through to the other
     * when it is empty or rate-limited (work-conserving). */
    take_w = prefer_w ? ok_w : !ok_r;

    if (take_w) {
        if (bpf_map_pop_elem(&q_iomax_queue_w, &popped) != 0 || !popped.rq_ptr)
            return 0;   /* raced with a concurrent popper */
    } else {
        if (bpf_map_pop_elem(&q_iomax_queue_r, &popped) != 0 || !popped.rq_ptr)
            return 0;
    }

    bpf_spin_lock(&b->lock);
    /* Deduct the ACTUAL popped request from its own direction's channels
     * (peek/pop may straddle a concurrent popper, so the popped request
     * can differ from the peeked head); floor at 0 instead of u64
     * wraparound — over-admit bounded by one request per race. */
    if (take_w) {
        if (b->wiops_tokens)
            b->wiops_tokens--;
        b->wbps_tokens = popped.bytes <= b->wbps_tokens
            ? b->wbps_tokens - popped.bytes : 0;
        b->write_dispatches++;
        b->rr_turn = 0;
    } else {
        if (b->riops_tokens)
            b->riops_tokens--;
        b->rbps_tokens = popped.bytes <= b->rbps_tokens
            ? b->rbps_tokens - popped.bytes : 0;
        b->read_dispatches++;
        b->rr_turn = 1;
    }
    b->dispatches++;
    b->lost = b->enqueued - b->dispatches - b->evac_pops;
    bpf_spin_unlock(&b->lock);

    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return popped.rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_iomax_qentry popped = {};
    struct q_iomax_bucket *b;

    if (!dctx)
        return 0;

    /* DRAINING gate (q_ratelimit's critical non-strict fix): while
     * active, the non-strict generic-fallback path (iosched_ext.c:3388)
     * must not steal requests past the token buckets; after the loader
     * marks graceful drain, release requests for the teardown
     * evacuation loop (iosched_ext.c:1112-1127).  Teardown drain order
     * is read FIFO first, then write FIFO (cross-direction order during
     * the drain is not semantically meaningful). */
    if (!bpf_scx_io_rescue_active((__u64)dctx->q) &&
        !iosched_policy_meta_is_draining(&policy_meta, dctx->cgroup_id))
        return 0;

    if (bpf_map_pop_elem(&q_iomax_queue_r, &popped) != 0 || !popped.rq_ptr) {
        if (bpf_map_pop_elem(&q_iomax_queue_w, &popped) != 0 || !popped.rq_ptr)
            return 0;
    }

    b = q_iomax_bucket_get(dctx->cgroup_id);
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
     * insert, under-charging the bps channel (q_db POL-06 rule). */
    return false;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_iomax = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .allow_merge = (void *)allow_merge,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_iomax",
};
