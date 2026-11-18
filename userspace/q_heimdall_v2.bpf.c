#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

#define Q_HMD_V2_MAX_HCTX 64U
#define Q_HMD_V2_LAT_EWMA_SHIFT 3U
#define Q_HMD_V2_TPUT_INF 99999999ULL

#define Q_HMD_V2_DEC_FAST_NO_WEIGHTS 1U
#define Q_HMD_V2_DEC_FAST_TEST 2U
#define Q_HMD_V2_DEC_HELD_TEST 3U
#define Q_HMD_V2_DEC_FAST_WRITE 4U
#define Q_HMD_V2_DEC_FAST_SECTOR0 5U
#define Q_HMD_V2_DEC_FAST_HELD_OVERFLOW 6U

#define q_hmd_v2_barrier_var(var) \
    asm volatile("" : "+r"(var) :: "memory")

#if IOEXT_DEBUG
#define Q_HMD_V2_DBG_SAMPLED(period, fmt, ...) \
    IOEXT_BPF_DBG_SAMPLED(period, "ioext: " fmt, ##__VA_ARGS__)
#else
#define Q_HMD_V2_DBG_SAMPLED(period, fmt, ...) do { } while (0)
#endif

struct q_hmd_v2_req {
    struct iosched_request_desc desc;
    __s64 score;
    __u32 decision;
    __u32 reserved;
};

struct q_hmd_v2_local_state {
    struct q_hmd_v2_req fast[IOSCHED_MAX_PENDING];
    struct q_hmd_v2_req held[IOSCHED_MAX_PENDING];
    __u32 fast_nr;
    __u32 held_nr;
    __u32 test_bucket;
    __u32 reserved;
    __u64 outstanding_read;
    __u64 outstanding_write;
    __u64 queue_len_history[3];
    __u64 latency_history_us[3];
    __u64 throughput_history_bpus[3];
};

struct q_hmd_v2_state_entry {
    struct bpf_spin_lock lock;
    struct q_hmd_v2_local_state state;
};

struct q_hmd_v2_stats_entry {
    struct bpf_spin_lock lock;
    struct iosched_heimdall_v2_stats stats;
};

struct q_hmd_v2_inflight_meta {
    __u64 queue_len_at_insert;
    __u64 bytes;
    __u64 opf;
    __u64 hctx_idx;
};

struct q_hmd_v2_cfg_snapshot {
    __u32 flags;
    __u32 test_hold_every;
    __u64 max_hold_ns;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, Q_HMD_V2_MAX_HCTX);
    __type(key, __u32);
    __type(value, struct q_hmd_v2_state_entry);
} hm_v2_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_config);
} hm_v2_cfg SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_norm_vector);
} hm_v2_min SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_norm_vector);
} hm_v2_scale SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_fc1_weight);
} hm_v2_fc1_w SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_fc1_bias);
} hm_v2_fc1_b SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_fc2_weight);
} hm_v2_fc2_w SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_fc2_bias);
} hm_v2_fc2_b SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_out_weight);
} hm_v2_out_w SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct iosched_heimdall_v2_out_bias);
} hm_v2_out_b SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u64);
    __type(value, struct q_hmd_v2_inflight_meta);
} hm_v2_inflight SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct q_hmd_v2_stats_entry);
} hm_v2_stats SEC(".maps");

IOEXT_POLICY_META_MAP(policy_meta);

static __always_inline struct q_hmd_v2_state_entry *q_hmd_v2_state_lookup(__u64 hctx_idx)
{
    __u32 key;

    if (hctx_idx >= Q_HMD_V2_MAX_HCTX)
        return 0;

    key = (__u32)hctx_idx;
    return bpf_map_lookup_elem(&hm_v2_state, &key);
}

static __always_inline struct q_hmd_v2_stats_entry *q_hmd_v2_stats_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&hm_v2_stats, &key);
}

static __always_inline struct iosched_heimdall_v2_config *q_hmd_v2_cfg_lookup(void)
{
    __u32 key = 0;

    return bpf_map_lookup_elem(&hm_v2_cfg, &key);
}

