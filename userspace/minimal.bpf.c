#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "ioext_bpf_debug.h"
#include "ioext_bpf_common.h"

char LICENSE[] SEC("license") = "GPL";

IOEXT_POLICY_META_MAP(policy_meta);

SEC("struct_ops/init_queue")
int BPF_PROG(init_queue, struct iosched_queue_ctx *qctx)
{
    IOEXT_BPF_DBG("minimal init_queue called\n");
    if (!qctx) {
        IOEXT_BPF_DBG("minimal init_queue ctx is NULL\n");
        return -1;
    }
    if (!qctx->q) {
        IOEXT_BPF_DBG("minimal init_queue ctx->q is NULL\n");
        return -1;
    }
    IOEXT_BPF_DBG("minimal init_queue OK\n");
    return 0;
}

SEC("struct_ops/exit_queue")
int BPF_PROG(exit_queue, struct iosched_queue_ctx *qctx)
{
    IOEXT_BPF_DBG("minimal exit_queue called\n");
    if (!qctx || !qctx->q) {
        IOEXT_BPF_DBG("minimal exit_queue ctx/q is NULL\n");
        return -1;
    }
    IOEXT_BPF_DBG("minimal exit_queue OK\n");
    return 0;
}

SEC("struct_ops/insert_request")
int BPF_PROG(insert_request, struct iosched_insert_ctx *ictx)
{
    IOEXT_BPF_DBG("minimal insert_request called\n");
    
    /* Validate ctx pointer first */
    if (!ictx) {
        IOEXT_BPF_DBG("minimal insert_request ctx is NULL\n");
        return -1;
    }
    
    /* Access ctx->q (offset 0) */
    if (!ictx->q) {
        IOEXT_BPF_DBG("minimal insert_request ctx->q is NULL\n");
        return -1;
    }
    
    /* Access ctx->rq (offset 8) */
    if (!ictx->rq) {
        IOEXT_BPF_DBG("minimal insert_request ctx->rq is NULL\n");
        return -1;
    }

    IOEXT_BPF_DBG("minimal insert_request all fields OK, rejecting to C-fallback\n");
    return -1; /* Always reject, forcing C-side fallback */
}

SEC("struct_ops/cgroup_dispatch")
__u64 BPF_PROG(cgroup_dispatch, struct iosched_arbiter_dispatch_ctx *dctx)
{
    IOEXT_BPF_DBG("minimal cgroup_dispatch called\n");
    if (!dctx) {
        IOEXT_BPF_DBG("minimal cgroup_dispatch ctx is NULL\n");
        return 0;
    }
    IOEXT_BPF_DBG("minimal cgroup_dispatch OK\n");
    return 0; 
}

SEC("struct_ops/evacuate")
__u64 BPF_PROG(evacuate, struct iosched_arbiter_dispatch_ctx *dctx)
{
    IOEXT_BPF_DBG("minimal evacuate called\n");
    if (!dctx) {
        IOEXT_BPF_DBG("minimal evacuate ctx is NULL\n");
        return 0;
    }
    IOEXT_BPF_DBG("minimal evacuate OK\n");
    return 0;
}

SEC("struct_ops/init_hctx")
int BPF_PROG(init_hctx, struct iosched_hctx_ctx *hctx)
{
    IOEXT_BPF_DBG("minimal init_hctx called\n");
    if (!hctx) {
        IOEXT_BPF_DBG("minimal init_hctx ctx is NULL\n");
        return -1;
    }
    if (!hctx->hctx) {
        IOEXT_BPF_DBG("minimal init_hctx ctx->hctx is NULL\n");
        return -1;
    }
    IOEXT_BPF_DBG("minimal init_hctx OK\n");
    return 0;
}

