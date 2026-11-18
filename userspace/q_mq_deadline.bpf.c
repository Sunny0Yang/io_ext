#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"
#include "ioext_bpf_experimental.h"

char LICENSE[] SEC("license") = "GPL";

#define IOEXT_REQ_OP_MASK ((1ULL << 8) - 1)
#define IOEXT_REQ_SYNC (1ULL << __REQ_SYNC)

#define Q_MQDD_MAX_HCTX 256U
#define Q_MQDD_LANES 6U
#define Q_MQDD_TREE_WALK_MAX 32U

#define Q_MQDD_RT_READ 0U
#define Q_MQDD_RT_WRITE 1U
#define Q_MQDD_BE_READ 2U
#define Q_MQDD_BE_WRITE 3U
#define Q_MQDD_IDLE_READ 4U
#define Q_MQDD_IDLE_WRITE 5U

#ifndef __noinline
#define __noinline __attribute__((noinline))
#endif

struct q_mqdd_node {
    struct iosched_request_desc desc;
    struct bpf_list_node fifo_node;
    struct bpf_rb_node sector_node;
    struct bpf_refcount refcount;
    __u32 stale;
    __u32 prio;
    __u32 dir;
    __u32 reserved;
};

struct q_mqdd_dispatch_node {
    struct iosched_request_desc desc;
    struct bpf_list_node fifo_node;
};

struct q_mqdd_lane_state {
    struct bpf_spin_lock lock;
    struct bpf_list_head fifo_queue __contains(q_mqdd_node, fifo_node);
    struct bpf_rb_root sector_tree __contains(q_mqdd_node, sector_node);
    __u64 latest_pos;
    __u32 count;
};

struct q_mqdd_dispatch_state {
    struct bpf_spin_lock lock;
    struct bpf_list_head dispatch_queue __contains(q_mqdd_dispatch_node, fifo_node);
    __u32 dispatch_nr;
};

struct q_mqdd_hctx_state {
    __u32 async_depth;
    __u32 batching;
    __u32 starved;
    __u32 last_dir;
};

struct q_mqdd_lane {
    struct q_mqdd_lane_state *state;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_MQDD_MAX_HCTX * Q_MQDD_LANES);
    __type(key, __u32);
    __type(value, struct q_mqdd_lane_state);
} lane_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_MQDD_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct q_mqdd_dispatch_state);
} dispatch_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_MQDD_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct q_mqdd_hctx_state);
} hctx_map SEC(".maps");

#if IOEXT_DEBUG
enum q_mqdd_debug_event {
    Q_MQDD_DBG_INSERT_ACCEPT = 0,
    Q_MQDD_DBG_INSERT_REJECT,
    Q_MQDD_DBG_DISPATCH_HIT,
    Q_MQDD_DBG_DISPATCH_ZERO,
    Q_MQDD_DBG_DISPATCH_BLOCKED,
    Q_MQDD_DBG_DISPATCH_STATE_MISS,
    Q_MQDD_DBG_EVACUATE_HIT,
    Q_MQDD_DBG_EVACUATE_ZERO,
    Q_MQDD_DBG_EVACUATE_BLOCKED,
    Q_MQDD_DBG_EVACUATE_STATE_MISS,
    Q_MQDD_DBG_GC_DISPATCH,
    Q_MQDD_DBG_GC_EVACUATE,
    Q_MQDD_DBG_PREPARE,
    Q_MQDD_DBG_FINISH,
    Q_MQDD_DBG_COMPLETE,
    Q_MQDD_DBG_REQUEUE,
};

struct q_mqdd_debug_counters {
    __u64 insert_accept;
    __u64 insert_reject;
    __u64 dispatch_hit;
    __u64 dispatch_zero;
    __u64 dispatch_blocked;
    __u64 dispatch_state_miss;
    __u64 evacuate_hit;
    __u64 evacuate_zero;
    __u64 evacuate_blocked;
    __u64 evacuate_state_miss;
    __u64 gc_dispatch;
    __u64 gc_evacuate;
    __u64 prepare;
    __u64 finish;
    __u64 complete;
    __u64 requeue;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_mqdd_debug_counters);
} q_mqdd_debug_map SEC(".maps");

static __always_inline __u64 q_mqdd_debug_inc(__u32 event)
{
    __u32 key = 0;
    struct q_mqdd_debug_counters *counters;

    counters = bpf_map_lookup_elem(&q_mqdd_debug_map, &key);
    if (!counters)
        return 0;

    switch (event) {
    case Q_MQDD_DBG_INSERT_ACCEPT:
        counters->insert_accept++;
        return counters->insert_accept;
    case Q_MQDD_DBG_INSERT_REJECT:
        counters->insert_reject++;
        return counters->insert_reject;
    case Q_MQDD_DBG_DISPATCH_HIT:
        counters->dispatch_hit++;
        return counters->dispatch_hit;
    case Q_MQDD_DBG_DISPATCH_ZERO:
        counters->dispatch_zero++;
        return counters->dispatch_zero;
    case Q_MQDD_DBG_DISPATCH_BLOCKED:
        counters->dispatch_blocked++;
        return counters->dispatch_blocked;
    case Q_MQDD_DBG_DISPATCH_STATE_MISS:
        counters->dispatch_state_miss++;
        return counters->dispatch_state_miss;
    case Q_MQDD_DBG_EVACUATE_HIT:
        counters->evacuate_hit++;
        return counters->evacuate_hit;
    case Q_MQDD_DBG_EVACUATE_ZERO:
        counters->evacuate_zero++;
        return counters->evacuate_zero;
    case Q_MQDD_DBG_EVACUATE_BLOCKED:
        counters->evacuate_blocked++;
        return counters->evacuate_blocked;
    case Q_MQDD_DBG_EVACUATE_STATE_MISS:
        counters->evacuate_state_miss++;
        return counters->evacuate_state_miss;
    case Q_MQDD_DBG_GC_DISPATCH:
        counters->gc_dispatch++;
        return counters->gc_dispatch;
    case Q_MQDD_DBG_GC_EVACUATE:
        counters->gc_evacuate++;
        return counters->gc_evacuate;
    case Q_MQDD_DBG_PREPARE:
        counters->prepare++;
        return counters->prepare;
    case Q_MQDD_DBG_FINISH:
        counters->finish++;
        return counters->finish;
    case Q_MQDD_DBG_COMPLETE:
        counters->complete++;
        return counters->complete;
    case Q_MQDD_DBG_REQUEUE:
        counters->requeue++;
        return counters->requeue;
    default:
        return 0;
    }
}

#endif

struct q_mqdd_runtime_cfg {
    __u32 async_depth;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_mqdd_runtime_cfg);
} cfg_map SEC(".maps");

/*
 * Merge modes (loader --merge-mode):
 *   D0: allow_merge=false, bio_merge=false            -> deny everything
 *   D1: allow_merge=mqdd_merge_compatible, bio_merge=false
 *   D2: allow_merge=mqdd_merge_compatible, bio_merge=false
 *   D3: allow_merge=mqdd_merge_compatible, bio_merge=true
 *
 * D1 and D2 are INDISTINGUISHABLE by construction: both wire
 * allow_merge to the same mqdd_merge_compatible() predicate and both
 * leave bio_merge returning false (see BPF_PROG(allow_merge) and
 * BPF_PROG(bio_merge) below). Any trace-level difference between D1 and
 * D2 observed in this rig would be noise, not policy behavior — do not
 * invent a distinction.
 */
