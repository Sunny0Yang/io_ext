#!/bin/bash
# gen_golden_oracle.sh — Golden oracle 录制入口
#
# 用参考调度器 (native mq-deadline) 录制 golden expect JSON
# 或用 userspace 模拟器离线生成
#
# 用法:
#   gen_golden_oracle.sh --ref-scheduler mq-deadline --trace traces/micro/mqdd_seq_5.json --out expect/golden/mqdd_seq_5.json
#   gen_golden_oracle.sh --ref-scheduler mq-deadline --trace-dir traces/micro/ --out-dir expect/golden/
set -euo pipefail

REF_SCHED=""
TRACE=""
TRACE_DIR=""
OUT=""
OUT_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --ref-scheduler) REF_SCHED="${2:?}"; shift 2 ;;
        --trace)         TRACE="${2:?}"; shift 2 ;;
        --trace-dir)     TRACE_DIR="${2:?}"; shift 2 ;;
        --out)           OUT="${2:?}"; shift 2 ;;
        --out-dir)       OUT_DIR="${2:?}"; shift 2 ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

record_one() {
    local trace="$1"
    local out="$2"
    local ref="$3"
    local name
    name=$(python3 -c "import json;print(json.load(open('$trace')).get('name','unknown'))" 2>/dev/null || echo "unknown")

    echo "=== recording golden: $name (ref=$ref) ==="

    local dev="${TARGET_DEVICE:-vda}"
    local sys_block="/sys/block/$dev"
    local sched_path="$sys_block/queue/scheduler"

    [ -f "$sched_path" ] || { echo "missing $sched_path" >&2; exit 1; }
    [ -b "/dev/$dev" ] || { echo "missing /dev/$dev" >&2; exit 1; }

    pkill -9 -f 'loader_q_\|loader_arb_' 2>/dev/null || true
    echo none > "$sched_path" 2>/dev/null || true
    sleep 1
    echo "$ref" > "$sched_path" || { echo "failed to set $ref" >&2; exit 1; }
    sleep 1

    local bt_log="/tmp/golden_${name}.bt"
    bpftrace "$SCRIPT_DIR/bpftrace/trace_dispatch.bt" > "$bt_log" 2>&1 &
    local bt_pid=$!
    sleep 1

    "$SCRIPT_DIR/trace_driver" "/dev/$dev" "$trace" > /tmp/golden_${name}.trace 2>&1 || true
    sleep 1

    kill -INT "$bt_pid" 2>/dev/null || true
    wait "$bt_pid" 2>/dev/null || true

    echo none > "$sched_path" 2>/dev/null || true

    python3 -c "
import json, re, sys

events = {'inserts': [], 'completes': [], 'issues': []}
ins_re = re.compile(r'INSERT ts=(\d+) sector=(\d+) opf=(\d+) cgid=(\d+) nr=(\d+)')
comp_re = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+)')
with open('$bt_log') as f:
    for line in f:
        m = ins_re.search(line)
        if m:
            events['inserts'].append({'sector': int(m.group(2)), 'nr': int(m.group(5))})
            continue
        m = comp_re.search(line)
        if m:
            events['completes'].append({'sector': int(m.group(2)), 'nr': int(m.group(3))})
            continue

td = json.load(open('$trace'))
order = [{'sector': e['sector'], 'op': 'read', 'nr_sectors': e['nr']} for e in events['completes']]
result = {
    'name': td.get('name', 'unknown'),
    'tier': 2,
    'match': 'set',
    'oracle_source': 'blktrace',
    'oracle_ref': '$ref',
    'kernel_commit': 'recorded_in_vm',
    'recorded_at': 'auto',
    'expected_complete_order': order,
    'expected_merge': False,
    'notes': 'auto-recorded golden from $ref; set match for robustness'
}
with open('$out', 'w') as f:
    json.dump(result, f, indent=2)
print(f'golden written: $out ({len(order)} completes)')
" 2>&1
}

if [ -n "$TRACE" ] && [ -n "$OUT" ]; then
    record_one "$TRACE" "$OUT" "$REF_SCHED"
elif [ -n "$TRACE_DIR" ] && [ -n "$OUT_DIR" ]; then
    mkdir -p "$OUT_DIR"
    for t in "$TRACE_DIR"/*.json; do
        [ -f "$t" ] || continue
        name=$(python3 -c "import json;print(json.load(open('$t')).get('name','x'))" 2>/dev/null || echo "x")
        record_one "$t" "$OUT_DIR/${name}.json" "$REF_SCHED"
    done
else
    echo "usage: --trace+--out or --trace-dir+--out-dir required" >&2
    exit 1
fi