static __always_inline struct q_hmd_v2_cfg_snapshot q_hmd_v2_read_cfg(void)
{
    struct q_hmd_v2_cfg_snapshot snap = {};
    struct iosched_heimdall_v2_config *cfg;

    cfg = q_hmd_v2_cfg_lookup();
    if (!cfg) {
        snap.max_hold_ns = IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS;
        return snap;
    }

    snap.flags = cfg->flags;
    snap.test_hold_every = cfg->test_hold_every;
    snap.max_hold_ns = cfg->max_hold_us ?
        (__u64)cfg->max_hold_us * 1000ULL :
        IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS;
    if (!snap.test_hold_every)
        snap.test_hold_every = IOSCHED_HEIMDALL_V2_DEFAULT_TEST_HOLD_EVERY;
    return snap;
}

static __always_inline void q_hmd_v2_build_features_locked(
    const struct q_hmd_v2_local_state *state,
    const struct iosched_request_desc *req,
    __s64 features[IOSCHED_HEIMDALL_V2_FEATURES])
{
    __u64 queue_len = state->outstanding_read + state->outstanding_write;

    features[0] = iosched_req_is_write(req) ? 0 : 1;
    features[1] = (__s64)req->cost * 512LL;
    features[2] = (__s64)queue_len;
    features[3] = (__s64)state->queue_len_history[0];
    features[4] = (__s64)state->queue_len_history[1];
    features[5] = (__s64)state->queue_len_history[2];
    features[6] = (__s64)state->latency_history_us[0];
    features[7] = (__s64)state->latency_history_us[1];
    features[8] = (__s64)state->latency_history_us[2];
    features[9] = (__s64)state->throughput_history_bpus[0];
    features[10] = (__s64)state->throughput_history_bpus[1];
    features[11] = (__s64)state->throughput_history_bpus[2];
}

static __always_inline int q_hmd_v2_push_req(struct q_hmd_v2_req *entries,
                                             __u32 *count,
                                             const struct q_hmd_v2_req *req)
{
    if (*count >= IOSCHED_MAX_PENDING)
        return -1;

    entries[*count] = *req;
    (*count)++;
    return 0;
}

static __always_inline __u64 q_hmd_v2_pop_head(struct q_hmd_v2_req *entries,
                                               __u32 *count,
                                               __u32 *decision)
{
    struct q_hmd_v2_req picked;
    __u32 i;

    if (!*count)
        return 0;

    picked = entries[0];
    for (i = 0; i < IOSCHED_MAX_PENDING - 1; i++) {
        if (i + 1 >= *count)
            break;
        entries[i] = entries[i + 1];
    }

    (*count)--;
    if (decision)
        *decision = picked.decision;
    return picked.desc.rq_ptr;
}

static __always_inline __u32 q_hmd_v2_decide_locked(
    struct q_hmd_v2_local_state *state,
    const struct iosched_request_desc *req,
    const struct q_hmd_v2_cfg_snapshot *cfg)
{
    if (iosched_req_is_write(req))
        return Q_HMD_V2_DEC_FAST_WRITE;
    if (!req->sector)
        return Q_HMD_V2_DEC_FAST_SECTOR0;
    if (!(cfg->flags & IOSCHED_HEIMDALL_V2_F_ENABLE))
        return Q_HMD_V2_DEC_FAST_NO_WEIGHTS;
    if (cfg->flags & IOSCHED_HEIMDALL_V2_F_TEST_RULE) {
        state->test_bucket++;
        if (state->test_bucket >= cfg->test_hold_every) {
            state->test_bucket = 0;
            return Q_HMD_V2_DEC_HELD_TEST;
        }
        return Q_HMD_V2_DEC_FAST_TEST;
    }

    return Q_HMD_V2_DEC_FAST_NO_WEIGHTS;
}