SEC("struct_ops/exit_hctx")
void BPF_PROG(exit_hctx, struct iosched_hctx_ctx *hctx)
{
    IOEXT_BPF_DBG("minimal exit_hctx called\n");
    if (!hctx) {
        IOEXT_BPF_DBG("minimal exit_hctx ctx is NULL\n");
        return;
    }
    if (!hctx->hctx) {
        IOEXT_BPF_DBG("minimal exit_hctx ctx->hctx is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal exit_hctx OK\n");
}

SEC("struct_ops/init_icq")
void BPF_PROG(init_icq, struct iosched_icq_ctx *ictx)
{
    IOEXT_BPF_DBG("minimal init_icq called\n");
    if (!ictx) {
        IOEXT_BPF_DBG("minimal init_icq ctx is NULL\n");
        return;
    }
    if (!ictx->icq) {
        IOEXT_BPF_DBG("minimal init_icq ctx->icq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal init_icq OK\n");
}

SEC("struct_ops/exit_icq")
void BPF_PROG(exit_icq, struct iosched_icq_ctx *ictx)
{
    IOEXT_BPF_DBG("minimal exit_icq called\n");
    if (!ictx) {
        IOEXT_BPF_DBG("minimal exit_icq ctx is NULL\n");
        return;
    }
    if (!ictx->icq) {
        IOEXT_BPF_DBG("minimal exit_icq ctx->icq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal exit_icq OK\n");
}

SEC("struct_ops/allow_merge")
bool BPF_PROG(allow_merge, struct iosched_allow_merge_ctx *mctx)
{
    IOEXT_BPF_DBG("minimal allow_merge called\n");
    if (!mctx) {
        IOEXT_BPF_DBG("minimal allow_merge ctx is NULL\n");
        return false;
    }
    if (!mctx->rq) {
        IOEXT_BPF_DBG("minimal allow_merge ctx->rq is NULL\n");
        return false;
    }
    if (!mctx->bio) {
        IOEXT_BPF_DBG("minimal allow_merge ctx->bio is NULL\n");
        return false;
    }
    IOEXT_BPF_DBG("minimal allow_merge OK\n");
    return true;
}

SEC("struct_ops/bio_merge")
bool BPF_PROG(bio_merge, struct iosched_bio_merge_ctx *mctx)
{
    IOEXT_BPF_DBG("minimal bio_merge called\n");
    if (!mctx) {
        IOEXT_BPF_DBG("minimal bio_merge ctx is NULL\n");
        return false;
    }
    if (!mctx->bio) {
        IOEXT_BPF_DBG("minimal bio_merge ctx->bio is NULL\n");
        return false;
    }
    IOEXT_BPF_DBG("minimal bio_merge OK\n");
    return false;
}

SEC("struct_ops/request_merge")
__u64 BPF_PROG(request_merge, struct iosched_request_merge_ctx *mctx)
{
    IOEXT_BPF_DBG("minimal request_merge called\n");
    if (!mctx) {
        IOEXT_BPF_DBG("minimal request_merge ctx is NULL\n");
        return ELEVATOR_NO_MERGE;
    }
    if (!mctx->bio) {
        IOEXT_BPF_DBG("minimal request_merge ctx->bio is NULL\n");
        return ELEVATOR_NO_MERGE;
    }
    IOEXT_BPF_DBG("minimal request_merge OK\n");
    return ELEVATOR_NO_MERGE;
}

SEC("struct_ops/request_merged")
void BPF_PROG(request_merged, struct iosched_request_merged_ctx *mctx)
{
    IOEXT_BPF_DBG("minimal request_merged called\n");
    if (!mctx) {
        IOEXT_BPF_DBG("minimal request_merged ctx is NULL\n");
        return;
    }
    if (!mctx->rq) {
        IOEXT_BPF_DBG("minimal request_merged ctx->rq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal request_merged OK\n");
}

SEC("struct_ops/requests_merged")
void BPF_PROG(requests_merged, struct iosched_requests_merged_ctx *mctx)
{
    IOEXT_BPF_DBG("minimal requests_merged called\n");
    if (!mctx) {
        IOEXT_BPF_DBG("minimal requests_merged ctx is NULL\n");
        return;
    }
    if (!mctx->rq) {
        IOEXT_BPF_DBG("minimal requests_merged ctx->rq is NULL\n");
        return;
    }
    if (!mctx->next) {
        IOEXT_BPF_DBG("minimal requests_merged ctx->next is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal requests_merged OK\n");
}

SEC("struct_ops/limit_depth")
__u64 BPF_PROG(limit_depth, struct iosched_limit_depth_ctx *dctx)
{
    IOEXT_BPF_DBG("minimal limit_depth called\n");
    if (!dctx) {
        IOEXT_BPF_DBG("minimal limit_depth ctx is NULL\n");
        return 0;
    }
    if (!dctx->data) {
        IOEXT_BPF_DBG("minimal limit_depth ctx->data is NULL\n");
        return 0;
    }
    IOEXT_BPF_DBG("minimal limit_depth OK\n");
    return 0;
}

SEC("struct_ops/depth_updated")
void BPF_PROG(depth_updated, struct iosched_depth_updated_ctx *dctx)
{
    IOEXT_BPF_DBG("minimal depth_updated called\n");
    if (!dctx) {
        IOEXT_BPF_DBG("minimal depth_updated ctx is NULL\n");
        return;
    }
    if (!dctx->q) {
        IOEXT_BPF_DBG("minimal depth_updated ctx->q is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal depth_updated OK\n");
}

SEC("struct_ops/prepare_request")
void BPF_PROG(prepare_request, struct iosched_rq_ctx *rctx)
{
    IOEXT_BPF_DBG("minimal prepare_request called\n");
    if (!rctx) {
        IOEXT_BPF_DBG("minimal prepare_request ctx is NULL\n");
        return;
    }
    if (!rctx->rq) {
        IOEXT_BPF_DBG("minimal prepare_request ctx->rq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal prepare_request OK\n");
}

SEC("struct_ops/finish_request")
void BPF_PROG(finish_request, struct iosched_rq_ctx *rctx)
{
    IOEXT_BPF_DBG("minimal finish_request called\n");
    if (!rctx) {
        IOEXT_BPF_DBG("minimal finish_request ctx is NULL\n");
        return;
    }
    if (!rctx->rq) {
        IOEXT_BPF_DBG("minimal finish_request ctx->rq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal finish_request OK\n");
}

SEC("struct_ops/completed_request")
void BPF_PROG(completed_request, struct iosched_completed_request_ctx *rctx)
{
    IOEXT_BPF_DBG("minimal completed_request called\n");
    if (!rctx) {
        IOEXT_BPF_DBG("minimal completed_request ctx is NULL\n");
        return;
    }
    if (!rctx->rq) {
        IOEXT_BPF_DBG("minimal completed_request ctx->rq is NULL\n");
        return;
    }
    IOEXT_BPF_DBG("minimal completed_request OK\n");
}

SEC("struct_ops/former_request")
__u64 BPF_PROG(former_request, struct iosched_rq_ctx *rctx)
{
    IOEXT_BPF_DBG("minimal former_request called\n");
    if (!rctx) {
        IOEXT_BPF_DBG("minimal former_request ctx is NULL\n");
        return 0;
    }
    if (!rctx->rq) {
        IOEXT_BPF_DBG("minimal former_request ctx->rq is NULL\n");
        return 0;
    }
    IOEXT_BPF_DBG("minimal former_request OK\n");
    return 0;
}

SEC("struct_ops/next_request")
__u64 BPF_PROG(next_request, struct iosched_rq_ctx *rctx)
{
    IOEXT_BPF_DBG("minimal next_request called\n");
    if (!rctx) {
        IOEXT_BPF_DBG("minimal next_request ctx is NULL\n");
        return 0;
    }
    if (!rctx->rq) {
        IOEXT_BPF_DBG("minimal next_request ctx->rq is NULL\n");
        return 0;
    }
    IOEXT_BPF_DBG("minimal next_request OK\n");
    return 0;
}

SEC(".struct_ops.link")
struct iosched_ext_ops scx_template = {
    .init_queue      = (void *)init_queue,
    .exit_queue      = (void *)exit_queue,
    .insert_request  = (void *)insert_request,
    .cgroup_dispatch = (void *)cgroup_dispatch,
    .evacuate        = (void *)evacuate,
    .init_hctx       = (void *)init_hctx,
    .exit_hctx       = (void *)exit_hctx,
    .init_icq        = (void *)init_icq,
    .exit_icq        = (void *)exit_icq,
    .allow_merge     = (void *)allow_merge,
    .bio_merge       = (void *)bio_merge,
    .request_merge   = (void *)request_merge,
    .request_merged  = (void *)request_merged,
    .requests_merged = (void *)requests_merged,
    .limit_depth     = (void *)limit_depth,
    .depth_updated   = (void *)depth_updated,
    .prepare_request = (void *)prepare_request,
    .finish_request  = (void *)finish_request,
    .completed_request = (void *)completed_request,
    .former_request  = (void *)former_request,
    .next_request    = (void *)next_request,
    .name            = "scx_template",
};