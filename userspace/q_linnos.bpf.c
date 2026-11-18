/* q_linnos.bpf.c - LinnOS (OSDI'20) homo reproduction: offline calibrated
 * (load state -> interception ratio) LOOKUP-TABLE interceptor.
 *
 * *** OFFLINE LOOKUP-TABLE APPROXIMATION, NOT the online neural network of
 * the LinnOS paper. ***  The paper predicts per-request SSD latency with a
 * lightweight NN and intercepts (delays) latency-critical requests whose
 * predicted latency exceeds the SLO, at a per-workload interception ratio.
 * This policy keeps only the outer control law of that design, with the NN
 * replaced by an offline-calibrated table: the observable device-load state
 * is bucketed (busy-degree bands) and each (band, class) bucket carries one
 * fixed interception ratio, pushed by the loader (--ir-low/--ir-mid/
 * --ir-high) or left at the compiled conservative defaults.  No online
 * learning, no latency prediction, no feature vector.
 *
 * State model (device-busy bucketing):
 *   - busy proxy  : global in-flight count (dispatched-not-completed, all
 *                   hctx/cgroups of the policy).  Per-cgroup in-flight was
 *                   rejected on purpose: the completed_request hook's
 *                   cgroup attribution is known-unreliable in this kernel
 *                   (ARB_POLICY_DEV_GUIDE.md pitfall 10), while the
 *                   dispatch/completion pairing without cgroup ids is
 *                   exact.  The single device behind the queue makes a
 *                   global busy degree the faithful LinnOS "device state".
 *   - busy_ref    : in-flight count treated as "100% busy" (default 64,
 *                   --busy-ref).  band 0 = <50% busy, band 1 = 50-80%,
 *                   band 2 = >80% (boundaries fixed at 50/80, the "档位").
 *   - class       : LC if the effective cgroup's arb_prio < --lc-prio-max
 *                   (default 3, arb_mix convention; unknown/0 prio -> BE).
 *   - ir[band][class]: interception ratio in percent (default table:
 *                   band0 LC 0 / BE 0, band1 LC 0 / BE 20, band2 LC 5 /
 *                   BE 60 -- conservative starting values, tune offline).
 *
 * Decision points (same as q_ratelimit):
 *   insert_request  : the bucket decision + interception draw.  Every
 *                     request is accepted; the intercepted ones are placed
 *                     on the held FIFO instead of the fast FIFO.  The draw
 *                     is deterministic and exactly ratio-faithful: a per
 *                     (band,class) sequence counter, intercept iff
 *                     (seq % 100) < ir -- e.g. ir=20 intercepts precisely
 *                     the first 20 of every 100 requests in that bucket.
 *   cgroup_dispatch : fast FIFO first; a held head whose hold deadline
 *                     (arrival + max_hold) expired is released ahead of it
 *                     (bounded stall, heimdall_v2 held semantics); when the
 *                     fast FIFO runs empty the held head is soft-released
 *                     (work conservation / the device is no longer busy);
 *                     otherwise NULL (blk-mq 3ms self-kick retries, the same
 *                     liveness path q_ratelimit relies on).
 *   evacuate        : DRAINING-gated (q_ratelimit's non-strict fallback
 *                     protection): returns 0 while the policy is active so
 *                     the generic fallback path cannot steal requests past
 *                     the interceptor; after the loader marks graceful
 *                     drain it releases fast first, then held.
 *   allow_merge     : false (a merge would change a queued request's
 *                     identity/size under the bucket accounting).
 *
 * Telemetry: the single q_linnos_stats map entry carries per-(band,class)
 * insert/intercept counts, dispatch counters per mode, and conservation
 * counters (enqueued - dispatched - evacuated - lost must be 0 at quiesce
 * for both FIFOs); the loader dumps it on graceful stop.
 *
 * Model spec: tests/io/q_linnos_model.py (keep in sync).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_LINNOS_QUEUE_MAX   4096
#define Q_LINNOS_BANDS       3
#define Q_LINNOS_CLASSES     2      /* 0 = LC, 1 = BE */
#define Q_LINNOS_CLASS_LC    0
#define Q_LINNOS_CLASS_BE    1
#define Q_LINNOS_BAND_LOW_PCT  50U
#define Q_LINNOS_BAND_HIGH_PCT 80U

