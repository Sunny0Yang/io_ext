#!/bin/bash
# =========================================================================
# experiments/reproduction / io_latency / arms_iox.sh
# 实验6 io.latency（最高优先级 [PLAN]），IOX 臂：scx_io 基座 + arb_iolatency
# + LC q_none + BE q_fifo。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_arb_iolatency.c）：
#   loader_arb_iolatency --lat-target-ns 2000000
#     --lat-target-ns    loader_arb_iolatency.c:76（option 表）/ :50-53（解析）；
#                        默认 100000=100us（:30,85）—— 必须显式覆盖成 2ms [PLAN]
#     --protect-inflight 默认 32（:31,bm）—— [PLAN] 其余默认，省略
#     --boost-max        默认 4，clamp [1,16]（:32,87,58-66）—— [PLAN] 省略
#     arbiter 不接受 -c/-p/-W（ioext_cli_args.h:226-232）
#   LC/BE 角色来自 q 侧 -p（0-2 = LC，[FIX-C] arb_iolatency.c:5-6 注释；
#   默认 prio 7 = BE band，ioext_loader_common.h:39，LC 必须显式 -p 2）。
#   LC 用 q_none（冻结自 homo_v2 ctrl_x_latency_arb：LC latency probe rides
#   q_none，gen_homo_v2_yamls.py:396-404），BE 用 q_fifo -p 5 -W 200
#   （homo_v2 BE_W_ARGS 冻结 [GAP-C]：plan 未定本臂 BE 权重，沿 homo_v2 100:200）。
# 启动顺序：arb -> scx_io -> q（guest_hotswitch.sh:97-123 模式）。
# 核心图：LC P99（fio）+ BE 带宽（fio）时序 —— 方向分歧问题（[PLAN]）：native
# 自节流（压 LC 队列深）vs IOX boost（压 BE 保 LC）；IOX 内部 boost/FSM 状态
# 时序来自 loader stats（iolatency: per-tenant boost/FSM state，字段以子智能体A
# ALGORITHM_AUDIT.md 为准【占位引用】）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_latency_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# [FIX-C] 逐 loader 探测：loaders_stats1 bundle 仅 5 个 loader 支持
# --stats-interval-ms（loader_arb_iolatency 属支持集；见 env_preflight.sh loader_supports_stats）。
if loader_supports_stats "$IOEXT_LOADER_DIR/loader_arb_iolatency"; then
    STATS_ARGS=(--stats-interval-ms "$IOEXT_STATS_INTERVAL_MS")
else
    STATS_ARGS=()   # 核心图（LC P99 + BE 带宽）来自 fio；boost/FSM 时序为补充
    env_log "WARN: stats 开关未落地，boost/FSM 内部时序缺失（核心图不受影响）"
fi

# ---- 激活 ----------------------------------------------------------------
loader_start "$IOEXT_LOADER_DIR/loader_arb_iolatency" "$STATS_LOG" \
    --lat-target-ns 2000000 "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_arb_iolatency
ARB_PID=$LOADER_PID
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_none" "$STATS_LOG" \
    -c "$IOEXT_LC_CG" -p 2 -W 100
loader_wait_alive "$LOADER_PID" loader_q_none_lc
LC_Q_PID=$LOADER_PID
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_BE_CG" -p 5 -W 200
loader_wait_alive "$LOADER_PID" loader_q_fifo_be
BE_Q_PID=$LOADER_PID
LOADER_PIDS=("$ARB_PID" "$LC_Q_PID" "$BE_Q_PID")

for pid in "${LOADER_PIDS[@]}"; do
    kill -0 "$pid" || env_die "loader pid=$pid 提前退出（检查 stats.log）"
done

# ---- 运行 ----------------------------------------------------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_BE_CG" "$HERE/fio_iox_be.fio" "$RUN_DIR" "$RUN_ID.be"
BE_PID=$FIO_PID
fio_in_cg "$IOEXT_LC_CG" "$HERE/fio_iox_lc.fio" "$RUN_DIR" "$RUN_ID.lc"
LC_PID=$FIO_PID
wait "$LC_PID"; wait "$BE_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 1 "$STATS_LOG"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
