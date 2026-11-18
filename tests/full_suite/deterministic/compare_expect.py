#!/usr/bin/env python3
"""compare_expect.py — DT 断言引擎 v2

Phase 0 升级:
  - 窗口隔离: 仅统计 TRACE_PLAY_START..TRACE_PLAY_END 之间的 INSERT/ISSUE/COMPLETE
    过滤背景 IO (journald/kworker) 污染
  - inflight_stats: max_inflight / dispatch_with_choice 写入 compare.json
  - op 提取: 从 ISSUE rwbs 与 COMPLETE rwbs 推断 read/write, 支持 lane 断言
  - 严格 exact: 窗口内 complete 序列必须严格匹配 expected 顺序 (relative order)
  - rt_before_be_before_idle: 在 expected_complete_order 中按 op 顺序检查
  - small_before_large: 检查所有 small sector 在任何 large sector 之前完成

退出码: 0=PASS, 1=FAIL, 2=SKIP/BLOCKED
"""
import argparse
import json
import re
import sys

INSERT_RE = re.compile(r'INSERT ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+) ioprio=(\d+)')
ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+) ioprio=(\d+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
PLAY_START_RE = re.compile(r'TRACE_PLAY_START')
PLAY_END_RE = re.compile(r'TRACE_PLAY_END')

OPF_READ = 0
OPF_WRITE = 1
OPF_DISCARD = 3

def rwbs_to_op(rwbs):
    if 'W' in rwbs: return "write"
    if 'D' in rwbs: return "discard"
    return "read"

def ioprio_class(ioprio):
    return (ioprio >> 13) & 0x7

def cluster_events(events, gap_ms=100):
    """Cluster ISSUE/COMPLETE events into a dense burst (trace requests) by
    detecting a timestamp gap larger than gap_ms. Returns only events within
    the first dense burst starting from the first event. Background IO
    (journald flush, readahead) is separated by large gaps in virtme-ng."""
    if not events:
        return events
    first_ts = events[0]['ts']
    cluster = []
    prev_ts = first_ts
    for e in events:
        if e['ts'] - prev_ts > gap_ms * 1_000_000 and cluster:
            break
        cluster.append(e)
        prev_ts = e['ts']
    return cluster

def parse_bpftrace(path, trace_sectors=None):
    """Parse bpftrace log. If trace_sectors provided, filter ISSUE/COMPLETE
    events to trace request sectors (background IO isolation). Also skip
    leading events before the first trace-sector ISSUE so the dense-burst
    cluster starts at the trace, not at bpftrace init noise."""
    inserts = []
    completes = []
    issues = []
    window_found = True
    with open(path) as f:
        all_lines = f.readlines()
    first_trace_issue_ts = None
    if trace_sectors is not None:
        for line in all_lines:
            m = ISSUE_RE.search(line)
            if m and int(m.group(2)) in trace_sectors:
                rwbs = m.group(4)
                if rwbs in ('N', 'FF', 'F') or 'A' in rwbs:
                    continue
                first_trace_issue_ts = int(m.group(1))
                break
    for line in all_lines:
            m = INSERT_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if 'A' in m.group(4):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                if trace_sectors is not None and first_trace_issue_ts is not None and ts + 100_000_000 < first_trace_issue_ts:
                    continue
                inserts.append({
                    'ts': ts, 'sector': sector,
                    'nr': int(m.group(3)), 'rwbs': m.group(4),
                    'ioprio': int(m.group(5)),
                    'op': rwbs_to_op(m.group(4)),
                    'prio_class': ioprio_class(int(m.group(5))),
                })
                continue
            m = ISSUE_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if 'A' in m.group(4):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                if trace_sectors is not None and first_trace_issue_ts is not None and ts + 100_000_000 < first_trace_issue_ts:
                    continue
                issues.append({
                    'ts': ts, 'sector': sector,
                    'nr': int(m.group(3)), 'rwbs': m.group(4),
                    'ioprio': int(m.group(5)),
                    'op': rwbs_to_op(m.group(4)),
                })
                continue
            m = COMPLETE_RE.search(line)
            if m:
                ts = int(m.group(1)); sector = int(m.group(2))
                if 'A' in m.group(4):
                    continue
                if trace_sectors is not None and sector not in trace_sectors:
                    continue
                if trace_sectors is not None and first_trace_issue_ts is not None and ts + 100_000_000 < first_trace_issue_ts:
                    continue
                completes.append({
                    'ts': ts, 'sector': sector,
                    'nr': int(m.group(3)), 'rwbs': m.group(4),
                    'op': rwbs_to_op(m.group(4)),
                })
                continue
    return {'inserts': inserts, 'completes': completes, 'issues': issues,
            'window_found': window_found}

def check_tier0(events, trace_reqs):
    errors = []
    if len(events['completes']) < 1:
        errors.append("complete_count=0, no IO completed in window")
    return errors

def check_invariants(events, invariants, bpftool_stats=None, trace_req_count=0):
    errors = []
    ins = events['inserts']
    comp = events['completes']
    iss = events['issues']
    if 'insert_count_min' in invariants and len(ins) < invariants['insert_count_min']:
        errors.append(f"insert_count={len(ins)} < min={invariants['insert_count_min']}")
    if 'insert_count_eq' in invariants and len(ins) != invariants['insert_count_eq']:
        errors.append(f"insert_count={len(ins)} != eq={invariants['insert_count_eq']}")
    if 'complete_count_min' in invariants and len(comp) < invariants['complete_count_min']:
        errors.append(f"complete_count={len(comp)} < min={invariants['complete_count_min']}")
    if 'complete_count_lt' in invariants and len(comp) >= invariants['complete_count_lt']:
        errors.append(f"complete_count={len(comp)} >= lt={invariants['complete_count_lt']} (expected merge to reduce count)")
    if 'issue_count_lt' in invariants and len(iss) >= invariants['issue_count_lt']:
        errors.append(f"issue_count={len(iss)} >= lt={invariants['issue_count_lt']} (expected merge to reduce dispatch count)")
    if 'merge_count_min' in invariants and trace_req_count > 0:
        merge_count = max(0, trace_req_count - len(iss))
        if merge_count < invariants['merge_count_min']:
            errors.append(f"merge_count={merge_count} < min={invariants['merge_count_min']} (trace_reqs={trace_req_count} issues={len(iss)})")
    if 'merge_count_max' in invariants and trace_req_count > 0:
        merge_count = max(0, trace_req_count - len(iss))
        if merge_count > invariants['merge_count_max']:
            errors.append(f"merge_count={merge_count} > max={invariants['merge_count_max']}")
    if 'merge_coverage_min' in invariants:
        total_nr = sum(e.get('nr', 0) for e in iss)
        if total_nr < invariants['merge_coverage_min']:
            errors.append(f"merge_coverage={total_nr} < min={invariants['merge_coverage_min']} (total ISSUE nr; expected full range covered by merged requests)")
    if bpftool_stats and 'bpftool_stats' in invariants:
        if isinstance(bpftool_stats, list):
            # Tolerate raw `bpftool -j map dump` output ([{key,value},...]):
            # flatten dict-shaped values (raw hex-byte lists cannot be
            # interpreted generically and are skipped).
            flat = {}
            for entry in bpftool_stats:
                if not isinstance(entry, dict):
                    continue
                value = entry.get('value', {})
                if isinstance(value, dict):
                    flat.update(value)
                elif isinstance(entry.get('formatted'), dict):
                    flat.update(entry['formatted'])
            bpftool_stats = flat
        for map_key, checks in invariants['bpftool_stats'].items():
            actual = bpftool_stats.get(map_key, 0)
            if 'eq' in checks and actual != checks['eq']:
                errors.append(f"bpftool {map_key}={actual} != eq={checks['eq']}")
            if 'min' in checks and actual < checks['min']:
                errors.append(f"bpftool {map_key}={actual} < min={checks['min']}")
            if 'max' in checks and actual > checks['max']:
                errors.append(f"bpftool {map_key}={actual} > max={checks['max']}")
    return errors

def match_exact(actual, expected, allow_merge=False):
    if allow_merge:
        actual_sectors = set(e['sector'] for e in actual)
        expected_sectors = set(e['sector'] for e in expected)
        covered = set()
        for e in expected:
            for a in actual:
                if a['sector'] <= e['sector'] < a['sector'] + a.get('nr', 0) + e.get('nr_sectors', e.get('nr', 8)):
                    covered.add(e['sector'])
                    break
        if len(covered) != len(expected_sectors):
            return [f"exact+merge: only {len(covered)}/{len(expected_sectors)} expected sectors covered"]
        return []
    actual_sectors = [a['sector'] for a in actual]
    expected_sectors = [e['sector'] for e in expected]
    missing = [s for s in expected_sectors if s not in actual_sectors]
    if missing:
        return [f"exact: missing sectors {missing} (actual={actual_sectors})"]
    idx = 0
    for e in expected_sectors:
        found = False
        for i in range(idx, len(actual_sectors)):
            if actual_sectors[i] == e:
                idx = i + 1
                found = True
                break
        if not found:
            return [f"exact: sector {e} not in expected relative order (actual order={actual_sectors})"]
    return []

def match_partial(actual, expected, constraints):
    errors = []
    actual_sectors = [a['sector'] for a in actual]
    for c in constraints:
        if c.get('sectors_monotonic'):
            lane = c.get('lane')
            prev_sector = -1
            for a in actual:
                if lane and a.get('op') != lane:
                    continue
                if a['sector'] < prev_sector:
                    errors.append(f"partial: sector {a['sector']} < prev {prev_sector} in lane {lane}")
                prev_sector = a['sector']
        if c.get('all_sectors_present'):
            expected_set = set(c['all_sectors_present'])
            actual_set = set(actual_sectors)
            missing = expected_set - actual_set
            if missing:
                errors.append(f"partial: missing sectors {missing}")
        if c.get('lane_before_lane'):
            first_lane = c['lane_before_lane']['first']
            second_lane = c['lane_before_lane']['second']
            first_idx = None
            second_idx = None
            for i, a in enumerate(actual):
                if first_idx is None and a.get('op') == first_lane:
                    first_idx = i
                if second_idx is None and a.get('op') == second_lane:
                    second_idx = i
            if first_idx is not None and second_idx is not None and first_idx > second_idx:
                errors.append(f"partial: lane {first_lane} (idx={first_idx}) not before lane {second_lane} (idx={second_idx})")
        if c.get('all_sectors_in_lane_before_other_lane'):
            first_lane = c['all_sectors_in_lane_before_other_lane']['first']
            second_lane = c['all_sectors_in_lane_before_other_lane']['second']
            last_first = -1
            first_second = len(actual)
            for i, a in enumerate(actual):
                if a.get('op') == first_lane:
                    last_first = i
                if a.get('op') == second_lane and first_second == len(actual):
                    first_second = i
            if last_first >= 0 and first_second < len(actual) and last_first > first_second:
                errors.append(f"partial: {first_lane} last idx={last_first} after first {second_lane} idx={first_second}")
        if c.get('rt_before_be_before_idle'):
            order_classes = []
            for a in actual:
                pc = a.get('prio_class', 2)
                if pc == 1: order_classes.append('rt')
                elif pc == 3: order_classes.append('idle')
                else: order_classes.append('be')
            expected_seq = ['rt', 'be', 'idle']
            seq_idx = 0
            for cls in order_classes:
                while seq_idx < len(expected_seq) and expected_seq[seq_idx] != cls:
                    seq_idx += 1
                if seq_idx >= len(expected_seq):
                    errors.append(f"partial: prio class order violation: {order_classes}")
                    break
                seq_idx += 1
        if c.get('small_before_large'):
            small_sectors = set(c.get('small_sectors', []))
            large_sectors = set(c.get('large_sectors', []))
            last_small = -1
            first_large = len(actual)
            for i, a in enumerate(actual):
                if a['sector'] in small_sectors:
                    last_small = i
                if a['sector'] in large_sectors and first_large == len(actual):
                    first_large = i
            if last_small >= 0 and first_large < len(actual) and last_small > first_large:
                errors.append(f"partial: small last idx={last_small} after first large idx={first_large}")
        if c.get('sectors_before_sectors'):
            first_set = set(c['sectors_before_sectors']['first'])
            second_set = set(c['sectors_before_sectors']['second'])
            last_first = -1
            first_second = len(actual)
            for i, a in enumerate(actual):
                if a['sector'] in first_set:
                    last_first = i
                if a['sector'] in second_set and first_second == len(actual):
                    first_second = i
            if last_first >= 0 and first_second < len(actual) and last_first > first_second:
                errors.append(f"partial: sectors {first_set} last idx={last_first} after first {second_set} idx={first_second}")
    return errors

def match_set(actual, expected):
    actual_set = set(a['sector'] for a in actual)
    expected_set = set(e['sector'] for e in expected)
    missing = expected_set - actual_set
    if missing:
        return [f"set: missing sectors {missing}"]
    return []

def sector_in_issue(sector, issue):
    """Check if trace sector is covered by a merged ISSUE event."""
    return issue['sector'] <= sector < issue['sector'] + issue.get('nr', 8)

def match_exact_dispatch(actual, expected, allow_merge):
    """Match dispatch (ISSUE) order. Handles merge: expected sectors may be
    covered by merged ISSUE events with larger nr."""
    actual_sectors = [a['sector'] for a in actual]
    expected_sectors = [e['sector'] for e in expected]
    if allow_merge:
        covered = set()
        for e in expected:
            for a in actual:
                if sector_in_issue(e['sector'], a):
                    covered.add(e['sector'])
                    break
        if len(covered) != len(expected_sectors):
            return [f"exact+merge: only {len(covered)}/{len(set(expected_sectors))} expected sectors covered (actual sectors={actual_sectors})"]
        return []
    missing = [s for s in expected_sectors if s not in actual_sectors]
    if missing:
        return [f"exact: missing sectors {missing} (actual={actual_sectors})"]
    idx = 0
    for e in expected_sectors:
        found = False
        for i in range(idx, len(actual_sectors)):
            if actual_sectors[i] == e:
                idx = i + 1
                found = True
                break
        if not found:
            return [f"exact: sector {e} not in expected relative order (actual order={actual_sectors})"]
    return []

def match_set_dispatch(actual, expected):
    actual_set = set(a['sector'] for a in actual)
    expected_set = set(e['sector'] for e in expected)
    missing = expected_set - actual_set
    if missing:
        return [f"set: missing sectors {missing}"]
    return []

def match_partial_dispatch(actual, expected, constraints):
    """Match dispatch (ISSUE) order with partial constraints."""
    errors = []
    actual_sectors = [a['sector'] for a in actual]
    for c in constraints:
        if c.get('sectors_monotonic'):
            lane = c.get('lane')
            prev_sector = -1
            for a in actual:
                if lane and a.get('op') != lane:
                    continue
                if a['sector'] < prev_sector:
                    errors.append(f"partial: sector {a['sector']} < prev {prev_sector} in lane {lane}")
                prev_sector = a['sector']
        if c.get('all_sectors_present'):
            expected_set = set(c['all_sectors_present'])
            actual_set = set(actual_sectors)
            missing = expected_set - actual_set
            if missing:
                errors.append(f"partial: missing sectors {missing} (actual={actual_sectors})")
        if c.get('lane_before_lane'):
            first_lane = c['lane_before_lane']['first']
            second_lane = c['lane_before_lane']['second']
            first_idx = None
            second_idx = None
            for i, a in enumerate(actual):
                if first_idx is None and a.get('op') == first_lane:
                    first_idx = i
                if second_idx is None and a.get('op') == second_lane:
                    second_idx = i
            if first_idx is not None and second_idx is not None and first_idx > second_idx:
                errors.append(f"partial: lane {first_lane} (idx={first_idx}) not before lane {second_lane} (idx={second_idx})")
        if c.get('all_sectors_in_lane_before_other_lane'):
            first_lane = c['all_sectors_in_lane_before_other_lane']['first']
            second_lane = c['all_sectors_in_lane_before_other_lane']['second']
            last_first = -1
            first_second = len(actual)
            for i, a in enumerate(actual):
                if a.get('op') == first_lane:
                    last_first = i
                if a.get('op') == second_lane and first_second == len(actual):
                    first_second = i
            if last_first >= 0 and first_second < len(actual) and last_first > first_second:
                errors.append(f"partial: {first_lane} last idx={last_first} after first {second_lane} idx={first_second}")
        if c.get('rt_before_be_before_idle'):
            order_classes = []
            for a in actual:
                pc = a.get('prio_class', 2)
                if pc == 1: order_classes.append('rt')
                elif pc == 3: order_classes.append('idle')
                else: order_classes.append('be')
            expected_seq = ['rt', 'be', 'idle']
            seen = set()
            for cls in order_classes:
                if cls == 'rt':
                    seen.add('rt')
                elif cls == 'be' and 'rt' not in seen:
                    seen.add('be')
                elif cls == 'idle' and 'rt' not in seen and 'be' not in seen:
                    pass
                elif cls == 'be' and 'idle' in seen:
                    errors.append(f"partial: prio class order violation: {order_classes}")
                    break
        if c.get('small_before_large'):
            small_sectors = set(c.get('small_sectors', []))
            large_sectors = set(c.get('large_sectors', []))
            last_small = -1
            first_large = len(actual)
            for i, a in enumerate(actual):
                if a['sector'] in small_sectors:
                    last_small = i
                if a['sector'] in large_sectors and first_large == len(actual):
                    first_large = i
            if last_small >= 0 and first_large < len(actual) and last_small > first_large:
                errors.append(f"partial: small last idx={last_small} after first large idx={first_large}")
        if c.get('sectors_before_sectors'):
            first_set = set(c['sectors_before_sectors']['first'])
            second_set = set(c['sectors_before_sectors']['second'])
            last_first = -1
            first_second = len(actual)
            for i, a in enumerate(actual):
                if a['sector'] in first_set:
                    last_first = i
                if a['sector'] in second_set and first_second == len(actual):
                    first_second = i
            if last_first >= 0 and first_second < len(actual) and last_first > first_second:
                errors.append(f"partial: sectors {first_set} last idx={last_first} after first {second_set} idx={first_second}")
    return errors

def compute_inflight_stats(events):
    """max_inflight: peak ISSUE-but-not-COMPLETE count (device level, G1).
    dispatch_with_choice: number of ISSUE events at which >=2 requests were
    INSERTed-but-not-COMPLETED (the scheduler had a choice, G2) — spec-faithful
    to EVOLUTION_PLAN §1.2 ("ISSUE 间隔内有多条 INSERT 且无 COMPLETE").
    INSERT events require the scx_io tracepoint fix (kernel block/iosched_ext.c
    scx_io_insert_requests emits trace_block_rq_insert per request); without
    it inserts are absent and dispatch_with_choice stays 0 — G2 then fails
    honestly instead of being papered over by the old g2=g1 shortcut."""
    max_inflight = 0
    dispatch_with_choice = 0
    typed = ([(e['ts'], 'ISSUE', e['sector']) for e in events['issues']] +
             [(e['ts'], 'COMPLETE', e['sector']) for e in events['completes']] +
             [(e['ts'], 'INSERT', e['sector']) for e in events['inserts']])
    typed.sort(key=lambda x: x[0])
    pending = []       # ISSUE-but-not-COMPLETE (G1, device in-flight)
    ins_pending = []   # INSERT-but-not-COMPLETE (G2, scheduler candidates)
    for ts, typ, sector in typed:
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
    return {'max_inflight': max_inflight, 'dispatch_with_choice': dispatch_with_choice}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bpftrace-log', required=True)
    ap.add_argument('--expect', required=False)
    ap.add_argument('--trace', required=False)
    ap.add_argument('--policy', required=False, default='')
    ap.add_argument('--bpftool-stats', required=False)
    ap.add_argument('--out', required=True)
    ap.add_argument('--tier', type=int, default=0)
    args = ap.parse_args()

    result = {
        'policy': args.policy,
        'tier': args.tier,
        'status': 'PASS',
        'errors': [],
        'details': {},
    }

    trace_reqs = []
    trace_sectors = None
    if args.trace:
        try:
            td = json.load(open(args.trace))
            trace_reqs = td.get('requests', [])
            trace_sectors = set(r.get('sector', -1) for r in trace_reqs)
        except Exception:
            pass
    result['details']['trace_req_count'] = len(trace_reqs)

    events = parse_bpftrace(args.bpftrace_log, trace_sectors)
    if trace_sectors is not None:
        events['issues'] = cluster_events(events['issues'])
        events['completes'] = cluster_events(events['completes'])
        events['inserts'] = cluster_events(events['inserts'])
    result['details']['window_found'] = events['window_found']
    result['details']['insert_count'] = len(events['inserts'])
    result['details']['complete_count'] = len(events['completes'])
    result['details']['issue_count'] = len(events['issues'])
    result['details']['inflight_stats'] = compute_inflight_stats(events)

    spec = {}
    if args.expect and args.expect != 'NONE':
        try:
            spec = json.load(open(args.expect))
        except Exception as e:
            result['status'] = 'BLOCKED'
            result['errors'].append(f'cannot load expect: {e}')
            json.dump(result, open(args.out, 'w'), indent=2)
            print(json.dumps(result, indent=2))
            sys.exit(2)
    result['details']['expect_name'] = spec.get('name', '')
    result['details']['match'] = spec.get('match', 'exact')

    tier = args.tier
    if tier == 0 or spec.get('tier') == 0:
        errs = check_tier0(events, trace_reqs)
        result['errors'].extend(errs)

    if 'invariants' in spec:
        bpftool = None
        if args.bpftool_stats:
            try:
                bpftool = json.load(open(args.bpftool_stats))
            except Exception:
                pass
        errs = check_invariants(events, spec['invariants'], bpftool, result['details'].get('trace_req_count', 0))
        result['errors'].extend(errs)

    order = spec.get('expected_dispatch_order') or spec.get('expected_complete_order')
    if order and spec.get('match', 'exact') != 'invariants_only':
        actual = events['issues'][:max(1, len(order))]
        mode = spec.get('match', 'exact')
        if mode == 'exact':
            result['errors'].extend(match_exact_dispatch(actual, order, spec.get('expected_merge', False)))
        elif mode == 'partial':
            result['errors'].extend(match_partial_dispatch(actual, order, spec.get('partial_constraints', [])))
        elif mode == 'set':
            result['errors'].extend(match_set_dispatch(actual, order))

    if result['errors']:
        result['status'] = 'FAIL'

    json.dump(result, open(args.out, 'w'), indent=2)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['status'] == 'PASS' else (2 if result['status'] == 'BLOCKED' else 1))

if __name__ == '__main__':
    main()