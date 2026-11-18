#!/bin/bash
# =========================================================================
# experiments/reproduction / io_max / arms_none.sh
# 实验3 io.max，none 臂：scheduler=none、无任何限速，4k randwrite 60s。
# 对照柱：展示无 cap 时的裸带宽（[PLAN] none 臂同 workload 只用外部指标）。
# 用法：bash arms_none.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_none.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_max_none_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
# 单 BE cgroup（[PLAN] 单 BE cgroup 4k randwrite）：三臂统一放进同名 cgroup
cg_prep "$IOEXT_MAIN_CG"

local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_MAIN_CG" "$HERE/fio_none.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)

snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
