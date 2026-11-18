#!/usr/bin/env python3
# sentinel_stats.py — 3x10 s QD1 sentinel drift for every run:
#   ratio = median(after IOPS) / median(before IOPS); flag |ratio-1| > 0.20.
# Writes RESULTS/sentinel_drift.csv (plus a per-run table for DATA_SUMMARY).
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ALGOS, ARMS_ALL, CAMPAIGN, REPS, RESULTS, sentinel_iops,
                    run_dir)

# mq_deadline_v2 lives outside campaign/ (results/mq_deadline_v2) — 70/30
# replacement set for the U1-defective 50/50 v1 batch.
MQ_V2_ROOT = os.path.join(os.path.dirname(CAMPAIGN), "mq_deadline_v2")

rows = []
flagged = []

runs = []
for algo in ALGOS:
    arms = ARMS_ALL if algo not in ("d2fq", "linnos") else ["none", "iox"]
    for rep in REPS:
        for arm in arms:
            run = run_dir(algo, rep, arm)
            if not os.path.isdir(run):
                continue
            runs.append((algo, rep, arm, run))
for rep in REPS:
    for arm in ARMS_ALL:
        run = os.path.join(MQ_V2_ROOT, "rep%d" % rep, arm)
        if os.path.isdir(run):
            runs.append(("mq_deadline_v2", rep, arm, run))

for algo, rep, arm, run in runs:
    b = sentinel_iops(run, "before")
    a = sentinel_iops(run, "after")
    miss = [i for i, v in enumerate(b + a) if v is None]
    if miss:
        flagged.append((algo, rep, arm, "missing sentinel json: idx %s" % miss))
    bvals = [v for v in b if v is not None]
    avals = [v for v in a if v is not None]
    ratio = None
    if bvals and avals:
        import numpy as np
        ratio = float(np.median(avals) / np.median(bvals))
    row = {"algo": algo, "rep": rep, "arm": arm,
           "before_1": b[0], "before_2": b[1], "before_3": b[2],
           "after_1": a[0], "after_2": a[1], "after_3": a[2],
           "ratio_med_after_over_before": ratio,
           "flag_gt20pct": bool(ratio is not None and abs(ratio - 1) > 0.20)}
    rows.append(row)
    if row["flag_gt20pct"]:
        flagged.append((algo, rep, arm, "ratio=%.3f" % ratio))

with open(os.path.join(RESULTS, "sentinel_drift.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})

print("runs:", len(rows), "| flagged >20%% drift:", len(flagged))
for algo, rep, arm, msg in flagged:
    print("  FLAG %s rep%s %s: %s" % (algo, rep, arm, msg))