#define Q_LINNOS_DEFAULT_BUSY_REF   64ULL
#define Q_LINNOS_DEFAULT_MAX_HOLD_NS (2ULL * 1000000ULL)
#define Q_LINNOS_DEFAULT_LC_PRIO_MAX 3U

/* Offline-calibrated default interception table (percent), [band][class]:
 * band0 (<50% busy):  nobody intercepted -- device not busy, LinnOS
 *                     would predict in-SLO latency;
 * band1 (50-80%):     LC 0 / BE 20 -- start shielding LC from BE noise;
 * band2 (>80%):       LC 5 / BE 60 -- heavy interference, aggressive BE
 *                     interception and a small LC self-interception.
 * These are conservative starting points for offline calibration; the
 * loader overrides them per bucket (--ir-low/--ir-mid/--ir-high). */
#define Q_LINNOS_DEFAULT_IR_LOW  {0, 0}
#define Q_LINNOS_DEFAULT_IR_MID  {0, 20}
#define Q_LINNOS_DEFAULT_IR_HIGH {5, 60}

struct q_linnos_cfg {
    __u64 busy_ref;                 /* in-flight count == 100% busy */
    __u64 max_hold_ns;              /* held deadline = arrival + max_hold */
    __u32 ir[Q_LINNOS_BANDS][Q_LINNOS_CLASSES];
    __u32 lc_prio_max;              /* arb_prio < lc_prio_max -> LC */
    __u32 flags;                    /* reserved */
};

struct q_linnos_entry {
    __u64 rq_ptr;
    __u64 deadline_ns;              /* arrival + max_hold (held FIFO only) */
};

struct q_linnos_stats {
    __u64 inserts;
    __u64 inserts_by_band[Q_LINNOS_BANDS];
    __u64 inserts_by_class[Q_LINNOS_CLASSES];
    __u64 intercepted[Q_LINNOS_BANDS][Q_LINNOS_CLASSES];
    __u64 dispatch_fast;
    __u64 dispatch_held_timeout;
    __u64 dispatch_held_soft;
    __u64 evac_fast;
    __u64 evac_held;
    __u64 null_returns;
    __u64 push_fails;
    __u64 completions;
    __u64 inflight;                 /* dispatched - completed (busy proxy) */
    __u64 enqueued_fast;
    __u64 enqueued_held;
    __u64 lost_fast;                /* enqueued_fast - dispatch_fast - evac_fast; 0 at quiesce */
    __u64 lost_held;                /* enqueued_held - held releases - evac_held; 0 at quiesce */
    __u32 seq[Q_LINNOS_BANDS][Q_LINNOS_CLASSES];  /* interception draw counters */
};

struct q_linnos_state_entry {
    struct bpf_spin_lock lock;
    struct q_linnos_stats stats;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_linnos_cfg);
} q_linnos_cfg_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_linnos_state_entry);
} q_linnos_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, Q_LINNOS_QUEUE_MAX);
    __type(value, struct q_linnos_entry);
} q_linnos_fast SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_QUEUE);
    __uint(max_entries, Q_LINNOS_QUEUE_MAX);
    __type(value, struct q_linnos_entry);
} q_linnos_held SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_linnos_state_entry *q_linnos_state_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&q_linnos_state, &key);
}

