#!/usr/bin/env python3
"""q_sp_edf own G2/G3/G4 gates.

The shared harness cannot provide these today:
  - G2: compare_expect.py compute_inflight_stats never increments
    dispatch_with_choice (always 0) and check_inflight.py papers over it with
    g2=g1, while run_deterministic.sh lines 179-185 read the field and would
    always fail.
  - INSERT tracepoints never fire for scx_io-managed requests: the elevator
    insert path (kernel/linux-7.3.1/block/blk-mq.c:2957-2960
    elevator->ops.insert_requests) contains no trace_block_rq_insert call, so
    insert_count is structurally 0 and G4 cannot be computed from INSERTs.

G2 (EVOLUTION_PLAN 1.2: "at least one dispatch with >=2 INSERTs pending but
not completed at that ts") is therefore computed from ISSUE/COMPLETE events:
an ISSUE proves its request was inserted, so at an ISSUE ts t,
pending = (# ISSUE with ts<=t) - (# COMPLETE with ts<=t) counts requests that
were inserted and not yet completed at t. G2 passes when some ISSUE observes
pending >= 2. G3 (window purity) passes when every window ISSUE (sector, nr)
matches a trace request exactly and the window contains no foreign events at
trace sectors. G4 passes when the window ISSUE count equals the trace request
count (q_sp_edf traces are non-adjacent by design, so any merge would show up
as an (sector, nr) mismatch and fail G3 first).

Log parsing mirrors compare_expect.py parse_bpftrace + cluster_events so the
gates see exactly the same window the comparison asserted.
"""
import argparse
import json
import re
import sys

INSERT_RE = re.compile(r'INSERT ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+) ioprio=(\d+)')
ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+) ioprio=(\d+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
GAP_NS = 100 * 1_000_000


def cluster_events(events, gap_ms=100):
    if not events:
        return events
    cluster = []
    prev_ts = events[0]['ts']
    for e in events:
        if e['ts'] - prev_ts > gap_ms * 1_000_000 and cluster:
            break
        cluster.append(e)
        prev_ts = e['ts']
    return cluster


def parse(path, trace_sectors):
    inserts, completes, issues = [], [], []
    with open(path) as f:
        all_lines = f.readlines()
    first_trace_issue_ts = None
    for line in all_lines:
        m = ISSUE_RE.search(line)
        if m and int(m.group(2)) in trace_sectors:
            if m.group(4) in ('N', 'FF', 'F'):
                continue
            first_trace_issue_ts = int(m.group(1))
            break
    for line in all_lines:
        m = INSERT_RE.search(line)
        if m:
            ts, sector = int(m.group(1)), int(m.group(2))
            if sector not in trace_sectors:
                continue
            if first_trace_issue_ts is not None and ts + GAP_NS < first_trace_issue_ts:
                continue
            inserts.append({'ts': ts, 'sector': sector, 'nr': int(m.group(3))})
            continue
        m = ISSUE_RE.search(line)
        if m:
            ts, sector = int(m.group(1)), int(m.group(2))
            if sector not in trace_sectors:
                continue
            if first_trace_issue_ts is not None and ts + GAP_NS < first_trace_issue_ts:
                continue
            issues.append({'ts': ts, 'sector': sector, 'nr': int(m.group(3)),
                           'rwbs': m.group(4)})
            continue
        m = COMPLETE_RE.search(line)
        if m:
            ts, sector = int(m.group(1)), int(m.group(2))
            if sector not in trace_sectors:
                continue
            if first_trace_issue_ts is not None and ts + GAP_NS < first_trace_issue_ts:
                continue
            completes.append({'ts': ts, 'sector': sector, 'nr': int(m.group(3))})
    return inserts, completes, issues, first_trace_issue_ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bpftrace-log', required=True)
    ap.add_argument('--trace', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    td = json.load(open(args.trace))
    trace_reqs = td.get('requests', [])
    trace_sectors = set(r['sector'] for r in trace_reqs)
    expected_pairs = set((r['sector'], r['nr_sectors']) for r in trace_reqs)
    n_reqs = len(trace_reqs)

    inserts, completes, issues, first_issue_ts = parse(args.bpftrace_log,
                                                       trace_sectors)
    window_issues = cluster_events(issues)
    window_completes = cluster_events(completes)
    window_inserts = cluster_events(inserts)

    # G2: >=1 ISSUE ts with >=2 requests inserted-but-not-completed
    g2_pass = False
    g2_evidence = None
    max_inflight = 0
    pending = []
    typed = sorted([(e['ts'], 'I', e['sector']) for e in issues] +
                   [(e['ts'], 'C', e['sector']) for e in completes],
                   key=lambda x: x[0])
    for ts, typ, sector in typed:
        if typ == 'I':
            pending.append(sector)
            if len(pending) >= 2 and not g2_pass:
                g2_pass = True
                g2_evidence = {'ts': ts, 'pending_not_completed': len(pending),
                               'pending_sectors': list(pending)}
            max_inflight = max(max_inflight, len(pending))
        else:
            if sector in pending:
                pending.remove(sector)
            elif pending:
                pending.pop(0)

    # G3: window purity — every window ISSUE matches a trace (sector, nr)
    g3_errors = []
    foreign = [e for e in window_issues
               if (e['sector'], e['nr']) not in expected_pairs]
    if foreign:
        g3_errors.append(f"foreign ISSUE events at trace sectors: "
                         f"{[(e['sector'], e['nr'], e['rwbs']) for e in foreign]}")
    if len(window_issues) != n_reqs:
        g3_errors.append(f"window issue_count={len(window_issues)} != "
                         f"trace requests={n_reqs}")
    if len(window_completes) < n_reqs:
        g3_errors.append(f"window complete_count={len(window_completes)} < "
                         f"trace requests={n_reqs}")
    g3_pass = not g3_errors

    # G4: window dispatch count == trace request count (inserts recorded;
    # merges are not intentional for q_sp_edf — any merge would break G3
    # (sector,nr) purity first)
    g4_pass = len(window_issues) == n_reqs
    g4_detail = {
        'window_issue_count': len(window_issues),
        'trace_request_count': n_reqs,
        'window_insert_count': len(window_inserts),
        'insert_note': ('G4 measured from ISSUE counts (non-adjacent traces: '
                        'any merge breaks G3 (sector,nr) purity first). '
                        'window_insert_count is informational; INSERT events '
                        'require the scx_io tracepoint fix (kernel '
                        'block/iosched_ext.c scx_io_insert_requests, Phase-2)'),
    }

    result = {
        'g2_pass': g2_pass,
        'g2_evidence': g2_evidence,
        'g3_pass': g3_pass,
        'g3_errors': g3_errors,
        'g4_pass': g4_pass,
        'g4_detail': g4_detail,
        'max_inflight_issue_complete': max_inflight,
        'window_issue_count': len(window_issues),
        'window_complete_count': len(window_completes),
        'window_insert_count': len(window_inserts),
        'first_trace_issue_ts': first_issue_ts,
    }
    json.dump(result, open(args.out, 'w'), indent=2)
    print(json.dumps(result, indent=2))
    sys.exit(0 if (g2_pass and g3_pass and g4_pass) else 1)


if __name__ == '__main__':
    main()