static __always_inline void q_hmd_v2_note_insert(
    __u32 decision,
    const __s64 features[IOSCHED_HEIMDALL_V2_FEATURES],
    __u32 fast_nr,
    __u32 held_nr,
    __u64 outstanding_read,
    __u64 outstanding_write,
    const struct q_hmd_v2_cfg_snapshot *cfg)
{
    struct q_hmd_v2_stats_entry *entry;
    __u32 i;

    entry = q_hmd_v2_stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    entry->stats.insert_total++;
    entry->stats.current_fast_nr = fast_nr;
    entry->stats.current_held_nr = held_nr;
    if (fast_nr > entry->stats.max_fast_nr)
        entry->stats.max_fast_nr = fast_nr;
    if (held_nr > entry->stats.max_held_nr)
        entry->stats.max_held_nr = held_nr;
    entry->stats.outstanding_read = outstanding_read;
    entry->stats.outstanding_write = outstanding_write;
    entry->stats.model_flags = cfg->flags;
    entry->stats.feature_schema_version = IOSCHED_HEIMDALL_V2_SCHEMA_VERSION;
    entry->stats.weights_ready = !!(cfg->flags & IOSCHED_HEIMDALL_V2_F_WEIGHTS_READY);
    entry->stats.q_shift = IOSCHED_HEIMDALL_V2_Q_SHIFT;
    entry->stats.threshold = 0;
    entry->stats.last_score = 0;
    entry->stats.last_decision = decision;

    for (i = 0; i < IOSCHED_HEIMDALL_V2_FEATURES; i++)
        entry->stats.last_features[i] = features[i];

    switch (decision) {
    case Q_HMD_V2_DEC_HELD_TEST:
        entry->stats.held_enqueued++;
        entry->stats.test_held++;
        break;
    case Q_HMD_V2_DEC_FAST_TEST:
        entry->stats.fast_enqueued++;
        entry->stats.test_fast++;
        break;
    case Q_HMD_V2_DEC_FAST_WRITE:
        entry->stats.fast_enqueued++;
        entry->stats.write_bypass_fast++;
        break;
    case Q_HMD_V2_DEC_FAST_SECTOR0:
        entry->stats.fast_enqueued++;
        entry->stats.sector0_bypass_fast++;
        break;
    case Q_HMD_V2_DEC_FAST_HELD_OVERFLOW:
        entry->stats.fast_enqueued++;
        entry->stats.held_overflow_fail_open++;
        break;
    default:
        entry->stats.fast_enqueued++;
        entry->stats.model_unavailable_fast++;
        break;
    }
    bpf_spin_unlock(&entry->lock);
}

static __always_inline void q_hmd_v2_note_simple_error(__u32 kind)
{
    struct q_hmd_v2_stats_entry *entry;

    entry = q_hmd_v2_stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    if (kind == 0)
        entry->stats.enqueue_errors++;
    else if (kind == 1)
        entry->stats.fast_overflow_errors++;
    else
        entry->stats.inflight_map_errors++;
    bpf_spin_unlock(&entry->lock);
}

static __always_inline void q_hmd_v2_note_dispatch(__u32 mode,
                                                   __u32 fast_nr,
                                                   __u32 held_nr,
                                                   __u64 oldest_age_ns)
{
    struct q_hmd_v2_stats_entry *entry;

    entry = q_hmd_v2_stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    entry->stats.current_fast_nr = fast_nr;
    entry->stats.current_held_nr = held_nr;
    entry->stats.oldest_held_age_ns = oldest_age_ns;
    if (mode == 0)
        entry->stats.dispatch_fast++;
    else if (mode == 1)
        entry->stats.dispatch_held_timeout++;
    else if (mode == 6)
        entry->stats.dispatch_held_soft++;
    else if (mode == 2)
        entry->stats.dispatch_wait_held++;
    else if (mode == 3)
        entry->stats.evacuate_fast++;
    else if (mode == 4)
        entry->stats.evacuate_held++;
    bpf_spin_unlock(&entry->lock);
}