#define MQDD_MERGE_MODE_D0  0
#define MQDD_MERGE_MODE_D1  1
#define MQDD_MERGE_MODE_D2  2
#define MQDD_MERGE_MODE_D3  3

struct q_mqdd_merge_cfg {
    __u32 merge_mode;
    __u32 reserved;
};

struct q_mqdd_merge_stats {
    __u64 allow_merge_calls;
    __u64 allow_merge_true;
    __u64 allow_merge_false;
    __u64 request_merged_calls;
    __u64 bio_merge_calls;
    __u64 bio_merge_true;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_mqdd_merge_cfg);
} merge_cfg_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_mqdd_merge_stats);
} merge_stats_map SEC(".maps");

/*
 * Selection-telemetry map (Phase-3): the write-starvation branch
 * (starved++ >= IOSCHED_MQD_MAX_READ_STARVATION in local_dispatch_prio)
 * had no coverage in any registered DT case; these counters make the
 * direction-selection walk directly observable from userspace
 * (bpftool map dump id N). Additive only — no dispatch behavior change.
 */
struct q_mqdd_sel_stats {
    __u64 cont_dispatch;   /* continuation dispatches (batching++) */
    __u64 cont_empty;      /* continuation found current-dir lane empty */
    __u64 sel_calls;       /* direction selections entered */
    __u64 sel_mixed;       /* both lanes non-empty (starved++ evaluated) */
    __u64 sel_read_only;   /* reads non-empty, writes empty (dir=READ, no state change) */
    __u64 sel_reset;       /* reads empty, writes non-empty (starved=0, dir=WRITE) */
    __u64 sel_flip;        /* starved evaluated >= MAX_READ_STARVATION -> dir=WRITE */
    __u64 sel_empty;       /* both lanes empty (return 0) */
    __u64 starved_value;   /* last starved value seen at a selection */
    __u64 prio_passes;     /* local_dispatch_prio invocations */
    __u64 sel_last_read_count;
    __u64 sel_last_write_count;
    __u64 insert_accept;
    __u64 insert_reject;
    __u64 insert_defer;
    __u64 insert_be_read;
    __u64 insert_be_write;
    __u64 insert_other_lane;
    __u64 last_insert_ioprio;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_mqdd_sel_stats);
} sel_stats_map SEC(".maps");

static __always_inline __u32 mqdd_merge_mode(void)
{
    __u32 key = 0;
    struct q_mqdd_merge_cfg *cfg = bpf_map_lookup_elem(&merge_cfg_map, &key);
    return cfg ? cfg->merge_mode : MQDD_MERGE_MODE_D0;
}

static __always_inline struct q_mqdd_merge_stats *mqdd_merge_stats(void)
{
    __u32 key = 0;
    return bpf_map_lookup_elem(&merge_stats_map, &key);
}

static __always_inline void mqdd_merge_stat_inc(__u64 *field)
{
    if (field)
        __sync_fetch_and_add(field, 1);
}

IOEXT_POLICY_META_MAP(policy_meta);

struct q_mqdd_gc {
    struct q_mqdd_node *stale0;
    struct q_mqdd_node *stale1;
    struct q_mqdd_node *tree;
    struct q_mqdd_node *fifo;
    struct q_mqdd_dispatch_node *dispatch;
};

static __always_inline __u32 lane_key(__u64 hctx_idx, __u32 lane_idx)
{
    return (__u32)hctx_idx * Q_MQDD_LANES + lane_idx;
}

static __always_inline struct q_mqdd_lane_state *lane_state_lookup(__u64 hctx_idx,
                                                                   __u32 lane_idx)
{
    __u32 key = lane_key(hctx_idx, lane_idx);

    return bpf_map_lookup_elem(&lane_map, &key);
}

static __always_inline struct q_mqdd_dispatch_state *dispatch_lookup(__u64 hctx_idx)
{
    __u32 key = hctx_idx;

    return bpf_map_lookup_elem(&dispatch_map, &key);
}

static __always_inline struct q_mqdd_hctx_state *hctx_lookup(__u64 hctx_idx)
{
    __u32 key = hctx_idx;

    return bpf_map_lookup_elem(&hctx_map, &key);
}

static __always_inline struct q_mqdd_runtime_cfg *cfg_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&cfg_map, &key);
}

static __always_inline bool is_write_op(__u64 opf)
{
    __u64 op = opf & IOEXT_REQ_OP_MASK;

    return op == REQ_OP_WRITE || op == REQ_OP_WRITE_ZEROES ||
           op == REQ_OP_ZONE_APPEND;
}

/*
 * mqdd_merge_compatible — semantic gate for the scheduler-level merge hooks.
 *
 * Back-adjacency only (bio_sector == rq_sector + rq_nr_sectors):
 * a BACK merge only extends the tail of the request, so the insert-time
 * sector key recorded in the port's rb-tree stays valid. A FRONT merge
 * moves the request's starting sector (blk_rq_pos changes after the
 * request is already linked into our sector tree / fifo copies); this port
 * has no invalidation path for that (its request_merged hook only counts,
 * see BPF_PROG(request_merged)), so a front-merged request's tree node
 * would be orphaned under a stale key. Front merges are therefore
 * structurally not approvable through this hook for this port.
 *
 * Op-class check: the kernel ctx provides the request's opcode flags in
 * ctx->rq_opf (rq->cmd_flags, filled in scx_io_allow_merge) and the bio's
 * in ctx->bio_opf. Both go through is_write_op() so that WRITE,
 * WRITE_ZEROES and ZONE_APPEND are treated as one write class (mirroring
 * the direction classification used at insert/lookup time, e.g.
 * iosched_rq_flags_from_opf / BPF_PROG(insert_request)).
 *
 * HISTORY BUG (fixed here): before ctx->rq_opf existed, this function
 * passed ctx->rq_sector | 0 — the SECTOR NUMBER — to is_write_op() as if
 * it were an opcode. Effect: write bios were effectively always denied
 * (is_write_op(sector) is only accidentally true when the sector's low
 * byte equals a write op), reads were approved iff back-adjacent, and
 * scheduler-level merges were denied based on sector arithmetic instead
 * of request/bio semantics.
 */
static __always_inline bool mqdd_merge_compatible(struct iosched_allow_merge_ctx *mctx)
{
    __u64 rq_end;

    if (!mctx || !mctx->rq || !mctx->bio)
        return false;

    rq_end = mctx->rq_sector + mctx->rq_nr_sectors;
    if (mctx->bio_sector != rq_end)
        return false;

    if (is_write_op(mctx->bio_opf) != is_write_op(mctx->rq_opf))
        return false;

    return true;
}

static __always_inline __u64 expire_ns(bool is_write)
{
    return is_write ? IOSCHED_MQD_WRITE_EXPIRE_NS : IOSCHED_MQD_READ_EXPIRE_NS;
}

static __always_inline __u64 rq_end_sector(const struct iosched_request_desc *desc)
{
    __u64 cost = desc->cost ? desc->cost : 1;

    return desc->sector + cost;
}

