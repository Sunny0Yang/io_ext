#!/bin/bash
# =========================================================================
# experiments/reproduction / linnos / arms_iox.sh
# 实验8 LinnOS，IOX 臂：scx_io 基座 + LC/BE 两实例 q_linnos（无 arbiter）。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_q_linnos.c）：
#   loader_q_linnos -c <CG> -p <PRIO> -W 100 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60
#                    --max-hold-us 500 --busy-ref <FROZEN>
#     -c/-p/-W        ioext_cli_args.h:119-121
#     --ir-low/--ir-mid/--ir-high  loader_q_linnos.c:181-183（option 表），
#                     值格式 "LC:BE"（:74-91 q_linnos_parse_ir），各 0..100
#                     [PLAN] 显式 IR 表：LC 全带 0 拦截、BE 分 0/40/60%，
#                     两实例必须同值（homo_v2 iteration#2 教训：默认表 5:60 会
#                     拦 LC；gen_homo_v2_yamls.py LINNOS_IR_* 冻结）
#     --max-hold-us   loader_q_linnos.c:185，默认 2000；[PLAN] 显式 500
#                     （homo_v2 iteration#3：2000 的批量释放会以 burst 冲击 LC）
#     --busy-ref      loader_q_linnos.c:184，默认 64；[PLAN] 诊断后冻结
#     --lc-prio-max   默认 3（loader_q_linnos.c:186,70）：arb_prio < 3 = LC，
#                     LC 实例必须 -p 2、BE 实例 -p 5 [FIX-C]
# 两实例模式（冻结自 homo_v2 front_x_linnos_q，gen_homo_v2_yamls.py:436-447）：
#   LC/BE 各起一个 loader 实例、IR/hold/busy-ref 参数完全一致（IR 表是
#   band x class 的全局表，两实例同值幂等）。
# 诊断模式（[MSC]）：LINNOS_DIAG=1 bash arms_iox.sh <RUN_DIR> diag
#   以 busy-ref 默认 64 跑一次完整 120s 协议；判定：loader stats 的 per-band
#   inserts（band0/band1/band2）每条 busy band 至少占一个 1s window。
#   诊断允许、结果不入正式 [MSC]。判定通过后：把 busy-ref 写进本脚本
#   LINNOS_BUSY_REF_FROZEN 并置 LINNOS_BUSY_REF_FROZEN_OK=1，正式 rep 放行。
# 核心图：BE 目标拦截率 vs 实际拦截率（loader stats per-band inserts/intercepted
# 计数；字段以子智能体A ALGORITHM_AUDIT.md 为准【占位引用】）。
#   目标拦截率 = 每段 IR 表（s1: 0%、s2: 40%、s3: 40%?...）——注意 [PLAN] 的
#   比较对象是 band（0/40/60%）而非 QD 段：per-band 实际拦截率 =
#   intercepted[band][BE]/inserts_by_band[band]，目标 = IR 表 BE 列（0/40/60）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>   （正式 rep，需已冻结 busy-ref）
#       LINNOS_DIAG=1 bash arms_iox.sh <RUN_DIR> diag  （诊断，busy-ref=64）
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
LINNOS_BUSY_REF_FROZEN="${LINNOS_BUSY_REF_FROZEN:-unset}"
LINNOS_BUSY_REF_FROZEN_OK="${LINNOS_BUSY_REF_FROZEN_OK:-0}"
# run_id：正式 rep = linnos_iox_r<REP>（RUN_INDEX 约定）；诊断 = linnos_diag
if [ "${LINNOS_DIAG:-0}" = "1" ]; then
    RUN_ID="linnos_diag"
else
    RUN_ID="linnos_iox_r${REP}"
fi
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight_${RUN_ID}"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# stats 开关：per-band 拦截率时序是本实验核心图 + 诊断判据，硬依赖
# [FIX-C] 逐 loader 探测：loaders_stats1 bundle 仅 5 个 loader 支持
# --stats-interval-ms（loader_q_linnos 属支持集；见 env_preflight.sh loader_supports_stats）。
if loader_supports_stats "$IOEXT_LOADER_DIR/loader_q_linnos"; then
    STATS_ARGS=(--stats-interval-ms "$IOEXT_STATS_INTERVAL_MS")
else
    STATS_ARGS=()
    env_log "WARN: stats 开关未落地 —— 诊断判据（per-band window 占据）与核心图均不可得；"
    env_log "      降级口径 = 只用 loader 收尾 dump 总数（loader_q_linnos.c:132-176,243）+ 外部 P99 时序，矩阵记偏差"
    [ "${IOEXT_ALLOW_NO_STATS:-0}" = "1" ] || [ "${LINNOS_DIAG:-0}" = "1" ] \
        || env_die "refusing formal IOX run without --stats-interval-ms"