static __always_inline void q_hmd_v2_note_completion(
    bool meta_miss,
    bool is_write,
    __u64 latency_ns,
    __u64 last_completion_ns,
    const __u64 queue_len_history[3],
    const __u64 latency_history_us[3],
    const __u64 throughput_history_bpus[3],
    __u64 outstanding_read,
    __u64 outstanding_write)
{
    struct q_hmd_v2_stats_entry *entry;

    entry = q_hmd_v2_stats_lookup();
    if (!entry)
        return;

    bpf_spin_lock(&entry->lock);
    entry->stats.completions++;
    if (meta_miss)
        entry->stats.completion_meta_miss++;
    if (is_write) {
        if (!entry->stats.ewma_write_latency_ns)
            entry->stats.ewma_write_latency_ns = latency_ns;
        else
            entry->stats.ewma_write_latency_ns =
                entry->stats.ewma_write_latency_ns -
                (entry->stats.ewma_write_latency_ns >> Q_HMD_V2_LAT_EWMA_SHIFT) +
                (latency_ns >> Q_HMD_V2_LAT_EWMA_SHIFT);
    } else {
        if (!entry->stats.ewma_read_latency_ns)
            entry->stats.ewma_read_latency_ns = latency_ns;
        else
            entry->stats.ewma_read_latency_ns =
                entry->stats.ewma_read_latency_ns -
                (entry->stats.ewma_read_latency_ns >> Q_HMD_V2_LAT_EWMA_SHIFT) +
                (latency_ns >> Q_HMD_V2_LAT_EWMA_SHIFT);
    }
    entry->stats.last_completion_ns = last_completion_ns;
    entry->stats.latency_history_us[0] = latency_history_us[0];
    entry->stats.latency_history_us[1] = latency_history_us[1];
    entry->stats.latency_history_us[2] = latency_history_us[2];
    entry->stats.throughput_history_bpus[0] = throughput_history_bpus[0];
    entry->stats.throughput_history_bpus[1] = throughput_history_bpus[1];
    entry->stats.throughput_history_bpus[2] = throughput_history_bpus[2];
    entry->stats.queue_len_history[0] = queue_len_history[0];
    entry->stats.queue_len_history[1] = queue_len_history[1];
    entry->stats.queue_len_history[2] = queue_len_history[2];
    entry->stats.outstanding_read = outstanding_read;
    entry->stats.outstanding_write = outstanding_write;
    bpf_spin_unlock(&entry->lock);
}

