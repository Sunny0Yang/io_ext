#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"

char LICENSE[] SEC("license") = "GPL";

#define SP_PRIO_LEVELS 8

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, __u64);
} active_cgroup_ids SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u64);
    __type(value, struct iosched_policy_meta);
} policy_meta SEC(".maps");

/*
 * Per-hctx sticky cursor: the scan starts at cursor and the cursor
 * advances past the slot that won the pick, so backlogged tenants at the
 * same priority level are served in strict round-robin.  The previous
 * time-hash base ((ktime>>8)+hctx)%64 was a lottery that picked the
 * first matching slot clockwise from a random point: with slots packed
 * at 0..4 the earliest-registered tenant at each level won ~57/64 of the
 * picks (D3 hetero reproduced 3.07:1 BE1 skew, 20260916 baseline 28:1).
 */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 64);
    __type(key, __u32);
    __type(value, __u32);
} sp_cursor SEC(".maps");

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    __u32 base;
    __u32 prio;
    __u32 *cursor;
    __u32 hctx_idx;

    if (!dctx)
        return 0;

    hctx_idx = (__u32)dctx->hctx_idx;
    cursor = bpf_map_lookup_elem(&sp_cursor, &hctx_idx);
    if (!cursor)
        return 0;
    base = *cursor;

    for (prio = 0; prio < SP_PRIO_LEVELS; prio++) {
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

            if (meta->arb_prio >= SP_PRIO_LEVELS)
                continue;

            if (meta->arb_prio != prio)
                continue;

            *cursor = (idx + 1) % 64;
            return *cgid;
        }
    }

    return 0;
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops sp_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .name = "arb_sp",
};