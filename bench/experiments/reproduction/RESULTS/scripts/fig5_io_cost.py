#!/usr/bin/env python3
# fig5_io_cost.py — vrate time series (single metric), IOX vs native.
#   IOX:    loader_arb_iocost.stdout vrate (= vrate_fp/256, FSM multiplier 0.25..4.0)
#           t=0 aligned at first sample with win_total>0 (= start of fio p1 traffic;
#           loader attaches ~32 s earlier during sentinels).
#   native: campaign/io_cost_vrate_native/rep{1..5}/native/vrate.log
#           ("epoch_ts vrate=<float|NA> usage=...", io.stat cost.vrate absolute rate)
#           t=0 aligned at fio_p1_lc.json timestamp.
# Both normalized to their own first non-NA value -> relative units on one axis.
# NOTE (semantics differ): IOX vrate is a multiplicative FSM knob (0.25x-4.0x);
# native vrate is an absolute rate (cost/s). Only response direction and the
# QD step phases (t=30s LC QD32->64, t=60s recovery) are comparable.
import csv
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, RESULTS, ARM_COLOR, ARM_LABEL, REPS,
                    load_jsonl, load_json, read_text_maybe_gz, run_dir)

ALGO = "io_cost"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)

SRC_ROOT = os.path.dirname(CAMPAIGN)  # .../results (companion dirs live here)
VRATE_LOG = os.path.join(SRC_ROOT, "io_cost_vrate_native", "rep%d", "native", "vrate.log")
VRATE_RE = re.compile(r"^\s*([\d.]+)\s+vrate=(NA|[-\d.]+)")


def iox_series(rep):
    """[(t_s, vrate)] relative to first active sample; pre-onset rows dropped."""
    recs = load_jsonl(os.path.join(run_dir(ALGO, rep, "iox"),
                                   "loader_arb_iocost.stdout"))
    if not recs:
        return None, None
    recs = [r for r in recs if r.get("pol") == "arb_iocost"]
    onset = None
    for r in recs:
        if (r.get("win_total") or 0) > 0 and (r.get("vrate_fp") or 0) > 0:
            onset = r["t_ms"]
            break
    if onset is None:
        return None, None
    ser = []
    for r in recs:
        fp = r.get("vrate_fp") or 0
        if fp <= 0 or r["t_ms"] < onset:
            continue
        ser.append((round((r["t_ms"] - onset) / 1000.0, 3), fp / 256.0))
    return ser, onset


def native_series(rep):
    """[(t_s, vrate)] relative to the companion run's p1 start; None if trace
    missing. Trace format: 'epoch_ts vrate=<float|NA> usage=<int>' (1 s, native
    blk-iocost io.stat cost.vrate, absolute cost/s). NA rows are skipped (the
    line breaks there). Alignment: fio JSON timestamp = job END -> p1 start =
    ts - 30."""
    path = VRATE_LOG % rep
    txt = read_text_maybe_gz(path)  # handles .log and .log.gz
    if txt is None:
        txt = read_text_maybe_gz(path + ".gz")
    if txt is None:
        return None
    run = os.path.join(SRC_ROOT, "io_cost_vrate_native", "rep%d", "native") % rep
    st = load_json(os.path.join(run, "RESULT.json"))
    if not st or st.get("status") != "OK":
        return None  # incomplete companion run (rep5: no RESULT.json, p3 JSONs empty)
    j = load_json(os.path.join(run, "fio_p1_lc.json"))
    if j is None or not j.get("timestamp"):
        return None
    t1 = j["timestamp"] - 30
    ser = []
    for line in txt.splitlines():
        m = VRATE_RE.match(line)
        if not m:
            continue
        ts = float(m.group(1))
        v = m.group(2)
        if v == "NA":
            continue
        ser.append((ts, float(v)))
    if not ser:
        return None
    rel = [(round(ts - t1, 3), v) for ts, v in ser if -5 <= ts - t1 <= 125]
    return rel, t1


def normalize(ser):
    if not ser:
        return None
    base = ser[0][1]
    if not base:
        return None
    return [(t, v / base) for t, v in ser]


csv_rows = []
fig, ax = plt.subplots(figsize=(8.2, 4.8))

avail = {"iox": 0, "native": 0}
for arm in ("native", "iox"):
    all_norm = []
    for rep in REPS:
        res = native_series(rep) if arm == "native" else iox_series(rep)
        if not res:
            print("%s rep%d: no series" % (arm, rep))
            continue
        ser, onset = res
        norm = normalize(ser)
        if not norm:
            print("%s rep%d: base value zero" % (arm, rep))
            continue
        all_norm.append(norm)
        for t, v in ser:
            csv_rows.append([arm, rep, t, v, ""])
        for t, v in norm:
            csv_rows.append([arm, rep, t, "", v])
        avail[arm] += 1
        ts = [t for t, _ in norm]
        vs = [v for _, v in norm]
        ax.plot(ts, vs, color=ARM_COLOR[arm], alpha=0.30, lw=0.9)
    if all_norm:
        # median across reps on a common 1 s grid
        grid = np.arange(0, 96, 1.0)
        mat = np.full((len(all_norm), len(grid)), np.nan)
        for i, norm in enumerate(all_norm):
            tt = np.array([t for t, _ in norm])
            vv = np.array([v for _, v in norm])
            order = np.argsort(tt)
            mat[i] = np.interp(grid, tt[order], vv[order],
                               left=np.nan, right=np.nan)
        med = np.nanmedian(mat, axis=0)
        ax.plot(grid, med, color=ARM_COLOR[arm], lw=2.6,
                label="%s median (n=%d)" % (ARM_LABEL[arm], len(all_norm)))

for x in (30, 60):
    ax.axvline(x, color="black", ls="--", lw=1.0, alpha=0.7)
ax.axhline(1.0, color="gray", lw=0.8, alpha=0.6)
ax.text(30.6, ax.get_ylim()[1] * 0.97, "t=30s  LC QD 32->64", fontsize=8)
ax.text(60.6, ax.get_ylim()[1] * 0.97, "t=60s  LC QD back to 32", fontsize=8)

ax.set_xlabel("Time since LC workload start (s)")
ax.set_ylabel("vrate relative to own initial value (x)")
ax.set_title("io_cost: vrate response to LC iodepth step (both normalized to first value)\n"
             "NOTE: IOX vrate = FSM multiplier 0.25-4.0x; native vrate = absolute cost/s rate —\n"
             "only response direction / phase steps are comparable")
ax.set_xlim(-2, 95)
ax.grid(alpha=0.3)
ax.legend(frameon=False, loc="lower left")
fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.io_cost.pdf"))
fig.savefig(os.path.join(OUT, "fig.io_cost.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["side", "rep", "t_s", "vrate_raw", "vrate_normalized"])
    for r in csv_rows:
        w.writerow(r)

notes = []
if avail["native"] < 5:
    notes.append("native vrate trace reps used: %d/5 (rep5 companion run incomplete: "
                 "no RESULT.json, empty p3 fio JSONs, trace ends ~t=72s — excluded)" % avail["native"])
if avail["iox"] < 5:
    notes.append("iox loader stats reps available: %d/5" % avail["iox"])
for n in notes:
    print("NOTE:", n)
print("figure written to", OUT)
