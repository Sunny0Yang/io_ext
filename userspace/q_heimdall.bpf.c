#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_HEIMDALL_MAX_HCTX 64U
#define Q_HEIMDALL_DEFAULT_MAX_HOLD_NS IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS
#define Q_HEIMDALL_LAT_EWMA_SHIFT 3U
#define Q_HEIMDALL_MODE_MODEL_DISABLED 0U
#define Q_HEIMDALL_MODE_MODEL_ACCEPT 1U
#define Q_HEIMDALL_MODE_FAIL_OPEN 2U
#define Q_HEIMDALL_MODE_HELD 3U
#define Q_HEIMDALL_MODEL_SCAN_LIMIT 4U

#define q_heimdall_barrier_var(var) \
    asm volatile("" : "+r"(var) :: "memory")

#if IOEXT_DEBUG
#define Q_HEIMDALL_DBG_SAMPLED(period, fmt, ...) \
    IOEXT_BPF_DBG_SAMPLED(period, "ioext: " fmt, ##__VA_ARGS__)
#else
#define Q_HEIMDALL_DBG_SAMPLED(period, fmt, ...) do { } while (0)
#endif

struct q_heimdall_local_state {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
    __u32 reserved;
};

struct q_heimdall_state_entry {
    struct bpf_spin_lock lock;
    struct q_heimdall_local_state state;
};

struct q_heimdall_stats_entry {
    struct bpf_spin_lock lock;
    struct iosched_heimdall_q_stats stats;
};

struct q_heimdall_feature_snapshot {
    __u64 queued;
    __u64 latency_history_ns[3];
    __u64 throughput_history_bps[3];
};

struct q_heimdall_model_ctx {
    struct iosched_heimdall_model_config *cfg;
    struct iosched_heimdall_norm_vector *minv;
    struct iosched_heimdall_norm_vector *scalev;
    struct iosched_heimdall_w0_vector *w0;
    struct iosched_heimdall_b0_vector *b0;
    struct iosched_heimdall_w1_vector *w1;
    struct iosched_heimdall_b1_vector *b1;
    struct q_heimdall_feature_snapshot snap;
    __u32 shift;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_HEIMDALL_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct q_heimdall_state_entry);
} state_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_model_config);
} heimdall_model_cfg SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_norm_vector);
} heimdall_norm_min SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_norm_vector);
} heimdall_norm_scale SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_w0_vector);
} heimdall_w0 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_b0_vector);
} heimdall_b0 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_w1_vector);
} heimdall_w1 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_b1_vector);
} heimdall_b1 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_heimdall_stats_entry);
} heimdall_q_stats SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_heimdall_state_entry *state_lookup(__u64 hctx_idx)
{
    __u32 key;

    if (hctx_idx >= Q_HEIMDALL_MAX_HCTX)
        return 0;

    key = (__u32)hctx_idx;
    return bpf_map_lookup_elem(&state_map, &key);
}

static __always_inline struct q_heimdall_stats_entry *stats_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&heimdall_q_stats, &key);
}

static __always_inline struct iosched_heimdall_model_config *model_cfg_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&heimdall_model_cfg, &key);
}

static __always_inline __u64 q_heimdall_max_hold_ns(void)
{
    struct iosched_heimdall_model_config *cfg;

    cfg = model_cfg_lookup();
    if (cfg && cfg->max_hold_us)
        return (__u64)cfg->max_hold_us * 1000ULL;

    return Q_HEIMDALL_DEFAULT_MAX_HOLD_NS;
}

static __always_inline void q_heimdall_snapshot_stats(struct q_heimdall_feature_snapshot *snap)
{
    struct q_heimdall_stats_entry *entry;

    __builtin_memset(snap, 0, sizeof(*snap));
    entry = stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    if (entry->stats.enqueued >= entry->stats.dispatched)
        snap->queued = entry->stats.enqueued - entry->stats.dispatched;
    snap->latency_history_ns[0] = entry->stats.latency_history_ns[0];
    snap->latency_history_ns[1] = entry->stats.latency_history_ns[1];
    snap->latency_history_ns[2] = entry->stats.latency_history_ns[2];
    snap->throughput_history_bps[0] = entry->stats.throughput_history_bps[0];
    snap->throughput_history_bps[1] = entry->stats.throughput_history_bps[1];
    snap->throughput_history_bps[2] = entry->stats.throughput_history_bps[2];
    bpf_spin_unlock(&entry->lock);
}

