#!/usr/bin/env python3
# fig7_d2fq.py — cumulative byte-share time series of the two tenants
# (t100 weight 100 vs t400 weight 400), single metric. share_t400(T) =
# cum_bytes_t400 / (cum_bytes_t400 + cum_bytes_t100) from 1 s bw logs.
# Targets: t400 = 0.8, t100 = 0.2 (reference lines). Native arm N/A (virtio VM).
# Note: none arm included as reference.
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, RESULTS, ARM_COLOR, ARM_LABEL, REPS,
                    fio_bytes, find_log, load_json, parse_fio_log, run_dir)

ALGO = "d2fq"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)
TARGET_T400 = 0.8
SRC_ROOT = os.path.dirname(CAMPAIGN)  # .../results (stray_logs live here)

ARMS = ["none", "iox"]  # native N/A in the VM
csv_rows = []
fig, ax = plt.subplots(figsize=(8.4, 5.2))

have_logs = 0
json_share = {arm: [] for arm in ARMS}
for arm in ARMS:
    for rep in REPS:
        run = run_dir(ALGO, rep, arm)
        lcj = load_json(os.path.join(run, "fio_d2fq_lc.json"))   # t100 tenant
        bej = load_json(os.path.join(run, "fio_d2fq_be.json"))   # t400 tenant
        lb, bb = fio_bytes(lcj, "write"), fio_bytes(bej, "write")
        if lb and bb:
            json_share[arm].append((rep, bb / (lb + bb)))
        pl = find_log(run, "lat_t100", "bw")
        pb = find_log(run, "lat_t400", "bw")
        if pl is None or pb is None:
            print("bw log missing: %s rep%d" % (arm, rep))
            continue
        rows_l = parse_fio_log(pl)
        rows_b = parse_fio_log(pb)
        if not rows_l or not rows_b:
            continue
        have_logs += 1
        dl = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_l}
        db = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_b}
        cum_l = cum_b = 0.0
        ser = []
        for s in range(0, 91):
            if s in dl:
                cum_l += dl[s]
            if s in db:
                cum_b += db[s]
            if cum_l + cum_b > 0:
                ser.append((s, cum_b / (cum_l + cum_b)))
                csv_rows.append([arm, rep, s, cum_b * 1048576, cum_l * 1048576,
                                 cum_b / (cum_l + cum_b)])
        if ser:
            ts = [t for t, _ in ser]
            vs = [v for _, v in ser]
            ax.plot(ts, vs, color=ARM_COLOR[arm], alpha=0.45, lw=1.1,
                    label=("_%s per-rep" % arm))

if have_logs:
    # median curves per arm
    grid = np.arange(0, 91, 1.0)
    for arm in ARMS:
        mat = []
        for rep in REPS:
            rows = [r for r in csv_rows if r[0] == arm and r[1] == rep]
            if not rows:
                continue
            tt = np.array([r[2] for r in rows])
            vv = np.array([r[5] for r in rows])
            mat.append(np.interp(grid, tt, vv, left=np.nan, right=np.nan))
        if mat:
            med = np.nanmedian(np.array(mat), axis=0)
            ax.plot(grid, med, color=ARM_COLOR[arm], lw=2.4,
                    label="%s median (n=%d)" % (ARM_LABEL[arm], len(mat)))
