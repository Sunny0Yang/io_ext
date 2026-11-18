#!/bin/bash
# tests/io/full_suite/00_setup.sh
#
# VM 内环境初始化: cgroup/bpffs/disk/scheduler/loader 检查
#
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)}"
source "$BASE_DIR/tests/io/full_suite/lib_test.sh"

fs_log "=== full_suite environment setup ==="

# 1. VM 检测
fs_require_vm

# 2. 设备检测
fs_detect_device

# 3. 破坏性测试守卫
fs_require_destructive_guard

# 4. BPF 文件系统
fs_ensure_bpffs

# 5. cgroup IO 委托
fs_setup_cgroup_io

# 6. 清理僵尸
fs_cleanup_ioext

# 7. 切换到 scx_io
fs_set_scx_scheduler

# 8. 检查 loader 存在
fs_log "checking loaders..."
for pol in "${FS_NON_TRIVIAL_POLICIES[@]}"; do
    loader="$FS_LOADER_DIR/loader_${pol}"
    if [ ! -x "$loader" ]; then
        fs_warn "missing loader: $loader (will skip $pol)"
    fi
done

# 9. 检查工具
for cmd in fio bpftool; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fs_warn "missing tool: $cmd (some tests may be skipped)"
    fi
done

# 10. 初始化运行
fs_init_run

# 11. dmesg 基线
FS_DMESG_BASE="$(fs_dmesg_mark "setup")"
echo "$FS_DMESG_BASE" > "$FS_RESULTS_DIR/dmesg_base.txt"

fs_log "=== setup complete: device=$FS_DEVICE scheduler=$(fs_active_scheduler) ==="
fs_log "=== results_dir=$FS_RESULTS_DIR ==="