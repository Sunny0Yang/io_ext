#!/bin/bash
# =========================================================================
# experiments/reproduction / io_cost / arms_native.sh
# 实验5 io.cost，native 臂：根 cgroup io.cost.model + io.cost.qos，
# LC/BE cgroup io.weight 100/200，scheduler=none。
#
# [PLAN] 固定项：
#   io.weight LC=100/BE=200；
#   io.cost.qos enable=1 rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150；
#   io.cost.model 需 guest 内 virtio 盘重测（诊断步），冻结后写死。
#
# [FIX-C] 内核写序核对（/opt/ioext-io_ext/kernel/linux-7.3.1/block/
# blk-iocost.c）：
#   - io.cost.model / io.cost.qos 是 CFTYPE_ONLY_ON_ROOT（blk-iocost.c:3583-3594）
#     -> 只能在根 cgroup 写，行首带 "<MAJ:MIN>" 设备前缀。
#   - 写顺序 model 先于 qos（[PLAN] 要求确认的顺序）：本树 ioc_qos_write 在
#     ioc 未初始化时会自动 blk_iocost_init（blk-iocost.c:3310-3315），但按
#     "先 model 激活、再 qos 调参" 的惯例顺序写，保证 model 在 qos enable=1
#     开始调节前就位；同时 per-cgroup io.weight per-device 写需要 iocost 已
#     激活（blk-cgroup.c blkg_conf_prep 的 blkcg_policy_enabled 检查，
#     激活点 = blk_iocost_init，blk-iocost.c:3311/3503），因此本脚本顺序：
#     1) io.cost.model  2) LC/BE io.weight  3) io.cost.qos。
#   - 读回格式：io.cost.qos "<MM> enable=1 ctrl=user rpct=99.00 rlat=2000
#     wpct=95.00 wlat=1000000 min=50.00 max=150.00"（blk-iocost.c:3232
#     seq_printf）。
#   - native vrate 可从 io.stat 读 "cost.vrate=X.YZ"（blk-iocost.c:3110，
#     ioc_pd_stat，仅 level-0 iocg 打印、ioc enabled 时）—— 1s 采样进
#     native_state.log，即 native vrate 时序数据源（[PLAN] native vrate
#     归一化处理：vrate/100 相对 1.0 = 满模型速率）。
# model 行（guest vdb 诊断重测后冻结；下行数值为占位 MUST-FREEZE，重测脚本：
#   /opt/ioext/experiments/dyn_tenant_bm/build/
#   iocost_coef_gen_bm.py（[FIX-C] 实际位置，plan 猜测的 scripts/ 不存在），
#   guest 内对文件模式跑：--testfile-size-gb 16（勿用 --testdev，会毁盘），
#   输出直接可写 io.cost.model。bm host 的 calibration_bm.json 是 nvme0n1
#   的（experiments/homo_v2/calibration_bm.json），guest virtio 不适用。
# 用法：bash arms_native.sh <RUN_DIR> <REP>
# =========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../common/env_preflight.sh
source "$HERE/../common/env_preflight.sh"

RUN_DIR="${1:?用法: arms_native.sh <RUN_DIR> <REP>}"
REP="${2:?缺 REP}"
RUN_ID="io_cost_native_r${REP}"
mkdir -p "$RUN_DIR"

env_preflight
snapshot_state "$RUN_DIR/${RUN_ID}.preflight.txt" "preflight"
[ "$(sched_current)" = "none" ] || env_die "起步 scheduler != none"
cg_prep "$IOEXT_LC_CG"
cg_prep "$IOEXT_BE_CG"
MM="$(ioext_maj_min)"

# ---- 冻结的 model 行（MUST-FREEZE：由诊断步产出后替换占位；诊断步见文件头） --
# fmt: <MM> ctrl=user model=linear rbps=.. rseqiops=.. rrandiops=.. wbps=.. wseqiops=.. wrandiops=..
MODEL_LINE="$MM ctrl=user model=linear rbps=1877961028 rseqiops=436504 rrandiops=504812 wbps=1895474286 wseqiops=436118 wrandiops=324051"
# 上面占位 = bm host nvme0n1 值（calibration_bm.json），仅作格式样例：
if grep -q "MUST-FREEZE" "$RUN_DIR/.model_frozen" 2>/dev/null || [ "${IOCOST_MODEL_FROZEN:-0}" != "1" ]; then
    env_log "WARN: io.cost.model 使用 bm host nvme0n1 占位值（calibration_bm.json），"
    env_log "      guest virtio vdb 未重测。正式 rep 前必须跑诊断步并置 IOCOST_MODEL_FROZEN=1。"
    [ "${IOEXT_ALLOW_UNFROZEN_MODEL:-0}" = "1" ] || env_die " refusing formal native run with unfrozen io.cost.model（[PLAN] guest virtio 盘重测为诊断步，冻结后写死）"
fi

# ---- 激活（顺序 [FIX-C]：model -> weight -> qos） --------------------------
echo "$MODEL_LINE" > "$IOEXT_CG_ROOT/io.cost.model" || env_die "io.cost.model 写入失败"
echo "$MM 100"  > "$IOEXT_LC_CG/io.weight" || env_die "LC io.weight 写入失败（model 后策略已激活）"
echo "$MM 200"  > "$IOEXT_BE_CG/io.weight" || env_die "BE io.weight 写入失败"
echo "$MM enable=1 ctrl=user rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150" \
    > "$IOEXT_CG_ROOT/io.cost.qos" || env_die "io.cost.qos 写入失败"

# ---- 臂级 preflight 断言 --------------------------------------------------
local_line="$(cat "$IOEXT_CG_ROOT/io.cost.qos")"
echo "$local_line" | grep -q "enable=1"  || env_die "io.cost.qos 读回无 enable=1: $local_line"
echo "$local_line" | grep -q "rlat=2000" || env_die "io.cost.qos 读回无 rlat=2000: $local_line"
grep -q "$MM 100" "$IOEXT_LC_CG/io.weight" || env_die "LC io.weight 读回 != $MM 100"
grep -q "$MM 200" "$IOEXT_BE_CG/io.weight" || env_die "BE io.weight 读回 != $MM 200"
env_log "io.cost armed: $local_line"

# ---- 运行（coloc step workload，同 kyber/io_weight 形状） ------------------
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
# qos 应仍 enable=1 且 vrate 有波动（归一化后的时序图数据）
grep -q "enable=1" "$IOEXT_CG_ROOT/io.cost.qos" || env_die "运行后 io.cost.qos enable 漂移"
snapshot_state "$RUN_DIR/${RUN_ID}.postflight.txt" "postflight"
teardown_common "$RUN_DIR" "$RUN_ID" 0 "-"
# 根 io.cost 复位（下次 env_preflight 的 enable=0 断言依赖此步）
echo "$MM enable=0 ctrl=user rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150" \
    > "$IOEXT_CG_ROOT/io.cost.qos" || env_log "WARN: io.cost.qos enable=0 复位失败"
printf 'RUN\t%s\t%s\t%s\t%s\n' "$RUN_ID" "$local_ts_start" "$local_ts_end" "done" \
    > "$RUN_DIR/${RUN_ID}.status.txt"
env_log "arm done: $RUN_ID"
