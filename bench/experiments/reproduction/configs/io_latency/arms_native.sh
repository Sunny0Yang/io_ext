#!/bin/bash
# =========================================================================
# experiments/reproduction / io_latency / arms_native.sh
# 实验6 io.latency（最高优先级 [PLAN]），native 臂：scheduler=none +
# LC cgroup io.latency target=2000（us = 2ms）；BE cgroup 无 knob
# （同 homo_v2 ctrl_n_io_latency：be_knob {}，gen_homo_v2_yamls.py）。
#
# 写行格式：<MAJ:MIN> target=2000（[FIX-C] 内核解析 blk-iolatency.c:867-876
# 按 "key=val" token 解析；读回格式 "<dev> target=<us>"（blk-iolatency.c:908），
# target 单位 us）。
# [MSC] 坑位预录（gen_homo_v2_yamls.py IO_LATENCY_TARGET_US 注释）：blk-iolatency
# 的自节流对象是 LC 自己的提交队列——若 2ms 目标低于可达延迟地板，会把 LC 自己
# 节流掉（homo_v2 iteration#2：1ms 目标 LC iops 84k->72k 且 p99 仍 5.7ms）。
# 2ms 目标介于 ~4.9ms none 基线与硬件地板之间（同注释 iteration#3 依据）。
# 本实验核心问题（[PLAN]）：native 自节流 vs IOX boost 压 BE 的方向分歧。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_latency_native_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"
MM="$(ioext_maj_min)"

# ---- 臂级 preflight 断言：blk-iolatency 必须在盘上可用 ----------------------
test -f "$IOEXT_LC_CG/io.latency" || env_die "LC cgroup 无 io.latency 文件（guest 内核 blk-iolatency 未启用/未激活？RUNBOOK 口径：缺失会被静默跳过，此处 fail-stop）"

# ---- 激活 ----------------------------------------------------------------
echo "$MM target=2000" > "$IOEXT_LC_CG/io.latency" || env_die "io.latency 写入失败"

# ---- 读回断言（[FIX-C] 格式 "<dev> target=<us>" blk-iolatency.c:908） -------
local_line="$(cat "$IOEXT_LC_CG/io.latency")"
echo "$local_line" | grep -q "target=2000" || env_die "io.latency 读回无 target=2000: $local_line"
env_log "io.latency armed: $local_line"

# ---- 运行 ----------------------------------------------------------------
external_sampler "$RUN_DIR/${RUN_ID}.native_state.log" 95 &
SAMPLER_PID_START=$!
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_BE_CG" "$HERE/fio_native_be.fio" "$RUN_DIR" "$RUN_ID.be"
BE_PID=$FIO_PID
fio_in_cg "$IOEXT_LC_CG" "$HERE/fio_native_lc.fio" "$RUN_DIR" "$RUN_ID.lc"
LC_PID=$FIO_PID
wait "$LC_PID"; wait "$BE_PID"
local_ts_end=$(date -Iseconds)
kill "$SAMPLER_PID_START" 2>/dev/null || true

# ---- postflight ----------------------------------------------------------
grep -q "target=2000" "$IOEXT_LC_CG/io.latency" || env_die "运行后 io.latency 漂移"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
