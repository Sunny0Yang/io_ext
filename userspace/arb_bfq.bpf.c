#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"

char LICENSE[] SEC("license") = "GPL";

#define ARB_BFQ_VTIME_SHIFT 20U
#define ARB_BFQ_IDLE_NS     2000000ULL

struct arb_bfq_tenant_key {
    __u64 cgroup_id;
    __u32 hctx_idx;
    __u32 reserved;
};

struct iosched_bfq_arbiter_tenant_state {
    __u64 start_tag;
    __u64 finish_tag;
};

struct iosched_bfq_arbiter_hctx_state {
    __u64 virtual_time;
    __u64 idle_until_ns;
    __u64 idle_cgroup_id;
    __u64 last_cgroup_id;
    __u64 last_finish_tag;
    __u32 last_dispatch_class;
    __u32 reserved;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, __u64);
} active_cgroup_ids SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 64);
    __type(key, __u64);
    __type(value, struct iosched_policy_meta);
} policy_meta SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, IOSCHED_BFQ_ACTIVE_FLOW_LIMIT);
    __type(key, struct iosched_bfq_tenant_key);
    __type(value, struct iosched_bfq_cgroup_state);
} bfq_cgroup_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, struct arb_bfq_tenant_key);
    __type(value, struct iosched_bfq_arbiter_tenant_state);
} arbiter_tenant_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct iosched_bfq_arbiter_hctx_state);
} arbiter_hctx_state SEC(".maps");

static __always_inline struct iosched_bfq_arbiter_hctx_state *hctx_state_lookup(__u64 hctx_idx)
{
    __u32 key = hctx_idx;

    return bpf_map_lookup_elem(&arbiter_hctx_state, &key);
}

static __always_inline struct iosched_bfq_cgroup_state *summary_lookup(__u64 cgroup_id,
                                                                        __u64 hctx_idx)
{
    struct iosched_bfq_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
    };

    return bpf_map_lookup_elem(&bfq_cgroup_state, &key);
}

static __always_inline struct iosched_bfq_arbiter_tenant_state *tenant_state_lookup(__u64 cgroup_id,
                                                                                     __u64 hctx_idx)
{
    struct arb_bfq_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
    };

    return bpf_map_lookup_elem(&arbiter_tenant_state, &key);
}

static __always_inline struct iosched_bfq_arbiter_tenant_state *tenant_state_ensure(__u64 cgroup_id,
                                                                                      __u64 hctx_idx)
{
    struct iosched_bfq_arbiter_tenant_state zero = {};
    struct arb_bfq_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
    };

    bpf_map_update_elem(&arbiter_tenant_state, &key, &zero, BPF_NOEXIST);
    return bpf_map_lookup_elem(&arbiter_tenant_state, &key);
}

static __always_inline __u64 arb_bfq_service_tag(__u32 service,
                                                 __u32 weight)
{
    __u64 scaled;

    if (!service)
        service = 1;
    if (!weight)
        weight = IOSCHED_BFQ_WEIGHT_DEFAULT;

    scaled = ((__u64)service << ARB_BFQ_VTIME_SHIFT) + weight - 1;
    scaled /= weight;
    return scaled ? scaled : 1;
}

static __always_inline bool arb_bfq_cgroup_eligible(const struct iosched_policy_meta *meta,
                                                    const struct iosched_bfq_cgroup_state *state,
                                                    __u32 dispatch_class)
{
    return meta && state && meta->queue_depth > 0 &&
           state->active_flows > 0 &&
           state->dispatch_class == dispatch_class;
}

static __always_inline void arb_bfq_clear_idle(struct iosched_bfq_arbiter_hctx_state *state)
{
    state->idle_until_ns = 0;
    state->idle_cgroup_id = 0;
}