static __always_inline void q_linnos_cfg_read(struct q_linnos_cfg *out)
{
    struct q_linnos_cfg *cfg;
    __u32 key = 0;
    __u32 b, c;

    out->busy_ref = Q_LINNOS_DEFAULT_BUSY_REF;
    out->max_hold_ns = Q_LINNOS_DEFAULT_MAX_HOLD_NS;
    out->ir[0][0] = 0;   out->ir[0][1] = 0;    /* band0: LC 0 / BE 0 */
    out->ir[1][0] = 0;   out->ir[1][1] = 20;   /* band1: LC 0 / BE 20 */
    out->ir[2][0] = 5;   out->ir[2][1] = 60;   /* band2: LC 5 / BE 60 */
    out->lc_prio_max = Q_LINNOS_DEFAULT_LC_PRIO_MAX;

    cfg = bpf_map_lookup_elem(&q_linnos_cfg_map, &key);
    if (!cfg)
        return;
    if (cfg->busy_ref)
        out->busy_ref = cfg->busy_ref;
    if (cfg->max_hold_ns)
        out->max_hold_ns = cfg->max_hold_ns;
    for (b = 0; b < Q_LINNOS_BANDS; b++)
        for (c = 0; c < Q_LINNOS_CLASSES; c++)
            if (cfg->ir[b][c] <= 100)
                out->ir[b][c] = cfg->ir[b][c];
    if (cfg->lc_prio_max)
        out->lc_prio_max = cfg->lc_prio_max;
}

/* Device-busy band from the global in-flight proxy. */
static __always_inline __u32 q_linnos_band(__u64 inflight, __u64 busy_ref)
{
    __u64 pct;

    if (!busy_ref)
        return 0;   /* unconfigured reference: never busy, never intercept */
    pct = inflight * 100ULL / busy_ref;
    if (pct < Q_LINNOS_BAND_LOW_PCT)
        return 0;
    if (pct < Q_LINNOS_BAND_HIGH_PCT)
        return 1;
    return 2;
}

/* LC/BE class from the effective cgroup's arb_prio (policy_meta is the
 * loader-registered per-cgroup record; 0/unset prio -> BE conservative). */