static __always_inline bool q_heimdall_prepare_model_ctx(struct q_heimdall_model_ctx *ctx)
{
    __u32 key = 0;

    __builtin_memset(ctx, 0, sizeof(*ctx));
#if IOEXT_DEBUG
    /*
     * The debug build validates root q-layer takeover and the default
     * model-disabled FIFO safety path. Keeping inference disabled here avoids
     * combining bpf_printk instrumentation with the verifier-expensive fixed
     * point model path. Non-debug builds still exercise the full model path.
    */
    return false;
#endif
    ctx->cfg = model_cfg_lookup();
    ctx->minv = bpf_map_lookup_elem(&heimdall_norm_min, &key);
    ctx->scalev = bpf_map_lookup_elem(&heimdall_norm_scale, &key);
    ctx->w0 = bpf_map_lookup_elem(&heimdall_w0, &key);
    ctx->b0 = bpf_map_lookup_elem(&heimdall_b0, &key);
    ctx->w1 = bpf_map_lookup_elem(&heimdall_w1, &key);
    ctx->b1 = bpf_map_lookup_elem(&heimdall_b1, &key);

    if (!ctx->cfg || !ctx->minv || !ctx->scalev || !ctx->w0 || !ctx->b0 ||
        !ctx->w1 || !ctx->b1)
        return false;
    if (!(ctx->cfg->flags & IOSCHED_HEIMDALL_MODEL_F_ENABLE))
        return false;
    if (!(ctx->cfg->flags & IOSCHED_HEIMDALL_MODEL_F_WEIGHTS_READY))
        return false;
    if (ctx->cfg->feature_count != IOSCHED_HEIMDALL_FEATURES)
        return false;
    if (ctx->cfg->hidden_count != IOSCHED_HEIMDALL_HIDDEN)
        return false;

    ctx->shift = ctx->cfg->score_shift ?
        ctx->cfg->score_shift : IOSCHED_HEIMDALL_SCORE_SHIFT;
    if (ctx->shift > 30)
        ctx->shift = IOSCHED_HEIMDALL_SCORE_SHIFT;
    q_heimdall_snapshot_stats(&ctx->snap);

    return true;
}

static __always_inline int q_heimdall_insert(struct q_heimdall_local_state *state,
                                             const struct iosched_request_desc *req)
{
    return iosched_push_tail_32(state->pending, &state->pending_nr, req);
}

static __always_inline void q_heimdall_note_enqueue(void)
{
    struct q_heimdall_stats_entry *entry;

    entry = stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    entry->stats.enqueued++;
    bpf_spin_unlock(&entry->lock);
}

static __always_inline void q_heimdall_note_dispatch(__u32 mode)
{
    struct q_heimdall_stats_entry *entry;

    entry = stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    entry->stats.dispatched++;
    if (mode == 0)
        entry->stats.model_disabled_dispatches++;
    else if (mode == 1)
        entry->stats.model_accept_dispatches++;
    else if (mode == 2)
        entry->stats.fail_open_dispatches++;
    else if (mode == 3)
        entry->stats.held_dispatches++;
    bpf_spin_unlock(&entry->lock);
}

static __always_inline void q_heimdall_build_features(const struct iosched_request_desc *req,
                                                      const struct q_heimdall_feature_snapshot *snap,
                                                      __s64 features[IOSCHED_HEIMDALL_FEATURES])
{
    features[0] = iosched_req_is_write(req) ? 1 : 0;
    features[1] = (__s64)req->cost * 512LL;
    features[2] = (__s64)snap->queued;
    features[3] = (__s64)snap->queued;
    features[4] = (__s64)snap->queued;
    features[5] = (__s64)snap->queued;
    features[6] = (__s64)snap->latency_history_ns[0];
    features[7] = (__s64)snap->latency_history_ns[1];
    features[8] = (__s64)snap->latency_history_ns[2];
    features[9] = (__s64)snap->throughput_history_bps[0];
    features[10] = (__s64)snap->throughput_history_bps[1];
    features[11] = (__s64)snap->throughput_history_bps[2];
}

static __always_inline __s64 q_heimdall_norm_feature(__u32 idx,
                                                     __s64 value,
                                                     struct iosched_heimdall_norm_vector *minv,
                                                     struct iosched_heimdall_norm_vector *scalev)
{
    __s64 scale;

    scale = scalev->values[idx];
    return (value - minv->values[idx]) * scale;
}