fi

# ---- busy-ref 冻结门（fail-closed） ----------------------------------------
if [ "${LINNOS_DIAG:-0}" = "1" ]; then
    BUSY_REF=64   # 诊断用 loader 默认值（loader_q_linnos.c:67）
    env_log "DIAG mode: busy-ref=$BUSY_REF（诊断允许，结果不入正式 [MSC]）"
else
    [ "$LINNOS_BUSY_REF_FROZEN_OK" = "1" ] || env_die "busy-ref 未冻结：先跑 LINNOS_DIAG=1 诊断，判定通过后设 LINNOS_BUSY_REF_FROZEN 与 LINNOS_BUSY_REF_FROZEN_OK=1"
    BUSY_REF="$LINNOS_BUSY_REF_FROZEN"
fi
IR_ARGS=(--ir-low 0:0 --ir-mid 0:40 --ir-high 0:60)
HOLD_ARGS=(--max-hold-us 500)

# ---- 激活（q_linnos 无 arbiter：先切 scx_io 再挂两实例） --------------------
sched_set scx_io
# LC 实例（prio 2 < lc_prio_max 3 => LC，全带 0 拦截）
loader_start "$IOEXT_LOADER_DIR/loader_q_linnos" "$STATS_LOG" \
    -c "$IOEXT_LC_CG" -p 2 -W 100 "${IR_ARGS[@]}" "${HOLD_ARGS[@]}" --busy-ref "$BUSY_REF" \
    "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_q_linnos_lc
LC_PID_LOADER=$LOADER_PID
# BE 实例（prio 5 >= 3 => BE；IR/hold/busy-ref 与 LC 实例完全一致 [PLAN]）
loader_start "$IOEXT_LOADER_DIR/loader_q_linnos" "$STATS_LOG" \
    -c "$IOEXT_BE_CG" -p 5 -W 100 "${IR_ARGS[@]}" "${HOLD_ARGS[@]}" --busy-ref "$BUSY_REF" \
    "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_q_linnos_be
BE_PID_LOADER=$LOADER_PID
LOADER_PIDS=("$LC_PID_LOADER" "$BE_PID_LOADER")

for pid in "${LOADER_PIDS[@]}"; do
    kill -0 "$pid" || env_die "loader pid=$pid 提前退出（检查 stats.log）"
done

# ---- 运行：BE 先起（4 段阶梯 120s），LC 紧随（120s 连续） ------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_BE_CG" "$HERE/fio_iox_be.fio" "$RUN_DIR" "${RUN_ID}.be"
BE_FIO_PID=$FIO_PID
fio_in_cg "$IOEXT_LC_CG" "$HERE/fio_iox_lc.fio" "$RUN_DIR" "${RUN_ID}.lc"
LC_FIO_PID=$FIO_PID
wait "$LC_FIO_PID"; wait "$BE_FIO_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
# 诊断判定：stats.log 中 band0/band1/band2 每条至少占一个 1s window
# （字段名以 ALGORITHM_AUDIT.md 为准【占位引用】；此处按 band 关键词宽松计数）
if [ "${LINNOS_DIAG:-0}" = "1" ]; then
    {
        echo "# linnos busy-ref 诊断判定（[MSC]）：三条 busy band 每条至少占一个 window"
        echo "# 判据：stats.log per-band inserts（band0/band1/band2）在 120s 内均有非零 window"
        grep -c "band0" "$STATS_LOG" || true; grep -c "band1" "$STATS_LOG" || true; grep -c "band2" "$STATS_LOG" || true
        echo "# 若三行计数均 > 0 且分布覆盖 4 个 QD 段 -> 冻结该 busy-ref 值"
        echo "PROPOSAL: LINNOS_BUSY_REF_FROZEN=$BUSY_REF（按实际诊断调整后再冻结）"
    } > "$RUN_DIR/diag_verdict.txt" 2>&1
    env_log "诊断运行完成，判定材料 -> $RUN_DIR/diag_verdict.txt"
fi
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight_${RUN_ID}"
if [ "${LINNOS_DIAG:-0}" = "1" ]; then
    loader_stop "${LOADER_PIDS[@]}"
    rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    sched_set none
    cg_rm "$IOEXT_LC_CG" || true; cg_rm "$IOEXT_BE_CG" || true
else
    teardown_common "$RUN_DIR" "$RUN_ID" 1 "$STATS_LOG"
    printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
        > "$RUN_DIR/${RUN_ID}.status.txt"
fi
env_log "arm done: linnos iox (rep=$REP diag=${LINNOS_DIAG:-0} busy_ref=$BUSY_REF)"
