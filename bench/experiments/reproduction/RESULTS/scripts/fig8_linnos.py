#!/usr/bin/env python3
# fig8_linnos.py — LinnOS BE per-band target vs actual interception rate.
# Data: loader_q_be.stdout (pol=q_linnos) LAST cumulative line per iox rep:
#   band[i] = cumulative inserts into band i, ir{i}[1] = BE intercepted in band i
#   (intercepted[band][class], class 1 = BE).
# Targets (q_be CLI): band0 0%, band1 40%, band2 60%.
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import RESULTS, REPS, load_jsonl, run_dir

ALGO = "linnos"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)

TARGET = [0.0, 40.0, 60.0]  # BE class per-band target interception %

rows = []
for rep in REPS:
    recs = load_jsonl(os.path.join(run_dir(ALGO, rep, "iox"), "loader_q_be.stdout"))
    if not recs:
        rows.append({"rep": rep, "ok": False})
        continue
    last = [r for r in recs if r.get("pol") == "q_linnos"][-1]
    ins = last.get("ins", 0)
    band = last.get("band", [0, 0, 0])
    ir_be = [last.get("ir0", [0, 0])[1], last.get("ir1", [0, 0])[1],
             last.get("ir2", [0, 0])[1]]
    row = {"rep": rep, "ok": True, "ins": ins, "completions": last.get("completions"),
           "inflight": last.get("inflight")}
    for b in range(3):
        row["band%d_ins" % b] = band[b]
        row["band%d_be_intercepted" % b] = ir_be[b]
        row["band%d_actual_pct" % b] = (100.0 * ir_be[b] / band[b]) if band[b] else None
    rows.append(row)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["arm", "rep", "ins_total", "band", "band_inserts",
                "be_intercepted", "actual_pct", "target_pct",
                "completions", "inflight"])
    for r in rows:
        if not r.get("ok"):
            w.writerow(["iox", r["rep"], "", "", "", "", "", "", "", ""])
            continue
        for b in range(3):
            w.writerow(["iox", r["rep"], r["ins"], b, r["band%d_ins" % b],
                        r["band%d_be_intercepted" % b],
                        "" if r["band%d_actual_pct" % b] is None
                        else "%.3f" % r["band%d_actual_pct" % b],
                        TARGET[b], r["completions"], r["inflight"]])
    # none arm has no interception mechanism: record an explicit marker row
    for rep in REPS:
        w.writerow(["none", rep, "", "", "", "", "", "", "", "no-interception-mechanism"])

fig, ax = plt.subplots(figsize=(7.2, 4.8))
x = np.arange(3)
bw = 0.5
med = []
for b in range(3):
    vals = [r["band%d_actual_pct" % b] for r in rows if r.get("ok")
            and r["band%d_actual_pct" % b] is not None]
    med.append(float(np.median(vals)) if vals else np.nan)
ax.bar(x, med, bw * 0.55, color="#d62728", alpha=0.55, edgecolor="black",
       linewidth=0.6, label="IOX actual (median of 5 reps)", zorder=2)

rng = np.random.default_rng(11)
for b in range(3):
    vals = [r["band%d_actual_pct" % b] for r in rows if r.get("ok")
            and r["band%d_actual_pct" % b] is not None]
    if vals:
        xj = x[b] + rng.uniform(-0.12, 0.12, len(vals))
        ax.scatter(xj, vals, s=26, color="black", alpha=0.8, zorder=3,
                   label="per-rep actual" if b == 0 else None)

# target markers (horizontal short lines)
for b in range(3):
    ax.plot([x[b] - 0.32, x[b] + 0.32], [TARGET[b], TARGET[b]],
            color="#1f77b4", lw=2.4, ls="--", zorder=4,
            label="BE target" if b == 0 else None)

ins_med = [float(np.median([r["band%d_ins" % b] for r in rows if r.get("ok")]))
           for b in range(3)]
tot = float(np.median([r["ins"] for r in rows if r.get("ok")]))
for b in range(3):
    share = 100.0 * ins_med[b] / tot if tot else 0.0
    ax.annotate("median inserts: %s\n(%.4f%% of all)" %
                ("{:,.0f}".format(ins_med[b]), share),
                (x[b], 4.0), ha="center", va="bottom", fontsize=7.5, alpha=0.85)

ax.set_xticks(x)
ax.set_xticklabels(["band0 (<50% busy)\ntarget 0%", "band1 (50-80%)\ntarget 40%",
                    "band2 (>80%)\ntarget 60%"])
ax.set_ylabel("BE interception rate (%)")
ax.set_ylim(0, 105)
ax.set_title("linnos: BE target vs actual interception rate by band\n"
             "(5 IOX reps, cumulative loader counters)\n"
             "none arm: no interception mechanism (annotated, no data)")
ax.grid(axis="y", alpha=0.3, zorder=0)
ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0.01, 0.88))
fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.linnos.pdf"))
fig.savefig(os.path.join(OUT, "fig.linnos.png"), dpi=160)
plt.close(fig)

for r in rows:
    if r.get("ok"):
        print("rep%d ins=%d band=%s actual=%s" %
              (r["rep"], r["ins"], r["band_ins"] if "band_ins" in r else
               [r["band0_ins"], r["band1_ins"], r["band2_ins"]],
               [r["band0_actual_pct"], r["band1_actual_pct"], r["band2_actual_pct"]]))
print("figure written to", OUT)
