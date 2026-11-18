#!/bin/bash
# =========================================================================
# experiments/reproduction / kyber / arms_none.sh
# 实验2 kyber，none 臂：scheduler=none + LC/BE coloc step workload（90s）。
# [PLAN] LC 4k randread + BE 128k seqwrite（BE 持续饱和）；[MSC] 90s step
# 协议：0-30s 基线 / 30-60s LC QD64 / 60-90s 恢复；BE 90s 不间断。
# none 臂无内部状态：图上只用外部指标（fio p99/带宽），不得填零 [PLAN]。
# 用法：bash arms_none.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_none.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="kyber_none_r${REP}"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"

# cgroup 准备（none 臂也建 LC/BE cgroup：三臂运行环境逐位一致，仅无 knob）
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# ---- 激活：无 ------------------------------------------------------------

# ---- 运行：BE 先起（90s 连续），LC 紧随（3 phase 串行） --------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_BE_CG" "$HERE/fio_none_be.fio" "$RUN_DIR" "$RUN_ID.be"
BE_PID=$FIO_PID
fio_in_cg "$IOEXT_LC_CG" "$HERE/fio_none_lc.fio" "$RUN_DIR" "$RUN_ID.lc"
LC_PID=$FIO_PID
wait "$LC_PID"; wait "$BE_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
