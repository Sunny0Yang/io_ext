#!/bin/bash
# =========================================================================
# experiments/reproduction / mq_deadline / arms_iox.sh
# 实验1 mq-deadline，IOX 臂：scx_io 基座 + loader_q_mq_deadline（root 注册）。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/）：
#   loader_q_mq_deadline -c /sys/fs/cgroup
#     -c  公共 policy 参数（ioext_cli_args.h:119 common_opts{cgroup,'c'}，
#         ioext_cli_args.h:167 getopt ":c:p:W:hv"；默认即根 cgroup，ioext_cli_args.h:17）
#   --merge-mode 0 是 loader 默认（loader_q_mq_deadline.c:42,51-52），省略。
# 启动顺序（冻结自 guest_hotswitch.sh:97-123 字段验证模式）：
#   对本臂无 arbiter：先切 scx_io，再挂 q policy。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="mq_deadline_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"

# stats 开关：[FIX-C] loader_q_mq_deadline 在 loaders_stats1 bundle 中是冻结
# 副本，二进制内无 --stats-interval-ms 选项（strings 计数=0），不传该参数；
# mq_deadline 核心图只用 fio 外部指标，无影响。

# ---- 激活（顺序冻结自 guest_hotswitch.sh） --------------------------------
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_mq_deadline" "$STATS_LOG" \
    -c "$IOEXT_CG_ROOT"
loader_wait_alive "$LOADER_PID" loader_q_mq_deadline
LOADER_PIDS=("$LOADER_PID")

# ---- 运行 ----------------------------------------------------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_CG_ROOT" "$HERE/fio_iox.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 1 "$STATS_LOG"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