static __always_inline __u32 q_linnos_class(__u64 cgroup_id, __u32 lc_prio_max)
{
    struct iosched_policy_meta *meta;

    meta = bpf_map_lookup_elem(&policy_meta, &cgroup_id);
    if (!meta || !meta->arb_prio || meta->arb_prio >= lc_prio_max)
        return Q_LINNOS_CLASS_BE;
    return Q_LINNOS_CLASS_LC;
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
    struct q_linnos_state_entry *entry;
    struct q_linnos_entry qentry = {};
    struct q_linnos_cfg cfg;
    __u64 cgroup_id, rq_ptr, now;
    __u32 band, cls, seq, bc, ir_val;
    bool intercept;

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

    entry = q_linnos_state_lookup();
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
        return -1;
    }

    q_linnos_cfg_read(&cfg);
    now = bpf_ktime_get_ns();

    /* LC/BE class from the cgroup's policy_meta row.  q_linnos_class() does
     * a bpf_map_lookup_elem helper call, so it MUST run OUTSIDE the state
     * lock: the verifier rejects any function/helper call while a spin
     * lock is held ("function calls are not allowed while holding a lock",
     * -EINVAL; q_ratelimit's convention -- helpers before the lock, only
     * plain state-field updates inside).  The class depends solely on
     * (cgroup_id, cfg), never on lock-protected state, so hoisting it out
     * cannot change the decision. */
    cls = q_linnos_class(cgroup_id, cfg.lc_prio_max);

    /* Bucket the device state and draw the interception decision under the
     * state lock (the seq counter makes the draw exactly ratio-faithful:
     * (seq % 100) < ir intercepts precisely ir of every 100 requests);
     * the critical section is plain state reads/updates only.  All
     * bucketed-table accesses go through the FLAT index bc = band *
     * Q_LINNOS_CLASSES + cls (one pointer plus one scalar add per
     * access): indexing them as arr[band][cls] lets clang fold the two
     * scaled indices into (ptr + band*8) | (cls*4), and the verifier
     * rejects bitwise operators on pointer registers (-EACCES). */
    bpf_spin_lock(&entry->lock);
    band = q_linnos_band(entry->stats.inflight, cfg.busy_ref);
    bc = band * Q_LINNOS_CLASSES + cls;
    seq = ++((__u32 *)entry->stats.seq)[bc];
    ir_val = ((__u32 *)cfg.ir)[bc];
    intercept = (seq % 100) < ir_val;
    entry->stats.inserts++;
    entry->stats.inserts_by_band[band]++;
    entry->stats.inserts_by_class[cls]++;
    if (intercept)
        ((__u64 *)entry->stats.intercepted)[bc]++;
    bpf_spin_unlock(&entry->lock);

    qentry.rq_ptr = rq_ptr;
    if (intercept) {
        qentry.deadline_ns = now + cfg.max_hold_ns;
        if (bpf_map_push_elem(&q_linnos_held, &qentry, 0) != 0) {
            /* Held FIFO full -> fail open to the fast FIFO (never lose a
             * request to the interceptor). */
            bpf_spin_lock(&entry->lock);
            entry->stats.push_fails++;
            bpf_spin_unlock(&entry->lock);
            intercept = false;
        } else {
            bpf_spin_lock(&entry->lock);
            entry->stats.enqueued_held++;
            entry->stats.lost_held = entry->stats.enqueued_held -
                entry->stats.dispatch_held_timeout -
                entry->stats.dispatch_held_soft - entry->stats.evac_held;
            bpf_spin_unlock(&entry->lock);
        }
    }
    if (!intercept) {
        if (bpf_map_push_elem(&q_linnos_fast, &qentry, 0) != 0) {
            /* Queue full -> fail-open bypass for this single request
             * (Q_LINNOS_QUEUE_MAX=4096 >> nr_requests: must not fire). */
            bpf_spin_lock(&entry->lock);
            entry->stats.push_fails++;
            bpf_spin_unlock(&entry->lock);
            iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
            return -1;
        }
        bpf_spin_lock(&entry->lock);
        entry->stats.enqueued_fast++;
        entry->stats.lost_fast = entry->stats.enqueued_fast -
            entry->stats.dispatch_fast - entry->stats.evac_fast;
        bpf_spin_unlock(&entry->lock);
    }

    iosched_policy_meta_note_insert(&policy_meta, cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(64,
        "[q_linnos] insert rq=%llx cg=%llu band=%u cls=%u ir=%u intercept=%d\n",
        rq_ptr, cgroup_id, band, cls, ir_val, intercept);
    return IOSCHED_INSERT_ACCEPT;
}

/*
 * Dispatch decision point (q_ratelimit's insert/next pattern):
 *   1. an expired held head is released first (bounded stall);
 *   2. fast FIFO;
 *   3. held head soft-released when fast is empty (work conservation: the
 *      interceptor only bites while faster work exists or the hold is
 *      actually pending);
 *   4. NULL: blk-mq's 3ms self-kick plus completion kicks retry.
 */
SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_linnos_state_entry *entry;
    struct q_linnos_entry head = {};
    struct q_linnos_entry popped = {};
    __u64 now;
    __u32 mode = 0;

    if (!dctx)
        return 0;
    entry = q_linnos_state_lookup();
    if (!entry)
        return 0;

    now = bpf_ktime_get_ns();

    /* 1. expired held head (peek then pop; a raced pop is skipped) */
    if (bpf_map_peek_elem(&q_linnos_held, &head) == 0 && head.rq_ptr &&
        now >= head.deadline_ns) {
        if (bpf_map_pop_elem(&q_linnos_held, &popped) == 0 && popped.rq_ptr) {
            mode = 2;   /* held, hold deadline expired */
            goto out;
        }
    }

    /* 2. fast FIFO */
    if (bpf_map_pop_elem(&q_linnos_fast, &popped) == 0 && popped.rq_ptr) {
        mode = 1;   /* fast, not intercepted */
        goto out;
    }

    /* 3. held soft release when nothing faster is queued */
    if (bpf_map_pop_elem(&q_linnos_held, &popped) == 0 && popped.rq_ptr) {
        mode = 3;   /* held, soft release (fast empty) */
        goto out;
    }

    /* 4. nothing releasable: NULL (self-kick retry) */
    bpf_spin_lock(&entry->lock);
    entry->stats.null_returns++;
    bpf_spin_unlock(&entry->lock);
    return 0;

