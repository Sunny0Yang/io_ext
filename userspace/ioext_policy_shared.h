#ifndef IOSCHED_POLICY_SHARED_H
#define IOSCHED_POLICY_SHARED_H

#ifndef __always_inline
#define __always_inline inline __attribute__((always_inline))
#endif

#define IOSCHED_REQF_WRITE   (1U << 0)
#define IOSCHED_REQF_SYNC    (1U << 1)
#define IOSCHED_REQF_DISCARD (1U << 2)
#define IOSCHED_REQF_OTHER   (1U << 3)

#define IOSCHED_POLICY_META_F_DRAINING (1U << 0)

#define IOSCHED_MAX_PENDING 32
#define IOSCHED_INVALID_INDEX ((__u32)-1)

#define IOSCHED_MQD_READ_EXPIRE_NS  50000000ULL
#define IOSCHED_MQD_WRITE_EXPIRE_NS 500000000ULL
#define IOSCHED_MQD_MAX_READ_STARVATION 2U
#define IOSCHED_MQD_FIFO_BATCH 16U
#define IOSCHED_MQD_PRIO_AGING_EXPIRE_NS 10000000000ULL

#define IOSCHED_MQD_PRIO_RT 0U
#define IOSCHED_MQD_PRIO_BE 1U
#define IOSCHED_MQD_PRIO_IDLE 2U
#define IOSCHED_MQD_PRIO_COUNT 3U

#define IOSCHED_MQD_DIR_READ 0U
#define IOSCHED_MQD_DIR_WRITE 1U
#define IOSCHED_MQD_DIR_COUNT 2U

#define IOSCHED_KYBER_READ_BATCH    16U
#define IOSCHED_KYBER_WRITE_BATCH   8U
#define IOSCHED_KYBER_DISCARD_BATCH 1U
#define IOSCHED_KYBER_OTHER_BATCH   1U
#define IOSCHED_KYBER_READ_TARGET_NS      2000000ULL
#define IOSCHED_KYBER_WRITE_TARGET_NS    10000000ULL
#define IOSCHED_KYBER_DISCARD_TARGET_NS 5000000000ULL
#define IOSCHED_KYBER_OTHER_TARGET_NS     5000000ULL
#define IOSCHED_KYBER_MIN_DEPTH 1U

/*
 * Native kyber dynamic token allocation law, mirroring kernel/linux-7.3.1/
 * block/kyber-iosched.c: 8 histogram buckets of width target >> SHIFT (the
 * first GOOD_BUCKETS are <= target latency); total+I/O latency sampled for
 * every domain except OTHER; per-domain window gate (MIN_PCT_SAMPLES
 * samples or WINDOW_TIMEOUT_NS); congestion = p90 of any domain's I/O
 * latency in a bad bucket; resize new = current * (p99+1) >> SHIFT.
 */
#define IOSCHED_KYBER_LATENCY_SHIFT     2U
#define IOSCHED_KYBER_GOOD_BUCKETS      (1U << IOSCHED_KYBER_LATENCY_SHIFT)
#define IOSCHED_KYBER_LATENCY_BUCKETS   (2U << IOSCHED_KYBER_LATENCY_SHIFT)
#define IOSCHED_KYBER_TOTAL_LATENCY     0U
#define IOSCHED_KYBER_IO_LATENCY        1U
#define IOSCHED_KYBER_LATENCY_TYPES     2U
#define IOSCHED_KYBER_P99_INVALID       (-1)
#define IOSCHED_KYBER_MIN_PCT_SAMPLES   500U
#define IOSCHED_KYBER_WINDOW_TIMEOUT_NS 1000000000ULL
#define IOSCHED_KYBER_P90               90U
#define IOSCHED_KYBER_P99               99U
#define IOSCHED_KYBER_DEPTH_CAP_READ    256U
#define IOSCHED_KYBER_DEPTH_CAP_WRITE   128U
#define IOSCHED_KYBER_DEPTH_CAP_DISCARD 64U
#define IOSCHED_KYBER_DEPTH_CAP_OTHER   16U

#define IOSCHED_BFQ_MAX_FLOWS 2
#define IOSCHED_BFQ_FLOW_DEPTH 2
#define IOSCHED_BFQ_ACTIVE_FLOW_LIMIT 1024U
#define IOSCHED_BFQ_ACTIVE_QUEUE_DEPTH 128U
#define IOSCHED_BFQ_OVERFLOW_DEPTH 256U
#define IOSCHED_BFQ_CLASS_SYNC 0U
#define IOSCHED_BFQ_CLASS_ASYNC 1U
#define IOSCHED_BFQ_DISPATCH_CLASS_SOFT_RT 0U
#define IOSCHED_BFQ_DISPATCH_CLASS_INTERACTIVE 1U
#define IOSCHED_BFQ_DISPATCH_CLASS_SYNC 2U
#define IOSCHED_BFQ_DISPATCH_CLASS_ASYNC 3U
#define IOSCHED_BFQ_DISPATCH_CLASS_NR 4U
#define IOSCHED_BFQ_SYNC_BUDGET_SECTORS 64U
#define IOSCHED_BFQ_ASYNC_BUDGET_SECTORS 32U
#define IOSCHED_BFQ_ASYNC_CHARGE_FACTOR 3U
#define IOSCHED_BFQ_WEIGHT_DEFAULT 100U
#define IOSCHED_BFQ_WEIGHT_RT 200U
#define IOSCHED_BFQ_WEIGHT_IDLE 20U
#define IOSCHED_BFQ_WEIGHT_BOOST 2U
#define IOSCHED_BFQ_INTERACTIVE_WINDOW_NS 20000000ULL
#define IOSCHED_BFQ_INTERACTIVE_MAX_REQUESTS 1U
#define IOSCHED_BFQ_MAX_SERVICE_FROM_WR 120000U
#define IOSCHED_BFQ_BURST_INTERVAL_NS 600000000ULL
#define IOSCHED_BFQ_LARGE_BURST_THRESH 4U
#define IOSCHED_BFQ_SHORT_TTIME_NS 20000000ULL

enum iosched_kyber_domain {
    IOSCHED_KYBER_DOMAIN_READ = 0,
    IOSCHED_KYBER_DOMAIN_WRITE,
    IOSCHED_KYBER_DOMAIN_DISCARD,
    IOSCHED_KYBER_DOMAIN_OTHER,
    IOSCHED_KYBER_DOMAIN_NR,
};

struct iosched_request_desc {
    __u64 rq_ptr;
    __u64 issuer_id;
    __u64 insert_flags;
    __u64 sector;
    __u64 arrival_ns;
    __u64 deadline_ns;
    __u64 cgroup_id;
    __u64 original_cgroup_id;
    __u64 hctx_idx;
    __u64 ioprio;
    __u64 opf;
    __u32 owner_id;
    __u32 weight;
    __u32 cost;
    __u32 flags;
    __u32 domain;
};

struct iosched_policy_meta {
    __u64 queue_depth;
    __u64 dispatches;
    __u64 active_inserts;
    __u64 owner_token;
    __u32 arb_weight;
    __u32 arb_prio;
    __u32 flags;
    __u32 reserved;
};

#define IOSCHED_HEIMDALL_POLICY_F_ENABLED (1U << 0)
#define IOSCHED_HEIMDALL_POLICY_F_HOLD    (1U << 1)
#define IOSCHED_HEIMDALL_POLICY_F_PREFER  (1U << 2)

#define IOSCHED_HEIMDALL_ACTIVE_LIMIT 64U
#define IOSCHED_HEIMDALL_DEFAULT_WEIGHT 100U
#define IOSCHED_HEIMDALL_DEFAULT_STALE_NS 1000000000ULL
#define IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS 2000000ULL
#define IOSCHED_HEIMDALL_FEATURES 12U
#define IOSCHED_HEIMDALL_HIDDEN 4U
#define IOSCHED_HEIMDALL_MODEL_F_ENABLE        (1U << 0)
#define IOSCHED_HEIMDALL_MODEL_F_WEIGHTS_READY (1U << 1)
#define IOSCHED_HEIMDALL_SCORE_SHIFT 10U

struct iosched_heimdall_policy {
    __u64 epoch;
    __u64 updated_at_ns;
    __u64 hold_until_ns;
    __u64 budget_bytes;
    __u32 weight;
    __u32 flags;
    __u32 max_hold_us;
    __u32 reserved;
};

struct iosched_heimdall_stats {
    __u64 dispatches;
    __u64 completions;
    __u64 held_dispatches;
    __u64 skipped_dispatches;
    __u64 fail_open_dispatches;
    __u64 last_dispatch_ns;
    __u64 total_latency_ns;
    __u64 io_latency_ns;
};

struct iosched_heimdall_global_config {
    __u64 stale_timeout_ns;
    __u64 max_global_hold_ns;
    __u64 default_epoch;
    __u32 enable_global_hold;
    __u32 reserved;
};

struct iosched_heimdall_model_config {
    __u32 flags;
    __u32 feature_count;
    __u32 hidden_count;
    __s32 threshold;
    __u32 score_shift;
    __u32 max_hold_us;
    __u32 reserved[2];
};

struct iosched_heimdall_norm_vector {
    __s64 values[IOSCHED_HEIMDALL_FEATURES];
};

struct iosched_heimdall_w0_vector {
    __s32 values[IOSCHED_HEIMDALL_FEATURES * IOSCHED_HEIMDALL_HIDDEN];
};

struct iosched_heimdall_b0_vector {
    __s32 values[IOSCHED_HEIMDALL_HIDDEN];
};

struct iosched_heimdall_w1_vector {
    __s32 values[IOSCHED_HEIMDALL_HIDDEN];
};

struct iosched_heimdall_b1_vector {
    __s32 value;
};

struct iosched_heimdall_q_stats {
    __u64 enqueued;
    __u64 dispatched;
    __u64 completions;
    __u64 held_dispatches;
    __u64 fail_open_dispatches;
    __u64 model_disabled_dispatches;
    __u64 model_accept_dispatches;
    __u64 ewma_read_latency_ns;
    __u64 ewma_write_latency_ns;
    __u64 last_completion_ns;
    __u64 latency_history_ns[3];
    __u64 throughput_history_bps[3];
};

