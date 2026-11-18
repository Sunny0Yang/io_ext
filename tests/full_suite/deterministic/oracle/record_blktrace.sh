#!/bin/bash
# oracle/record_blktrace.sh — blktrace 录制辅助
#
# 用 blktrace/blkparse 替代 bpftrace 录制 dispatch 序列
# 适用于 bpftrace 不可用的环境
set -euo pipefail

DEV="${1:?usage: record_blktrace.sh /dev/<dev> <trace.json> <out.json>}"
TRACE="${2:?}"
OUT="${3:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEV_NAME=$(basename "$DEV")

# 启动 blktrace
blktrace -d "$DEV" -o - | blkparse -i - -f "%S %d %N\n" > /tmp/bt_${DEV_NAME}.txt 2>/dev/null &
BT_PID=$!
sleep 1

# 播放 trace
"$SCRIPT_DIR/trace_driver" "$DEV" "$TRACE" > /tmp/bt_${DEV_NAME}.trace 2>&1 || true
sleep 1

kill -INT "$BT_PID" 2>/dev/null || true
wait "$BT_PID" 2>/dev/null || true

# 解析 blkparse 输出为 expect JSON
python3 -c "
import json, re
events = []
with open('/tmp/bt_${DEV_NAME}.txt') as f:
    for line in f:
        parts = line.split()
        if len(parts) >= 3:
            try:
                sector = int(parts[0])
                nr = int(parts[2])
                events.append({'sector': sector, 'op': 'read', 'nr_sectors': nr})
            except: pass
td = json.load(open('$TRACE'))
result = {
    'name': td.get('name', 'unknown'),
    'tier': 2,
    'match': 'set',
    'oracle_source': 'blktrace',
    'expected_complete_order': events,
    'expected_merge': False,
    'notes': 'recorded via blktrace'
}
with open('$OUT', 'w') as f:
    json.dump(result, f, indent=2)
print(f'blktrace golden: $OUT ({len(events)} events)')
"