static __always_inline void q_mqdd_copy_desc(struct iosched_request_desc *dst,
                                             const struct iosched_request_desc *src)
{
    /* Keep scalar stores explicit for verifier-friendly dynamic objects. */
    dst->rq_ptr = src->rq_ptr;
    dst->issuer_id = src->issuer_id;
    dst->insert_flags = src->insert_flags;
    dst->sector = src->sector;
    dst->arrival_ns = src->arrival_ns;
    dst->deadline_ns = src->deadline_ns;
    dst->cgroup_id = src->cgroup_id;
    dst->original_cgroup_id = src->original_cgroup_id;
    dst->hctx_idx = src->hctx_idx;
    dst->ioprio = src->ioprio;
    dst->opf = src->opf;
    dst->owner_id = src->owner_id;
    dst->weight = src->weight;
    dst->cost = src->cost;
    dst->flags = src->flags;
    dst->domain = src->domain;
}

static bool sector_less(struct bpf_rb_node *a, const struct bpf_rb_node *b)
{
    struct q_mqdd_node *node_a = container_of(a, struct q_mqdd_node, sector_node);
    struct q_mqdd_node *node_b = container_of(b, struct q_mqdd_node, sector_node);

    if (node_a->desc.sector != node_b->desc.sector)
        return node_a->desc.sector < node_b->desc.sector;
    return node_a->desc.rq_ptr < node_b->desc.rq_ptr;
}

static __always_inline bool gc_has_work(struct q_mqdd_gc *gc)
{
    return gc->stale0 || gc->stale1 || gc->tree ||
           gc->fifo || gc->dispatch;
}

static __always_inline void gc_drop(struct q_mqdd_gc *gc)
{
    if (gc->stale0)
        bpf_obj_drop(gc->stale0);
    if (gc->stale1)
        bpf_obj_drop(gc->stale1);
    if (gc->tree)
        bpf_obj_drop(gc->tree);
    if (gc->fifo)
        bpf_obj_drop(gc->fifo);
    if (gc->dispatch)
        bpf_obj_drop(gc->dispatch);
}

static __always_inline bool lane_bind_idx(__u64 hctx_idx,
                                          __u32 lane_idx,
                                          struct q_mqdd_lane *lane)
{
    lane->state = lane_state_lookup(hctx_idx, lane_idx);
    return lane->state;
}

static __always_inline __u32 lane_idx_from_prio_dir(__u32 prio, __u32 dir)
{
    if (prio == IOSCHED_MQD_PRIO_RT) {
        if (dir == IOSCHED_MQD_DIR_WRITE)
            return Q_MQDD_RT_WRITE;
        return Q_MQDD_RT_READ;
    }

    if (prio == IOSCHED_MQD_PRIO_IDLE) {
        if (dir == IOSCHED_MQD_DIR_WRITE)
            return Q_MQDD_IDLE_WRITE;
        return Q_MQDD_IDLE_READ;
    }

    if (dir == IOSCHED_MQD_DIR_WRITE)
        return Q_MQDD_BE_WRITE;
    return Q_MQDD_BE_READ;
}

static __always_inline bool lane_bind_prio_dir(__u64 hctx_idx,
                                               __u32 prio,
                                               __u32 dir,
                                               struct q_mqdd_lane *lane)
{
    return lane_bind_idx(hctx_idx, lane_idx_from_prio_dir(prio, dir), lane);
}

static __always_inline bool lane_collect_stale_front(struct q_mqdd_lane *lane,
                                                     struct q_mqdd_gc *gc)
{
    struct bpf_list_node *front;
    struct bpf_list_node *owned;
    struct q_mqdd_node *node;
    bool progress = false;

    if (!gc->stale0) {
        front = bpf_list_front(&lane->state->fifo_queue);
        if (!front)
            return progress;

        node = container_of(front, struct q_mqdd_node, fifo_node);
        if (!node->stale)
            return progress;

        owned = bpf_list_pop_front(&lane->state->fifo_queue);
        if (!owned)
            return progress;

        gc->stale0 = container_of(owned, struct q_mqdd_node, fifo_node);
        progress = true;
    }

    if (!gc->stale1) {
        front = bpf_list_front(&lane->state->fifo_queue);
        if (!front)
            return progress;

        node = container_of(front, struct q_mqdd_node, fifo_node);
        if (!node->stale)
            return progress;

        owned = bpf_list_pop_front(&lane->state->fifo_queue);
        if (!owned)
            return progress;

        gc->stale1 = container_of(owned, struct q_mqdd_node, fifo_node);
        progress = true;
    }

    return progress;
}

static __always_inline struct q_mqdd_node *lane_fifo_head(struct q_mqdd_lane *lane,
                                                          struct q_mqdd_gc *gc)
{
    struct bpf_list_node *front;
    struct q_mqdd_node *node;

    if (gc_has_work(gc))
        return NULL;
    lane_collect_stale_front(lane, gc);
    if (gc_has_work(gc))
        return NULL;
    front = bpf_list_front(&lane->state->fifo_queue);
    if (!front)
        return NULL;

    node = container_of(front, struct q_mqdd_node, fifo_node);
    return node->stale ? NULL : node;
}

static __always_inline struct q_mqdd_node *lane_first_ge(struct q_mqdd_lane *lane,
                                                         __u64 sector)
{
    struct bpf_rb_node *rb = bpf_rbtree_root(&lane->state->sector_tree);
    struct q_mqdd_node *best = NULL;
    __u32 i;

    for (i = 0; i < Q_MQDD_TREE_WALK_MAX; i++) {
        struct q_mqdd_node *node;

        if (!rb)
            break;

        node = container_of(rb, struct q_mqdd_node, sector_node);
        if (node->desc.sector >= sector) {
            best = node;
            rb = bpf_rbtree_left(&lane->state->sector_tree, rb);
        } else {
            rb = bpf_rbtree_right(&lane->state->sector_tree, rb);
        }
    }

    return best;
}

static __always_inline struct q_mqdd_node *lane_first_sector(struct q_mqdd_lane *lane)
{
    struct bpf_rb_node *rb = bpf_rbtree_root(&lane->state->sector_tree);
    struct q_mqdd_node *best = NULL;
    __u32 i;

    for (i = 0; i < Q_MQDD_TREE_WALK_MAX; i++) {
        if (!rb)
            break;

        best = container_of(rb, struct q_mqdd_node, sector_node);
        rb = bpf_rbtree_left(&lane->state->sector_tree, rb);
    }

    return best;
}

static __always_inline struct q_mqdd_node *lane_prev_sector(struct q_mqdd_lane *lane,
                                                            __u64 sector)
{
    struct bpf_rb_node *rb = bpf_rbtree_root(&lane->state->sector_tree);
    struct q_mqdd_node *best = NULL;
    __u32 i;

    for (i = 0; i < Q_MQDD_TREE_WALK_MAX; i++) {
        struct q_mqdd_node *node;

        if (!rb)
            break;

        node = container_of(rb, struct q_mqdd_node, sector_node);
        if (node->desc.sector < sector) {
            best = node;
            rb = bpf_rbtree_right(&lane->state->sector_tree, rb);
        } else {
            rb = bpf_rbtree_left(&lane->state->sector_tree, rb);
        }
    }

    return best;
}

