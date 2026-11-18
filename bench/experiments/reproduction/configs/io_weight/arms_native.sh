#!/bin/bash
# =========================================================================
# experiments/reproduction / io_weight / arms_native.sh
# 实验4 io.weight，native 臂：scheduler=bfq + 权重 100:400。
#
# [PLAN] native=scheduler bfq + io.weight 100/400（两 cgroup 权重 100:400）。
#
# [FIX-C] 内核载体核对（/opt/ioext-io_ext/kernel/linux-7.3.1/block/）：
#   - io.weight 是 blk-iocost 注册的 cftypes（blk-iocost.c:3574-3579
#     .name="weight" -> io.weight）；其 per-device 写路径走 blkg_conf_prep
#     （blk-iocost.c:3186-3190），要求 iocost 策略已在盘上激活
#     （blk-cgroup.c blkg_conf_prep: "blkcg_policy_enabled" 检查，未激活
#     返回 -EOPNOTSUPP），而 iocost 只在写 io.cost.model/qos 时激活
#     （blk-iocost.c:3311/3503 blk_iocost_init）。
#   - BFQ 的权重载体是 io.bfq.weight（bfq-cgroup.c:1281 .name="bfq.weight"，
#     接受 "default N" 或 "N"，bfq_io_set_weight），scheduler=bfq 附着后即
#     可写（bfq 挂盘时激活 blkcg_policy_bfq）。
#   - homo_v2 冻结口径：ctrl_n_io_weight 臂 scheduler=bfq + io_weight knob
#     （gen_homo_v2_yamls.py ctrl_n_io_weight + 注释 "native io.weight's ONLY
#     effective native carrier is BFQ"），经 common.py setup_cgroup_io_controls
#     写 per-device 行（common.py:716 "<MAJ:MIN> <weight>"）。
# 因此本脚本激活顺序：echo bfq -> io.bfq.weight default 100/400（主载体，
# 读回断言）-> io.weight "<MM> 100/400"（homo_v2 口径 best-effort；若因
# iocost 未激活返回 EOPNOTSUPP 则记录不 fail-stop，矩阵偏差表注明）。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_weight_native_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
grep -qw bfq "$IOEXT_SCHED" || env_die "bfq 不在可用 scheduler 列表（guest 内核需编入 CONFIG_BFQ）"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"
MM="$(ioext_maj_min)"

# ---- 激活 ----------------------------------------------------------------
sched_set bfq

# BFQ 权重主载体（[FIX-C] bfq-cgroup.c:1281）
echo "default 100" > "$IOEXT_LC_CG/io.bfq.weight" || env_die "io.bfq.weight LC 写入失败"
echo "default 400" > "$IOEXT_BE_CG/io.bfq.weight" || env_die "io.bfq.weight BE 写入失败"

# homo_v2 冻结口径补充写 io.weight（best-effort，理由见文件头 [FIX-C]）
w_fail=0
echo "$MM 100" > "$IOEXT_LC_CG/io.weight" 2>/dev/null || w_fail=1
echo "$MM 400" > "$IOEXT_BE_CG/io.weight" 2>/dev/null || w_fail=1
[ "$w_fail" = "0" ] \
    && env_log "io.weight per-device 写入成功" \
    || env_log "WARN: io.weight per-device 写入被拒（iocost 未在盘上激活，-EOPNOTSUPP，[FIX-C] blk-cgroup.c blkg_conf_prep）；BFQ 载体 io.bfq.weight 已生效，记录为声明偏差"

# ---- 臂级 preflight 断言 --------------------------------------------------
grep -q "default 100" "$IOEXT_LC_CG/io.bfq.weight" || env_die "LC io.bfq.weight 读回 != default 100"
grep -q "default 400" "$IOEXT_BE_CG/io.bfq.weight" || env_die "BE io.bfq.weight 读回 != default 400"

# ---- 运行（同 none 臂；跑至份额稳定 = 90s step 全窗 [PLAN]+[MSC]） ---------
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
[ "$(sched_current)" = "bfq" ] || env_die "运行后 scheduler != bfq"
grep -q "default 100" "$IOEXT_LC_CG/io.bfq.weight" || env_die "运行后 LC 权重漂移"
grep -q "default 400" "$IOEXT_BE_CG/io.bfq.weight" || env_die "运行后 BE 权重漂移"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
