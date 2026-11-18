#!/bin/bash
# =========================================================================
# experiments/reproduction / linnos / arms_none.sh
# 实验8 LinnOS，none 臂：scheduler=none + LC 稳定 randread 120s +
# BE randwrite 阶梯 4x30s（QD 8->16->32->64）[MSC]。
# Native 臂 N/A（[PLAN] 与 d2fq 同口径：无可对照参考实现，论文在线 NN 语义
# 无法同态复现 —— D2FQ_NATIVE_PORT_20261110.md "LinnOS：native 不可行的原因"）。
# 用法：bash arms_none.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_none.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="linnos_none_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_BE_CG" "$HERE/fio_none_be.fio" "$RUN_DIR" "$RUN_ID.be"
BE_PID=$FIO_PID
fio_in_cg "$IOEXT_LC_CG" "$HERE/fio_none_lc.fio" "$RUN_DIR" "$RUN_ID.lc"
LC_PID=$FIO_PID
wait "$LC_PID"; wait "$BE_PID"
local_ts_end=$(date -Iseconds)

snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