static __always_inline struct q_mqdd_node *lane_next_sector(struct q_mqdd_lane *lane,
                                                            __u64 sector)
{
    struct bpf_rb_node *rb = bpf_rbtree_root(&lane->state->sector_tree);
    struct q_mqdd_node *best = NULL;
    __u32 i;

    for (i = 0; i < Q_MQDD_TREE_WALK_MAX; i++) {
        struct q_mqdd_node *node;

        if (!rb)
            break;

        node = container_of(rb, struct q_mqdd_node, sector_node);
        if (node->desc.sector > sector) {
            best = node;
            rb = bpf_rbtree_left(&lane->state->sector_tree, rb);
        } else {
            rb = bpf_rbtree_right(&lane->state->sector_tree, rb);
        }
    }

    return best;
}

static __always_inline __u64 lane_dispatch_fifo(struct q_mqdd_lane *lane,
                                                struct q_mqdd_gc *gc)
{
    struct bpf_list_node *owned;
    struct bpf_rb_node *rb;
    struct q_mqdd_node *node;
    __u64 rq_ptr;

    if (gc->fifo || gc->tree)
        return 0;

    owned = bpf_list_pop_front(&lane->state->fifo_queue);
    if (!owned)
        return 0;

    node = container_of(owned, struct q_mqdd_node, fifo_node);
    gc->fifo = node;
    if (node->stale)
        return 0;

    rq_ptr = node->desc.rq_ptr;
    lane->state->latest_pos = rq_end_sector(&node->desc);
    if (lane->state->count)
        lane->state->count--;

    node->stale = 1;
    rb = bpf_rbtree_remove(&lane->state->sector_tree, &node->sector_node);
    if (rb)
        gc->tree = container_of(rb, struct q_mqdd_node, sector_node);
    return rq_ptr;
}

static __always_inline __u64 lane_dispatch_tree_node(struct q_mqdd_lane *lane,
                                                     struct q_mqdd_node *node,
                                                     struct q_mqdd_gc *gc)
{
    struct bpf_rb_node *rb;
    struct q_mqdd_node *picked;
    __u64 rq_ptr;

    if (!node || gc->tree)
        return 0;

    rb = bpf_rbtree_remove(&lane->state->sector_tree, &node->sector_node);
    if (!rb)
        return 0;

    picked = container_of(rb, struct q_mqdd_node, sector_node);
    gc->tree = picked;
    if (picked->stale)
        return 0;

    rq_ptr = picked->desc.rq_ptr;
    lane->state->latest_pos = rq_end_sector(&picked->desc);
    if (lane->state->count)
        lane->state->count--;

    picked->stale = 1;
    return rq_ptr;
}

static __always_inline __u64 lane_dispatch_c_look(struct q_mqdd_lane *lane,
                                                  __u64 now_ns,
                                                  struct q_mqdd_gc *gc)
{
    struct q_mqdd_node *head;
    struct q_mqdd_node *next;

    lane_collect_stale_front(lane, gc);
    if (gc_has_work(gc))
        return 0;
    if (!lane->state->count)
        return 0;

    head = lane_fifo_head(lane, gc);
    if (gc_has_work(gc))
        return 0;
    if (head && head->desc.deadline_ns <= now_ns)
        return lane_dispatch_fifo(lane, gc);

    next = lane_first_ge(lane, lane->state->latest_pos);
    if (next)
        return lane_dispatch_tree_node(lane, next, gc);

    next = lane_first_sector(lane);
    return lane_dispatch_tree_node(lane, next, gc);
}

static __always_inline __u64 lane_evac_tree(struct q_mqdd_lane *lane,
                                            struct q_mqdd_gc *gc)
{
    struct q_mqdd_node *node;

    lane_collect_stale_front(lane, gc);
    if (gc_has_work(gc))
        return 0;
    if (!lane->state->count || gc->tree)
        return 0;

    node = lane_first_ge(lane, lane->state->latest_pos);
    if (node)
        return lane_dispatch_tree_node(lane, node, gc);

    node = lane_first_sector(lane);
    return lane_dispatch_tree_node(lane, node, gc);
}

static __always_inline bool lane_has_aged_request(struct q_mqdd_lane *lane,
                                                  __u64 now_ns,
                                                  struct q_mqdd_gc *gc)
{
    struct q_mqdd_node *head;
    __u64 latest_start_ns;

    if (now_ns < IOSCHED_MQD_PRIO_AGING_EXPIRE_NS || !lane->state->count)
        return false;

    latest_start_ns = now_ns - IOSCHED_MQD_PRIO_AGING_EXPIRE_NS;
    head = lane_fifo_head(lane, gc);
    if (gc_has_work(gc))
        return false;
    return head && head->desc.arrival_ns <= latest_start_ns;
}

static __always_inline __u64 lane_dispatch_aged(struct q_mqdd_lane *lane,
                                                __u64 now_ns,
                                                struct q_mqdd_gc *gc)
{
    if (!lane_has_aged_request(lane, now_ns, gc))
        return 0;
    if (gc_has_work(gc))
        return 0;
    return lane_dispatch_fifo(lane, gc);
}

static __always_inline int lane_insert(struct q_mqdd_lane *lane,
                                       struct q_mqdd_node *tree_ref,
                                       struct q_mqdd_node *fifo_ref,
                                       struct q_mqdd_gc *gc)
{
    struct bpf_rb_node *rollback;

    lane_collect_stale_front(lane, gc);

    if (bpf_rbtree_add(&lane->state->sector_tree, &tree_ref->sector_node, sector_less)) {
        gc->fifo = fifo_ref;
        return -1;
    }

    if (bpf_list_push_back(&lane->state->fifo_queue, &fifo_ref->fifo_node)) {
        rollback = bpf_rbtree_remove(&lane->state->sector_tree, &tree_ref->sector_node);
        if (rollback)
            gc->tree = container_of(rollback, struct q_mqdd_node, sector_node);
        return -1;
    }

    lane->state->count++;
    return 0;
}

static __always_inline int dispatch_insert(struct q_mqdd_dispatch_state *state,
                                           struct q_mqdd_dispatch_node *node)
{
    if (bpf_list_push_back(&state->dispatch_queue, &node->fifo_node))
        return -1;
    state->dispatch_nr++;
    return 0;
}

static __always_inline __u64 dispatch_pop(struct q_mqdd_dispatch_state *state,
                                          struct q_mqdd_gc *gc)
{
    struct bpf_list_node *owned;
    struct q_mqdd_dispatch_node *node;
    __u64 rq_ptr;

    if (!state->dispatch_nr || gc->dispatch)
        return 0;

    owned = bpf_list_pop_front(&state->dispatch_queue);
    if (!owned)
        return 0;

    node = container_of(owned, struct q_mqdd_dispatch_node, fifo_node);
    rq_ptr = node->desc.rq_ptr;
    if (state->dispatch_nr)
        state->dispatch_nr--;
    gc->dispatch = node;
    return rq_ptr;
}

static __always_inline __u64 lane_dispatch_c_look_locked(struct q_mqdd_lane *lane,
                                                         __u64 now_ns,
                                                         struct q_mqdd_gc *gc)
{
    __u64 rq_ptr;

    bpf_spin_lock(&lane->state->lock);
    rq_ptr = lane_dispatch_c_look(lane, now_ns, gc);
    bpf_spin_unlock(&lane->state->lock);
    return rq_ptr;
}

static __always_inline __u64 lane_dispatch_aged_locked(struct q_mqdd_lane *lane,
                                                       __u64 now_ns,
                                                       struct q_mqdd_gc *gc)
{
    __u64 rq_ptr;

    bpf_spin_lock(&lane->state->lock);
    rq_ptr = lane_dispatch_aged(lane, now_ns, gc);
    bpf_spin_unlock(&lane->state->lock);
    return rq_ptr;
}

