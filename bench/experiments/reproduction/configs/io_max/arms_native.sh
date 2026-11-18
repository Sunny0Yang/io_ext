#!/bin/bash
# =========================================================================
# experiments/reproduction / io_max / arms_native.sh
# 实验3 io.max，native 臂：scheduler=none + BE cgroup 写 io.max。
#
# 写行格式（[PLAN] wbps=33554432(32MiB/s) rbps=max wiops=max；写入型负载）：
#   echo "<MAJ:MIN> rbps=max wbps=33554432 riops=max wiops=max" > <BE_CG>/io.max
# 内核核对（[FIX-C]）：blk-throttle.c:1588 接受 rbps/wbps/riops/wiops，
#   值可为 %llu 或字面 "max"（strcmp(p,"max")，blk-throttle.c:1588）；
#   注意 0 值会被 -ERANGE 拒绝（blk-throttle.c:1591-1593）→ 不能用 rbps=0。
# 读回断言：<MAJ:MIN> rbps=max wbps=33554432 riops=max wiops=max。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_max_native_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_MAIN_CG"

MM="$(ioext_maj_min)"
CAP="$IOEXT_WBPS_CAP"   # 33554432 = 32 MiB/s [PLAN]

# ---- 激活 ----------------------------------------------------------------
echo "$MM rbps=max wbps=$CAP riops=max wiops=max" > "$IOEXT_MAIN_CG/io.max" \
    || env_die "io.max 写入失败"

# ---- 臂级 preflight 断言 --------------------------------------------------
local_line="$(cat "$IOEXT_MAIN_CG/io.max")"
echo "$local_line" | grep -q "wbps=$CAP" || env_die "io.max 读回无 wbps=$CAP: $local_line"
echo "$local_line" | grep -q "rbps=max" && echo "$local_line" | grep -q "riops=max" \
    && echo "$local_line" | grep -q "wiops=max" \
    || env_die "io.max 读回缺 max 字段: $local_line"
env_log "io.max armed: $local_line"

# ---- 运行 ----------------------------------------------------------------
external_sampler "$RUN_DIR/${RUN_ID}.native_state.log" 65 &
SAMPLER_PID_START=$!
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_MAIN_CG" "$HERE/fio_native.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)
kill "$SAMPLER_PID_START" 2>/dev/null || true

# ---- postflight ----------------------------------------------------------
# 运行后限速仍应生效（读回一致），带宽时序见 io_max_native_main_bw.log
[ "$(sched_current)" = "none" ] || env_die "运行后 scheduler != none"
grep -q "wbps=$CAP" "$IOEXT_MAIN_CG/io.max" || env_die "运行后 io.max 漂移"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
