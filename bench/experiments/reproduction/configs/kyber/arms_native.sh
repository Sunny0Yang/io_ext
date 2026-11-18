#!/bin/bash
# =========================================================================
# experiments/reproduction / kyber / arms_native.sh
# 实验2 kyber，native 臂：echo kyber > /sys/block/vdb/queue/scheduler。
#
# native kyber 目标核对（[FIX-C]）：本内核树 kyber 有 sysfs lat target：
# /sys/block/vdb/queue/iosched/kyber/{read,write}_lat_nsec（kyber-iosched.c:877-883
# KYBER_LAT_ATTR，kstrtoull 解析，单位 ns）；编译内默认 read=2ms write=10ms
# （kyber-iosched.c:69-74 kyber_latency_targets[]）。默认值恰等于冻结的 IOX
# 目标 --read-target-ns 2000000 --write-target-ns 10000000（[PLAN] 冻结自
# homo_v2，gen_homo_v2_yamls.py:111-112），但 native 臂仍显式写入并读回断言
# （knob 落点可审计，两侧同值同源）。深度 limit 时序仅 IOX 臂可观测
# （loader stats），native 臂核心图回退用外部 LC P99 时序对照（矩阵注明）。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="kyber_native_r${REP}"
mkdir -p "$RUN_DIR"

# ---- preflight ----------------------------------------------------------
env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
grep -qw kyber "$IOEXT_SCHED" || env_die "kyber 不在可用 scheduler 列表"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"

# ---- 激活 ----------------------------------------------------------------
sched_set kyber
# 显式写入 latency targets（[FIX-C] kyber-iosched.c:877-883，单位 ns；与 IOX 同值）
KIOS="/sys/block/$IOEXT_DEV/queue/iosched/kyber"
test -d "$KIOS" || env_die "iosched/kyber 目录不存在（scheduler 未切到 kyber？）"
echo 2000000  > "$KIOS/read_lat_nsec"  || env_die "read_lat_nsec 写入失败"
echo 10000000 > "$KIOS/write_lat_nsec" || env_die "write_lat_nsec 写入失败"
# best-effort 记录 iosched 子目录全部文件（证据留档）
ls -l "$KIOS" > "$RUN_DIR/${RUN_ID}.kyber_iosched.txt" 2>&1 || true

# ---- 运行（同 none 臂：BE 先起 90s，LC 3 phase） ---------------------------
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
[ "$(sched_current)" = "kyber" ] || env_die "运行后 scheduler != kyber"
grep -q "^2000000$" "$KIOS/read_lat_nsec" || env_die "运行后 read_lat_nsec 漂移"
grep -q "^10000000$" "$KIOS/write_lat_nsec" || env_die "运行后 write_lat_nsec 漂移"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