static __always_inline int local_insert_node(__u64 hctx_idx,
                                             struct q_mqdd_node *tree_ref,
                                             struct q_mqdd_node *fifo_ref,
                                             struct q_mqdd_gc *gc)
{
    struct q_mqdd_lane lane;
    int ret;

    if (!lane_bind_prio_dir(hctx_idx, tree_ref->prio, tree_ref->dir, &lane)) {
        gc->tree = tree_ref;
        gc->fifo = fifo_ref;
        return -1;
    }

    bpf_spin_lock(&lane.state->lock);
    ret = lane_insert(&lane, tree_ref, fifo_ref, gc);
    bpf_spin_unlock(&lane.state->lock);
    return ret;
}

static __always_inline struct q_mqdd_sel_stats *mqdd_sel_stats(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&sel_stats_map, &key);
}

#define MQDD_SEL_INC(st, field)                         \
    do {                                                \
        if (st)                                         \
            __sync_fetch_and_add(&(st)->field, 1);      \
    } while (0)

static __noinline __u64 local_dispatch_prio(struct q_mqdd_hctx_state *state,
                                            struct q_mqdd_lane *reads,
                                            struct q_mqdd_lane *writes,
                                            __u64 now_ns,
                                            struct q_mqdd_gc *gc)
{
    struct q_mqdd_sel_stats *sst = mqdd_sel_stats();
    __u32 dir = state->last_dir;
    __u32 read_count = reads->state->count;
    __u32 write_count = writes->state->count;
    __u64 rq_ptr;

    MQDD_SEL_INC(sst, prio_passes);
    if (state->batching < IOSCHED_MQD_FIFO_BATCH) {
        if (dir == IOSCHED_MQD_DIR_WRITE) {
            if (write_count) {
                rq_ptr = lane_dispatch_c_look_locked(writes, now_ns, gc);
                if (rq_ptr) {
                    state->batching++;
                    MQDD_SEL_INC(sst, cont_dispatch);
                }
                return rq_ptr;
            }
            MQDD_SEL_INC(sst, cont_empty);
        } else if (read_count) {
            rq_ptr = lane_dispatch_c_look_locked(reads, now_ns, gc);
            if (rq_ptr) {
                state->batching++;
                MQDD_SEL_INC(sst, cont_dispatch);
            }
            return rq_ptr;
        } else {
            MQDD_SEL_INC(sst, cont_empty);
        }
    }

    MQDD_SEL_INC(sst, sel_calls);
    if (sst) {
        sst->sel_last_read_count = read_count;
        sst->sel_last_write_count = write_count;
    }
    if (read_count) {
        if (write_count && state->starved++ >= IOSCHED_MQD_MAX_READ_STARVATION) {
            dir = IOSCHED_MQD_DIR_WRITE;
            MQDD_SEL_INC(sst, sel_flip);
        } else {
            dir = IOSCHED_MQD_DIR_READ;
            if (write_count)
                MQDD_SEL_INC(sst, sel_mixed);
            else
                MQDD_SEL_INC(sst, sel_read_only);
        }
        if (sst)
            sst->starved_value = state->starved;
    } else if (write_count) {
        state->starved = 0;
        dir = IOSCHED_MQD_DIR_WRITE;
        MQDD_SEL_INC(sst, sel_reset);
    } else {
        MQDD_SEL_INC(sst, sel_empty);
        return 0;
    }

    state->last_dir = dir;
    state->batching = 0;
    if (dir == IOSCHED_MQD_DIR_WRITE)
        rq_ptr = lane_dispatch_c_look_locked(writes, now_ns, gc);
    else
        rq_ptr = lane_dispatch_c_look_locked(reads, now_ns, gc);
    if (rq_ptr)
        state->batching++;
    return rq_ptr;
}

static __noinline __u64 local_dispatch_prio_aged(struct q_mqdd_hctx_state *state,
                                                 struct q_mqdd_lane *reads,
                                                 struct q_mqdd_lane *writes,
                                                  __u64 now_ns,
                                                  struct q_mqdd_gc *gc)
{
    bool read_aged;
    bool write_aged;
    __u32 dir = state->last_dir;
    __u64 rq_ptr;

    bpf_spin_lock(&reads->state->lock);
    read_aged = lane_has_aged_request(reads, now_ns, gc);
    bpf_spin_unlock(&reads->state->lock);
    if (gc_has_work(gc))
        return 0;
    bpf_spin_lock(&writes->state->lock);
    write_aged = lane_has_aged_request(writes, now_ns, gc);
    bpf_spin_unlock(&writes->state->lock);
    if (gc_has_work(gc))
        return 0;

    if (state->batching < IOSCHED_MQD_FIFO_BATCH) {
        if (dir == IOSCHED_MQD_DIR_WRITE) {
            if (write_aged) {
                rq_ptr = lane_dispatch_aged_locked(writes, now_ns, gc);
                if (rq_ptr)
                    state->batching++;
                return rq_ptr;
            }
        } else if (read_aged) {
            rq_ptr = lane_dispatch_aged_locked(reads, now_ns, gc);
            if (rq_ptr)
                state->batching++;
            return rq_ptr;
        }
    }

    if (read_aged) {
        if (write_aged && state->starved++ >= IOSCHED_MQD_MAX_READ_STARVATION)
            dir = IOSCHED_MQD_DIR_WRITE;
        else
            dir = IOSCHED_MQD_DIR_READ;
    } else if (write_aged) {
        state->starved = 0;
        dir = IOSCHED_MQD_DIR_WRITE;
    } else {
        return 0;
    }

    state->last_dir = dir;
    state->batching = 0;
    if (dir == IOSCHED_MQD_DIR_WRITE)
        rq_ptr = lane_dispatch_aged_locked(writes, now_ns, gc);
    else
        rq_ptr = lane_dispatch_aged_locked(reads, now_ns, gc);
    if (rq_ptr)
        state->batching++;
    return rq_ptr;
}

static __always_inline __u32 lane_count_by_idx(__u64 hctx_idx, __u32 lane_idx)
{
    struct q_mqdd_lane_state *state = lane_state_lookup(hctx_idx, lane_idx);

    return state ? state->count : 0;
}

#if IOEXT_DEBUG
static __always_inline __u32 q_mqdd_debug_pending_total(__u64 hctx_idx)
{
    return lane_count_by_idx(hctx_idx, Q_MQDD_RT_READ) +
           lane_count_by_idx(hctx_idx, Q_MQDD_RT_WRITE) +
           lane_count_by_idx(hctx_idx, Q_MQDD_BE_READ) +
           lane_count_by_idx(hctx_idx, Q_MQDD_BE_WRITE) +
           lane_count_by_idx(hctx_idx, Q_MQDD_IDLE_READ) +
           lane_count_by_idx(hctx_idx, Q_MQDD_IDLE_WRITE);
}

static __always_inline __u32 q_mqdd_debug_dispatch_nr(__u64 hctx_idx)
{
    struct q_mqdd_dispatch_state *dispatch = dispatch_lookup(hctx_idx);

    return dispatch ? dispatch->dispatch_nr : 0;
}
#endif