static __always_inline bool arb_bfq_any_queued_work(struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 step;

    if (!dctx)
        return false;

    for (step = 0; step < 64; step++) {
        __u32 idx = step;
        __u64 *cgid;
        struct iosched_policy_meta *meta;
        struct iosched_bfq_cgroup_state *shared;

        cgid = bpf_map_lookup_elem(&active_cgroup_ids, &idx);
        if (!cgid || *cgid == 0)
            continue;

        meta = bpf_map_lookup_elem(&policy_meta, cgid);
        if (meta && (meta->queue_depth > 0 || meta->active_inserts > 0))
            return true;

        shared = summary_lookup(*cgid, dctx->hctx_idx);
        if (shared && shared->queued_requests > 0)
            return true;
    }

    return false;
}

static __always_inline __u64 arb_bfq_pick_class(struct iosched_arbiter_dispatch_ctx *dctx,
                                                struct iosched_bfq_arbiter_hctx_state *hctx_state,
                                                __u32 dispatch_class,
                                                __u32 base)
{
    __u64 eligible_cgid = 0;
    __u64 eligible_finish = ~0ULL;
    __u64 future_cgid = 0;
    __u64 future_start = ~0ULL;
    __u64 future_finish = ~0ULL;
    __u64 chosen_start = 0;
    __u32 chosen_class = IOSCHED_BFQ_DISPATCH_CLASS_ASYNC;
    __u32 step;

    for (step = 0; step < 64; step++) {
        __u32 idx = (base + step) % 64;
        __u64 *cgid;
        struct iosched_policy_meta *meta;
        struct iosched_bfq_cgroup_state *shared;
        struct iosched_bfq_arbiter_tenant_state *tenant;
        __u32 service;
        __u64 start;
        __u64 finish;

        cgid = bpf_map_lookup_elem(&active_cgroup_ids, &idx);
        if (!cgid || *cgid == 0)
            continue;

        meta = bpf_map_lookup_elem(&policy_meta, cgid);
        shared = summary_lookup(*cgid, dctx->hctx_idx);
        if (!arb_bfq_cgroup_eligible(meta, shared, dispatch_class))
            continue;

        tenant = tenant_state_lookup(*cgid, dctx->hctx_idx);
        start = tenant ? tenant->finish_tag : 0;
        service = shared->next_cost ? shared->next_cost : shared->dispatch_budget;
        finish = start + arb_bfq_service_tag(service,
                                             meta->arb_weight);

        if (start <= hctx_state->virtual_time) {
            if (!eligible_cgid || finish < eligible_finish) {
                eligible_cgid = *cgid;
                eligible_finish = finish;
                chosen_start = start;
                chosen_class = shared->dispatch_class;
            }
            continue;
        }

        if (!future_cgid || start < future_start ||
            (start == future_start && finish < future_finish)) {
            future_cgid = *cgid;
            future_start = start;
            future_finish = finish;
            chosen_start = start;
            chosen_class = shared->dispatch_class;
        }
    }

    if (!eligible_cgid && future_cgid) {
        if (future_start > hctx_state->virtual_time)
            hctx_state->virtual_time = future_start;
        eligible_cgid = future_cgid;
        eligible_finish = future_finish;
    }

    if (!eligible_cgid)
        return 0;

    {
        struct iosched_bfq_arbiter_tenant_state *tenant;

        tenant = tenant_state_ensure(eligible_cgid, dctx->hctx_idx);
        if (!tenant)
            return 0;
        tenant->start_tag = chosen_start;
        tenant->finish_tag = eligible_finish;
    }

    hctx_state->last_cgroup_id = eligible_cgid;
    hctx_state->last_finish_tag = eligible_finish;
    hctx_state->last_dispatch_class = chosen_class;
    arb_bfq_clear_idle(hctx_state);

    IOEXT_BPF_DBG_SAMPLED(64,
                          "[arb_bfq] pick cg=%llu class=%u vtime=%llu finish=%llu\n",
                          eligible_cgid,
                          chosen_class,
                          hctx_state->virtual_time,
                          eligible_finish);
    return eligible_cgid;
}

