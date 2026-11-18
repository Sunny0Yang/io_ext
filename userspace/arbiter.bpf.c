#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_policy_shared.h"

char LICENSE[] SEC("license") = "GPL";

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

SEC("struct_ops/pick_cgroup")
__u64 BPF_PROG(pick_cgroup, struct iosched_arbiter_dispatch_ctx *dctx)
{
    IOEXT_BPF_DBG_SAMPLED(128,"arbiter noop: pick_cgroup called, deferring to kernel fallback\n");
     return 0;
}

SEC(".struct_ops.link")
struct iosched_arbiter_ops dummy_arbiter = {
    .pick_cgroup = (void *)pick_cgroup,
    .name = "arbiter",
};
