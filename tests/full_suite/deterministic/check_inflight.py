#!/usr/bin/env python3
"""check_inflight.py — Phase 0 G1 门禁: 调度器是否真正参与决策

解析 bpftrace 日志 (INSERT/COMPLETE 行 + 可选 ISSUE), 计算:
  - max_inflight: 任意时刻 INSERT 但未 COMPLETE 的请求数 (按 ts 排序扫描)
  - dispatch_with_choice: 至少一次 ISSUE 时队列中存在 >=2 个 pending (INSERT 未 COMPLETE)
  - play_window: [TRACE_PLAY_START, TRACE_PLAY_END] 内的事件计数

门禁 (G1): max_inflight >= 2  →  调度器有 >=2 个候选可选, 重排序可观测
门禁 (G2): dispatch_with_choice >= 1

退出码: 0=PASS (G1+G2 满足), 1=FAIL (scheduler_inactive), 2=BLOCKED (log 无效)

用法:
  check_inflight.py --bpftrace-log LOG [--out OUT.json] [--min-inflight 2]
"""
import argparse
import json
import re
import sys

INSERT_RE = re.compile(r'INSERT ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')

def is_background_readahead(rwbs):
    """REQ_RAHEAD is encoded as trailing 'A' in the block trace rwbs string
    (kernel blk_fill_rwbs). trace_driver submits O_DIRECT sync IO only, so
    readahead events at trace sectors are always background (partition scans,
    readahead storms) — excluded from gate accounting."""
    return 'A' in rwbs

def parse(path, trace_sectors=None):
    events = []
    with open(path) as f:
        for line in f:
            m = INSERT_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if is_background_readahead(m.group(4)):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                events.append((ts, 'INSERT', sector, int(m.group(3))))
                continue
            m = ISSUE_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if is_background_readahead(m.group(4)):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                events.append((ts, 'ISSUE', sector, int(m.group(3))))
                continue
            m = COMPLETE_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if is_background_readahead(m.group(4)):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                events.append((ts, 'COMPLETE', sector, int(m.group(3))))
                continue
    events.sort(key=lambda e: e[0])
    return events

def compute_inflight(events):
    """扫描 INSERT/ISSUE/COMPLETE 事件流:
    - max_inflight: ISSUE-but-not-COMPLETE 峰值 (G1, 设备级 in-flight)
    - dispatch_with_choice: 存在 >=2 个 INSERT-but-not-COMPLETED 的 ISSUE 次数
      (G2, EVOLUTION_PLAN §1.2: dispatch 时存在可选 request)
    INSERT 事件依赖 scx_io 路径的 trace_block_rq_insert (kernel
    block/iosched_ext.c scx_io_insert_requests, Phase-2 修复); 缺失时
    dispatch_with_choice 如实为 0, G2 诚实失败而非放水."""
    max_inflight = 0
    dispatch_with_choice = 0
    pending = []
    ins_pending = []
    for ts, typ, sector, nr in events:
        if typ == 'INSERT':
            ins_pending.append(sector)
        elif typ == 'ISSUE':
            if len(ins_pending) >= 2:
                dispatch_with_choice += 1
            pending.append(sector)
            if len(pending) > max_inflight:
                max_inflight = len(pending)
        elif typ == 'COMPLETE':
            if sector in pending:
                pending.remove(sector)
            elif pending:
                pending.pop(0)
            if sector in ins_pending:
                ins_pending.remove(sector)
            elif ins_pending:
                ins_pending.pop(0)
    return max_inflight, dispatch_with_choice

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bpftrace-log', required=True)
    ap.add_argument('--trace', required=False)
    ap.add_argument('--out', required=False)
    ap.add_argument('--min-inflight', type=int, default=2)
    args = ap.parse_args()

    trace_sectors = None
    if args.trace:
        try:
            td = json.load(open(args.trace))
            trace_sectors = set(r.get('sector', -1) for r in td.get('requests', []))
        except Exception:
            pass

    events = parse(args.bpftrace_log, trace_sectors)
    if not events:
        result = {'status': 'BLOCKED', 'reason': 'no_events_in_log',
                  'max_inflight': 0, 'dispatch_with_choice': 0}
        if args.out: json.dump(result, open(args.out,'w'), indent=2)
        print(json.dumps(result, indent=2))
        sys.exit(2)

    max_inf, dwc = compute_inflight(events)
    g1 = max_inf >= args.min_inflight
    g2 = dwc >= 1

    result = {
        'max_inflight': max_inf,
        'dispatch_with_choice': dwc,
        'min_inflight_required': args.min_inflight,
        'G1_pass': g1,
        'G2_pass': g2,
        'status': 'PASS' if (g1 and g2) else 'FAIL',
        'reason': '' if (g1 and g2) else 'scheduler_inactive' if not g1 else 'no_dispatch_with_choice',
    }
    if args.out: json.dump(result, open(args.out,'w'), indent=2)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['status']=='PASS' else 1)

if __name__ == '__main__':
    main()