else:
    # single surviving pair of window logs (stray_logs = d2fq rep5 iox, the
    # experiment's last run; same-name fio logs overwrote each other per run)
    pl = os.path.join(SRC_ROOT, "stray_logs", "lat_t100_bw.1.log")
    pb = os.path.join(SRC_ROOT, "stray_logs", "lat_t400_bw.1.log")
    rows_l = parse_fio_log(pl)
    rows_b = parse_fio_log(pb)
    if rows_l and rows_b:
        dl = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_l}
        db = {int(t // 1000): v / 1024.0 for (t, v, d, bs, o) in rows_b}
        cum_l = cum_b = 0.0
        ser = []
        for s in range(0, 91):
            if s in dl:
                cum_l += dl[s]
            if s in db:
                cum_b += db[s]
            if cum_l + cum_b > 0:
                ser.append((s, cum_b / (cum_l + cum_b)))
                csv_rows.append(["iox", 5, s, cum_b * 1048576, cum_l * 1048576,
                                 cum_b / (cum_l + cum_b)])
        ax.plot([t for t, _ in ser], [v for _, v in ser], color=ARM_COLOR["iox"],
                lw=1.8, ls="--",
                label="IOX example series (rep5 iox, single surviving log pair)")
        ax.text(0.5, 0.32, "full per-rep 1 s cumulative-share series unavailable: per-run window "
                "logs were overwritten in the fio CWD (matrix D1);\n"
                "only the experiment's last run (rep5 iox) survived -> example series; "
                "endpoint markers use whole-run fio JSON bytes",
                transform=ax.transAxes, ha="center", va="bottom", fontsize=8.5,
                color="firebrick")
        # endpoint markers per rep (whole-run shares)
        rng = np.random.default_rng(5)
        for arm in ARMS:
            vals = json_share[arm]
            if vals:
                xj = 90 + rng.uniform(-1.5, 1.5, len(vals))
                ax.scatter(xj, [v for _, v in vals], s=30, color=ARM_COLOR[arm],
                           alpha=0.8,
                           label="%s per-rep whole-run share (n=%d)" % (ARM_LABEL[arm], len(vals)))
    else:
        ax.text(0.5, 0.5,
                "1 s cumulative-share series unavailable: fio window bw logs "
                "(lat_t100/lat_t400 _bw.1.log) not present.\n"
                "Endpoint markers below use whole-run fio JSON bytes.",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=10, color="firebrick")
        rng = np.random.default_rng(5)
        for arm in ARMS:
            vals = json_share[arm]
            if vals:
                xj = 90 + rng.uniform(-1.5, 1.5, len(vals))
                ax.scatter(xj, [v for _, v in vals], s=30, color=ARM_COLOR[arm],
                           alpha=0.8,
                           label="%s per-rep whole-run share (n=%d)" % (ARM_LABEL[arm], len(vals)))

ax.axhline(TARGET_T400, color="black", ls="--", lw=1.4)
ax.text(90.5, TARGET_T400 + 0.01, "t400 target 0.8", ha="right", fontsize=9)
ax.axhline(0.2, color="black", ls=":", lw=1.2)
ax.text(90.5, 0.21, "t100 target 0.2", ha="right", fontsize=9)
ax.set_xlabel("Time since workload start (s)")
ax.set_ylabel("cumulative byte share of t400 tenant")
ax.set_ylim(0, 1.0)
ax.set_xlim(-1, 92)
ax.set_title("d2fq: cumulative byte share, two saturating 128k seqwrite QD16 tenants, 90 s\n"
             "IOX arb_d2fq weights t100:t400 = 100:400; native arm N/A (virtio VM, no NVMe WRRU)")
ax.grid(alpha=0.3)
ax.legend(frameon=False, fontsize=8, loc="upper left")

# annotate whole-run medians (incl. the 50/50 case if present)
txt = "whole-run share t400 (fio JSON):\n"
for arm in ARMS:
    vals = [s for _, s in json_share[arm]]
    if vals:
        txt += "  %s: median %.3f (n=%d)\n" % (ARM_LABEL[arm], float(np.median(vals)), len(vals))
ax.annotate(txt, xy=(0.995, 0.02), xycoords="axes fraction", ha="right",
            va="bottom", fontsize=8, family="monospace",
            bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))

fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.d2fq.pdf"))
fig.savefig(os.path.join(OUT, "fig.d2fq.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["arm", "rep", "t_s", "cum_t400_bytes", "cum_t100_bytes",
                "share_t400_cum", "share_t400_whole_run"])
    for r in csv_rows:
        w.writerow(r)
    for arm in ARMS:
        for rep, s in json_share[arm]:
            w.writerow([arm, rep, "whole_run", "", "", "", "%.4f" % s])
    w.writerow(["native", "", "", "", "", "", "N/A (virtio VM: ioextd2fq needs NVMe WRRU)"])

print("bw log series count:", have_logs,
      "| JSON share n:", {a: len(v) for a, v in json_share.items()})
print("figure written to", OUT)
