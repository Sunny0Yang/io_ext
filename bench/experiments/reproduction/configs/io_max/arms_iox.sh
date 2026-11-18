#!/bin/bash
# =========================================================================
# experiments/reproduction / io_max / arms_iox.sh
# 实验3 io.max，IOX 臂：scx_io 基座 + loader_q_iomax 注册在 BE cgroup。
#
# loader CLI（已核对 /opt/ioext-io_ext/userspace/io/loader_q_iomax.c）：
#   loader_q_iomax -c <BE_CG> --wbps 33554432 --rbps 0 --wiops 0
#     -c        ioext_cli_args.h:119
#     --wbps    loader_q_iomax.c:230（option 表）/ :118-119（解析）
#     --rbps/--wiops  loader_q_iomax.c:228-229
#
# [FIX-C] 据源码修正 plan 写法："rbps=max wiops=max" 是 native cgroup 语法；
# q_iomax 的值解析是严格 U64（q_iomax_parse_u64_strict，loader_q_iomax.c:84-97，
# strtoull 全串校验），字面 "max" 会解析失败直接报错退出。正确写法：
#   0 = 不限速（loader 头注释 loader_q_iomax.c:5-8 "0 or omitted = unlimited"；
#       明确写 0 而非省略，语义显式可审计）。
#   全 0 会被拒绝（loader_q_iomax.c:249-253 "at least one ... must be > 0"）；
#   本臂 wbps=33554432 > 0，满足。
# burst 语义：per-channel burst cap = rate * 0.5s（loader_q_iomax.c:72-75），
#   wbps cap = 16777216 bytes —— 与 native io.max 无 burst 语义不同，矩阵注明
#   （同态偏差，非同值点）。
# 用法：bash arms_iox.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_iox.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_max_iox_r${REP}"
STATS_LOG="$RUN_DIR/${RUN_ID}.stats.log"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_MAIN_CG"

# ---- stats 开关说明 -------------------------------------------------------
# [FIX-C] q_iomax 在 loaders_stats1 bundle 中是冻结副本，二进制内无
# --stats-interval-ms 选项（strings 计数=0），不传 stats 参数；
# io.max 核心图（写带宽时序）来自 fio，无影响。

# ---- 激活（q_iomax = queue policy，无 arbiter：先切 scx_io 再挂） ----------
sched_set scx_io
loader_start "$IOEXT_LOADER_DIR/loader_q_iomax" "$STATS_LOG" \
    -c "$IOEXT_MAIN_CG" --wbps "$IOEXT_WBPS_CAP" --rbps 0 --wiops 0
loader_wait_alive "$LOADER_PID" loader_q_iomax
LOADER_PIDS=("$LOADER_PID")

# 臂级 preflight 断言：loader 仍存活（全 0/非法值会即刻退出）
kill -0 "$LOADER_PID" || env_die "loader_q_iomax 提前退出（检查 stats.log 与 --wbps 值）"

# ---- 运行 ----------------------------------------------------------------
local_ts_start=$(date -Iseconds)
fio_in_cg "$IOEXT_MAIN_CG" "$HERE/fio_iox.fio" "$RUN_DIR" "$RUN_ID.main"
wait "$FIO_PID"
local_ts_end=$(date -Iseconds)

# ---- postflight ----------------------------------------------------------
# q_iomax 收尾会把 bucket 统计 dump 到 stderr（loader_q_iomax.c:155-222,293），
# 已随 stats.log 留档：dispatches 应约等于 60s * 8192 IOPS 上限。
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 1 "$STATS_LOG"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
