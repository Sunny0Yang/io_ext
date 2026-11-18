#!/bin/bash
# =========================================================================
# experiments/reproduction / mq_deadline / arms_none.sh
# 实验1 mq-deadline，none 臂（native block 层 scheduler=none，无 loader）。
# 用法（guest VM 内 root 执行）：bash arms_none.sh <RUN_DIR> <REP>
# 产物：$RUN_DIR/mq_deadline_none_r<REP>.{fio.json,preflight.txt,postflight.txt,
#       sentinel.txt} + fio *_lat/_bw/_iops log（job 文件内命名）
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_none.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="mq_deadline_none_r${REP}"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
# 臂级断言：目标态 = none（none 臂无 knob，数据盘 scheduler 必须已是 none）
[ "$(sched_current)" = "none" ] || env_die "mq_deadline none 臂要求起步 scheduler=none"

# ---- 激活：无（none 臂无调度器、无 cgroup knob；fio 在根 cgroup 跑） ------
# [PLAN] none 臂与 native/iox 完全相同 workload，图上只用外部指标。

# ---- 运行 ----------------------------------------------------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_CG_ROOT" "$HERE/fio_none.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