static __noinline __u64 local_dispatch(__u64 hctx_idx,
                                       struct q_mqdd_hctx_state *state,
                                       struct q_mqdd_dispatch_state *dispatch,
                                       __u64 now_ns,
                                       struct q_mqdd_gc *gc)
{
    struct q_mqdd_lane rt_reads;
    struct q_mqdd_lane rt_writes;
    struct q_mqdd_lane reads;
    struct q_mqdd_lane writes;
    struct q_mqdd_lane idle_reads;
    struct q_mqdd_lane idle_writes;
    __u64 rq_ptr;

    if (!lane_bind_idx(hctx_idx, Q_MQDD_RT_READ, &rt_reads) ||
        !lane_bind_idx(hctx_idx, Q_MQDD_RT_WRITE, &rt_writes) ||
        !lane_bind_idx(hctx_idx, Q_MQDD_BE_READ, &reads) ||
        !lane_bind_idx(hctx_idx, Q_MQDD_BE_WRITE, &writes) ||
        !lane_bind_idx(hctx_idx, Q_MQDD_IDLE_READ, &idle_reads) ||
        !lane_bind_idx(hctx_idx, Q_MQDD_IDLE_WRITE, &idle_writes))
        return 0;

    bool rt_has = lane_count_by_idx(hctx_idx, Q_MQDD_RT_READ) ||
                  lane_count_by_idx(hctx_idx, Q_MQDD_RT_WRITE);
    bool be_has = lane_count_by_idx(hctx_idx, Q_MQDD_BE_READ) ||
                  lane_count_by_idx(hctx_idx, Q_MQDD_BE_WRITE);
    bool idle_has = lane_count_by_idx(hctx_idx, Q_MQDD_IDLE_READ) ||
                    lane_count_by_idx(hctx_idx, Q_MQDD_IDLE_WRITE);

    bpf_spin_lock(&dispatch->lock);
    rq_ptr = dispatch_pop(dispatch, gc);
    bpf_spin_unlock(&dispatch->lock);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;

    if ((rt_has + be_has + idle_has) > 1) {
        rq_ptr = local_dispatch_prio_aged(state,
                                          &reads,
                                          &writes,
                                          now_ns,
                                          gc);
        if (rq_ptr || gc_has_work(gc))
            return rq_ptr;
        rq_ptr = local_dispatch_prio_aged(state,
                                          &idle_reads,
                                          &idle_writes,
                                          now_ns,
                                          gc);
        if (rq_ptr || gc_has_work(gc))
            return rq_ptr;
    }

    rq_ptr = local_dispatch_prio(state,
                                 &rt_reads,
                                 &rt_writes,
                                 now_ns,
                                 gc);
    if (rq_ptr || gc_has_work(gc) || rt_has)
        return rq_ptr;
    rq_ptr = local_dispatch_prio(state,
                                 &reads,
                                 &writes,
                                 now_ns,
                                 gc);
    if (rq_ptr || gc_has_work(gc) || be_has)
        return rq_ptr;
    rq_ptr = local_dispatch_prio(state,
                                 &idle_reads,
                                 &idle_writes,
                                 now_ns,
                                 gc);
    if (rq_ptr || gc_has_work(gc) || idle_has)
        return rq_ptr;
    return 0;
}

static __always_inline __u64 local_evacuate_lane(__u64 hctx_idx,
                                                 __u32 lane_idx,
                                                 struct q_mqdd_gc *gc)
{
    struct q_mqdd_lane lane;
    __u64 rq_ptr;

    if (!lane_bind_idx(hctx_idx, lane_idx, &lane))
        return 0;

    bpf_spin_lock(&lane.state->lock);
    rq_ptr = lane_evac_tree(&lane, gc);
    bpf_spin_unlock(&lane.state->lock);
    return rq_ptr;
}

static __noinline __u64 local_evacuate(__u64 hctx_idx,
                                       struct q_mqdd_dispatch_state *dispatch,
                                       struct q_mqdd_gc *gc)
{
    __u64 rq_ptr;

    bpf_spin_lock(&dispatch->lock);
    rq_ptr = dispatch_pop(dispatch, gc);
    bpf_spin_unlock(&dispatch->lock);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;

    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_RT_READ, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_RT_WRITE, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_BE_READ, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_BE_WRITE, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_IDLE_READ, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    rq_ptr = local_evacuate_lane(hctx_idx, Q_MQDD_IDLE_WRITE, gc);
    if (rq_ptr || gc_has_work(gc))
        return rq_ptr;
    return 0;
}

static __always_inline __u64 lane_former(struct q_mqdd_lane *lane, __u64 sector)
{
    struct q_mqdd_node *node = lane_prev_sector(lane, sector);

    return node && !node->stale ? node->desc.rq_ptr : 0;
}

static __always_inline __u64 lane_next(struct q_mqdd_lane *lane, __u64 sector)
{
    struct q_mqdd_node *node = lane_next_sector(lane, sector);

    return node && !node->stale ? node->desc.rq_ptr : 0;
}