SEC("struct_ops/hold_dispatch")
bool BPF_PROG(hold_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct iosched_bfq_arbiter_hctx_state *hctx_state;
    struct iosched_policy_meta *meta;
    struct iosched_bfq_cgroup_state *shared;
    __u64 idle_cgroup_id;
    __u64 now;

    if (!dctx)
        return false;

    hctx_state = hctx_state_lookup(dctx->hctx_idx);
    if (!hctx_state || !hctx_state->idle_cgroup_id)
        return false;

    now = bpf_ktime_get_ns();
    if (now >= hctx_state->idle_until_ns) {
        arb_bfq_clear_idle(hctx_state);
        return false;
    }

    idle_cgroup_id = hctx_state->idle_cgroup_id;
    if (arb_bfq_any_queued_work(dctx)) {
        arb_bfq_clear_idle(hctx_state);
        return false;
    }

    meta = bpf_map_lookup_elem(&policy_meta, &idle_cgroup_id);
    shared = summary_lookup(idle_cgroup_id, dctx->hctx_idx);
    if (!meta || !shared || shared->queued_requests > 0 || meta->queue_depth > 0) {
        arb_bfq_clear_idle(hctx_state);
        return false;
    }

    IOEXT_BPF_DBG_SAMPLED(128,
                          "[arb_bfq] hold hctx=%llu idle_cg=%llu until=%llu\n",
                          dctx->hctx_idx,
                          hctx_state->idle_cgroup_id,
                          hctx_state->idle_until_ns);
    return true;
}

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct iosched_bfq_arbiter_hctx_state *hctx_state;
    __u32 base;
    __u32 dispatch_class;

    if (!dctx)
        return 0;

    hctx_state = hctx_state_lookup(dctx->hctx_idx);
    if (!hctx_state)
        return 0;

    base = ((__u32)(bpf_ktime_get_ns() >> 8) + (__u32)dctx->hctx_idx) % 64;
    for (dispatch_class = 0; dispatch_class < IOSCHED_BFQ_DISPATCH_CLASS_NR; dispatch_class++) {
        __u64 cgroup_id;

        cgroup_id = arb_bfq_pick_class(dctx, hctx_state, dispatch_class, base);
        if (cgroup_id)
            return cgroup_id;
    }

    return 0;
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *cctx)
{
    struct iosched_bfq_arbiter_hctx_state *hctx_state;
    struct iosched_bfq_arbiter_tenant_state *tenant;
    struct iosched_policy_meta *meta;
    struct iosched_bfq_cgroup_state *shared;
    __u64 cgroup_id;

    if (!cctx)
        return;

    hctx_state = hctx_state_lookup(cctx->hctx_idx);
    if (!hctx_state)
        return;

    cgroup_id = cctx->cgroup_id;
    if (cgroup_id) {
        tenant = tenant_state_lookup(cgroup_id, cctx->hctx_idx);
        if (tenant && tenant->finish_tag > hctx_state->virtual_time)
            hctx_state->virtual_time = tenant->finish_tag;
        hctx_state->last_cgroup_id = cgroup_id;
    }

    meta = cgroup_id ? bpf_map_lookup_elem(&policy_meta, &cgroup_id) : NULL;
    shared = cgroup_id ? summary_lookup(cgroup_id, cctx->hctx_idx) : NULL;
    if (cgroup_id &&
        hctx_state->last_dispatch_class <= IOSCHED_BFQ_DISPATCH_CLASS_INTERACTIVE &&
        meta && shared && shared->queued_requests == 0 && meta->queue_depth == 0) {
        hctx_state->idle_cgroup_id = cgroup_id;
        hctx_state->idle_until_ns = cctx->now + ARB_BFQ_IDLE_NS;
    } else {
        arb_bfq_clear_idle(hctx_state);
    }
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops bfq_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .hold_dispatch = (void *)hold_dispatch,
    .completed_request = (void *)completed_request,
    .name = "arb_bfq",
};