out:
    bpf_spin_lock(&entry->lock);
    entry->stats.inflight++;
    if (mode == 1)
        entry->stats.dispatch_fast++;
    else if (mode == 2)
        entry->stats.dispatch_held_timeout++;
    else
        entry->stats.dispatch_held_soft++;
    entry->stats.lost_fast = entry->stats.enqueued_fast -
        entry->stats.dispatch_fast - entry->stats.evac_fast;
    entry->stats.lost_held = entry->stats.enqueued_held -
        entry->stats.dispatch_held_timeout -
        entry->stats.dispatch_held_soft - entry->stats.evac_held;
    bpf_spin_unlock(&entry->lock);

    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    IOEXT_BPF_DBG_SAMPLED(128,
        "[q_linnos] dispatch mode=%u rq=%llx now=%llu\n",
        mode, popped.rq_ptr, now);
    return popped.rq_ptr;
}

/* DRAINING-gated evacuate (q_ratelimit's non-strict fallback protection):
 * while active returns 0 so the generic fallback path cannot steal requests
 * past the interceptor; after the graceful-drain mark, fast first, then
 * held. */
SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_linnos_state_entry *entry;
    struct q_linnos_entry popped = {};
    __u32 mode = 0;
    bool rescuing;

    if (!dctx)
        return 0;
    rescuing = bpf_scx_io_rescue_active((__u64)dctx->q);
    if (!rescuing &&
        !iosched_policy_meta_is_draining(&policy_meta, dctx->cgroup_id))
        return 0;

    if (bpf_map_pop_elem(&q_linnos_fast, &popped) != 0 || !popped.rq_ptr) {
        popped.rq_ptr = 0;
        if (bpf_map_pop_elem(&q_linnos_held, &popped) != 0 || !popped.rq_ptr)
            return 0;
        mode = 5;
    } else {
        mode = 4;
    }

    entry = q_linnos_state_lookup();
    if (entry) {
        bpf_spin_lock(&entry->lock);
        if (rescuing)
            entry->stats.inflight++;
        if (mode == 4)
            entry->stats.evac_fast++;
        else
            entry->stats.evac_held++;
        entry->stats.lost_fast = entry->stats.enqueued_fast -
            entry->stats.dispatch_fast - entry->stats.evac_fast;
        entry->stats.lost_held = entry->stats.enqueued_held -
            entry->stats.dispatch_held_timeout -
            entry->stats.dispatch_held_soft - entry->stats.evac_held;
        bpf_spin_unlock(&entry->lock);
    }
    iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return popped.rq_ptr;
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct q_linnos_state_entry *entry;

    if (!cctx || !cctx->rq)
        return;
    entry = q_linnos_state_lookup();
    if (!entry)
        return;
    bpf_spin_lock(&entry->lock);
    if (entry->stats.inflight)
        entry->stats.inflight--;
    entry->stats.completions++;
    bpf_spin_unlock(&entry->lock);
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    /* A merge would grow a queued request past the size recorded at insert
     * and change its FIFO identity under the bucket accounting. */
    return false;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_linnos = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .completed_request = (void *)completed_request,
    .allow_merge = (void *)allow_merge,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_linnos",
};