#define IOSCHED_HEIMDALL_V2_FEATURES 12U
#define IOSCHED_HEIMDALL_V2_H1 128U
#define IOSCHED_HEIMDALL_V2_H2 16U
#define IOSCHED_HEIMDALL_V2_Q_SHIFT 30U
#define IOSCHED_HEIMDALL_V2_SCHEMA_VERSION 1U
#define IOSCHED_HEIMDALL_V2_DEFAULT_TEST_HOLD_EVERY 8U
#define IOSCHED_HEIMDALL_V2_MAX_HOLD_US \
    (IOSCHED_HEIMDALL_DEFAULT_MAX_HOLD_NS / 1000ULL)

#define IOSCHED_HEIMDALL_V2_F_ENABLE        (1U << 0)
#define IOSCHED_HEIMDALL_V2_F_WEIGHTS_READY (1U << 1)
#define IOSCHED_HEIMDALL_V2_F_ENFORCE       (1U << 2)
#define IOSCHED_HEIMDALL_V2_F_TEST_RULE     (1U << 3)

struct iosched_heimdall_v2_config {
    __u32 flags;
    __u32 feature_count;
    __u32 h1_count;
    __u32 h2_count;
    __u32 q_shift;
    __s32 threshold;
    __u32 max_hold_us;
    __u32 test_hold_every;
    __u32 schema_version;
    __u32 reserved[7];
};

struct iosched_heimdall_v2_norm_vector {
    __s64 values[IOSCHED_HEIMDALL_V2_FEATURES];
};

struct iosched_heimdall_v2_fc1_weight {
    __s32 values[IOSCHED_HEIMDALL_V2_H1 * IOSCHED_HEIMDALL_V2_FEATURES];
};

struct iosched_heimdall_v2_fc1_bias {
    __s32 values[IOSCHED_HEIMDALL_V2_H1];
};

struct iosched_heimdall_v2_fc2_weight {
    __s32 values[IOSCHED_HEIMDALL_V2_H2 * IOSCHED_HEIMDALL_V2_H1];
};

struct iosched_heimdall_v2_fc2_bias {
    __s32 values[IOSCHED_HEIMDALL_V2_H2];
};

struct iosched_heimdall_v2_out_weight {
    __s32 values[IOSCHED_HEIMDALL_V2_H2];
};

struct iosched_heimdall_v2_out_bias {
    __s32 value;
};

struct iosched_heimdall_v2_stats {
    __u64 insert_total;
    __u64 fast_enqueued;
    __u64 held_enqueued;
    __u64 model_unavailable_fast;
    __u64 test_fast;
    __u64 test_held;
    __u64 write_bypass_fast;
    __u64 sector0_bypass_fast;
    __u64 held_overflow_fail_open;
    __u64 fast_overflow_errors;
    __u64 enqueue_errors;
    __u64 inflight_map_errors;
    __u64 dispatch_fast;
    __u64 dispatch_held_timeout;
    __u64 dispatch_held_soft;
    __u64 dispatch_wait_held;
    __u64 evacuate_fast;
    __u64 evacuate_held;
    __u64 completions;
    __u64 completion_meta_miss;
    __u64 ewma_read_latency_ns;
    __u64 ewma_write_latency_ns;
    __u64 last_completion_ns;
    __u64 latency_history_us[3];
    __u64 throughput_history_bpus[3];
    __u64 queue_len_history[3];
    __u64 current_fast_nr;
    __u64 current_held_nr;
    __u64 max_fast_nr;
    __u64 max_held_nr;
    __u64 outstanding_read;
    __u64 outstanding_write;
    __u64 oldest_held_age_ns;
    __u64 model_flags;
    __u64 feature_schema_version;
    __u64 weights_ready;
    __u64 q_shift;
    __s64 threshold;
    __s64 last_score;
    __s64 min_score;
    __s64 max_score;
    __u32 last_decision;
    __u32 reserved;
    __s64 last_features[IOSCHED_HEIMDALL_V2_FEATURES];
    __u64 model_fast;
    __u64 model_held;
};

struct iosched_fifo_model_state {
    struct iosched_request_desc pending[IOSCHED_MAX_PENDING];
    __u32 pending_nr;
};

struct iosched_mq_deadline_state {
    struct iosched_request_desc dispatch[IOSCHED_MAX_PENDING];
    struct iosched_request_desc fifo[IOSCHED_MQD_PRIO_COUNT][IOSCHED_MQD_DIR_COUNT][IOSCHED_MAX_PENDING];
    __u32 dispatch_nr;
    __u32 fifo_nr[IOSCHED_MQD_PRIO_COUNT][IOSCHED_MQD_DIR_COUNT];
    __u64 latest_pos[IOSCHED_MQD_PRIO_COUNT][IOSCHED_MQD_DIR_COUNT];
    __u32 last_dir;
    __u32 batching;
    __u32 starved;
    __u32 async_depth;
};

struct iosched_kyber_state {
    struct iosched_request_desc urgent[IOSCHED_KYBER_DOMAIN_NR][IOSCHED_MAX_PENDING];
    struct iosched_request_desc domains[IOSCHED_KYBER_DOMAIN_NR][IOSCHED_MAX_PENDING];
    __u32 urgent_nr[IOSCHED_KYBER_DOMAIN_NR];
    __u32 domain_nr[IOSCHED_KYBER_DOMAIN_NR];
    __u32 cur_domain;
    __u32 batching;
    __u32 inflight[IOSCHED_KYBER_DOMAIN_NR];
    __u32 depth_limit[IOSCHED_KYBER_DOMAIN_NR];
    __u64 avg_total_lat_ns[IOSCHED_KYBER_DOMAIN_NR];
    __u64 avg_io_lat_ns[IOSCHED_KYBER_DOMAIN_NR];
    __u32 async_depth;
    __u32 queue_async_depth;
    __u64 last_depth_update_ns;
    __u64 target_lat_ns[IOSCHED_KYBER_DOMAIN_NR];
    /* native law fields (kyber-iosched.c): per-(domain,type) latency
     * histogram buckets, per-domain window deadline, remembered p99.
     * avg_*_lat_ns and last_depth_update_ns above are kept ABI-stable but
     * unused since the EWMA law was replaced by the native law. */
    __u32 lat_buckets[IOSCHED_KYBER_DOMAIN_NR - 1][IOSCHED_KYBER_LATENCY_TYPES][IOSCHED_KYBER_LATENCY_BUCKETS];
    __u64 window_timeout_ns[IOSCHED_KYBER_DOMAIN_NR - 1];
    __s32 domain_p99[IOSCHED_KYBER_DOMAIN_NR - 1];
};

struct iosched_bfq_flow {
    struct iosched_request_desc pending[IOSCHED_BFQ_FLOW_DEPTH];
    __u64 entity_id;
    __u32 flow_class;
    __u32 weight;
    __u32 pending_nr;
    __u32 budget_left;
};

struct iosched_bfq_state {
    struct iosched_bfq_flow flows[IOSCHED_BFQ_MAX_FLOWS];
    __u32 current_slot;
    __u32 next_slot;
};

struct iosched_bfq_burst_state {
    __u64 last_activation_ns;
    __u32 burst_size;
    bool large_burst;
};

struct iosched_bfq_tenant_key {
    __u64 cgroup_id;
    __u32 hctx_idx;
    __u32 reserved;
};

struct iosched_bfq_cgroup_state {
    __u64 last_dispatch_ns;
    __u32 active_flows;
    __u32 interactive_flows;
    __u32 soft_rt_flows;
    __u32 sync_flows;
    __u32 queued_requests;
    __u32 dispatch_class;
    __u32 dispatch_budget;
    __u32 next_cost;
};

static __always_inline int iosched_req_is_write(const struct iosched_request_desc *req)
{
    return !!(req->flags & IOSCHED_REQF_WRITE);
}

static __always_inline int iosched_req_is_sync(const struct iosched_request_desc *req)
{
    return !!(req->flags & IOSCHED_REQF_SYNC);
}

static __always_inline __u32 iosched_mqd_dir(const struct iosched_request_desc *req)
{
    return iosched_req_is_write(req) ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ;
}

static __always_inline __u32 iosched_mqd_prio_from_ioprio(__u64 ioprio)
{
    __u32 class = (__u32)((ioprio >> 13) & 0x7);

    switch (class) {
    case 1:
        return IOSCHED_MQD_PRIO_RT;
    case 3:
        return IOSCHED_MQD_PRIO_IDLE;
    default:
        return IOSCHED_MQD_PRIO_BE;
    }
}

static __always_inline bool iosched_mq_deadline_merge_compatible(__u64 rq_ioprio,
                                                                 bool rq_is_write,
                                                                 __u64 bio_ioprio,
                                                                 bool bio_is_write)
{
    return rq_is_write == bio_is_write &&
           iosched_mqd_prio_from_ioprio(rq_ioprio) ==
           iosched_mqd_prio_from_ioprio(bio_ioprio);
}

static __always_inline int iosched_push_tail_32(struct iosched_request_desc *entries,
                                                __u32 *count,
                                                const struct iosched_request_desc *req)
{
    if (*count >= IOSCHED_MAX_PENDING)
        return -1;

    entries[*count] = *req;
    (*count)++;
    return 0;
}

static __always_inline int iosched_push_head_32(struct iosched_request_desc *entries,
                                                __u32 *count,
                                                const struct iosched_request_desc *req)
{
    __u32 i;

    if (*count >= IOSCHED_MAX_PENDING)
        return -1;

    for (i = *count; i > 0; i--)
        entries[i] = entries[i - 1];
    entries[0] = *req;
    (*count)++;
    return 0;
}

static __always_inline __u64 iosched_pop_head_32(struct iosched_request_desc *entries,
                                                 __u32 *count)
{
    struct iosched_request_desc picked;
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
    return picked.rq_ptr;
}

