#!/bin/bash
# =========================================================================
# experiments/reproduction / d2fq / arms_none.sh
# 实验7 d2fq，none 臂：scheduler=none + 两租户 4k randread QD32（[MSC]）
# + LC step 协议（90s）。Native 臂 VM 内 N/A（[PLAN] 主会话已定决策：
# ioextd2fq 需要 NVMe WRRU，virtio-blk 上 queue/d2fq_en 写 1 被 -EINVAL
# 拒绝 —— D2FQ_NATIVE_PORT_20261110.md:70 "非 NVMe 队列写 1 -> 被 -EINVAL
# 安全拒绝"；口径与 LinnOS 相同；bm 裸机 front_n_d2fq 三臂列为后续独立补充项）。
# 用法：bash arms_none.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_none.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="d2fq_none_r${REP}"
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
