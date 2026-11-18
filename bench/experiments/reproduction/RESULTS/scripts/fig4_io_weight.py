#!/usr/bin/env python3
# fig4_io_weight.py — BE byte-share time series (share = be_bw/(be_bw+lc_bw),
# 1 s bw logs) + BE target 0.8 line. Corner annotation: whole-run share median
# per arm from fio JSON bytes (always available fallback).
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, RESULTS, ARM_COLOR, ARM_LABEL, ARMS_ALL, REPS,
                    fio_bytes, find_log, load_json, parse_fio_log, run_dir)

ALGO = "io_weight"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)
TARGET_BE = 0.8  # weights LC:BE = 100:400
SRC_ROOT = os.path.dirname(CAMPAIGN)  # .../results (stray_logs live here)

csv_rows = []
json_share = {arm: [] for arm in ARMS_ALL}
fig, ax = plt.subplots(figsize=(8.4, 5.0))

have_logs = 0
for arm in ARMS_ALL:
    per_rep = []
    for rep in REPS:
        run = run_dir(ALGO, rep, arm)
        lcj = load_json(os.path.join(run, "fio_w_lc.json"))
        bej = load_json(os.path.join(run, "fio_w_be.json"))
        lb, bb = fio_bytes(lcj, "write"), fio_bytes(bej, "write")
        if lb and bb:
            json_share[arm].append((rep, bb / (lb + bb)))
        pl = find_log(run, "lat_w_lc", "bw")
        pb = find_log(run, "lat_w_be", "bw")
        if pl is None or pb is None:
            print("bw log missing: %s rep%d (lc:%s be:%s)" %
                  (arm, rep, pl is not None, pb is not None))
            continue
        rows_l = parse_fio_log(pl)
        rows_b = parse_fio_log(pb)
        if not rows_l or not rows_b:
            continue
        have_logs += 1
        dl = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_l}
        db = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_b}
        ser = []
        for s in range(0, 91):
            if s in dl and s in db and (dl[s] + db[s]) > 0:
                ser.append((s, db[s] / (dl[s] + db[s])))
                csv_rows.append([arm, rep, s, db[s], dl[s], db[s] / (dl[s] + db[s]), ""])
        per_rep.append(ser)
        # raw bw series also into CSV
        for (t, v, d, bs, o) in rows_b:
            pass  # share rows already recorded above
    if per_rep:
        grid = np.arange(0, 91, 1.0)
        mat = np.full((len(per_rep), len(grid)), np.nan)
        for i, ser in enumerate(per_rep):
            tt = np.array([t for t, _ in ser])
            vv = np.array([v for _, v in ser])
            mat[i] = np.interp(grid, tt, vv, left=np.nan, right=np.nan)
            ax.plot(grid, mat[i], color=ARM_COLOR[arm], alpha=0.25, lw=0.8)
        med = np.nanmedian(mat, axis=0)
        ax.plot(grid, med, color=ARM_COLOR[arm], lw=2.2,
                label="%s median (n=%d)" % (ARM_LABEL[arm], len(per_rep)))

if have_logs == 0:
    # single surviving pair of window logs (stray_logs = io_weight rep5 iox, the
    # experiment's last run; same-name fio logs overwrote each other per run)
    pl = os.path.join(SRC_ROOT, "stray_logs", "lat_w_lc_bw.1.log")
    pb = os.path.join(SRC_ROOT, "stray_logs", "lat_w_be_bw.1.log")
    rows_l = parse_fio_log(pl)
    rows_b = parse_fio_log(pb)
    if rows_l and rows_b:
        dl = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_l}
        db = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_b}
        ser = [(s, db[s] / (dl[s] + db[s])) for s in sorted(set(dl) & set(db))
               if (dl[s] + db[s]) > 0]
        ax.plot([t for t, _ in ser], [v for _, v in ser], color=ARM_COLOR["iox"],
                lw=1.6, ls="--",
                label="IOX example series (rep5 iox, single surviving log pair)")
        for t, v in ser:
            csv_rows.append(["iox", 5, t, db[t], dl[t], v, ""])
        ax.text(0.5, 0.30, "full 3-arm x 5-rep 1 s share series unavailable: per-run window "
                "logs were overwritten in the fio CWD (matrix D1);\n"
                "only the experiment's last run (rep5 iox) survived -> example series; "
                "corner stats use whole-run fio JSON bytes",
                transform=ax.transAxes, ha="center", va="bottom", fontsize=8.5,
                color="firebrick")
    else:
        ax.text(0.5, 0.5, "1 s share series unavailable: fio window bw logs "
                "(lat_w_{lc,be}_bw.1.log) not present — corner stats use whole-run fio JSON bytes",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="firebrick")

ax.axhline(TARGET_BE, color="black", ls="--", lw=1.4)
ax.text(90.5, TARGET_BE + 0.012, "BE target share = 0.8 (weights LC:BE = 100:400)",
        ha="right", fontsize=9)
ax.set_xlabel("Time since workload start (s)")
ax.set_ylabel("BE byte share  be/(be+lc)")
ax.set_ylim(0, 1.0)
ax.set_xlim(-1, 91)
ax.set_title("io_weight: two saturating 128k seqwrite QD16 tenants, 90 s\n"
             "native = BFQ io.bfq.weight 100:400; IOX = arb_wdrr weights 100:400; none = uncontrolled")
ax.grid(alpha=0.3)
ax.legend(frameon=False, fontsize=9, loc="lower left")

# corner annotation: whole-run share median per arm (from JSON bytes)
txt = "whole-run BE share (fio JSON):\n"
for arm in ARMS_ALL:
    vals = [s for _, s in json_share[arm]]
    if vals:
        txt += "  %s: median %.3f (n=%d)\n" % (ARM_LABEL[arm], float(np.median(vals)), len(vals))
ax.annotate(txt, xy=(0.995, 0.02), xycoords="axes fraction", ha="right",
            va="bottom", fontsize=8, family="monospace",
            bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))

fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.io_weight.pdf"))
fig.savefig(os.path.join(OUT, "fig.io_weight.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["arm", "rep", "t_s", "be_bw_mib_s", "lc_bw_mib_s",
                "be_share_1s", "be_share_whole_run"])
    for r in csv_rows:
        w.writerow(r)
    for arm in ARMS_ALL:
        for rep, s in json_share[arm]:
            w.writerow([arm, rep, "whole_run", "", "", "", "%.4f" % s])

print("bw log series count:", have_logs,
      "| JSON share n:", {a: len(v) for a, v in json_share.items()})
print("figure written to", OUT)
