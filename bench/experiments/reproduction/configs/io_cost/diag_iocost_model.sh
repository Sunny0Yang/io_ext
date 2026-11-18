#!/bin/bash
# =========================================================================
# experiments/reproduction / io_cost / diag_iocost_model.sh
# 实验5 前置诊断步（[PLAN] 主会话补定：io.cost.model 需 guest 内 virtio 盘
# 重测，作为诊断步；冻结后写死进配置；诊断允许，结果不入正式 rep）。
#
# 工具（[FIX-C] 位置核对）：plan 猜测的 scripts/iocost_coef_gen_bm.py 不存在；
#   实际可用（[FIX-C] 实测路径）：
#   1) /opt/ioext/experiments/dyn_tenant_bm/build/
#      iocost_coef_gen_bm.py（bm host nvme0n1 校准所用版本；
#      CLI 参数 = --testdev/--testfile-size-gb/--duration/--seqio-block-mb/
#      --seqio-block-kb/--seq-depth/--rand-depth/--numjobs/--quiet/--verbose，
#      无 --json 参数，model 行打到 stdout）
#   2) 内核树上游原版 /opt/ioext-io_ext/kernel/linux-7.3.1/
#      tools/cgroup/iocost_coef_gen.py
#   guest VM 内把任一脚本拷入 /data 运行（file 模式，勿用 --testdev 毁盘）。
#
# 产出：/data/iocost_coef_stdout.log（含一行可直接写 io.cost.model 的文本）。
# 冻结流程：跑本脚本 -> 人工核对输出 -> 把 model 行替换进 arms_native.sh 的
#   MODEL_LINE -> IOCOST_MODEL_FROZEN=1 放行正式 rep。
# 用法（guest 内 root）：bash diag_iocost_model.sh
# 预计耗时：~15-20 min（每测试段 120s x 数段 + 二分饱和段）
# =========================================================================
set -euo pipefail

COEF_GEN="/data/iocost_coef_gen.py"     # 预先从 (1) 或 (2) 拷贝到 guest /data

[ -x "$COEF_GEN" ] || { echo "缺 $COEF_GEN（先拷贝 iocost_coef_gen_bm.py 或内核树 iocost_coef_gen.py 到 guest）"; exit 2; }
command -v fio >/dev/null || { echo "guest 内无 fio"; exit 2; }

# file 模式：16GB 测试文件，不用 --testdev（会整盘毁写）[FIX-C 安全决策]
python3 "$COEF_GEN" \
    --testfile-size-gb 16 \
    --duration 120 \
    --numjobs 2 \
    2>&1 | tee /data/iocost_coef_stdout.log

echo "=== io.cost.model 行（写根 cgroup 用） ==="
grep -E "ctrl=user|model=linear" /data/iocost_coef_stdout.log | tail -2 || true
echo
echo "下一步：把 model 行替换进 configs/io_cost/arms_native.sh 的 MODEL_LINE，"
echo "并 IOCOST_MODEL_FROZEN=1 放行正式 rep（[PLAN] 冻结后写死进配置）。"