static __always_inline bool q_heimdall_candidate_slow(const struct iosched_request_desc *req,
                                                      const struct q_heimdall_model_ctx *ctx)
{
    __s64 features[IOSCHED_HEIMDALL_FEATURES];
    __s64 hidden[IOSCHED_HEIMDALL_HIDDEN];
    __s64 score;
    __u32 h;
    __u32 f;

    q_heimdall_build_features(req, &ctx->snap, features);

    for (h = 0; h < IOSCHED_HEIMDALL_HIDDEN; h++) {
        __s64 acc = ctx->b0->values[h];

        for (f = 0; f < IOSCHED_HEIMDALL_FEATURES; f++) {
            __u32 off = h * IOSCHED_HEIMDALL_FEATURES + f;
            __s64 norm = q_heimdall_norm_feature(f,
                                                 features[f],
                                                 ctx->minv,
                                                 ctx->scalev);

            acc += (norm * (__s64)ctx->w0->values[off]) >> ctx->shift;
        }

        hidden[h] = acc > 0 ? acc : 0;
    }

    score = ctx->b1->value;
    for (h = 0; h < IOSCHED_HEIMDALL_HIDDEN; h++)
        score += (hidden[h] * (__s64)ctx->w1->values[h]) >> ctx->shift;

    return score >= ctx->cfg->threshold;
}

static __always_inline __u64 q_heimdall_dispatch_locked(struct q_heimdall_local_state *state,
                                                         __u64 now,
                                                         bool use_model,
                                                         __u64 max_hold_ns,
                                                         const struct q_heimdall_model_ctx *model,
                                                         __u32 *mode)
{
    __u32 oldest_idx = 0;
    __u32 scan_limit;
    bool saw_slow = false;
    __u32 i;

    if (!state->pending_nr)
        return 0;

    if (!use_model) {
        *mode = Q_HEIMDALL_MODE_MODEL_DISABLED;
        return iosched_pop_head_32(state->pending, &state->pending_nr);
    }

    scan_limit = state->pending_nr;
    if (scan_limit > Q_HEIMDALL_MODEL_SCAN_LIMIT)
        scan_limit = Q_HEIMDALL_MODEL_SCAN_LIMIT;

    for (i = 0; i < Q_HEIMDALL_MODEL_SCAN_LIMIT; i++) {
        struct iosched_request_desc *req;

        if (i >= scan_limit)
            break;

        req = &state->pending[i];
        if (req->arrival_ns < state->pending[oldest_idx].arrival_ns)
            oldest_idx = i;

        if (q_heimdall_candidate_slow(req, model)) {
            saw_slow = true;
            continue;
        }

        *mode = Q_HEIMDALL_MODE_MODEL_ACCEPT;
        return iosched_pop_index_32(state->pending, &state->pending_nr, i);
    }

    if (saw_slow && now >= state->pending[oldest_idx].arrival_ns &&
        now - state->pending[oldest_idx].arrival_ns < max_hold_ns) {
        *mode = Q_HEIMDALL_MODE_HELD;
        return 0;
    }

    *mode = Q_HEIMDALL_MODE_FAIL_OPEN;
    return iosched_pop_index_32(state->pending, &state->pending_nr, oldest_idx);
}

static __always_inline __u64 q_heimdall_evacuate_locked(struct q_heimdall_local_state *state)
{
    return iosched_pop_head_32(state->pending, &state->pending_nr);
}