static __always_inline __u64 iosched_pop_index_32(struct iosched_request_desc *entries,
                                                  __u32 *count,
                                                  __u32 idx)
{
    struct iosched_request_desc picked;
    __u32 i;

    if (!*count || idx >= *count)
        return 0;

    picked = entries[idx];
    for (i = 0; i < IOSCHED_MAX_PENDING - 1; i++) {
        if (i < idx || i + 1 >= *count)
            continue;
        entries[i] = entries[i + 1];
    }

    (*count)--;
    return picked.rq_ptr;
}

static __always_inline int iosched_find_rq_index_32(const struct iosched_request_desc *entries,
                                                    __u32 count,
                                                    __u64 rq_ptr)
{
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= count)
            break;
        if (entries[i].rq_ptr == rq_ptr)
            return (int)i;
    }

    return -1;
}

static __always_inline struct iosched_request_desc iosched_take_index_32(
    struct iosched_request_desc *entries,
    __u32 *count,
    __u32 idx)
{
    struct iosched_request_desc picked = {};
    struct iosched_request_desc tail = {};
    __u32 last;

    if (!*count || idx >= *count)
        return picked;

    picked = entries[idx];
    last = *count - 1;

    switch (*count) {
    case 32:
        tail = entries[31];
        break;
    case 31:
        tail = entries[30];
        break;
    case 30:
        tail = entries[29];
        break;
    case 29:
        tail = entries[28];
        break;
    case 28:
        tail = entries[27];
        break;
    case 27:
        tail = entries[26];
        break;
    case 26:
        tail = entries[25];
        break;
    case 25:
        tail = entries[24];
        break;
    case 24:
        tail = entries[23];
        break;
    case 23:
        tail = entries[22];
        break;
    case 22:
        tail = entries[21];
        break;
    case 21:
        tail = entries[20];
        break;
    case 20:
        tail = entries[19];
        break;
    case 19:
        tail = entries[18];
        break;
    case 18:
        tail = entries[17];
        break;
    case 17:
        tail = entries[16];
        break;
    case 16:
        tail = entries[15];
        break;
    case 15:
        tail = entries[14];
        break;
    case 14:
        tail = entries[13];
        break;
    case 13:
        tail = entries[12];
        break;
    case 12:
        tail = entries[11];
        break;
    case 11:
        tail = entries[10];
        break;
    case 10:
        tail = entries[9];
        break;
    case 9:
        tail = entries[8];
        break;
    case 8:
        tail = entries[7];
        break;
    case 7:
        tail = entries[6];
        break;
    case 6:
        tail = entries[5];
        break;
    case 5:
        tail = entries[4];
        break;
    case 4:
        tail = entries[3];
        break;
    case 3:
        tail = entries[2];
        break;
    case 2:
        tail = entries[1];
        break;
    case 1:
        tail = entries[0];
        break;
    default:
        return picked;
    }

    if (idx != last)
        entries[idx] = tail;
    (*count)--;
    return picked;
}

static __always_inline int iosched_insert_sorted_sector_32(struct iosched_request_desc *entries,
                                                           __u32 *count,
                                                           const struct iosched_request_desc *req)
{
    __u32 pos = *count;
    __u32 i;

    if (*count >= IOSCHED_MAX_PENDING)
        return -1;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= *count)
            break;
        if (req->sector < entries[i].sector) {
            pos = i;
            break;
        }
    }

    for (i = IOSCHED_MAX_PENDING - 1; i > 0; i--) {
        if (i <= pos || i > *count)
            continue;
        entries[i] = entries[i - 1];
    }

    entries[pos] = *req;
    (*count)++;
    return 0;
}

static __always_inline __u32 iosched_find_expired_index_32(const struct iosched_request_desc *entries,
                                                           __u32 count,
                                                           __u64 now_ns)
{
    __u64 best_deadline = ~0ULL;
    __u32 best = IOSCHED_INVALID_INDEX;
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= count)
            break;
        if (entries[i].deadline_ns > now_ns)
            continue;
        if (entries[i].deadline_ns < best_deadline) {
            best_deadline = entries[i].deadline_ns;
            best = i;
        }
    }

    return best;
}

static __always_inline int iosched_fifo_model_insert(struct iosched_fifo_model_state *state,
                                                     const struct iosched_request_desc *req)
{
    return iosched_push_tail_32(state->pending, &state->pending_nr, req);
}

static __always_inline __u64 iosched_fifo_model_dispatch(struct iosched_fifo_model_state *state)
{
    return iosched_pop_head_32(state->pending, &state->pending_nr);
}

static __always_inline struct iosched_request_desc *iosched_mqd_fifo_lane(
    struct iosched_mq_deadline_state *state,
    __u32 prio,
    __u32 dir,
    __u32 **count)
{
    switch (prio) {
    case IOSCHED_MQD_PRIO_RT:
        if (dir == IOSCHED_MQD_DIR_READ) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_READ];
            return state->fifo[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_READ];
        }
        if (dir == IOSCHED_MQD_DIR_WRITE) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_WRITE];
            return state->fifo[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_WRITE];
        }
        break;
    case IOSCHED_MQD_PRIO_BE:
        if (dir == IOSCHED_MQD_DIR_READ) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_READ];
            return state->fifo[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_READ];
        }
        if (dir == IOSCHED_MQD_DIR_WRITE) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_WRITE];
            return state->fifo[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_WRITE];
        }
        break;
    case IOSCHED_MQD_PRIO_IDLE:
        if (dir == IOSCHED_MQD_DIR_READ) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_READ];
            return state->fifo[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_READ];
        }
        if (dir == IOSCHED_MQD_DIR_WRITE) {
            *count = &state->fifo_nr[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_WRITE];
            return state->fifo[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_WRITE];
        }
        break;
    default:
        break;
    }

    *count = &state->dispatch_nr;
    return state->dispatch;
}

static __always_inline __u32 iosched_mqd_count(struct iosched_mq_deadline_state *state,
                                               __u32 prio,
                                               __u32 dir)
{
    switch (prio) {
    case IOSCHED_MQD_PRIO_RT:
        return dir == IOSCHED_MQD_DIR_READ ?
            state->fifo_nr[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_READ] :
            state->fifo_nr[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_WRITE];
    case IOSCHED_MQD_PRIO_BE:
        return dir == IOSCHED_MQD_DIR_READ ?
            state->fifo_nr[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_READ] :
            state->fifo_nr[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_WRITE];
    case IOSCHED_MQD_PRIO_IDLE:
        return dir == IOSCHED_MQD_DIR_READ ?
            state->fifo_nr[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_READ] :
            state->fifo_nr[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_WRITE];
    default:
        return 0;
    }
}

static __always_inline void iosched_mqd_set_latest_pos(struct iosched_mq_deadline_state *state,
                                                        __u32 prio,
                                                        __u32 dir,
                                                        __u64 sector)
{
    switch (prio) {
    case IOSCHED_MQD_PRIO_RT:
        if (dir == IOSCHED_MQD_DIR_WRITE)
            state->latest_pos[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_WRITE] = sector;
        else
            state->latest_pos[IOSCHED_MQD_PRIO_RT][IOSCHED_MQD_DIR_READ] = sector;
        break;
    case IOSCHED_MQD_PRIO_BE:
        if (dir == IOSCHED_MQD_DIR_WRITE)
            state->latest_pos[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_WRITE] = sector;
        else
            state->latest_pos[IOSCHED_MQD_PRIO_BE][IOSCHED_MQD_DIR_READ] = sector;
        break;
    case IOSCHED_MQD_PRIO_IDLE:
        if (dir == IOSCHED_MQD_DIR_WRITE)
            state->latest_pos[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_WRITE] = sector;
        else
            state->latest_pos[IOSCHED_MQD_PRIO_IDLE][IOSCHED_MQD_DIR_READ] = sector;
        break;
    }
}

static __always_inline __u64 iosched_mqd_expire_ns(__u32 dir)
{
    return dir == IOSCHED_MQD_DIR_WRITE ?
           IOSCHED_MQD_WRITE_EXPIRE_NS : IOSCHED_MQD_READ_EXPIRE_NS;
}

static __always_inline int iosched_push_fifo_tail_32(struct iosched_request_desc *entries,
                                                     __u32 *count,
                                                     const struct iosched_request_desc *req)
{
    return iosched_push_tail_32(entries, count, req);
}

static __always_inline __u32 iosched_mqd_queued_prio(const struct iosched_mq_deadline_state *state,
                                                     __u32 prio)
{
    return state->fifo_nr[prio][IOSCHED_MQD_DIR_READ] +
           state->fifo_nr[prio][IOSCHED_MQD_DIR_WRITE];
}

static __always_inline bool iosched_mqd_has_work_prio(const struct iosched_mq_deadline_state *state,
                                                      __u32 prio)
{
    return iosched_mqd_queued_prio(state, prio) > 0;
}

static __always_inline __u32 iosched_find_first_ge_sector(const struct iosched_request_desc *entries,
                                                          __u32 count,
                                                          __u64 sector)
{
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= count)
            break;
        if (entries[i].sector >= sector)
            return i;
    }

    return IOSCHED_INVALID_INDEX;
}

static __always_inline struct iosched_request_desc *iosched_mqd_fifo_request(
    struct iosched_mq_deadline_state *state,
    __u32 prio,
    __u32 dir)
{
    __u32 *count;
    struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &count);

    if (!*count)
        return NULL;
    return &fifo[0];
}

static __always_inline bool iosched_mqd_fifo_expired(struct iosched_mq_deadline_state *state,
                                                     __u32 prio,
                                                     __u32 dir,
                                                     __u64 now_ns)
{
    struct iosched_request_desc *rq = iosched_mqd_fifo_request(state, prio, dir);

    return rq && rq->deadline_ns <= now_ns;
}

static __always_inline bool iosched_mqd_started_after(const struct iosched_request_desc *req,
                                                      __u32 dir,
                                                      __u64 latest_start_ns)
{
    __u64 start_ns = req->deadline_ns - iosched_mqd_expire_ns(dir);

    return start_ns > latest_start_ns;
}

