#!/usr/bin/env python3
# fig1_mq_deadline.py — Read P99 + Write P99, 3 arms x 5 reps, grouped bars
# (median) with per-rep scatter. Data: mq_deadline_v2 fio_mq.json
# (frozen plan workload: 4k randrw 70/30, rwmixread=70, QD32, 60 s).
# NOTE: the original campaign/mq_deadline (v1) ran 50/50 — rwmixread=70 was
# lost from the job line (config defect U1, matrix adoption record); v2
# replaces it as the formal metric. v1 raw data kept in place, not plotted.
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, RESULTS, ARM_COLOR, ARM_LABEL, REPS, ARMS_ALL,
                    fio_p99_ns, load_json, run_dir)

ALGO = "mq_deadline"
V2_ROOT = os.path.join(os.path.dirname(CAMPAIGN), "mq_deadline_v2")  # results/mq_deadline_v2
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)


def v2_dir(rep, arm):
    return os.path.join(V2_ROOT, "rep%d" % rep, arm)


rows = []
for rep in REPS:
    for arm in ARMS_ALL:
        j = load_json(os.path.join(v2_dir(rep, arm), "fio_mq.json"))
        for direction in ("read", "write"):
            p99 = fio_p99_ns(j, direction)
            rows.append({"direction": direction, "arm": arm, "rep": rep,
                         "p99_us": (p99 / 1000.0) if p99 is not None else None})

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["algo", "arm", "rep", "direction", "p99_us"])
    for r in rows:
        w.writerow([ALGO + "_v2", r["arm"], r["rep"], r["direction"],
                    "" if r["p99_us"] is None else "%.1f" % r["p99_us"]])

fig, ax = plt.subplots(figsize=(7.2, 4.6))
dirs = ["read", "write"]
x = np.arange(len(dirs))
bw = 0.24
offsets = {"none": -bw, "native": 0.0, "iox": bw}
rng = np.random.default_rng(7)

for arm in ARMS_ALL:
    med = []
    for direction in dirs:
        vals = [r["p99_us"] for r in rows
                if r["arm"] == arm and r["direction"] == direction
                and r["p99_us"] is not None]
        med.append(float(np.median(vals)) if vals else np.nan)
    ax.bar(x + offsets[arm], med, bw * 0.92, color=ARM_COLOR[arm],
           alpha=0.55, edgecolor="black", linewidth=0.6,
           label=ARM_LABEL[arm], zorder=2)
    for i, direction in enumerate(dirs):
        vals = [r["p99_us"] for r in rows
                if r["arm"] == arm and r["direction"] == direction
                and r["p99_us"] is not None]
        if vals:
            xj = x[i] + offsets[arm] + rng.uniform(-0.045, 0.045, len(vals))
            ax.scatter(xj, vals, s=22, color="black", alpha=0.75, zorder=3)

ax.set_yscale("log")
ax.set_xticks(x)
ax.set_xticklabels(["Read P99", "Write P99"])
ax.set_ylabel("P99 completion latency (us, log scale)")
ax.set_title("mq_deadline: P99 latency by arm (5 reps, dots = per-rep)\n"
             "frozen plan workload: 4k randrw 70/30 (rwmixread=70), QD32, 60 s")
ax.grid(axis="y", which="both", alpha=0.3, zorder=0)
ax.legend(frameon=False, loc="upper left")
fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.mq_deadline.pdf"))
fig.savefig(os.path.join(OUT, "fig.mq_deadline.png"), dpi=160)
plt.close(fig)

for direction in dirs:
    for arm in ARMS_ALL:
        vals = sorted(r["p99_us"] for r in rows
                      if r["arm"] == arm and r["direction"] == direction
                      and r["p99_us"] is not None)
        print(direction, arm, ["%.0f" % v for v in vals],
              "median=%.0f us" % float(np.median(vals)) if vals else "EMPTY")
print("figure written to", OUT)
