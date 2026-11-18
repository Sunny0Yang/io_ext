#!/usr/bin/env python3
"""q_kyber_gates.py — G2/G3/G4 computation for the q_kyber acceptance runner.

Shared-layer context (see tests/io/q_kyber_accept.sh diagnostics):
  - The shared bpftrace script (trace_dispatch.bt v2) probes the
    tracepoint block:block_rq_insert for INSERT.  In this kernel that
    tracepoint NEVER fires for scx_io-managed requests (the scx_io insert
    path bypasses blk_mq_sched_insert_request's tracepoint), so the
    literal G2 ("ISSUE while >=2 INSERTs pending") and G4 ("window
    insert_count == trace request count") cannot be computed from INSERT
    events.  This helper computes the honest ISSUE/COMPLETE-based
    equivalents plus the raw (structurally 0) INSERT tracepoint count as
    diagnostic evidence:
      G2 literal: some ISSUE ts has >=2 requests issued-and-not-completed
                  (the task definition: >=2 INSERTs pending-not-completed).
      G2 strict:  some ISSUE ts has >=2 OTHER requests outstanding plus
                  the issued one (>=3 outstanding total) — proves >=2 other
                  requests coexisted at dispatch time.
      G3 purity:  fraction of in-window ISSUE events that belong to the
                  trace sectors; background share must stay <10%.
      G4 sub:     every trace request accounted for exactly once among
                  in-window trace-sector ISSUEs (issue count == trace
                  request count for non-merge cases; full sector coverage
                  for merge cases).

Usage: q_kyber_gates.py --bpftrace-log LOG --trace TRACE --out OUT.json
Exit: 0 when all requested gates pass, 1 otherwise.
"""
import argparse
import json
import re
import sys

ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
INSERT_RE = re.compile(r'INSERT ts=(\d+)')
PLAY_START_RE = re.compile(r'TRACE_PLAY_START ts=(\d+)')
PLAY_END_RE = re.compile(r'TRACE_PLAY_END ts=(\d+)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bpftrace-log', required=True)
    ap.add_argument('--trace', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--merge-case', action='store_true',
                    help='G4 allows issue_count < trace_reqs (plug merges)')
    args = ap.parse_args()

    trace = json.load(open(args.trace))
    reqs = trace.get('requests', [])
    trace_sectors = set(r.get('sector', -1) for r in reqs)
    trace_reqs = len(reqs)
    expected_coverage = sum(r.get('nr_sectors', 8) for r in reqs)

    window_start = None
    window_end = None
    insert_tp_count = 0
    issues = []
    completes = []
    with open(args.bpftrace_log) as f:
        for line in f:
            m = PLAY_START_RE.search(line)
            if m:
                window_start = int(m.group(1))
                continue
            m = PLAY_END_RE.search(line)
            if m:
                window_end = int(m.group(1))
                continue
            if INSERT_RE.search(line):
                insert_tp_count += 1
                continue
            m = ISSUE_RE.search(line)
            if m:
                issues.append({'ts': int(m.group(1)), 'sector': int(m.group(2)),
                               'nr': int(m.group(3)), 'rwbs': m.group(4)})
                continue
            m = COMPLETE_RE.search(line)
            if m:
                completes.append({'ts': int(m.group(1)), 'sector': int(m.group(2)),
                                  'nr': int(m.group(3)), 'rwbs': m.group(4)})

    window_found = window_start is not None and window_end is not None
    in_window = (lambda e: window_found and window_start <= e['ts'] <= window_end) \
        if window_found else (lambda e: False)

    trace_issues = [e for e in issues if e['sector'] in trace_sectors]
    trace_issues_in_win = [e for e in trace_issues if in_window(e)]
    issues_in_win = [e for e in issues if in_window(e)]
    background_issues_in_win = [e for e in issues_in_win
                                if e['sector'] not in trace_sectors]
    purity = (len(trace_issues_in_win) / len(issues_in_win)) if issues_in_win else 0.0

    coverage = sum(e['nr'] for e in trace_issues_in_win)
    covered = set()
    for e in trace_issues_in_win:
        for s in range(e['sector'], e['sector'] + e['nr']):
            if s in trace_sectors:
                covered.add(s)

    max_outstanding = 0
    trace_completes = [e for e in completes if e['sector'] in trace_sectors]
    for e in trace_issues:
        outstanding = sum(1 for i in trace_issues if i['ts'] <= e['ts']) - \
                      sum(1 for c in trace_completes if c['ts'] <= e['ts'])
        max_outstanding = max(max_outstanding, outstanding)

    g2_literal = max_outstanding >= 2
    g2_strict = max_outstanding >= 3
    g3_purity_pass = window_found and purity >= 0.9 and len(covered) == len(trace_sectors)
    if args.merge_case:
        g4_pass = (coverage >= expected_coverage and
                   len(trace_issues_in_win) <= trace_reqs and window_found)
    else:
        g4_pass = (len(trace_issues_in_win) == trace_reqs and
                   coverage == expected_coverage and window_found)

    result = {
        'window_found': window_found,
        'play_window': [window_start, window_end],
        'insert_tp_count': insert_tp_count,
        'trace_req_count': trace_reqs,
        'expected_coverage_sectors': expected_coverage,
        'issues_total': len(issues),
        'trace_sector_issues_total': len(trace_issues),
        'trace_sector_issues_in_window': len(trace_issues_in_win),
        'background_issues_in_window': len(background_issues_in_win),
        'purity': round(purity, 4),
        'issue_coverage_nr': coverage,
        'trace_sectors_covered': len(covered),
        'trace_sectors_expected': len(trace_sectors),
        'max_outstanding_at_issue': max_outstanding,
        'G2_literal_pass': g2_literal,
        'G2_strict_pass': g2_strict,
        'G3_purity_pass': g3_purity_pass,
        'G4_issue_accounting_pass': g4_pass,
        'all_pass': (g2_strict and g3_purity_pass and g4_pass) if not args.merge_case
                    else (g2_literal and g3_purity_pass and g4_pass),
    }
    json.dump(result, open(args.out, 'w'), indent=2)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['all_pass'] else 1)


if __name__ == '__main__':
    main()