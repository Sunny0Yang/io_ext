/* q_db.bpf.c - DB-workload two-lane size-threshold FIFO queue policy
 *
 * Design rationale (results/isol_bench/rig_ycsb/D5_YCSB_DESIGN.md §3):
 *   Measured YCSB/db_bench request profiles on this rig:
 *     - foreground point reads ~13KB (sync, low QD); WAL writes small (KBs)
 *     - background SST/compaction writes ~350KB (async bulk)
 *   q_prio's 8-sector (4KB) small-threshold misfiles the 13KB foreground
 *   reads into the slow lane - one root cause of its D4 collapse.
 *   The 64KB threshold below sits inside the 27x separation band between
 *   the 13KB foreground and 350KB background populations.
 *
 * Lanes (classification by intrinsic request size ONLY - no direction,
 * no ioprio, no cgroup, no arbiter; single root policy):
 *   F fast: cost <= 128 sectors (64KB), reads AND writes (WAL small
 *           writes are foreground-critical)  -> strict-priority FIFO
 *   B bulk: cost >  128 sectors (SST flush / compaction output)
 *           -> FIFO + anti-starvation:
 *              B head waiting >= 10ms (arrival_ns recorded at insert via
 *              bpf_ktime_get_ns) OR >= 32 consecutive F dispatches
 *              forces one B dispatch.  Guards against the q_prio D4
 *              failure mode (bulk writes starved -> memtable backpressure
 *              -> foreground collapse): a 350KB bulk stream needs only
 *              ~1 dispatch per 32 F dispatches to keep compaction debt
 *              from growing, and the 10ms cap bounds worst-case B wait.
 *
 * Skeleton mirrors q_ddl.bpf.c (proven sibling): per-hctx ARRAY state map
 * (auto-zero, no creator needed - HASH spin_lock maps cannot be updated
 * from BPF and lookups would always miss, see q_ddl phase-3 note),
 * per-lane pending arrays with IOSCHED_MAX_PENDING=32 capacity, overflow
 * insert returns -1 (kernel fail-open bypasses that single request).
 *
 * Merge: allow_merge = false (same POL-06 correctness rule as q_ddl):
 * merging can grow an F-classified request past the 64KB contract while
 * it sits in the F lane.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_DB_F_MAX_SECTORS     128         /* 64KB size threshold */
#define Q_DB_B_STARVATION_NS   10000000ULL /* 10ms B-head anti-starvation */
#define Q_DB_F_BURST_MAX       32          /* consecutive F dispatch cap  */

enum db_class {
    DB_F     = 0,
    DB_B     = 1,
    DB_NR    = 2,
};

struct q_db_lane {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
    __u64 enqueued;
    __u64 dispatched;
};

struct q_db_local_state {
    struct q_db_lane lanes[DB_NR];
    __u32 f_burst;               /* consecutive F dispatches since last B */
};

struct q_db_state_entry {
    struct bpf_spin_lock lock;
    struct q_db_local_state state;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct q_db_state_entry);
} state_map SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_db_state_entry *db_state_lookup(__u64 cgroup_id,
                                                                __u64 hctx_idx)
{
    __u32 key = (__u32)hctx_idx;

    (void)cgroup_id;
    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline enum db_class db_classify(const struct iosched_request_desc *desc)
{
    /* Size only; reads and writes are equal (WAL small writes ride F).
     * cost == nr_sectors, or 1 when 0 (ioext_bpf_common.h). */
    return desc->cost <= Q_DB_F_MAX_SECTORS ? DB_F : DB_B;
}

static __always_inline int db_push_tail(struct q_db_lane *lane,
                                        const struct iosched_request_desc *req)
{
    if (lane->pending_nr >= IOSCHED_MAX_PENDING)
        return -1;
    lane->pending[lane->pending_nr] = *req;
    lane->pending_nr++;
    lane->enqueued++;
    return 0;
}

static __always_inline __u64 db_pop_head(struct q_db_lane *lane)
{
    struct iosched_request_desc picked;
    __u64 rq_ptr;

    if (lane->pending_nr == 0)
        return 0;

    picked = lane->pending[0];
    if (!iosched_pop_head_32(lane->pending, &lane->pending_nr))
        return 0;

    lane->dispatched++;
    rq_ptr = picked.rq_ptr;
    return rq_ptr;
}

/* Dispatch precedence (now_ns = bpf_ktime_get_ns() at cgroup_dispatch):
 *   1. B pending AND (B head waited >= 10ms OR f_burst >= 32) -> B
 *      (forced anti-starvation; f_burst reset)
 *   2. F pending -> F (strict priority; f_burst++)
 *   3. B pending -> B (work-conserving fallback; f_burst reset)
 *   4. none -> 0
 */
static __always_inline __u64 db_dispatch(struct q_db_local_state *state,
                                          __u64 now_ns)
{
    bool b_starved = false;

    if (state->lanes[DB_B].pending_nr > 0) {
        __u64 b_wait = now_ns - state->lanes[DB_B].pending[0].arrival_ns;

        if (b_wait >= Q_DB_B_STARVATION_NS)
            b_starved = true;
    }

    if (state->lanes[DB_B].pending_nr > 0 &&
        (b_starved || state->f_burst >= Q_DB_F_BURST_MAX)) {
        state->f_burst = 0;
        return db_pop_head(&state->lanes[DB_B]);
    }

    if (state->lanes[DB_F].pending_nr > 0) {
        state->f_burst++;
        return db_pop_head(&state->lanes[DB_F]);
    }

    if (state->lanes[DB_B].pending_nr > 0) {
        state->f_burst = 0;
        return db_pop_head(&state->lanes[DB_B]);
    }

    return 0;
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
    struct q_db_state_entry *entry;
    struct iosched_request_desc desc;
    enum db_class cls;
    int ret;

    if (!ictx || !ictx->rq)
        return -1;
    if ((__u64)ictx->rq < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    entry = db_state_lookup(ictx->cgroup_id, ictx->hctx_idx);
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    cls = db_classify(&desc);

    bpf_spin_lock(&entry->lock);
    ret = db_push_tail(&entry->state.lanes[cls], &desc);
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);

    IOEXT_BPF_DBG_SAMPLED(64,
        "[q_db] insert rq=%llx cls=%u cost=%u f_burst=%u\n",
        desc.rq_ptr, cls, desc.cost, entry->state.f_burst);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_db_state_entry *entry;
    __u64 now_ns, rq_ptr;

    if (!dctx)
        return 0;

    entry = db_state_lookup(dctx->cgroup_id, dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = db_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_db_state_entry *entry;
    __u64 now_ns, rq_ptr;

    if (!dctx)
        return 0;

    entry = db_state_lookup(dctx->cgroup_id, dctx->hctx_idx);
    if (!entry)
        return 0;

    now_ns = bpf_ktime_get_ns();
    bpf_spin_lock(&entry->lock);
    rq_ptr = db_dispatch(&entry->state, now_ns);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    return rq_ptr;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    return false;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_db = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .allow_merge = (void *)allow_merge,
    .bypass_mode = IOSCHED_BYPASS_MODE_NONE,
    .name = "q_db",
};