static __always_inline __u32 iosched_mqd_pick_index(struct iosched_mq_deadline_state *state,
                                                    __u32 prio,
                                                    __u32 dir,
                                                    __u64 now_ns)
{
    __u32 *count;
    struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &count);
    __u32 expired_idx = IOSCHED_INVALID_INDEX;
    __u32 best_idx = IOSCHED_INVALID_INDEX;
    __u64 expired_deadline = ~0ULL;
    __u64 best_sector = ~0ULL;
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= *count)
            break;
        if (fifo[i].deadline_ns <= now_ns && fifo[i].deadline_ns < expired_deadline) {
            expired_deadline = fifo[i].deadline_ns;
            expired_idx = i;
        }
        if (fifo[i].sector < best_sector) {
            best_sector = fifo[i].sector;
            best_idx = i;
        }
    }

    return expired_idx != IOSCHED_INVALID_INDEX ? expired_idx : best_idx;
}

static __always_inline __u32 iosched_mqd_pick_index_before(struct iosched_mq_deadline_state *state,
                                                           __u32 prio,
                                                           __u32 dir,
                                                           __u64 now_ns,
                                                           __u64 latest_start_ns)
{
    __u32 *count;
    struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &count);
    __u32 expired_idx = IOSCHED_INVALID_INDEX;
    __u32 best_idx = IOSCHED_INVALID_INDEX;
    __u64 expired_deadline = ~0ULL;
    __u64 best_sector = ~0ULL;
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= *count)
            break;
        if (fifo[i].arrival_ns > latest_start_ns)
            continue;
        if (fifo[i].deadline_ns <= now_ns && fifo[i].deadline_ns < expired_deadline) {
            expired_deadline = fifo[i].deadline_ns;
            expired_idx = i;
        }
        if (fifo[i].sector < best_sector) {
            best_sector = fifo[i].sector;
            best_idx = i;
        }
    }

    return expired_idx != IOSCHED_INVALID_INDEX ? expired_idx : best_idx;
}

static __always_inline bool iosched_mqd_has_aged_request_dir(struct iosched_mq_deadline_state *state,
                                                             __u32 prio,
                                                             __u32 dir,
                                                             __u64 now_ns)
{
    __u64 latest_start_ns;

    if (now_ns < IOSCHED_MQD_PRIO_AGING_EXPIRE_NS)
        return false;
    latest_start_ns = now_ns - IOSCHED_MQD_PRIO_AGING_EXPIRE_NS;
    return iosched_mqd_pick_index_before(state, prio, dir, now_ns,
                                         latest_start_ns) != IOSCHED_INVALID_INDEX;
}

static __always_inline bool iosched_mqd_has_aged_request(struct iosched_mq_deadline_state *state,
                                                         __u32 prio,
                                                         __u64 now_ns)
{
    return iosched_mqd_has_aged_request_dir(state, prio, IOSCHED_MQD_DIR_READ, now_ns) ||
           iosched_mqd_has_aged_request_dir(state, prio, IOSCHED_MQD_DIR_WRITE, now_ns);
}

static __always_inline int iosched_mqd_remove_from_fifo(struct iosched_request_desc *entries,
                                                        __u32 *count,
                                                        __u64 rq_ptr)
{
    int idx = iosched_find_rq_index_32(entries, *count, rq_ptr);

    if (idx >= 0)
        iosched_take_index_32(entries, count, idx);
    return idx;
}

static __always_inline __u64 iosched_mqd_take_request(struct iosched_mq_deadline_state *state,
                                                      __u32 prio,
                                                      __u32 dir,
                                                      __u32 fifo_idx)
{
    __u32 *fifo_nr;
    struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);
    struct iosched_request_desc picked;

    if (!*fifo_nr || fifo_idx >= *fifo_nr)
        return 0;

    picked = iosched_take_index_32(fifo, fifo_nr, fifo_idx);
    if (!picked.rq_ptr)
        return 0;

    iosched_mqd_set_latest_pos(state, prio, dir, picked.sector);
    return picked.rq_ptr;
}

static __always_inline int iosched_mq_deadline_insert(struct iosched_mq_deadline_state *state,
                                                       struct iosched_request_desc req)
{
    __u32 dir = iosched_mqd_dir(&req);
    __u32 prio = iosched_mqd_prio_from_ioprio(req.ioprio);
    __u32 *fifo_nr;
    struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);

    if (req.insert_flags & 0x1)
        return iosched_push_tail_32(state->dispatch, &state->dispatch_nr, &req);

    req.deadline_ns = req.arrival_ns + iosched_mqd_expire_ns(dir);
    return iosched_push_fifo_tail_32(fifo, fifo_nr, &req);
}

static __always_inline __u64 iosched_mqd_dispatch_prio(struct iosched_mq_deadline_state *state,
                                                       __u32 prio,
                                                       __u64 now_ns)
{
    __u32 dir;
    __u32 idx = IOSCHED_INVALID_INDEX;

    dir = state->last_dir;
    if (state->batching < IOSCHED_MQD_FIFO_BATCH &&
        iosched_mqd_count(state, prio, dir)) {
        idx = iosched_mqd_pick_index(state, prio, dir, now_ns);
        goto dispatch_request;
    }

    if (iosched_mqd_count(state, prio, IOSCHED_MQD_DIR_READ)) {
        if (iosched_mqd_count(state, prio, IOSCHED_MQD_DIR_WRITE) &&
            state->starved++ >= IOSCHED_MQD_MAX_READ_STARVATION) {
            dir = IOSCHED_MQD_DIR_WRITE;
        } else {
            dir = IOSCHED_MQD_DIR_READ;
        }
    } else if (iosched_mqd_count(state, prio, IOSCHED_MQD_DIR_WRITE)) {
        state->starved = 0;
        dir = IOSCHED_MQD_DIR_WRITE;
    } else {
        return 0;
    }

    state->last_dir = dir;
    state->batching = 0;
    idx = iosched_mqd_pick_index(state, prio, dir, now_ns);

dispatch_request:
    if (idx == IOSCHED_INVALID_INDEX)
        return 0;
    state->batching++;
    return iosched_mqd_take_request(state, prio, dir, idx);
}

static __always_inline __u64 iosched_mqd_dispatch_prio_aged(struct iosched_mq_deadline_state *state,
                                                            __u32 prio,
                                                            __u64 now_ns)
{
    __u64 latest_start_ns;
    bool read_aged;
    bool write_aged;
    __u32 dir = state->last_dir;
    __u32 idx = IOSCHED_INVALID_INDEX;

    if (now_ns < IOSCHED_MQD_PRIO_AGING_EXPIRE_NS)
        return 0;
    latest_start_ns = now_ns - IOSCHED_MQD_PRIO_AGING_EXPIRE_NS;
    read_aged = iosched_mqd_has_aged_request_dir(state, prio, IOSCHED_MQD_DIR_READ, now_ns);
    write_aged = iosched_mqd_has_aged_request_dir(state, prio, IOSCHED_MQD_DIR_WRITE, now_ns);

    if (state->batching < IOSCHED_MQD_FIFO_BATCH &&
        ((dir == IOSCHED_MQD_DIR_READ && read_aged) ||
         (dir == IOSCHED_MQD_DIR_WRITE && write_aged))) {
        idx = iosched_mqd_pick_index_before(state, prio, dir, now_ns,
                                            latest_start_ns);
        goto dispatch_request;
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
    idx = iosched_mqd_pick_index_before(state, prio, dir, now_ns,
                                        latest_start_ns);

dispatch_request:
    if (idx == IOSCHED_INVALID_INDEX)
        return 0;
    state->batching++;
    return iosched_mqd_take_request(state, prio, dir, idx);
}

static __always_inline __u64 iosched_mqd_dispatch_rt(struct iosched_mq_deadline_state *state,
                                                     __u64 now_ns)
{
    return iosched_mqd_dispatch_prio(state, IOSCHED_MQD_PRIO_RT, now_ns);
}

static __always_inline __u64 iosched_mqd_dispatch_be(struct iosched_mq_deadline_state *state,
                                                     __u64 now_ns)
{
    return iosched_mqd_dispatch_prio(state, IOSCHED_MQD_PRIO_BE, now_ns);
}

static __always_inline __u64 iosched_mqd_dispatch_idle(struct iosched_mq_deadline_state *state,
                                                       __u64 now_ns)
{
    return iosched_mqd_dispatch_prio(state, IOSCHED_MQD_PRIO_IDLE, now_ns);
}

static __always_inline __u64 iosched_mq_deadline_dispatch(struct iosched_mq_deadline_state *state,
                                                          __u64 now_ns)
{
    __u64 rq_ptr;
    bool rt_has;
    bool be_has;
    bool idle_has;

    rt_has = iosched_mqd_has_work_prio(state, IOSCHED_MQD_PRIO_RT);
    be_has = iosched_mqd_has_work_prio(state, IOSCHED_MQD_PRIO_BE);
    idle_has = iosched_mqd_has_work_prio(state, IOSCHED_MQD_PRIO_IDLE);

    if (state->dispatch_nr)
        return iosched_pop_head_32(state->dispatch, &state->dispatch_nr);

    if ((rt_has + be_has + idle_has) > 1) {
        rq_ptr = iosched_mqd_dispatch_prio_aged(state, IOSCHED_MQD_PRIO_BE, now_ns);
        if (rq_ptr)
            return rq_ptr;
        rq_ptr = iosched_mqd_dispatch_prio_aged(state, IOSCHED_MQD_PRIO_IDLE, now_ns);
        if (rq_ptr)
            return rq_ptr;
    }

    rq_ptr = iosched_mqd_dispatch_rt(state, now_ns);
    if (rq_ptr || rt_has)
        return rq_ptr;
    rq_ptr = iosched_mqd_dispatch_be(state, now_ns);
    if (rq_ptr || be_has)
        return rq_ptr;
    rq_ptr = iosched_mqd_dispatch_idle(state, now_ns);
    if (rq_ptr || idle_has)
        return rq_ptr;

    return 0;
}

static __always_inline void iosched_mq_deadline_depth_updated(
    struct iosched_mq_deadline_state *state,
    __u32 async_depth)
{
    state->async_depth = async_depth;
}

static __always_inline __u64 iosched_mq_deadline_limit_depth(
    const struct iosched_mq_deadline_state *state,
    bool is_sync_read)
{
    return is_sync_read ? 0 : state->async_depth;
}

static __always_inline __u64 iosched_mq_deadline_find_front_merge(
    struct iosched_mq_deadline_state *state,
    __u32 prio,
    bool is_write,
    __u64 bio_sector,
    __u64 bio_nr_sectors)
{
    __u64 bio_end = bio_sector + bio_nr_sectors;
    __u32 *count;
    struct iosched_request_desc *lane = iosched_mqd_fifo_lane(state,
                                                              prio,
                                                              is_write ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ,
                                                              &count);
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        if (i >= *count)
            break;
        if (lane[i].sector == bio_end)
            return lane[i].rq_ptr;
    }

    return 0;
}

