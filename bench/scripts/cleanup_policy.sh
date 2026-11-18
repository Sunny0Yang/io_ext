#!/bin/bash
# 策略清理 - 测试结束后或遇到 -EBUSY 时执行
# 用法: scripts/cleanup_policy.sh [TARGET_DEVICE]
set -euo pipefail

TARGET_DEVICE="${1:-nvme0n1}"

echo "=== 清理 io-ext 策略 ==="

echo "[1/4] 杀掉 loader 进程"
pkill -9 -f 'loader_arb_' 2>/dev/null || true
pkill -9 -f 'loader_q_' 2>/dev/null || true
pkill -9 -f 'loader_queue' 2>/dev/null || true

echo "[2/4] 清理 BPF pinned maps"
for map in ioext_cgroup_queues ioext_active_cgroups ioext_policy_meta \
	ioext_bfq_tenant_state ioext_prio_config \
	merge_stats merge_stats_map probe_cfg merge_cfg_map; do
	rm -rf "/sys/fs/bpf/${map}" 2>/dev/null || true
done

echo "[3/4] 回退调度器"
if [ -f "/sys/block/${TARGET_DEVICE}/queue/scheduler" ]; then
	echo none >"/sys/block/${TARGET_DEVICE}/queue/scheduler" 2>/dev/null || true
fi

echo "[4/4] 验证清理结果"
remaining=$(pgrep -f 'loader_arb_|loader_q_' 2>/dev/null | wc -l)
pinned=$(ls /sys/fs/bpf/ioext_* 2>/dev/null | wc -l)
sched=$(cat "/sys/block/${TARGET_DEVICE}/queue/scheduler" 2>/dev/null || echo "N/A")

echo ""
echo "Remaining loaders: ${remaining}"
echo "Pinned BPF maps:   ${pinned}"
echo "Scheduler:         ${sched}"

if [ "${remaining}" -eq 0 ] && [ "${pinned}" -eq 0 ]; then
	echo "=== 清理完成 ==="
else
	echo "=== 警告: 仍有残留，可能需要手动检查 ==="
fi
