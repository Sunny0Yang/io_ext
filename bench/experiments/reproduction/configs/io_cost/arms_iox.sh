#!/bin/bash
# =========================================================================
# experiments/reproduction / io_cost / arms_iox.sh
# 实验5 io.cost，IOX 臂：scx_io 基座 + arb_iocost + 两租户 q_fifo（100/200）。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_arb_iocost.c）：
#   loader_arb_iocost --qos-rlat-ns 2000000
#     --qos-rlat-ns    loader_arb_iocost.c:202（option 表）/ :136-139（解析），
#                      默认即 2000000 = 2ms（:52；显式写出便于审计）[PLAN]
#     其余全部默认（[PLAN] 未要求覆盖）：
#       --qos-rpct    默认 99（:53）
#       --vrate-min/max 默认 25%/400%（:54-55）
#       --cost-table  默认 64,256,1024,2048,4096,64,256,1024,2048,4096
#                     （:76-77，byte-linear read==write）
#       --cost-quantum 8 / --cost-min-budget 64（:50-51）
#     arbiter 不接受 -c/-p/-W（ioext_cli_args.h:226-232）
#   loader_q_fifo -c <CG> -p 2 -W 100 / -c <CG> -p 5 -W 200
#     [PLAN] io.cost 臂 io.weight LC=100/BE=200（注意与 io_weight 实验的
#     100:400 不同——本臂权重是 io.cost 的 cost-unit 权重）。
# 启动顺序：arb -> scx_io -> q（guest_hotswitch.sh:97-123 模式）。
# 核心图：vrate 时序 —— IOX 侧来自 loader stats（iocost: vrate/eff_quantum/
# 窗口状态，--stats-interval-ms；字段以子智能体A ALGORITHM_AUDIT.md 为准
# 【占位引用】）；native 侧来自 io.stat cost.vrate（1s 采样）。
# [PLAN] 归一化口径：两侧 vrate 均以 1.0 = 满模型速率归一后同图（IOX 为
# 256ths 定点，native 为 X.YZ 百分数）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_cost_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# vrate 时序是本实验核心图 -> IOX 正式 rep 强制要求 stats 开关
# [FIX-C] 逐 loader 探测：loaders_stats1 bundle 仅 5 个 loader 支持
# --stats-interval-ms（loader_arb_iocost 属支持集；见 env_preflight.sh loader_supports_stats）。
if loader_supports_stats "$IOEXT_LOADER_DIR/loader_arb_iocost"; then
    STATS_ARGS=(--stats-interval-ms "$IOEXT_STATS_INTERVAL_MS")
else
    STATS_ARGS=()
    [ "${IOEXT_ALLOW_NO_STATS:-0}" = "1" ] || env_die "refusing formal IOX run without --stats-interval-ms（vrate 时序图无法产出）"
fi

# ---- 激活 ----------------------------------------------------------------
loader_start "$IOEXT_LOADER_DIR/loader_arb_iocost" "$STATS_LOG" \
    --qos-rlat-ns 2000000 "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_arb_iocost
ARB_PID=$LOADER_PID
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_LC_CG" -p 2 -W 100
loader_wait_alive "$LOADER_PID" loader_q_fifo_lc
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
# 根 io.cost 复位（本臂不写 native io.cost，但诊断步可能开过；防残留）
MM="$(ioext_maj_min)"
echo "$MM enable=0 ctrl=user rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150" \
    > "$IOEXT_CG_ROOT/io.cost.qos" 2>/dev/null || true
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
