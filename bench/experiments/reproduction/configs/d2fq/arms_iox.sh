#!/bin/bash
# =========================================================================
# experiments/reproduction / d2fq / arms_iox.sh
# 实验7 d2fq，IOX 臂：scx_io 基座 + arb_d2fq + 两租户 q_fifo。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_arb_d2fq.c）：
#   loader_arb_d2fq                    # 全默认参数（[PLAN] 未要求覆盖）
#     --thrsh / --md-ratio / --lc-prio-max / --fast-share / --type-weights
#     / --cost-table / --di-period-ms   # option 表 loader_arb_d2fq.c:162-173
#   默认：thrsh=64*840<<10、md_ratio=70、fast_share=6（== homo_v2 x_d2fq_arb
#   冻结口径，gen_homo_v2_yamls.py front_x_d2fq_arb 注释）、type_weights=100:100、
#   cost=read{64,256,1024,2048,4096}:write{256,1024,4096,8192,16384}
#   （loader_arb_d2fq.c:57-69）
# 两租户 q_fifo：都是 -p 5 BE 档（[PLAN] 主会话补定），-W 100/-W 400 ——
#   权重经 policy_meta 进 arbiter（loader_arb_d2fq.c:5-7 注释）；两租户同为
#   BE 档时 D2FQ fast-share（LC 类加速）不介入，纯权重公平性检验。
# 启动顺序：arb -> scx_io -> q。
# 核心图：累计字节份额随时间（fio write_bw_log 时序积分 + tenant_state
# completed_bytes 佐证；d2fq per-lane vtime/band 内部时序依赖 --stats-interval-ms，
# 字段以子智能体A ALGORITHM_AUDIT.md 为准【占位引用】）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="d2fq_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# [FIX-C] 逐 loader 探测：loaders_stats1 bundle 仅 5 个 loader 支持
# --stats-interval-ms（loader_arb_d2fq 属支持集；见 env_preflight.sh loader_supports_stats）。
if loader_supports_stats "$IOEXT_LOADER_DIR/loader_arb_d2fq"; then
    STATS_ARGS=(--stats-interval-ms "$IOEXT_STATS_INTERVAL_MS")
else
    STATS_ARGS=()   # 份额核心图来自 fio bytes；vtime/band 内部时序为补充
fi

# ---- 激活 ----------------------------------------------------------------
loader_start "$IOEXT_LOADER_DIR/loader_arb_d2fq" "$STATS_LOG" \
    "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_arb_d2fq
ARB_PID=$LOADER_PID
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_LC_CG" -p 5 -W 100
loader_wait_alive "$LOADER_PID" loader_q_fifo_lc
LC_Q_PID=$LOADER_PID
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_BE_CG" -p 5 -W 400
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
