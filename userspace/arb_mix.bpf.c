#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"

char LICENSE[] SEC("license") = "GPL";

#define MIX_CLASS_LATENCY_MAX_PRIO 3U
#define MIX_LATENCY_BURST_LIMIT 8U
#define MIX_BULK_MAX_WAIT_NS 10000000ULL

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

struct arb_mix_state {
    __u64 latency_burst;
    __u64 last_bulk_dispatch_ns;
    __u64 latency_dispatches;
    __u64 bulk_dispatches;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, struct arb_mix_state);
} arb_state SEC(".maps");

static __always_inline bool meta_is_latency_class(const struct iosched_policy_meta *meta)
{
    return meta->arb_prio < MIX_CLASS_LATENCY_MAX_PRIO;
}

static __always_inline __u64 pick_class(bool latency_class,
                                        __u32 base,
                                        __u32 *picked_idx)
{
    __u32 step;

    for (step = 0; step < 64; step++) {
        __u32 idx = (base + step) % 64;
        __u64 *cgid;
        struct iosched_policy_meta *meta;

        cgid = bpf_map_lookup_elem(&active_cgroup_ids, &idx);
        if (!cgid || *cgid == 0)
            continue;

        meta = bpf_map_lookup_elem(&policy_meta, cgid);
        if (!meta || meta->queue_depth == 0)
            continue;

        if (meta_is_latency_class(meta) != latency_class)
            continue;

        *picked_idx = idx;
        return *cgid;
    }

    return 0;
}

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    struct arb_mix_state *state;
    __u64 now = bpf_ktime_get_ns();
    __u64 cgid = 0;
    __u32 base;
    __u32 picked_idx = 0;
    __u32 key;
    bool force_bulk = false;

    if (!dctx)
        return 0;

    key = (__u32)dctx->hctx_idx;
    state = bpf_map_lookup_elem(&arb_state, &key);
    base = ((__u32)(now >> 8) + (__u32)dctx->hctx_idx) % 64;

    if (state) {
        if (state->latency_burst >= MIX_LATENCY_BURST_LIMIT)
            force_bulk = true;
        if (state->last_bulk_dispatch_ns &&
            now - state->last_bulk_dispatch_ns >= MIX_BULK_MAX_WAIT_NS)
            force_bulk = true;
    }

    if (force_bulk)
        cgid = pick_class(false, base, &picked_idx);

    if (!cgid)
        cgid = pick_class(true, base, &picked_idx);

    if (!cgid)
        cgid = pick_class(false, base, &picked_idx);

    if (state && cgid) {
        struct iosched_policy_meta *meta;

        meta = bpf_map_lookup_elem(&policy_meta, &cgid);
        if (meta && meta_is_latency_class(meta)) {
            state->latency_burst++;
            state->latency_dispatches++;
        } else {
            state->latency_burst = 0;
            state->last_bulk_dispatch_ns = now;
            state->bulk_dispatches++;
        }
    }

    IOEXT_BPF_DBG_SAMPLED(64,
                          "[arb_mix] pick cg=%llu idx=%u force_bulk=%u\n",
                          cgid, picked_idx, force_bulk ? 1 : 0);
    return cgid;
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops mix_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .name = "arb_mix",
};
