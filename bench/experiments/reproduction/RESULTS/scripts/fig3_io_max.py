#!/usr/bin/env python3
# fig3_io_max.py — BE write bandwidth 1 s time series + 32 MiB/s cap reference.
#   Data: BE-side fio bw log (jobs/iomax.job -> lat_iomax_bw.1.log), 3 arms x 5 reps.
#   io.max arm: BE cgroup wbps=33554432 (32 MiB/s); none = uncapped reference.
#   Workload: single BE 4k randwrite QD32 60 s job in the BE cgroup.
#   Fallback if bw logs absent: per-rep mean bw from fio_iomax.json (write bw).
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, IO_MAX_WBPS, RESULTS, ARM_COLOR, ARM_LABEL, ARMS_ALL,
                    REPS, find_log, load_json, parse_fio_log, run_dir)

ALGO = "io_max"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)
CAP_MIB = IO_MAX_WBPS / 1048576.0  # 32 MiB/s
SRC_ROOT = os.path.dirname(CAMPAIGN)  # .../results (stray_logs live here)

csv_rows = []
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8.4, 6.8), sharex=False,
                               gridspec_kw={"height_ratios": [3, 1.6]})

have_logs = 0
json_means = {arm: [] for arm in ARMS_ALL}
for arm in ARMS_ALL:
    per_rep = []
    for rep in REPS:
        run = run_dir(ALGO, rep, arm)
        j = load_json(os.path.join(run, "fio_iomax.json"))
        if j is not None:
            for jo in j.get("jobs", []):
                if jo.get("write", {}).get("bw"):
                    json_means[arm].append((rep, jo["write"]["bw"] / 1024.0))
        p = find_log(run, "lat_iomax", "bw")
        if p is None:
            print("bw log missing: %s rep%d %s" % (arm, rep, run))
            continue
        rows = parse_fio_log(p)
        if not rows:
            continue
        have_logs += 1
        # randwrite job: single direction (write, dir==1); keep all rows
        ser = [(t / 1000.0, v / 1024.0) for (t, v, d, bs, off) in rows]
        per_rep.append(ser)
        for t, v in ser:
            csv_rows.append([arm, rep, t, v, ""])
    if per_rep:
        grid = np.arange(0, 61, 1.0)
        mat = np.full((len(per_rep), len(grid)), np.nan)
        for i, ser in enumerate(per_rep):
            tt = np.array([t for t, _ in ser])
            vv = np.array([v for _, v in ser])
            mat[i] = np.interp(grid, tt, vv, left=np.nan, right=np.nan)
            ax1.plot(grid, mat[i], color=ARM_COLOR[arm], alpha=0.25, lw=0.8)
        med = np.nanmedian(mat, axis=0)
        ax1.plot(grid, med, color=ARM_COLOR[arm], lw=2.0,
                 label="%s median (n=%d)" % (ARM_LABEL[arm], len(per_rep)))

if have_logs == 0:
    # single surviving window log (stray_logs, io_max rep5 iox — last run of the
    # experiment; same-name fio logs overwrote each other per run in the fio CWD)
    ex_path = os.path.join(SRC_ROOT, "stray_logs", "lat_iomax_bw.1.log")
    ex = parse_fio_log(ex_path)
    if ex:
        ser = [(t / 1000.0, v / 1024.0) for (t, v, d, bs, off) in ex]
        ax1.plot([t for t, _ in ser], [v for _, v in ser], color=ARM_COLOR["iox"],
                 lw=1.6, ls="--",
                 label="IOX example series (rep5 iox, single surviving log)")
        for t, v in ser:
            csv_rows.append(["iox", 5, t, v])
        ax1.text(0.5, 0.06, "full 3-arm x 5-rep 1 s series unavailable: per-run window "
                 "logs were overwritten in the fio CWD (matrix D1);\n"
                 "only the experiment's last run (rep5 iox) survived -> shown as example series",
                 transform=ax1.transAxes, ha="center", va="bottom", fontsize=8.5,
                 color="firebrick")
    else:
        ax1.text(0.5, 0.5, "1 s bw series unavailable: fio window logs (lat_iomax_bw.1.log) "
                 "not present — see lower panel for per-rep means from fio JSON",
                 transform=ax1.transAxes, ha="center", va="center", fontsize=10, color="firebrick")

ax1.axhline(CAP_MIB, color="black", ls="--", lw=1.4)
ax1.text(59.5, CAP_MIB + 0.8, "io.max cap = 32 MiB/s", ha="right", fontsize=9)
ax1.set_ylabel("BE write bandwidth (MiB/s)")
ax1.set_title("io_max: BE 4k randwrite QD32 bandwidth, 60 s\n"
              "native io.max wbps=32MiB/s vs IOX q_iomax vs none (uncapped reference)")
ax1.set_xlim(-1, 61)
ax1.grid(alpha=0.3)
ax1.legend(frameon=False, fontsize=9, loc="center right")

# ---- fallback panel: per-rep mean bw from fio JSON ---------------------------
x = np.arange(len(ARMS_ALL))
bw_map = {arm: [] for arm in ARMS_ALL}
for arm in ARMS_ALL:
    for rep, v in json_means[arm]:
        bw_map[arm].append(v)
meds = [float(np.median(bw_map[a])) if bw_map[a] else np.nan for a in ARMS_ALL]
ax2.bar(x, meds, 0.5, color=[ARM_COLOR[a] for a in ARMS_ALL], alpha=0.55,
        edgecolor="black", linewidth=0.6)
rng = np.random.default_rng(3)
for i, a in enumerate(ARMS_ALL):
    if bw_map[a]:
        xj = x[i] + rng.uniform(-0.12, 0.12, len(bw_map[a]))
        ax2.scatter(xj, bw_map[a], s=24, color="black", alpha=0.8, zorder=3)
ax2.axhline(CAP_MIB, color="black", ls="--", lw=1.4)
ax2.set_xticks(x)
ax2.set_xticklabels([ARM_LABEL[a] for a in ARMS_ALL])
ax2.set_ylabel("mean bw (MiB/s)\n(fio JSON, whole 60 s)")
ax2.grid(axis="y", alpha=0.3)
for i, a in enumerate(ARMS_ALL):
    if bw_map[a]:
        ax2.annotate("median %.2f" % meds[i], (x[i], meds[i]),
                     xytext=(0, -14), textcoords="offset points",
                     ha="center", fontsize=8)

fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.io_max.pdf"))
fig.savefig(os.path.join(OUT, "fig.io_max.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["arm", "rep", "t_s", "bw_mib_s"])
    for r in csv_rows:
        w.writerow(r)
    for arm in ARMS_ALL:
        for rep, v in json_means[arm]:
            w.writerow([arm, rep, "mean_whole_run", "%.3f" % v])

print("bw log series count:", have_logs, "| JSON means:", {a: len(v) for a, v in json_means.items()})
print("figure written to", OUT)