static __always_inline __u64 iosched_mq_deadline_find_back_merge(
    struct iosched_mq_deadline_state *state,
    __u32 prio,
    bool is_write,
    __u64 bio_sector)
{
    __u32 *count;
    struct iosched_request_desc *lane = iosched_mqd_fifo_lane(state,
                                                              prio,
                                                              is_write ? IOSCHED_MQD_DIR_WRITE : IOSCHED_MQD_DIR_READ,
                                                              &count);
    __u32 i;

    for (i = 0; i < IOSCHED_MAX_PENDING; i++) {
        __u64 rq_end;

        if (i >= *count)
            break;
        rq_end = lane[i].sector + lane[i].cost;
        if (rq_end == bio_sector)
            return lane[i].rq_ptr;
    }

    return 0;
}

static __always_inline void iosched_mq_deadline_update_request(
    struct iosched_mq_deadline_state *state,
    __u64 rq_ptr,
    __u64 sector,
    __u64 nr_sectors,
    __u64 deadline_ns)
{
    struct iosched_request_desc desc;
    __u32 prio, dir;
    __u32 *fifo_nr;
    struct iosched_request_desc *lane;
    int idx;

    for (prio = 0; prio < IOSCHED_MQD_PRIO_COUNT; prio++) {
        for (dir = 0; dir < IOSCHED_MQD_DIR_COUNT; dir++) {
            lane = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);
            idx = iosched_find_rq_index_32(lane, *fifo_nr, rq_ptr);
            if (idx < 0)
                continue;
            desc = lane[idx];
            desc.sector = sector;
            desc.cost = nr_sectors ? (__u32)nr_sectors : desc.cost;
            if (deadline_ns && (!desc.deadline_ns || deadline_ns < desc.deadline_ns))
                desc.deadline_ns = deadline_ns;
            lane[idx] = desc;
            return;
        }
    }
}

static __always_inline void iosched_mq_deadline_drop_request(struct iosched_mq_deadline_state *state,
                                                             __u64 rq_ptr)
{
    __u32 prio, dir;
    int idx;

    for (prio = 0; prio < IOSCHED_MQD_PRIO_COUNT; prio++) {
        for (dir = 0; dir < IOSCHED_MQD_DIR_COUNT; dir++) {
            __u32 *fifo_nr;
            struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);

            idx = iosched_find_rq_index_32(fifo, *fifo_nr, rq_ptr);
            if (idx < 0)
                continue;
            iosched_take_index_32(fifo, fifo_nr, idx);
            return;
        }
    }
}

static __always_inline __u64 iosched_mq_deadline_former(struct iosched_mq_deadline_state *state,
                                                        __u64 rq_ptr)
{
    __u32 prio, dir;
    int idx;

    for (prio = 0; prio < IOSCHED_MQD_PRIO_COUNT; prio++) {
        for (dir = 0; dir < IOSCHED_MQD_DIR_COUNT; dir++) {
            __u32 *fifo_nr;
            struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);
            __u64 rq_sector;
            __u64 best_sector = 0;
            __u64 best_rq = 0;
            __u32 j;

            idx = iosched_find_rq_index_32(fifo, *fifo_nr, rq_ptr);
            if (idx < 0)
                continue;
            rq_sector = fifo[idx].sector;

            for (j = 0; j < IOSCHED_MAX_PENDING; j++) {
                if (j >= *fifo_nr)
                    break;
                if (fifo[j].sector < rq_sector && fifo[j].sector >= best_sector) {
                    best_sector = fifo[j].sector;
                    best_rq = fifo[j].rq_ptr;
                }
            }
            if (best_rq)
                return best_rq;
        }
    }

    return 0;
}

static __always_inline __u64 iosched_mq_deadline_next(struct iosched_mq_deadline_state *state,
                                                      __u64 rq_ptr)
{
    __u32 prio, dir;
    int idx;

    for (prio = 0; prio < IOSCHED_MQD_PRIO_COUNT; prio++) {
        for (dir = 0; dir < IOSCHED_MQD_DIR_COUNT; dir++) {
            __u32 *fifo_nr;
            struct iosched_request_desc *fifo = iosched_mqd_fifo_lane(state, prio, dir, &fifo_nr);
            __u64 rq_sector = 0;
            __u64 best_sector = ~0ULL;
            __u64 best_rq = 0;
            __u32 j;

            idx = iosched_find_rq_index_32(fifo, *fifo_nr, rq_ptr);
            if (idx < 0)
                continue;
            rq_sector = fifo[idx].sector;

            for (j = 0; j < IOSCHED_MAX_PENDING; j++) {
                if (j >= *fifo_nr)
                    break;
                if (fifo[j].sector > rq_sector && fifo[j].sector < best_sector) {
                    best_sector = fifo[j].sector;
                    best_rq = fifo[j].rq_ptr;
                }
            }
            if (best_rq)
                return best_rq;
        }
    }

    return 0;
}

static __always_inline __u32 iosched_kyber_domain_from_desc(const struct iosched_request_desc *req)
{
    if (req->flags & IOSCHED_REQF_DISCARD)
        return IOSCHED_KYBER_DOMAIN_DISCARD;
    if (req->flags & IOSCHED_REQF_WRITE)
        return IOSCHED_KYBER_DOMAIN_WRITE;
    if (req->domain < IOSCHED_KYBER_DOMAIN_NR)
        return req->domain;
    if (req->flags & IOSCHED_REQF_OTHER)
        return IOSCHED_KYBER_DOMAIN_OTHER;
    return IOSCHED_KYBER_DOMAIN_READ;
}

static __always_inline __u32 iosched_kyber_batch_limit(__u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        return IOSCHED_KYBER_READ_BATCH;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        return IOSCHED_KYBER_WRITE_BATCH;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        return IOSCHED_KYBER_DISCARD_BATCH;
    default:
        return IOSCHED_KYBER_OTHER_BATCH;
    }
}

static __always_inline __u64 iosched_kyber_default_target_ns(__u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        return IOSCHED_KYBER_READ_TARGET_NS;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        return IOSCHED_KYBER_WRITE_TARGET_NS;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        return IOSCHED_KYBER_DISCARD_TARGET_NS;
    default:
        return IOSCHED_KYBER_OTHER_TARGET_NS;
    }
}

static __always_inline void iosched_kyber_set_targets(struct iosched_kyber_state *state,
                                                      __u64 read_target_ns,
                                                      __u64 write_target_ns,
                                                      __u64 discard_target_ns,
                                                      __u64 other_target_ns)
{
    if (!state)
        return;

    state->target_lat_ns[IOSCHED_KYBER_DOMAIN_READ] =
        read_target_ns ? read_target_ns : IOSCHED_KYBER_READ_TARGET_NS;
    state->target_lat_ns[IOSCHED_KYBER_DOMAIN_WRITE] =
        write_target_ns ? write_target_ns : IOSCHED_KYBER_WRITE_TARGET_NS;
    state->target_lat_ns[IOSCHED_KYBER_DOMAIN_DISCARD] =
        discard_target_ns ? discard_target_ns : IOSCHED_KYBER_DISCARD_TARGET_NS;
    state->target_lat_ns[IOSCHED_KYBER_DOMAIN_OTHER] =
        other_target_ns ? other_target_ns : IOSCHED_KYBER_OTHER_TARGET_NS;
}

static __always_inline __u64 iosched_kyber_target_ns(const struct iosched_kyber_state *state,
                                                     __u32 domain)
{
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return IOSCHED_KYBER_OTHER_TARGET_NS;
    if (state && state->target_lat_ns[domain])
        return state->target_lat_ns[domain];
    return iosched_kyber_default_target_ns(domain);
}

static __always_inline bool iosched_kyber_merge_compatible(__u32 rq_domain,
                                                            __u32 bio_domain)
{
    return rq_domain == bio_domain;
}

static __always_inline __u32 iosched_kyber_depth_cap(__u32 async_depth)
{
    return async_depth ? async_depth : IOSCHED_KYBER_MIN_DEPTH;
}

/* kyber_depth[] (kyber-iosched.c:59-64): fixed per-domain token caps. */
static __always_inline __u32 iosched_kyber_domain_depth_cap(__u32 domain)
{
    switch (domain) {
    case IOSCHED_KYBER_DOMAIN_READ:
        return IOSCHED_KYBER_DEPTH_CAP_READ;
    case IOSCHED_KYBER_DOMAIN_WRITE:
        return IOSCHED_KYBER_DEPTH_CAP_WRITE;
    case IOSCHED_KYBER_DOMAIN_DISCARD:
        return IOSCHED_KYBER_DEPTH_CAP_DISCARD;
    default:
        return IOSCHED_KYBER_DEPTH_CAP_OTHER;
    }
}

static __always_inline void iosched_kyber_depth_updated(struct iosched_kyber_state *state,
                                                        __u32 async_depth)
{
    __u32 domain;

    if (!state)
        return;

    state->queue_async_depth = iosched_kyber_depth_cap(async_depth);
    if (!state->async_depth || state->async_depth > state->queue_async_depth)
        state->async_depth = state->queue_async_depth;

    for (domain = 0; domain < IOSCHED_KYBER_DOMAIN_NR; domain++) {
        if (!state->depth_limit[domain])
            state->depth_limit[domain] = iosched_kyber_domain_depth_cap(domain);
    }
}

