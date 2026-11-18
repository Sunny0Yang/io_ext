#!/usr/bin/env python3
"""coverage gap analyzer for iosched_ext.c

读取 lcov 文件, 对比源码, 输出未覆盖行分类 + 补充用例建议
用法: analyze_gaps.py <lcov_file> <source_file>
"""
import json
import re
import sys

def parse_lcov(path):
    covered = set()
    uncovered = set()
    total_lines = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('DA:'):
                parts = line[3:].split(',')
                if len(parts) >= 2:
                    ln = int(parts[0])
                    cnt = int(parts[1])
                    total_lines += 1
                    if cnt > 0:
                        covered.add(ln)
                    else:
                        uncovered.add(ln)
    return covered, uncovered, total_lines

def classify_uncovered(source_path, uncovered_lines):
    gaps = {
        'error_handling': [],
        'edge_case': [],
        'concurrency': [],
        'cleanup': [],
        'other': []
    }
    with open(source_path) as f:
        src_lines = f.readlines()

    for ln in sorted(uncovered_lines):
        if ln > len(src_lines):
            continue
        ctx = ''.join(src_lines[max(0,ln-3):min(len(src_lines),ln+3)])
        line = src_lines[ln-1].strip() if ln <= len(src_lines) else ''
        entry = {'line': ln, 'code': line}
        if re.search(r'(ret\s*<\s*0|return\s+-\w+|goto\s+\w*err|ENODEV|EINVAL|ENOMEM|BUG|WARN)', ctx, re.I):
            gaps['error_handling'].append(entry)
        elif re.search(r'(list_empty|==\s*0|!=\s*0|refcount|atomic.*==)', ctx, re.I):
            gaps['edge_case'].append(entry)
        elif re.search(r'(spin_lock|mutex|rcu|synchronize|atomic_inc|atomic_dec)', ctx, re.I):
            gaps['concurrency'].append(entry)
        elif re.search(r'(goto\s+\w*(free|clean|destroy|out)|kfree|put\b)', ctx, re.I):
            gaps['cleanup'].append(entry)
        else:
            gaps['other'].append(entry)
    return gaps

def suggest_cases(gaps):
    suggestions = []
    if gaps['error_handling']:
        suggestions.append({
            'category': 'error_handling',
            'count': len(gaps['error_handling']),
            'suggestion': '构造 cgroup offline 期间 dispatch / invalid rq_ptr 以触发 -ENODEV 路径'
        })
    if gaps['concurrency']:
        suggestions.append({
            'category': 'concurrency',
            'count': len(gaps['concurrency']),
            'suggestion': '多 cgroup 并发 attach/detach 竞态测试'
        })
    if gaps['cleanup']:
        suggestions.append({
            'category': 'cleanup',
            'count': len(gaps['cleanup']),
            'suggestion': '强制 kill -9 loader 后重新 attach, 验证 cleanup 路径'
        })
    if gaps['edge_case']:
        suggestions.append({
            'category': 'edge_case',
            'count': len(gaps['edge_case']),
            'suggestion': 'iodepth=1 低并发 + iodepth=256 高并发对比'
        })
    return suggestions

def main():
    if len(sys.argv) < 3:
        print(json.dumps({'error': 'usage: analyze_gaps.py <lcov> <source>'}))
        sys.exit(1)
    lcov_file = sys.argv[1]
    source_file = sys.argv[2]

    covered, uncovered, total = parse_lcov(lcov_file)
    cov_pct = (len(covered) / total * 100) if total > 0 else 0

    gaps = classify_uncovered(source_file, uncovered)
    suggestions = suggest_cases(gaps)

    result = {
        'total_instrumented_lines': total,
        'covered_lines': len(covered),
        'uncovered_lines': len(uncovered),
        'line_coverage_pct': round(cov_pct, 1),
        'gaps': gaps,
        'suggested_cases': suggestions
    }
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()