static __always_inline bool q_heimdall_completed_write(__u64 opf)
{
    __u32 op = opf & IOSCHED_REQ_OP_MASK;

    switch (op) {
    case REQ_OP_WRITE:
    case REQ_OP_WRITE_ZEROES:
    case REQ_OP_ZONE_APPEND:
        return true;
    default:
        return false;
    }
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
    struct q_heimdall_state_entry *entry;
    struct iosched_request_desc desc;
    __u32 pending_after;
    int ret;

    if (!ictx || !ictx->rq)
        return -1;
    if ((__u64)ictx->rq < 0xffff000000000000ULL)
        return -1;

    iosched_policy_meta_begin_insert(&policy_meta, ictx->cgroup_id);
    if (iosched_policy_meta_is_draining(&policy_meta, ictx->cgroup_id)) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        Q_HEIMDALL_DBG_SAMPLED(64,
                               "[q_heimdall] insert drained cg=%llu hctx=%llu\n",
                               ictx->cgroup_id,
                               ictx->hctx_idx);
        return IOSCHED_INSERT_DEFER_TO_PARENT;
    }

    entry = state_lookup(ictx->hctx_idx);
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        Q_HEIMDALL_DBG_SAMPLED(64,
                               "[q_heimdall] insert no_state cg=%llu hctx=%llu\n",
                               ictx->cgroup_id,
                               ictx->hctx_idx);
        return -1;
    }

    desc = iosched_build_desc(ictx);
    bpf_spin_lock(&entry->lock);
    ret = q_heimdall_insert(&entry->state, &desc);
    pending_after = entry->state.pending_nr;
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        Q_HEIMDALL_DBG_SAMPLED(64,
                               "[q_heimdall] insert full cg=%llu pending=%u\n",
                               ictx->cgroup_id,
                               pending_after);
        return -1;
    }

    q_heimdall_note_enqueue();
    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
    Q_HEIMDALL_DBG_SAMPLED(64,
                           "[q_heimdall] insert rq=%llx cost=%u pending=%u\n",
                           desc.rq_ptr,
                           desc.cost,
                           pending_after);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_heimdall_state_entry *entry;
    struct q_heimdall_model_ctx model;
    __u64 max_hold_ns;
    __u64 now;
    bool use_model;
    __u64 rq_ptr;
    __u32 mode = 0;

    if (!dctx)
        return 0;

    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    use_model = q_heimdall_prepare_model_ctx(&model);
    max_hold_ns = q_heimdall_max_hold_ns();
    now = bpf_ktime_get_ns();
    q_heimdall_barrier_var(now);
    bpf_spin_lock(&entry->lock);
    rq_ptr = q_heimdall_dispatch_locked(&entry->state,
                                         now,
                                          use_model,
                                          max_hold_ns,
                                          &model,
                                          &mode);
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr) {
        q_heimdall_note_dispatch(mode);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    }
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_heimdall_state_entry *entry;
    __u64 rq_ptr;
    __u32 pending_before;
    __u32 pending_after;

    if (!dctx)
        return 0;

    entry = state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    bpf_spin_lock(&entry->lock);
    pending_before = entry->state.pending_nr;
    rq_ptr = q_heimdall_evacuate_locked(&entry->state);
    pending_after = entry->state.pending_nr;
    bpf_spin_unlock(&entry->lock);

    Q_HEIMDALL_DBG_SAMPLED(64,
                           "[q_heimdall] evacuate rq=%llx before=%u after=%u\n",
                           rq_ptr,
                           pending_before,
                           pending_after);

    if (rq_ptr) {
        q_heimdall_note_dispatch(2);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    }
    return rq_ptr;
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct q_heimdall_stats_entry *entry;
    __u64 latency;
    __u64 sectors;
    __u64 bytes;
    __u64 throughput = 0;
    __u64 completions;
    __u64 ewma_read;
    __u64 ewma_write;
    __u32 is_write;

    if (!cctx || !cctx->rq)
        return;

    entry = stats_lookup();
    if (!entry)
        return;

    latency = cctx->io_latency_ns ? cctx->io_latency_ns : cctx->total_latency_ns;
    sectors = cctx->nr_sectors ? cctx->nr_sectors : 1;
    bytes = sectors * 512ULL;
    if (latency)
        throughput = (bytes * 1000000000ULL) / latency;
    is_write = q_heimdall_completed_write(cctx->opf) ? 1U : 0U;

    bpf_spin_lock(&entry->lock);
    entry->stats.completions++;
    if (is_write) {
        if (!entry->stats.ewma_write_latency_ns)
            entry->stats.ewma_write_latency_ns = latency;
        else
            entry->stats.ewma_write_latency_ns =
                entry->stats.ewma_write_latency_ns -
                (entry->stats.ewma_write_latency_ns >> Q_HEIMDALL_LAT_EWMA_SHIFT) +
                (latency >> Q_HEIMDALL_LAT_EWMA_SHIFT);
    } else {
        if (!entry->stats.ewma_read_latency_ns)
            entry->stats.ewma_read_latency_ns = latency;
        else
            entry->stats.ewma_read_latency_ns =
                entry->stats.ewma_read_latency_ns -
                (entry->stats.ewma_read_latency_ns >> Q_HEIMDALL_LAT_EWMA_SHIFT) +
                (latency >> Q_HEIMDALL_LAT_EWMA_SHIFT);
    }
    entry->stats.latency_history_ns[2] = entry->stats.latency_history_ns[1];
    entry->stats.latency_history_ns[1] = entry->stats.latency_history_ns[0];
    entry->stats.latency_history_ns[0] = latency;
    entry->stats.throughput_history_bps[2] = entry->stats.throughput_history_bps[1];
    entry->stats.throughput_history_bps[1] = entry->stats.throughput_history_bps[0];
    entry->stats.throughput_history_bps[0] = throughput;
    entry->stats.last_completion_ns = cctx->now;
    completions = entry->stats.completions;
    ewma_read = entry->stats.ewma_read_latency_ns;
    ewma_write = entry->stats.ewma_write_latency_ns;
    bpf_spin_unlock(&entry->lock);

    Q_HEIMDALL_DBG_SAMPLED(128,
                           "[q_heimdall] complete write=%u lat=%llu tput=%llu\n",
                           is_write,
                           latency,
                           throughput);
    Q_HEIMDALL_DBG_SAMPLED(512,
                           "[q_heimdall] complete cnt=%llu ewma_r=%llu ewma_w=%llu\n",
                           completions,
                           ewma_read,
                           ewma_write);
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_heimdall = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .completed_request = (void *)completed_request,
    .name = "q_heimdall",
};