static __always_inline __u32 iosched_kyber_limit_depth(struct iosched_kyber_state *state,
                                                        __u32 domain,
                                                        __u32 async_depth)
{
    __u32 limit;

    iosched_kyber_depth_updated(state, async_depth);

    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return state->queue_async_depth ? state->queue_async_depth
                                        : IOSCHED_KYBER_MIN_DEPTH;

    limit = state->depth_limit[domain] ?
        state->depth_limit[domain] :
        iosched_kyber_domain_depth_cap(domain);
    /* Port deviation (the only one): native kyber tokens are scheduler-side
     * and never clamped by queue depth, but this port also feeds
     * depth_limit into limit_depth->shallow_depth, so clamp the feed to
     * the queue's async depth to avoid nonsense tag limits. The stored
     * depth_limit keeps the native [1, kyber_depth[domain]] bounds. */
    if (limit > state->queue_async_depth)
        limit = state->queue_async_depth;
    return limit;
}

static __always_inline void iosched_kyber_prepare(struct iosched_kyber_state *state,
                                                  __u32 domain)
{
    (void)state;
    (void)domain;
}

static __always_inline void iosched_kyber_release(struct iosched_kyber_state *state,
                                                  __u32 domain)
{
    if (domain < IOSCHED_KYBER_DOMAIN_NR && state->inflight[domain])
        state->inflight[domain]--;
}

static __always_inline void iosched_kyber_requeue(struct iosched_kyber_state *state,
                                                  __u32 domain)
{
    iosched_kyber_release(state, domain);
}

static __always_inline void iosched_kyber_finish(struct iosched_kyber_state *state,
                                                 __u32 domain)
{
    iosched_kyber_release(state, domain);
}

/* add_latency_sample (kyber-iosched.c:607-623). */
static __always_inline __u32 iosched_kyber_bucket_for(__u64 target_ns, __u64 latency_ns)
{
    __u64 divisor;
    __u64 bucket;

    if (latency_ns > 0) {
        divisor = target_ns >> IOSCHED_KYBER_LATENCY_SHIFT;
        if (!divisor)
            divisor = 1;
        bucket = (latency_ns - 1) / divisor;
        if (bucket >= IOSCHED_KYBER_LATENCY_BUCKETS)
            bucket = IOSCHED_KYBER_LATENCY_BUCKETS - 1;
        return (__u32)bucket;
    }
    return 0;
}

static __always_inline void iosched_kyber_add_latency_sample(struct iosched_kyber_state *state,
                                                             __u32 domain,
                                                             __u64 target_ns,
                                                             __u64 total_lat_ns,
                                                             __u64 io_lat_ns)
{
    __u32 total_bucket;
    __u32 io_bucket;

    if (domain >= IOSCHED_KYBER_DOMAIN_NR - 1)
        return;

    total_bucket = iosched_kyber_bucket_for(target_ns, total_lat_ns);
    io_bucket = iosched_kyber_bucket_for(target_ns, io_lat_ns);
    state->lat_buckets[domain][IOSCHED_KYBER_TOTAL_LATENCY][total_bucket]++;
    state->lat_buckets[domain][IOSCHED_KYBER_IO_LATENCY][io_bucket]++;
}

/* calculate_percentile (kyber-iosched.c:219-257): histogram bucket holding
 * the given percentile, or -1 if the window is not mature yet. The window
 * fires on >= MIN_PCT_SAMPLES samples or WINDOW_TIMEOUT_NS after the
 * window's first evaluation attempt, whichever comes first; a firing
 * clears the histogram. */
static __always_inline int iosched_kyber_calculate_percentile(struct iosched_kyber_state *state,
                                                              __u32 domain,
                                                              __u32 type,
                                                              __u32 percentile,
                                                              __u64 now_ns)
{
    __u64 percentile_samples;
    __u32 samples = 0;
    __u32 bucket;
    __u32 i;

    if (domain >= IOSCHED_KYBER_DOMAIN_NR - 1 ||
        type >= IOSCHED_KYBER_LATENCY_TYPES)
        return IOSCHED_KYBER_P99_INVALID;

    for (i = 0; i < IOSCHED_KYBER_LATENCY_BUCKETS; i++)
        samples += state->lat_buckets[domain][type][i];
    if (!samples)
        return IOSCHED_KYBER_P99_INVALID;

    if (!state->window_timeout_ns[domain])
        state->window_timeout_ns[domain] = now_ns + IOSCHED_KYBER_WINDOW_TIMEOUT_NS;
    if (samples < IOSCHED_KYBER_MIN_PCT_SAMPLES &&
        now_ns < state->window_timeout_ns[domain])
        return IOSCHED_KYBER_P99_INVALID;
    state->window_timeout_ns[domain] = 0;

    percentile_samples = (((__u64)samples * percentile) + 99) / 100;
    for (bucket = 0; bucket < IOSCHED_KYBER_LATENCY_BUCKETS - 1; bucket++) {
        if ((__u64)state->lat_buckets[domain][type][bucket] >= percentile_samples)
            break;
        percentile_samples -= state->lat_buckets[domain][type][bucket];
    }
    for (i = 0; i < IOSCHED_KYBER_LATENCY_BUCKETS; i++)
        state->lat_buckets[domain][type][i] = 0;

    return (int)bucket;
}

/* kyber_resize_domain (kyber-iosched.c:259-268): clamp to
 * [MIN_DEPTH, kyber_depth[domain]]. */
static __always_inline void iosched_kyber_resize_domain(struct iosched_kyber_state *state,
                                                        __u32 domain,
                                                        __u32 depth)
{
    if (domain >= IOSCHED_KYBER_DOMAIN_NR)
        return;
    if (depth < IOSCHED_KYBER_MIN_DEPTH)
        depth = IOSCHED_KYBER_MIN_DEPTH;
    if (depth > iosched_kyber_domain_depth_cap(domain))
        depth = iosched_kyber_domain_depth_cap(domain);
    state->depth_limit[domain] = depth;
}

/* kyber_timer_fn (kyber-iosched.c:270-348): congestion flag from the p90
 * of I/O latency of any sampled domain, then per-domain resize from the
 * p99 of total latency (current-depth scaling, bad-latency grows up to x2,
 * good-latency under congestion shrinks down to x0.25, otherwise hold).
 * Native cadence is a <=100ms timer rearmed per completion (timer_reduce,
 * kyber-iosched.c:644); BPF has no timers, so the port evaluates inline at
 * completed_request, timestamp-gated by the window rule. */
static __always_inline void iosched_kyber_update_depths(struct iosched_kyber_state *state,
                                                        __u64 now_ns)
{
    bool bad = false;
    __u32 domain;

    for (domain = 0; domain < IOSCHED_KYBER_DOMAIN_NR - 1; domain++) {
        int p90 = iosched_kyber_calculate_percentile(state, domain,
                                                      IOSCHED_KYBER_IO_LATENCY,
                                                      IOSCHED_KYBER_P90, now_ns);
        if (p90 >= (int)IOSCHED_KYBER_GOOD_BUCKETS)
            bad = true;
    }

    for (domain = 0; domain < IOSCHED_KYBER_DOMAIN_NR - 1; domain++) {
        int p99 = iosched_kyber_calculate_percentile(state, domain,
                                                      IOSCHED_KYBER_TOTAL_LATENCY,
                                                      IOSCHED_KYBER_P99, now_ns);
        __u32 orig_depth;
        __u32 depth;

        if (bad) {
            if (p99 < 0)
                p99 = state->domain_p99[domain];
            state->domain_p99[domain] = IOSCHED_KYBER_P99_INVALID;
        } else if (p99 >= 0) {
            state->domain_p99[domain] = p99;
        }
        if (p99 < 0)
            continue;

        if (bad || p99 >= (int)IOSCHED_KYBER_GOOD_BUCKETS) {
            orig_depth = state->depth_limit[domain] ?
                state->depth_limit[domain] :
                iosched_kyber_domain_depth_cap(domain);
            depth = (orig_depth * (((__u32)p99) + 1)) >> IOSCHED_KYBER_LATENCY_SHIFT;
            iosched_kyber_resize_domain(state, domain, depth);
        }
    }
}

/* kyber_completed_request (kyber-iosched.c:625-645): OTHER is never
 * sampled; every other completion adds one total + one I/O latency
 * sample, then the depths are re-evaluated. */
static __always_inline void iosched_kyber_complete_feedback(struct iosched_kyber_state *state,
                                                            __u32 domain,
                                                            __u32 async_depth,
                                                            __u64 now_ns,
                                                            __u64 total_lat_ns,
                                                            __u64 io_lat_ns)
{
    iosched_kyber_depth_updated(state, async_depth);

    if (domain >= IOSCHED_KYBER_DOMAIN_NR - 1)
        return;

    iosched_kyber_add_latency_sample(state, domain,
                                     iosched_kyber_target_ns(state, domain),
                                     total_lat_ns, io_lat_ns);
    iosched_kyber_update_depths(state, now_ns);
}

static __always_inline bool iosched_kyber_domain_can_dispatch(struct iosched_kyber_state *state,
                                                              __u32 domain)
{
    __u32 limit;

    if (domain >= IOSCHED_KYBER_DOMAIN_NR ||
        (!state->domain_nr[domain] && !state->urgent_nr[domain]))
        return false;

    limit = state->depth_limit[domain] ? state->depth_limit[domain] : IOSCHED_KYBER_MIN_DEPTH;
    return state->inflight[domain] < limit;
}

static __always_inline void iosched_kyber_note_dispatch(struct iosched_kyber_state *state,
                                                        __u32 domain,
                                                        __u64 rq_ptr)
{
    if (rq_ptr && domain < IOSCHED_KYBER_DOMAIN_NR)
        state->inflight[domain]++;
}

static __always_inline __u32 iosched_kyber_find_next_domain(struct iosched_kyber_state *state,
                                                            __u32 start_domain)
{
    __u32 step;

    for (step = 0; step < IOSCHED_KYBER_DOMAIN_NR; step++) {
        __u32 domain = (start_domain + step) % IOSCHED_KYBER_DOMAIN_NR;

        if (iosched_kyber_domain_can_dispatch(state, domain))
            return domain;
    }

    return IOSCHED_INVALID_INDEX;
}