static __always_inline bool q_hmd_v2_completed_write(__u64 opf)
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
    struct q_hmd_v2_state_entry *entry;
    struct q_hmd_v2_cfg_snapshot cfg;
    struct q_hmd_v2_inflight_meta meta = {};
    struct iosched_request_desc desc;
    struct q_hmd_v2_req req = {};
    __s64 features[IOSCHED_HEIMDALL_V2_FEATURES];
    __u32 decision;
    __u32 error_kind = 0;
    __u32 fast_nr = 0;
    __u32 held_nr = 0;
    __u64 outstanding_read = 0;
    __u64 outstanding_write = 0;
    __u64 rq_key;
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

    entry = q_hmd_v2_state_lookup(ictx->hctx_idx);
    if (!entry) {
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    cfg = q_hmd_v2_read_cfg();
    desc = iosched_build_desc(ictx);
    desc.deadline_ns = desc.arrival_ns + cfg.max_hold_ns;
    rq_key = desc.rq_ptr;

    bpf_spin_lock(&entry->lock);
    q_hmd_v2_build_features_locked(&entry->state, &desc, features);
    meta.queue_len_at_insert = (__u64)features[2];
    meta.bytes = (__u64)features[1];
    meta.opf = desc.opf;
    meta.hctx_idx = desc.hctx_idx;

    decision = q_hmd_v2_decide_locked(&entry->state, &desc, &cfg);
    req.desc = desc;
    req.decision = decision;
    req.score = 0;

    if (decision == Q_HMD_V2_DEC_HELD_TEST) {
        ret = q_hmd_v2_push_req(entry->state.held, &entry->state.held_nr, &req);
        if (ret) {
            decision = Q_HMD_V2_DEC_FAST_HELD_OVERFLOW;
            req.decision = decision;
            ret = q_hmd_v2_push_req(entry->state.fast, &entry->state.fast_nr, &req);
            if (ret)
                error_kind = 1;
        }
    } else {
        ret = q_hmd_v2_push_req(entry->state.fast, &entry->state.fast_nr, &req);
        if (ret) {
            decision = Q_HMD_V2_DEC_FAST_NO_WEIGHTS;
            error_kind = 1;
        }
    }

    if (!ret) {
        if (iosched_req_is_write(&desc))
            entry->state.outstanding_write++;
        else
            entry->state.outstanding_read++;
        fast_nr = entry->state.fast_nr;
        held_nr = entry->state.held_nr;
        outstanding_read = entry->state.outstanding_read;
        outstanding_write = entry->state.outstanding_write;
    }
    bpf_spin_unlock(&entry->lock);

    if (ret) {
        q_hmd_v2_note_simple_error(error_kind ? error_kind : 0);
        iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);
        return -1;
    }

    if (bpf_map_update_elem(&hm_v2_inflight, &rq_key, &meta, BPF_ANY))
        q_hmd_v2_note_simple_error(2);

    q_hmd_v2_note_insert(decision, features, fast_nr, held_nr,
                         outstanding_read, outstanding_write, &cfg);
    iosched_policy_meta_note_insert(&policy_meta, ictx->cgroup_id);
    iosched_policy_meta_end_insert(&policy_meta, ictx->cgroup_id);

    Q_HMD_V2_DBG_SAMPLED(128,
                         "[q_heimdall_v2] insert rq=%llx dec=%u fast=%u held=%u\n",
                         desc.rq_ptr,
                         decision,
                         fast_nr,
                         held_nr);
    return IOSCHED_INSERT_ACCEPT;
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_hmd_v2_state_entry *entry;
    __u64 now;
    __u64 rq_ptr = 0;
    __u64 oldest_age_ns = 0;
    __u32 fast_nr = 0;
    __u32 held_nr = 0;
    __u32 mode = 5;
    __u32 decision = 0;

    if (!dctx)
        return 0;

    entry = q_hmd_v2_state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    now = bpf_ktime_get_ns();
    q_hmd_v2_barrier_var(now);

    bpf_spin_lock(&entry->lock);
    if (entry->state.held_nr) {
        __u64 arrival = entry->state.held[0].desc.arrival_ns;

        if (now >= arrival)
            oldest_age_ns = now - arrival;
        if (now >= entry->state.held[0].desc.deadline_ns) {
            rq_ptr = q_hmd_v2_pop_head(entry->state.held,
                                       &entry->state.held_nr,
                                       &decision);
            mode = 1;
        } else if (!entry->state.fast_nr) {
            rq_ptr = q_hmd_v2_pop_head(entry->state.held,
                                       &entry->state.held_nr,
                                       &decision);
            mode = 6;
        }
    }
    if (!rq_ptr && entry->state.fast_nr) {
        rq_ptr = q_hmd_v2_pop_head(entry->state.fast,
                                   &entry->state.fast_nr,
                                   &decision);
        mode = 0;
    } else if (!rq_ptr && entry->state.held_nr) {
        mode = 2;
    }
    fast_nr = entry->state.fast_nr;
    held_nr = entry->state.held_nr;
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr) {
        q_hmd_v2_note_dispatch(mode, fast_nr, held_nr, oldest_age_ns);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    } else if (mode == 2) {
        q_hmd_v2_note_dispatch(mode, fast_nr, held_nr, oldest_age_ns);
    }
    return rq_ptr;
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct q_hmd_v2_state_entry *entry;
    __u64 rq_ptr = 0;
    __u32 fast_nr = 0;
    __u32 held_nr = 0;
    __u32 mode = 3;
    __u32 decision = 0;

    if (!dctx)
        return 0;

    entry = q_hmd_v2_state_lookup(dctx->hctx_idx);
    if (!entry)
        return 0;

    bpf_spin_lock(&entry->lock);
    if (entry->state.fast_nr) {
        rq_ptr = q_hmd_v2_pop_head(entry->state.fast,
                                   &entry->state.fast_nr,
                                   &decision);
        mode = 3;
    } else if (entry->state.held_nr) {
        rq_ptr = q_hmd_v2_pop_head(entry->state.held,
                                   &entry->state.held_nr,
                                   &decision);
        mode = 4;
    }
    fast_nr = entry->state.fast_nr;
    held_nr = entry->state.held_nr;
    bpf_spin_unlock(&entry->lock);

    if (rq_ptr) {
        q_hmd_v2_note_dispatch(mode, fast_nr, held_nr, 0);
        iosched_policy_meta_note_dispatch(&policy_meta, dctx->cgroup_id);
    }
    return rq_ptr;
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct q_hmd_v2_state_entry *entry;
    struct q_hmd_v2_inflight_meta *meta_ptr;
    struct q_hmd_v2_inflight_meta meta = {};
    __u64 rq_key;
    __u64 latency_ns;
    __u64 latency_us;
    __u64 throughput_bpus;
    __u64 queue_len;
    __u64 qhist[3];
    __u64 lhist[3];
    __u64 thist[3];
    __u64 outstanding_read;
    __u64 outstanding_write;
    bool meta_miss = true;
    bool is_write;

    if (!cctx || !cctx->rq)
        return;

    entry = q_hmd_v2_state_lookup(cctx->hctx_idx);
    if (!entry)
        return;

    rq_key = (__u64)cctx->rq;
    meta_ptr = bpf_map_lookup_elem(&hm_v2_inflight, &rq_key);
    if (meta_ptr) {
        meta = *meta_ptr;
        meta_miss = false;
        bpf_map_delete_elem(&hm_v2_inflight, &rq_key);
    }

    latency_ns = cctx->io_latency_ns ? cctx->io_latency_ns : cctx->total_latency_ns;
    latency_us = latency_ns / 1000ULL;
    if (!latency_us)
        throughput_bpus = Q_HMD_V2_TPUT_INF;
    else if (meta.bytes)
        throughput_bpus = meta.bytes / latency_us;
    else
        throughput_bpus = ((cctx->nr_sectors ? cctx->nr_sectors : 1) * 512ULL) /
                          latency_us;

    is_write = q_hmd_v2_completed_write(meta_miss ? cctx->opf : meta.opf);

    bpf_spin_lock(&entry->lock);
    if (is_write) {
        if (entry->state.outstanding_write)
            entry->state.outstanding_write--;
    } else if (entry->state.outstanding_read) {
        entry->state.outstanding_read--;
    }

    queue_len = meta_miss ?
        entry->state.outstanding_read + entry->state.outstanding_write :
        meta.queue_len_at_insert;
    entry->state.queue_len_history[2] = entry->state.queue_len_history[1];
    entry->state.queue_len_history[1] = entry->state.queue_len_history[0];
    entry->state.queue_len_history[0] = queue_len;
    entry->state.latency_history_us[2] = entry->state.latency_history_us[1];
    entry->state.latency_history_us[1] = entry->state.latency_history_us[0];
    entry->state.latency_history_us[0] = latency_us;
    entry->state.throughput_history_bpus[2] = entry->state.throughput_history_bpus[1];
    entry->state.throughput_history_bpus[1] = entry->state.throughput_history_bpus[0];
    entry->state.throughput_history_bpus[0] = throughput_bpus;
    qhist[0] = entry->state.queue_len_history[0];
    qhist[1] = entry->state.queue_len_history[1];
    qhist[2] = entry->state.queue_len_history[2];
    lhist[0] = entry->state.latency_history_us[0];
    lhist[1] = entry->state.latency_history_us[1];
    lhist[2] = entry->state.latency_history_us[2];
    thist[0] = entry->state.throughput_history_bpus[0];
    thist[1] = entry->state.throughput_history_bpus[1];
    thist[2] = entry->state.throughput_history_bpus[2];
    outstanding_read = entry->state.outstanding_read;
    outstanding_write = entry->state.outstanding_write;
    bpf_spin_unlock(&entry->lock);

    q_hmd_v2_note_completion(meta_miss, is_write, latency_ns, cctx->now,
                             qhist, lhist, thist,
                             outstanding_read, outstanding_write);
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_q_hmd_v2 = {
    .init_queue = (void *)init_queue,
    .exit_queue = (void *)exit_queue,
    .insert_request = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate = (void *)evacuate,
    .completed_request = (void *)completed_request,
    .name = "q_heimdall_v2",
};
