#!/bin/bash
# =========================================================================
# experiments/reproduction / io_weight / arms_iox.sh
# 实验4 io.weight，IOX 臂：scx_io 基座 + arb_wdrr + 两租户 q_fifo。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/）：
#   loader_arb_wdrr                    # 无额外参数；默认 STRICT
#     --allow-fallback 0|1             # loader_arb_wdrr.c:110（本实验不用，默认 0）
#     --diag-interval-ms U32           # loader_arb_wdrr.c:111（诊断可选项）
#     arbiter 不接受 -c/-p/-W（ioext_cli_args.h:226-232 注释明确）
#   loader_q_fifo -c <CG> -p <PRIO> -W <WEIGHT>
#     -c/-p/--arb-prio/-W/--arb-weight  ioext_cli_args.h:118-124
# 角色分类：arb_prio < 3 = LC、>= 3 = BE（arb_iolatency.c:5-6 注释、
# gen_homo_v2_yamls.py LC_W_ARGS -p 2 / BE_W_ARGS -p 5；
# IOSCHED_DEFAULT_ARB_PRIO=7（ioext_loader_common.h:39）= BE band，
# 所以 LC 侧必须显式 -p 2 [FIX-C]）。默认权重 100（ioext_loader_common.h:40）。
# 权重 100:400 经 q 侧 -W 进入 policy_meta（arb_d2fq.c:5-7 同机制注释）。
# 启动顺序（冻结自 guest_hotswitch.sh:97-123）：arb 先 attach -> 切 scx_io
#   -> 挂 q policies。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_weight_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# ---- stats 开关说明 -------------------------------------------------------
# [FIX-C] arb_wdrr 与 q_fifo 在 loaders_stats1 bundle 中是冻结副本，二进制内
# 无 --stats-interval-ms 选项（strings 计数=0），传参即 "unknown option" 退出。
# 本臂两 loader 均不传 stats 参数；份额核心图来自 fio bytes，无影响。

# ---- 激活 ----------------------------------------------------------------
loader_start "$IOEXT_LOADER_DIR/loader_arb_wdrr" "$STATS_LOG"
loader_wait_alive "$LOADER_PID" loader_arb_wdrr
ARB_PID=$LOADER_PID
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_LC_CG" -p 2 -W 100
loader_wait_alive "$LOADER_PID" loader_q_fifo_lc
LC_Q_PID=$LOADER_PID
loader_start "$IOEXT_LOADER_DIR/loader_q_fifo" "$STATS_LOG" \
    -c "$IOEXT_BE_CG" -p 5 -W 400
loader_wait_alive "$LOADER_PID" loader_q_fifo_be
BE_Q_PID=$LOADER_PID
LOADER_PIDS=("$ARB_PID" "$LC_Q_PID" "$BE_Q_PID")

# ---- 臂级 preflight 断言 --------------------------------------------------
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