static __always_inline int iosched_kyber_insert(struct iosched_kyber_state *state,
                                                const struct iosched_request_desc *req)
{
    struct iosched_request_desc staged = *req;
    __u32 domain = iosched_kyber_domain_from_desc(req);

    staged.domain = domain;
    if (staged.insert_flags & 0x1)
        return iosched_push_tail_32(state->urgent[domain],
                                    &state->urgent_nr[domain],
                                    &staged);
    return iosched_push_tail_32(state->domains[domain],
                                &state->domain_nr[domain],
                                &staged);
}

static __always_inline __u64 iosched_kyber_dispatch(struct iosched_kyber_state *state)
{
    __u32 domain = state->cur_domain;
    __u64 rq_ptr;

    if (iosched_kyber_domain_can_dispatch(state, domain)) {
        if (state->urgent_nr[domain]) {
            state->batching = 1;
            rq_ptr = iosched_pop_head_32(state->urgent[domain],
                                         &state->urgent_nr[domain]);
            iosched_kyber_note_dispatch(state, domain, rq_ptr);
            return rq_ptr;
        }
        if (state->batching < iosched_kyber_batch_limit(domain)) {
            state->batching++;
            rq_ptr = iosched_pop_head_32(state->domains[domain],
                                         &state->domain_nr[domain]);
            iosched_kyber_note_dispatch(state, domain, rq_ptr);
            return rq_ptr;
        }

        domain = (domain + 1) % IOSCHED_KYBER_DOMAIN_NR;
    } else {
        if (domain < IOSCHED_KYBER_DOMAIN_NR)
            domain = (domain + 1) % IOSCHED_KYBER_DOMAIN_NR;
        else
            domain = IOSCHED_KYBER_DOMAIN_READ;
    }

    domain = iosched_kyber_find_next_domain(state, domain);
    if (domain == IOSCHED_INVALID_INDEX) {
        state->cur_domain = IOSCHED_INVALID_INDEX;
        state->batching = 0;
        return 0;
    }

    state->cur_domain = domain;
    state->batching = 1;
    if (state->urgent_nr[domain]) {
        rq_ptr = iosched_pop_head_32(state->urgent[domain],
                                     &state->urgent_nr[domain]);
        iosched_kyber_note_dispatch(state, domain, rq_ptr);
        return rq_ptr;
    }
    rq_ptr = iosched_pop_head_32(state->domains[domain],
                                 &state->domain_nr[domain]);
    iosched_kyber_note_dispatch(state, domain, rq_ptr);
    return rq_ptr;
}

static __always_inline __u64 iosched_kyber_evacuate(struct iosched_kyber_state *state)
{
    __u32 start_domain = state->cur_domain < IOSCHED_KYBER_DOMAIN_NR ?
        state->cur_domain : IOSCHED_KYBER_DOMAIN_READ;
    __u32 step;

    for (step = 0; step < IOSCHED_KYBER_DOMAIN_NR; step++) {
        __u32 domain = (start_domain + step) % IOSCHED_KYBER_DOMAIN_NR;

        if (state->urgent_nr[domain]) {
            state->cur_domain = domain;
            state->batching = 0;
            return iosched_pop_head_32(state->urgent[domain],
                                       &state->urgent_nr[domain]);
        }
    }

    for (step = 0; step < IOSCHED_KYBER_DOMAIN_NR; step++) {
        __u32 domain = (start_domain + step) % IOSCHED_KYBER_DOMAIN_NR;

        if (state->domain_nr[domain]) {
            state->cur_domain = domain;
            state->batching = 0;
            return iosched_pop_head_32(state->domains[domain],
                                       &state->domain_nr[domain]);
        }
    }

    state->cur_domain = IOSCHED_INVALID_INDEX;
    state->batching = 0;
    return 0;
}

static __always_inline __u32 iosched_bfq_class(const struct iosched_request_desc *req)
{
    return iosched_req_is_sync(req) ? IOSCHED_BFQ_CLASS_SYNC : IOSCHED_BFQ_CLASS_ASYNC;
}

static __always_inline __u32 iosched_bfq_budget_for_class(__u32 flow_class)
{
    return flow_class == IOSCHED_BFQ_CLASS_SYNC ?
           IOSCHED_BFQ_SYNC_BUDGET_SECTORS : IOSCHED_BFQ_ASYNC_BUDGET_SECTORS;
}

static __always_inline __u32 iosched_bfq_weight_from_ioprio(__u64 ioprio)
{
    __u32 cls = (__u32)((ioprio >> 13) & 0x7);
    __u32 lvl = (__u32)(ioprio & 0x7);

    switch (cls) {
    case 1:
        return IOSCHED_BFQ_WEIGHT_RT;
    case 3:
        return IOSCHED_BFQ_WEIGHT_IDLE;
    case 2:
        return IOSCHED_BFQ_WEIGHT_DEFAULT > lvl * 8 ?
               IOSCHED_BFQ_WEIGHT_DEFAULT - lvl * 8 : IOSCHED_BFQ_WEIGHT_IDLE;
    default:
        return IOSCHED_BFQ_WEIGHT_DEFAULT;
    }
}

static __always_inline __u32 iosched_bfq_budget_for_req(const struct iosched_request_desc *req)
{
    __u32 base = iosched_bfq_budget_for_class(iosched_bfq_class(req));
    __u32 weight = req->weight ? req->weight : iosched_bfq_weight_from_ioprio(req->ioprio);

    return (base * weight) / IOSCHED_BFQ_WEIGHT_DEFAULT;
}

static __always_inline __u32 iosched_bfq_flow_base_budget(__u32 flow_class,
                                                           __u32 weight)
{
    __u32 base = iosched_bfq_budget_for_class(flow_class);

    if (!weight)
        weight = IOSCHED_BFQ_WEIGHT_DEFAULT;

    base = (base * weight) / IOSCHED_BFQ_WEIGHT_DEFAULT;
    return base ? base : 1;
}

static __always_inline __u32 iosched_bfq_summary_class(__u32 active_flows,
                                                       __u32 soft_rt_flows,
                                                       __u32 interactive_flows,
                                                       __u32 sync_flows)
{
    if (!active_flows)
        return IOSCHED_BFQ_DISPATCH_CLASS_ASYNC;
    if (soft_rt_flows)
        return IOSCHED_BFQ_DISPATCH_CLASS_SOFT_RT;
    if (interactive_flows)
        return IOSCHED_BFQ_DISPATCH_CLASS_INTERACTIVE;
    if (sync_flows)
        return IOSCHED_BFQ_DISPATCH_CLASS_SYNC;
    return IOSCHED_BFQ_DISPATCH_CLASS_ASYNC;
}

static __always_inline __u32 iosched_bfq_summary_budget(__u32 dispatch_class)
{
    switch (dispatch_class) {
    case IOSCHED_BFQ_DISPATCH_CLASS_SOFT_RT:
    case IOSCHED_BFQ_DISPATCH_CLASS_INTERACTIVE:
    case IOSCHED_BFQ_DISPATCH_CLASS_SYNC:
        return IOSCHED_BFQ_SYNC_BUDGET_SECTORS;
    default:
        return IOSCHED_BFQ_ASYNC_BUDGET_SECTORS;
    }
}

static __always_inline __u32 iosched_bfq_c_look_index(const struct iosched_request_desc *entries,
                                                       __u32 count,
                                                       __u64 last_sector)
{
    __u32 idx;

    if (!count)
        return IOSCHED_INVALID_INDEX;

    idx = iosched_find_first_ge_sector(entries, count, last_sector);
    if (idx == IOSCHED_INVALID_INDEX)
        return 0;
    return idx;
}

static __always_inline __u32 iosched_bfq_charge_cost(__u32 flow_class,
                                                     __u32 cost)
{
    if (!cost)
        cost = 1;

    if (flow_class != IOSCHED_BFQ_CLASS_ASYNC)
        return cost;
    if (cost > ~0U / IOSCHED_BFQ_ASYNC_CHARGE_FACTOR)
        return ~0U;
    return cost * IOSCHED_BFQ_ASYNC_CHARGE_FACTOR;
}

static __always_inline __u32 iosched_bfq_boost_weight(__u32 weight,
                                                       __u32 flow_class,
                                                       bool in_large_burst,
                                                       bool has_short_ttime,
                                                       __u32 requests,
                                                       __u64 now_ns,
                                                       __u64 last_completion_ns,
                                                       __u32 boost_budget)
{
    if (flow_class != IOSCHED_BFQ_CLASS_SYNC)
        return weight;
    if (in_large_burst)
        return weight;
    if (!has_short_ttime)
        return weight;

    if (boost_budget)
        return weight * IOSCHED_BFQ_WEIGHT_BOOST;

    if (requests > IOSCHED_BFQ_INTERACTIVE_MAX_REQUESTS)
        return weight;

    if (!last_completion_ns)
        return weight;

    if (now_ns >= last_completion_ns &&
        now_ns - last_completion_ns <= IOSCHED_BFQ_INTERACTIVE_WINDOW_NS)
        return weight * IOSCHED_BFQ_WEIGHT_BOOST;

    return weight;
}

static __always_inline __u32 iosched_bfq_boost_budget(__u32 requests,
                                                       __u32 flow_class,
                                                       bool in_large_burst,
                                                       bool has_short_ttime,
                                                       __u64 now_ns,
                                                       __u64 last_completion_ns,
                                                       __u32 current_budget)
{
    if (flow_class != IOSCHED_BFQ_CLASS_SYNC)
        return 0;
    if (in_large_burst)
        return 0;
    if (!has_short_ttime)
        return 0;

    if (current_budget)
        return current_budget;

    if (requests > IOSCHED_BFQ_INTERACTIVE_MAX_REQUESTS)
        return 0;
    if (!last_completion_ns)
        return 0;
    if (now_ns >= last_completion_ns &&
        now_ns - last_completion_ns <= IOSCHED_BFQ_INTERACTIVE_WINDOW_NS)
        return IOSCHED_BFQ_MAX_SERVICE_FROM_WR;

    return 0;
}

