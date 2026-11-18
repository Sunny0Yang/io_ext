#!/bin/bash
# =========================================================================
# experiments/reproduction / kyber / arms_iox.sh
# 实验2 kyber，IOX 臂：scx_io 基座 + loader_q_kyber（root 注册）。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_q_kyber.c）：
#   loader_q_kyber -c /sys/fs/cgroup --read-target-ns 2000000 --write-target-ns 10000000
#     -c                 ioext_cli_args.h:119（common_opts）
#     --read-target-ns   loader_q_kyber.c:70（option 表）/ :32-34（解析），
#                        默认 2000000（:84 help 文案）
#     --write-target-ns  loader_q_kyber.c:71，默认 10000000（:85）
#     值冻结自 homo_v2（gen_homo_v2_yamls.py:111-112 KYBER_*_TARGET_NS）[PLAN]
# 启动顺序：无 arbiter -> 先切 scx_io 再挂 q policy（guest_hotswitch.sh:115 模式）。
# 核心图：LC P99 时序（fio）+ depth limit 时序（loader stats --stats-interval-ms，
# kyber: per-domain depth_limit/inflight/p99 桶 —— 字段以子智能体A
# ALGORITHM_AUDIT.md 为准【占位引用】）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="kyber_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# kyber 核心图依赖 loader 周期状态输出；开关未落地则该臂 IOX 正式数据无效
# [FIX-C] 逐 loader 探测：loaders_stats1 bundle 仅 5 个 loader 支持
# --stats-interval-ms（loader_q_kyber 属支持集；见 env_preflight.sh loader_supports_stats）。
if loader_supports_stats "$IOEXT_LOADER_DIR/loader_q_kyber"; then
    STATS_ARGS=(--stats-interval-ms "$IOEXT_STATS_INTERVAL_MS")
else
    env_log "FAIL-GATE: --stats-interval-ms 未落地（ioext_cli_args.h 无该选项），"
    env_log "       kyber depth-limit 时序图无法产出。跑 smoke/外部指标可以，正式 rep 不行。"
    STATS_ARGS=()
    [ "${IOEXT_ALLOW_NO_STATS:-0}" = "1" ] || env_die " refusing formal IOX run without stats switch"
fi

# ---- 激活 ----------------------------------------------------------------
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_kyber" "$STATS_LOG" \
    -c "$IOEXT_CG_ROOT" --read-target-ns 2000000 --write-target-ns 10000000 \
    "${STATS_ARGS[@]+"${STATS_ARGS[@]}"}"
loader_wait_alive "$LOADER_PID" loader_q_kyber
LOADER_PIDS=("$LOADER_PID")

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