static __always_inline int init_lane_state(__u64 hctx_idx, __u32 lane_idx)
{
    struct q_mqdd_lane_state *state = lane_state_lookup(hctx_idx, lane_idx);

    if (!state)
        return -1;

    bpf_spin_lock(&state->lock);
    state->latest_pos = 0;
    state->count = 0;
    bpf_spin_unlock(&state->lock);
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

SEC("struct_ops/init_hctx")
int BPF_PROG(init_hctx, struct iosched_hctx_ctx *hctx)
{
    struct q_mqdd_dispatch_state *dispatch;
    struct q_mqdd_hctx_state *state;

    if (!hctx || !hctx->q || !hctx->hctx)
        return -1;

    dispatch = dispatch_lookup(hctx->hctx_idx);
    state = hctx_lookup(hctx->hctx_idx);
    if (!dispatch || !state)
        return -1;

    bpf_spin_lock(&dispatch->lock);
    dispatch->dispatch_nr = 0;
    bpf_spin_unlock(&dispatch->lock);

    state->async_depth = 0;
    state->batching = 0;
    state->starved = 0;
    state->last_dir = IOSCHED_MQD_DIR_READ;

    if (init_lane_state(hctx->hctx_idx, Q_MQDD_RT_READ) ||
        init_lane_state(hctx->hctx_idx, Q_MQDD_RT_WRITE) ||
        init_lane_state(hctx->hctx_idx, Q_MQDD_BE_READ) ||
        init_lane_state(hctx->hctx_idx, Q_MQDD_BE_WRITE) ||
        init_lane_state(hctx->hctx_idx, Q_MQDD_IDLE_READ) ||
        init_lane_state(hctx->hctx_idx, Q_MQDD_IDLE_WRITE))
        return -1;

    return 0;
}

SEC("struct_ops/exit_hctx")
void BPF_PROG(exit_hctx, struct iosched_hctx_ctx *hctx)
{
    if (!hctx || !hctx->q || !hctx->hctx)
        return;
}

SEC("struct_ops/limit_depth")
__u64 BPF_PROG(limit_depth, struct iosched_limit_depth_ctx *dctx)
{
    __u64 op;
    struct q_mqdd_runtime_cfg *cfg;

    if (!dctx || !dctx->data)
        return 0;

    op = dctx->opf & IOEXT_REQ_OP_MASK;
    cfg = cfg_lookup();
    return op == REQ_OP_READ && (dctx->opf & IOEXT_REQ_SYNC) ?
           0 : (cfg ? cfg->async_depth : 0);
}

SEC("struct_ops/depth_updated")
void BPF_PROG(depth_updated, struct iosched_depth_updated_ctx *dctx)
{
    struct q_mqdd_runtime_cfg *cfg;

    if (!dctx || !dctx->q)
        return;

    cfg = cfg_lookup();
    if (cfg)
        cfg->async_depth = dctx->async_depth;
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    struct iosched_request_desc desc;
    struct q_mqdd_gc gc = {};
    struct q_mqdd_sel_stats *sst = mqdd_sel_stats();
    int ret;

    if (!ictx || !ictx->rq)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        MQDD_SEL_INC(sst, insert_defer);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    desc = iosched_build_desc(ictx);
    desc.deadline_ns = desc.arrival_ns + expire_ns(is_write_op(desc.opf));

    if (desc.insert_flags & 0x1) {
        struct q_mqdd_dispatch_state *dispatch;
        struct q_mqdd_dispatch_node *node;

        dispatch = dispatch_lookup(ictx->hctx_idx);
        if (!dispatch) {
#if IOEXT_DEBUG
            __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
            bpf_printk("q_mqdd insert dispatch miss seq=%llu hctx=%llu cgid=%llu",
                       seq, ictx->hctx_idx, ictx->cgroup_id);
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }

        node = bpf_obj_new(typeof(*node));
        if (!node) {
#if IOEXT_DEBUG
            __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
            bpf_printk("q_mqdd insert dispatch alloc seq=%llu hctx=%llu cgid=%llu",
                       seq, ictx->hctx_idx, ictx->cgroup_id);
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }
        q_mqdd_copy_desc(&node->desc, &desc);

        iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
        bpf_spin_lock(&dispatch->lock);
        ret = dispatch_insert(dispatch, node);
        bpf_spin_unlock(&dispatch->lock);

        if (ret) {
            iosched_policy_meta_cancel_insert(&policy_meta, ictx->cgroup_id);
#if IOEXT_DEBUG
            {
                __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
                bpf_printk("q_mqdd insert dispatch reject seq=%llu hctx=%llu cgid=%llu",
                           seq, ictx->hctx_idx, ictx->cgroup_id);
            }
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }

#if IOEXT_DEBUG
        {
            __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_ACCEPT);
            bpf_printk("q_mqdd insert dispatch accept seq=%llu hctx=%llu cgid=%llu",
                       seq, ictx->hctx_idx, ictx->cgroup_id);
        }
#endif
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        MQDD_SEL_INC(sst, insert_accept);
        return IOSCHED_INSERT_ACCEPT;
    }

    {
        struct q_mqdd_node *tree_ref;
        struct q_mqdd_node *fifo_ref;
        __u32 dir = is_write_op(desc.opf) ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ;
        __u32 prio = iosched_mqd_prio_from_ioprio(desc.ioprio);

        if (sst)
            sst->last_insert_ioprio = desc.ioprio;
        if (prio == IOSCHED_MQD_PRIO_BE) {
            if (dir == IOSCHED_MQD_DIR_WRITE)
                MQDD_SEL_INC(sst, insert_be_write);
            else
                MQDD_SEL_INC(sst, insert_be_read);
        } else {
            MQDD_SEL_INC(sst, insert_other_lane);
        }

        tree_ref = bpf_obj_new(typeof(*tree_ref));
        if (!tree_ref) {
#if IOEXT_DEBUG
            __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
            bpf_printk("q_mqdd insert alloc seq=%llu hctx=%llu cgid=%llu",
                       seq, ictx->hctx_idx, ictx->cgroup_id);
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }

        q_mqdd_copy_desc(&tree_ref->desc, &desc);
        tree_ref->stale = 0;
        tree_ref->prio = prio;
        tree_ref->dir = dir;
        fifo_ref = bpf_refcount_acquire(tree_ref);
        if (!fifo_ref) {
            bpf_obj_drop(tree_ref);
#if IOEXT_DEBUG
            {
                __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
                bpf_printk("q_mqdd insert ref reject seq=%llu hctx=%llu cgid=%llu",
                           seq, ictx->hctx_idx, ictx->cgroup_id);
            }
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }

        iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
        ret = local_insert_node(ictx->hctx_idx,
                                tree_ref,
                                fifo_ref,
                                &gc);

        gc_drop(&gc);

        if (ret) {
            iosched_policy_meta_cancel_insert(&policy_meta, ictx->cgroup_id);
#if IOEXT_DEBUG
            {
                __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_REJECT);
                bpf_printk("q_mqdd insert lane reject seq=%llu hctx=%llu cgid=%llu",
                           seq, ictx->hctx_idx, ictx->cgroup_id);
            }
#endif
            iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
            return -1;
        }
    }

#if IOEXT_DEBUG
    {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_INSERT_ACCEPT);
        bpf_printk("q_mqdd insert accept seq=%llu hctx=%llu cgid=%llu",
                   seq, ictx->hctx_idx, ictx->cgroup_id);
    }
#endif
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    MQDD_SEL_INC(sst, insert_accept);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    struct q_mqdd_merge_stats *st;
    __u32 mode;
    bool allow = false;

    st = mqdd_merge_stats();
    mqdd_merge_stat_inc(&st->allow_merge_calls);

    mode = mqdd_merge_mode();
    if (mode == MQDD_MERGE_MODE_D0) {
        allow = false;
    } else {
        allow = mqdd_merge_compatible(mctx);
    }

    if (allow)
        mqdd_merge_stat_inc(&st->allow_merge_true);
    else
        mqdd_merge_stat_inc(&st->allow_merge_false);
    return allow;
}

SEC("struct_ops/bio_merge")
bool BPF_PROG(bio_merge, struct iosched_bio_merge_ctx *bctx)
{
    struct q_mqdd_merge_stats *st;
    __u32 mode;

    st = mqdd_merge_stats();
    mqdd_merge_stat_inc(&st->bio_merge_calls);

    mode = mqdd_merge_mode();
    if (mode >= MQDD_MERGE_MODE_D3) {
        mqdd_merge_stat_inc(&st->bio_merge_true);
        return true;
    }
    return false;
}

SEC("struct_ops/request_merged")
void BPF_PROG(request_merged, struct iosched_request_merged_ctx *mctx)
{
    struct q_mqdd_merge_stats *st;

    st = mqdd_merge_stats();
    mqdd_merge_stat_inc(&st->request_merged_calls);
}

SEC("struct_ops/former_request")
__u64 BPF_PROG(former_request, struct iosched_rq_ctx *rctx)
{
    struct q_mqdd_lane lane;
    __u32 dir;
    __u32 prio;
    __u64 rq_ptr;

    if (!rctx || !rctx->rq)
        return 0;

    dir = is_write_op(rctx->opf) ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ;
    prio = iosched_mqd_prio_from_ioprio(rctx->ioprio);
    if (!lane_bind_prio_dir(rctx->hctx_idx, prio, dir, &lane))
        return 0;

    bpf_spin_lock(&lane.state->lock);
    rq_ptr = lane_former(&lane, rctx->sector);
    bpf_spin_unlock(&lane.state->lock);
    return rq_ptr;
}