static __always_inline __u32 iosched_bfq_effective_boost_budget(__u64 now_ns,
                                                                __u32 current_budget,
                                                                __u64 boost_expires_ns)
{
    if (!current_budget)
        return 0;
    if (!boost_expires_ns)
        return current_budget;
    if (now_ns > boost_expires_ns)
        return 0;
    return current_budget;
}

static __always_inline bool iosched_bfq_burst_event(struct iosched_bfq_burst_state *burst,
                                                    __u64 now_ns)
{
    if (!burst->last_activation_ns ||
        now_ns - burst->last_activation_ns > IOSCHED_BFQ_BURST_INTERVAL_NS) {
        burst->last_activation_ns = now_ns;
        burst->burst_size = 1;
        burst->large_burst = false;
        return false;
    }

    burst->last_activation_ns = now_ns;
    if (burst->burst_size < ~0U)
        burst->burst_size++;
    if (burst->burst_size >= IOSCHED_BFQ_LARGE_BURST_THRESH)
        burst->large_burst = true;
    return burst->large_burst;
}

static __always_inline __u64 iosched_bfq_update_think_time(__u64 now_ns,
                                                           __u64 last_insert_ns,
                                                           __u64 think_avg_ns)
{
    __u64 sample;

    if (!last_insert_ns || now_ns <= last_insert_ns)
        return think_avg_ns;

    sample = now_ns - last_insert_ns;
    if (!think_avg_ns)
        return sample;
    return (think_avg_ns * 3 + sample) / 4;
}

static __always_inline bool iosched_bfq_has_short_ttime(__u64 think_avg_ns)
{
    return think_avg_ns && think_avg_ns <= IOSCHED_BFQ_SHORT_TTIME_NS;
}

static __always_inline bool iosched_bfq_interactive_state(bool in_large_burst,
                                                          bool has_short_ttime,
                                                          __u32 requests)
{
    return !in_large_burst && has_short_ttime &&
           requests <= IOSCHED_BFQ_INTERACTIVE_MAX_REQUESTS;
}

static __always_inline bool iosched_bfq_soft_rt_state(__u32 flow_class,
                                                      bool in_large_burst,
                                                      bool has_short_ttime,
                                                      __u32 requests,
                                                      __u64 now_ns,
                                                      __u64 last_completion_ns)
{
    if (flow_class != IOSCHED_BFQ_CLASS_SYNC)
        return false;
    if (in_large_burst || !has_short_ttime)
        return false;
    if (requests > IOSCHED_BFQ_INTERACTIVE_MAX_REQUESTS)
        return false;
    if (!last_completion_ns)
        return false;
    return now_ns >= last_completion_ns &&
           now_ns - last_completion_ns <= IOSCHED_BFQ_INTERACTIVE_WINDOW_NS / 2;
}

static __always_inline __u32 iosched_bfq_limit_depth_value(bool is_sync_read,
                                                           bool is_sync,
                                                           bool interactive,
                                                           bool soft_rt,
                                                           __u32 weight,
                                                           __u32 requests,
                                                           __u32 async_depth)
{
    __u32 depth;

    (void)is_sync_read;

    if (!async_depth)
        async_depth = 1;

    /*
     * Native BFQ can leave sync reads effectively unlimited because its
     * hrtimer/idling machinery provides an independent wakeup path. scx_io is
     * event-driven here, so small nr_requests pools need a shallow-depth cap to
     * prevent fio from parking every scheduler tag inside BPF before any
     * hardware dispatch/completion can provide the next event.
     */
    if (soft_rt)
        return async_depth;
    if (is_sync_read)
        return 1;

    depth = async_depth;
    if (interactive && depth > 1)
        depth = depth > 2 ? depth - 1 : depth;
    if (weight <= IOSCHED_BFQ_WEIGHT_IDLE)
        depth = depth > 1 ? depth / 2 : 1;
    else if (!is_sync)
        depth = depth > 1 ? depth / 2 : 1;

    if (requests > 4)
        depth = 1;

    return depth ? depth : 1;
}

static __always_inline __u64 iosched_bfq_entity_id(const struct iosched_request_desc *req)
{
    if (req->issuer_id)
        return req->issuer_id;
    if (req->cgroup_id)
        return req->cgroup_id;
    if (req->owner_id)
        return req->owner_id;
    return 1;
}

static __always_inline int iosched_insert_sorted_sector_8(struct iosched_request_desc *entries,
                                                          __u32 *count,
                                                          const struct iosched_request_desc *req)
{
    if (*count >= IOSCHED_BFQ_FLOW_DEPTH)
        return -1;

    if (*count == 0) {
        entries[0] = *req;
        *count = 1;
        return 0;
    }

    if (req->sector < entries[0].sector) {
        entries[1] = entries[0];
        entries[0] = *req;
    } else {
        entries[1] = *req;
    }
    *count = 2;
    return 0;
}

static __always_inline __u64 iosched_pop_head_8(struct iosched_request_desc *entries,
                                                __u32 *count,
                                                __u32 *cost_out)
{
    struct iosched_request_desc picked;

    if (!*count)
        return 0;

    picked = entries[0];
    if (*count > 1)
        entries[0] = entries[1];
    (*count)--;
    if (cost_out)
        *cost_out = picked.cost;
    return picked.rq_ptr;
}

static __always_inline void iosched_bfq_reset_flow(struct iosched_bfq_flow *flow)
{
    flow->entity_id = 0;
    flow->flow_class = IOSCHED_BFQ_CLASS_SYNC;
    flow->weight = IOSCHED_BFQ_WEIGHT_DEFAULT;
    flow->pending_nr = 0;
    flow->budget_left = 0;
}

static __always_inline void iosched_bfq_replenish(struct iosched_bfq_state *state)
{
    if (state->flows[0].pending_nr)
        state->flows[0].budget_left = iosched_bfq_budget_for_class(state->flows[0].flow_class) *
                                      state->flows[0].weight / IOSCHED_BFQ_WEIGHT_DEFAULT;
    if (state->flows[1].pending_nr)
        state->flows[1].budget_left = iosched_bfq_budget_for_class(state->flows[1].flow_class) *
                                      state->flows[1].weight / IOSCHED_BFQ_WEIGHT_DEFAULT;
}

static __always_inline int iosched_bfq_flow_can_dispatch(struct iosched_bfq_flow *flow)
{
    if (!flow->pending_nr || !flow->budget_left)
        return 0;

    return flow->pending[0].cost <= flow->budget_left;
}

static __always_inline int iosched_bfq_insert(struct iosched_bfq_state *state,
                                              struct iosched_request_desc req)
{
    __u64 entity_id;
    __u32 flow_class;
    struct iosched_bfq_flow *flow;

    if (!req.cost)
        req.cost = 1;

    entity_id = iosched_bfq_entity_id(&req);
    flow_class = iosched_bfq_class(&req);

    if (state->flows[0].pending_nr &&
        state->flows[0].entity_id == entity_id &&
        state->flows[0].flow_class == flow_class) {
        flow = &state->flows[0];
    } else if (state->flows[1].pending_nr &&
               state->flows[1].entity_id == entity_id &&
               state->flows[1].flow_class == flow_class) {
        flow = &state->flows[1];
    } else if (!state->flows[0].pending_nr) {
        flow = &state->flows[0];
        flow->entity_id = entity_id;
        flow->flow_class = flow_class;
        flow->weight = req.weight ? req.weight : iosched_bfq_weight_from_ioprio(req.ioprio);
        flow->budget_left = iosched_bfq_budget_for_req(&req);
    } else if (!state->flows[1].pending_nr) {
        flow = &state->flows[1];
        flow->entity_id = entity_id;
        flow->flow_class = flow_class;
        flow->weight = req.weight ? req.weight : iosched_bfq_weight_from_ioprio(req.ioprio);
        flow->budget_left = iosched_bfq_budget_for_req(&req);
    } else {
        return -1;
    }

    return iosched_insert_sorted_sector_8(flow->pending,
                                          &flow->pending_nr,
                                          &req);
}

static __always_inline int iosched_bfq_pick_slot(struct iosched_bfq_state *state)
{
    if (state->current_slot < IOSCHED_BFQ_MAX_FLOWS &&
        iosched_bfq_flow_can_dispatch(&state->flows[state->current_slot]))
        return (int)state->current_slot;

    if (iosched_bfq_flow_can_dispatch(&state->flows[0])) {
        state->current_slot = 0;
        state->next_slot = 1;
        return 0;
    }
    if (iosched_bfq_flow_can_dispatch(&state->flows[1])) {
        state->current_slot = 1;
        state->next_slot = 0;
        return 1;
    }

    iosched_bfq_replenish(state);

    if (iosched_bfq_flow_can_dispatch(&state->flows[0])) {
        state->current_slot = 0;
        state->next_slot = 1;
        return 0;
    }
    if (iosched_bfq_flow_can_dispatch(&state->flows[1])) {
        state->current_slot = 1;
        state->next_slot = 0;
        return 1;
    }

    return -1;
}

static __always_inline __u64 iosched_bfq_dispatch(struct iosched_bfq_state *state)
{
    struct iosched_bfq_flow *flow;
    __u32 cost = 0;
    __u32 charge = 0;
    __u64 rq_ptr;
    int slot;

    slot = iosched_bfq_pick_slot(state);
    if (slot < 0)
        return 0;

    flow = &state->flows[slot];
    rq_ptr = iosched_pop_head_8(flow->pending, &flow->pending_nr, &cost);
    if (!rq_ptr)
        return 0;

    charge = iosched_bfq_charge_cost(flow->flow_class, cost);
    if (charge >= flow->budget_left)
        flow->budget_left = 0;
    else
        flow->budget_left -= charge;

    if (!flow->pending_nr) {
        iosched_bfq_reset_flow(flow);
        state->current_slot = IOSCHED_INVALID_INDEX;
        return rq_ptr;
    }

    if (!iosched_bfq_flow_can_dispatch(flow))
        state->current_slot = IOSCHED_INVALID_INDEX;

    return rq_ptr;
}

#endif
