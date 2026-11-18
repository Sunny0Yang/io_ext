#!/bin/bash
# tests/io/full_suite/report/generate_report.sh
# 生成 Test_Report_<timestamp>.md
# 被 full_suite.sh source, 继承 FS_* 变量
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
FS_DIR="${FS_DIR:-$BASE_DIR/tests/io/full_suite}"
[ -z "${FS_RESULTS_DIR:-}" ] && source "$FS_DIR/lib_test.sh"

REPORT_FILE="$BASE_DIR/Test_Report_${FS_RUN_ID}.md"
TOTAL=$((FS_CASES_PASS + FS_CASES_FAIL + FS_CASES_BLOCKED))
PCT=0
if [ "$TOTAL" -gt 0 ]; then
    PCT=$((FS_CASES_PASS * 100 / TOTAL))
fi

# 覆盖率
COV_PCT=0
COV_GAPS=""
if [ -f "$FS_RESULTS_DIR/coverage/coverage_summary.json" ]; then
    COV_PCT=$(python3 -c "import json;d=json.load(open('$FS_RESULTS_DIR/coverage/coverage_summary.json'));print(d.get('iosched_ext_line_coverage_pct',0))" 2>/dev/null || echo 0)
fi
if [ -f "$FS_RESULTS_DIR/coverage/coverage_gaps.json" ]; then
    COV_GAPS=$(python3 -c "
import json
d=json.load(open('$FS_RESULTS_DIR/coverage/coverage_gaps.json'))
g=d.get('gaps',{})
for cat,items in g.items():
    if items:
        print(f'- {cat}: {len(items)} lines uncovered')
" 2>/dev/null || echo "")
fi

# 生成报告
cat > "$REPORT_FILE" <<EOF
# io_ext 测试报告 — ${FS_RUN_ID}

## 1. 执行摘要
- 运行时间: $(date '+%Y-%m-%d %H:%M:%S')
- 设备: ${FS_DEVICE}
- 调度器: scx_io
- 模式: ${FULL_SUITE_MODE:-quick}

## 2. 通过率
| 类型 | 总数 | Pass | Fail | Blocked | 通过率 |
|------|------|------|------|---------|--------|
| 总计 | ${TOTAL} | ${FS_CASES_PASS} | ${FS_CASES_FAIL} | ${FS_CASES_BLOCKED} | ${PCT}% |

## 3. Case 明细
| Case ID | 状态 | 详情 |
|---------|------|------|
EOF

for entry in "${FS_CASE_RESULTS[@]}"; do
    IFS='|' read -r cid status detail <<< "$entry"
    printf '| %s | %s | %s |\n' "$cid" "$status" "$detail" >> "$REPORT_FILE"
done

cat >> "$REPORT_FILE" <<EOF

## 4. 失败用例分析
EOF

FAILS=$(printf '%s\n' "${FS_CASE_RESULTS[@]}" | grep '|FAIL|' || true)
if [ -n "$FAILS" ]; then
    while IFS='|' read -r cid status detail; do
        printf '### %s\n- 详情: %s\n- 证据目录: %s/%s\n\n' \
            "$cid" "$detail" "$FS_RESULTS_DIR" "$(echo "$cid" | tr '/.' '__')" >> "$REPORT_FILE"
    done <<< "$FAILS"
else
    printf '无失败用例\n\n' >> "$REPORT_FILE"
fi

cat >> "$REPORT_FILE" <<EOF
## 5. 内核 Panic / Oops 记录
EOF

PANICS=$(dmesg 2>/dev/null | grep -iE 'panic|Oops|BUG:|hung_task' | tail -10 || true)
if [ -n "$PANICS" ]; then
    printf '```\n%s\n```\n\n' "$PANICS" >> "$REPORT_FILE"
else
    printf '无内核 panic/Oops 记录\n\n' >> "$REPORT_FILE"
fi

cat >> "$REPORT_FILE" <<EOF
## 6. 覆盖率
- iosched_ext.c 行覆盖: ${COV_PCT}%
- 覆盖率详情: ${FS_RESULTS_DIR}/coverage/

## 7. 盲区分类
${COV_GAPS:-无盲区分析数据}

## 8. 未覆盖关键代码行
见 ${FS_RESULTS_DIR}/coverage/coverage_gaps.json (若可用)

## 9. 结果目录
${FS_RESULTS_DIR}

## 10. 下次迭代建议
- 检查 BLOCKED 用例的阻塞原因
- 根据 coverage_gaps.json 补充测试用例
- 重试 FAIL 用例 (已尝试 5 次的标记 BLOCKED)
EOF

fs_log "report generated: $REPORT_FILE"
# 同时复制到 results 目录
cp "$REPORT_FILE" "$FS_RESULTS_DIR/Test_Report.md" 2>/dev/null || true

# 更新 Test_Design_IOExt.md 迭代历史
python3 -c "
import re
with open('$BASE_DIR/Test_Design_IOExt.md','r') as f:
    content=f.read()
new_row='| v0.2 | $(date +%Y-%m-%d) | ${FULL_SUITE_MODE:-quick} | ${PCT}% (P=${FS_CASES_PASS} F=${FS_CASES_FAIL} B=${FS_CASES_BLOCKED}) | ${COV_PCT}% | 自动生成 |'
content=re.sub(r'(\| v0\.1.*\|)', r'\1\n'+new_row, content)
with open('$BASE_DIR/Test_Design_IOExt.md','w') as f:
    f.write(content)
" 2>/dev/null || true

fs_log "Test_Design_IOExt.md updated with iteration history"