SEC("struct_ops/next_request")
__u64 BPF_PROG(next_request, struct iosched_rq_ctx *rctx)
{
    struct q_mqdd_lane lane;
    __u32 dir;
    __u32 prio;
    __u64 rq_ptr;

    if (!rctx || !rctx->rq)
        return 0;

    dir = is_write_op(rctx->opf) ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ;
    prio = iosched_mqd_prio_from_ioprio(rctx->ioprio);
    if (!lane_bind_prio_dir(rctx->hctx_idx, prio, dir, &lane))
        return 0;

    bpf_spin_lock(&lane.state->lock);
    rq_ptr = lane_next(&lane, rctx->sector);
    bpf_spin_unlock(&lane.state->lock);
    return rq_ptr;
}

static __always_inline __u64 state_dispatch(__u64 hctx_idx, __u64 cgroup_id, __u64 now_ns)
{
    struct q_mqdd_dispatch_state *dispatch;
    struct q_mqdd_hctx_state *state;
    struct q_mqdd_gc gc = {};
    __u64 rq_ptr;
#if IOEXT_DEBUG
    bool had_gc;
    __u32 pending;
    __u32 dispatch_nr;
#endif

    dispatch = dispatch_lookup(hctx_idx);
    state = hctx_lookup(hctx_idx);
    if (!dispatch || !state) {
#if IOEXT_DEBUG
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_DISPATCH_STATE_MISS);
        bpf_printk("q_mqdd dispatch miss seq=%llu hctx=%llu cgid=%llu",
                   seq, hctx_idx, cgroup_id);
#endif
        return 0;
    }

    rq_ptr = local_dispatch(hctx_idx, state, dispatch, now_ns, &gc);

#if IOEXT_DEBUG
    had_gc = gc_has_work(&gc);
    pending = q_mqdd_debug_pending_total(hctx_idx);
    dispatch_nr = q_mqdd_debug_dispatch_nr(hctx_idx);
#endif
    gc_drop(&gc);
    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
#if IOEXT_DEBUG
    if (rq_ptr) {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_DISPATCH_HIT);
        bpf_printk("q_mqdd dispatch hit seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd dispatch rq=%llu cgid=%llu dispatch_nr=%u",
                   rq_ptr, cgroup_id, dispatch_nr);
    } else if (had_gc) {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_GC_DISPATCH);
        bpf_printk("q_mqdd dispatch gc seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd dispatch gc cgid=%llu dispatch_nr=%u",
                   cgroup_id, dispatch_nr);
    } else {
        __u32 event = pending || dispatch_nr ?
            Q_MQDD_DBG_DISPATCH_BLOCKED : Q_MQDD_DBG_DISPATCH_ZERO;
        __u64 seq = q_mqdd_debug_inc(event);
        bpf_printk("q_mqdd dispatch zero seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd dispatch zero cgid=%llu dispatch_nr=%u",
                   cgroup_id, dispatch_nr);
    }
#endif
    return rq_ptr;
}

static __always_inline __u64 state_evacuate(__u64 hctx_idx, __u64 cgroup_id)
{
    struct q_mqdd_dispatch_state *dispatch;
    struct q_mqdd_gc gc = {};
    __u64 rq_ptr;
#if IOEXT_DEBUG
    bool had_gc;
    __u32 pending;
    __u32 dispatch_nr;
#endif

    dispatch = dispatch_lookup(hctx_idx);
    if (!dispatch) {
#if IOEXT_DEBUG
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_EVACUATE_STATE_MISS);
        bpf_printk("q_mqdd evacuate miss seq=%llu hctx=%llu cgid=%llu",
                   seq, hctx_idx, cgroup_id);
#endif
        return 0;
    }

    rq_ptr = local_evacuate(hctx_idx, dispatch, &gc);

#if IOEXT_DEBUG
    had_gc = gc_has_work(&gc);
    pending = q_mqdd_debug_pending_total(hctx_idx);
    dispatch_nr = q_mqdd_debug_dispatch_nr(hctx_idx);
#endif
    gc_drop(&gc);
    if (rq_ptr)
        iosched_policy_meta_note_dispatch(&policy_meta, cgroup_id);
#if IOEXT_DEBUG
    if (rq_ptr) {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_EVACUATE_HIT);
        bpf_printk("q_mqdd evacuate hit seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd evacuate rq=%llu cgid=%llu dispatch_nr=%u",
                   rq_ptr, cgroup_id, dispatch_nr);
    } else if (had_gc) {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_GC_EVACUATE);
        bpf_printk("q_mqdd evacuate gc seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd evacuate gc cgid=%llu dispatch_nr=%u",
                   cgroup_id, dispatch_nr);
    } else {
        __u32 event = pending || dispatch_nr ?
            Q_MQDD_DBG_EVACUATE_BLOCKED : Q_MQDD_DBG_EVACUATE_ZERO;
        __u64 seq = q_mqdd_debug_inc(event);
        bpf_printk("q_mqdd evacuate zero seq=%llu hctx=%llu pending=%u",
                   seq, hctx_idx, pending);
        bpf_printk("q_mqdd evacuate zero cgid=%llu dispatch_nr=%u",
                   cgroup_id, dispatch_nr);
    }
#endif
    return rq_ptr;
}

SEC("struct_ops/prepare_request")
void BPF_PROG(prepare_request, struct iosched_rq_ctx *rctx)
{
    if (!rctx || !rctx->rq)
        return;

#if IOEXT_DEBUG
    {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_PREPARE);
        bpf_printk("q_mqdd prepare seq=%llu hctx=%llu opf=%llu",
                   seq, rctx->hctx_idx, rctx->opf);
    }
#endif
}

SEC("struct_ops/finish_request")
void BPF_PROG(finish_request, struct iosched_rq_ctx *rctx)
{
    if (!rctx || !rctx->rq)
        return;

#if IOEXT_DEBUG
    {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_FINISH);
        bpf_printk("q_mqdd finish seq=%llu hctx=%llu opf=%llu",
                   seq, rctx->hctx_idx, rctx->opf);
    }
#endif
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *rctx)
{
    if (!rctx || !rctx->rq)
        return;

#if IOEXT_DEBUG
    {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_COMPLETE);
        bpf_printk("q_mqdd complete seq=%llu hctx=%llu cgid=%llu",
                   seq, rctx->hctx_idx, rctx->cgroup_id);
    }
#endif
}

SEC("struct_ops/requeue_request")
void BPF_PROG(requeue_request, struct iosched_rq_ctx *rctx)
{
    if (!rctx || !rctx->rq)
        return;

#if IOEXT_DEBUG
    {
        __u64 seq = q_mqdd_debug_inc(Q_MQDD_DBG_REQUEUE);
        bpf_printk("q_mqdd requeue seq=%llu hctx=%llu opf=%llu",
                   seq, rctx->hctx_idx, rctx->opf);
    }
#endif
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_dispatch(dctx->hctx_idx, dctx->cgroup_id, bpf_ktime_get_ns());
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    if (!dctx)
        return 0;

    return state_evacuate(dctx->hctx_idx, dctx->cgroup_id);
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_mq_deadline = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .init_hctx = (void *)init_hctx,
    .exit_hctx = (void *)exit_hctx,
    .allow_merge = (void *)allow_merge,
    .bio_merge = (void *)bio_merge,
    .former_request = (void *)former_request,
    .next_request = (void *)next_request,
    .request_merged = (void *)request_merged,
    .limit_depth = (void *)limit_depth,
    .depth_updated = (void *)depth_updated,
    .prepare_request = (void *)prepare_request,
    .finish_request = (void *)finish_request,
    .completed_request = (void *)completed_request,
    .requeue_request = (void *)requeue_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .name = "q_mq_deadline",
};
