#!/bin/bash
# =========================================================================
# experiments/reproduction / mq_deadline / arms_native.sh
# 实验1 mq-deadline，native 臂：echo mq-deadline > /sys/block/vdb/queue/scheduler
# 激活方式核对：native elevator 走 scheduler sysfs（[PLAN] 固定；本内核
# block/elev 路径标准接口，读回格式 "<list> [active]"）。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="mq_deadline_native_r${REP}"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
# 臂级断言：可用列表含 mq-deadline；当前为 none（激活前基线）
grep -qw mq-deadline "$IOEXT_SCHED" || env_die "mq-deadline 不在可用 scheduler 列表"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"

# ---- 激活 ----------------------------------------------------------------
sched_set mq-deadline

# ---- 运行（与 none/iox 完全相同 workload [PLAN]） -------------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_CG_ROOT" "$HERE/fio_native.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
# 断言：整个运行期间 scheduler 保持 mq-deadline（读回快照已在 postflight.txt）
[ "$(sched_current)" = "mq-deadline" ] || env_die "运行后 scheduler != mq-deadline（意外漂移）"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
