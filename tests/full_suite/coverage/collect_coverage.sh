#!/bin/bash
# tests/full_suite/coverage/collect_coverage.sh
# 收集内核 gcov 覆盖率数据 + 生成 lcov/html
# 被 full_suite.sh source, 继承 FS_* 变量
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
FS_DIR="${FS_DIR:-$BASE_DIR/tests/full_suite}"
[ -z "${FS_RESULTS_DIR:-}" ] && source "$FS_DIR/lib_test.sh"

COV_DIR="$FS_RESULTS_DIR/coverage"
mkdir -p "$COV_DIR"

fs_log "=== coverage collection ==="

# 检查 gcov 是否可用
if [ ! -d /sys/kernel/debug/gcov ]; then
    fs_warn "gcov not available (CONFIG_GCOV_KERNEL not set); skipping kernel coverage"
    echo '{"available":false,"reason":"gcov not mounted"}' > "$COV_DIR/coverage_summary.json"
    exit 0
fi

# 检查 iosched_ext.c 是否在 gcov 覆盖范围
GCOV_SRC="/sys/kernel/debug/gcov/block/iosched_ext.c"
if [ ! -d "$GCOV_SRC" ]; then
    # 尝试其他路径
    GCOV_SRC=$(find /sys/kernel/debug/gcov -name "iosched_ext*" -type d 2>/dev/null | head -1)
fi

if [ -z "$GCOV_SRC" ] || [ ! -d "$GCOV_SRC" ]; then
    fs_warn "iosched_ext.c not in gcov coverage; skipping"
    echo '{"available":false,"reason":"iosched_ext.c not instrumented"}' > "$COV_DIR/coverage_summary.json"
    exit 0
fi

# 复制 gcda 数据
fs_log "copying gcov data from $GCOV_SRC"
cp -r "$GCOV_SRC" "$COV_DIR/iosched_ext_gcov/" 2>/dev/null || true

# 尝试用 lcov 收集
if command -v lcov >/dev/null 2>&1; then
    fs_log "collecting with lcov..."
    lcov --capture --directory /sys/kernel/debug/gcov/block \
         --output-file "$COV_DIR/iosched_ext.lcov" 2>/dev/null || true

    if [ -f "$COV_DIR/iosched_ext.lcov" ]; then
        # 过滤仅 iosched_ext.c
        lcov --extract "$COV_DIR/iosched_ext.lcov" "*/block/iosched_ext.c" \
             --output-file "$COV_DIR/iosched_ext_only.lcov" 2>/dev/null || true

        # 生成 html
        if command -v genhtml >/dev/null 2>&1; then
            genhtml "$COV_DIR/iosched_ext_only.lcov" \
                    --output-directory "$COV_DIR/coverage_html" 2>/dev/null || true
        fi

        # 计算覆盖率百分比
        local cov_pct
        cov_pct=$(lcov --list "$COV_DIR/iosched_ext_only.lcov" 2>/dev/null | \
                  grep 'iosched_ext.c' | awk '{print $NF}' | tr -d '%' || echo 0)
        echo "{\"available\":true,\"iosched_ext_line_coverage_pct\":${cov_pct:-0}}" \
            > "$COV_DIR/coverage_summary.json"
        fs_log "iosched_ext.c line coverage: ${cov_pct:-0}%"
    fi
else
    # 无 lcov: 用 gcov 工具手动统计
    if command -v gcov >/dev/null 2>&1; then
        fs_log "using gcov tool (no lcov)..."
        cd "$COV_DIR"
        gcov -o "$(dirname "$GCOV_SRC")" "$BASE_DIR/kernel/linux-7.3.1/block/iosched_ext.c" 2>/dev/null || true
        echo '{"available":true,"method":"gcov_raw"}' > "$COV_DIR/coverage_summary.json"
    else
        fs_warn "neither lcov nor gcov available; raw gcda copied only"
        echo '{"available":true,"method":"raw_gcda_only"}' > "$COV_DIR/coverage_summary.json"
    fi
fi

# 运行盲区分析
if [ -f "$FS_DIR/coverage/analyze_gaps.py" ]; then
    fs_log "running gap analysis..."
    if [ -f "$COV_DIR/iosched_ext_only.lcov" ]; then
        python3 "$FS_DIR/coverage/analyze_gaps.py" "$COV_DIR/iosched_ext_only.lcov" \
                "$BASE_DIR/kernel/linux-7.3.1/block/iosched_ext.c" \
                > "$COV_DIR/coverage_gaps.json" 2>/dev/null || true
    fi
fi

fs_log "coverage collection done"