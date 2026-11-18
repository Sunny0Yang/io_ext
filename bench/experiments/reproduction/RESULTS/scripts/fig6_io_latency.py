#!/usr/bin/env python3
# fig6_io_latency.py — (highest priority) LC write p99 1 s series (top) +
# BE write bandwidth 1 s series (bottom). LC = 4k randwrite QD 32->64->32
# (p1/p2/p3, 30 s each); BE = 128k seqwrite QD16 through the same 90 s.
# LC p99 from per-IO logs (mode=full, dir==1); BE bw from 1 s bw logs.
# Fallback (logs absent): per-phase values from fio JSONs at phase centers.
import csv
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (IOLAT_TARGET_US, RESULTS, ARM_COLOR, ARM_LABEL, ARMS_ALL,
                    REPS, armed_unix, find_log, load_json, load_jsonl,
                    parse_fio_log, run_dir)

ALGO = "io_latency"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)

PHASES = [("p1", 0, 30), ("p2", 30, 60), ("p3", 60, 90)]

def ledger_bw_series(run):
    """IOX-only: 1 s BE bandwidth from the arbiter ledger (arb_iolatency
    tenants[].bytes = cumulative completed_bytes per (cgroup,hctx); sum per
    cgroup, diff consecutive samples). cg->LC/BE mapping via final ledger
    totals vs fio io_bytes (must match within 5%%). Returns [(t_rel, be_mib_s)]
    or None."""
    recs = load_jsonl(os.path.join(run, "loader_arb_iolatency.stdout")) or []
    ts = [r for r in recs if r.get("pol") == "arb_iolatency" and "tenants" in r]
    if len(ts) < 10:
        return None
    j1 = load_json(os.path.join(run, "fio_p1_lc.json"))
    if j1 is None or not j1.get("timestamp"):
        return None
    t1 = j1["timestamp"] - 30
    fio_tot = {"lc": 0, "be": 0}
    for side in ("lc", "be"):
        for ph in ("p1", "p2", "p3"):
            j = load_json(os.path.join(run, "fio_%s_%s.json" % (ph, side)))
            if j:
                for jo in j.get("jobs", []):
                    fio_tot[side] += jo.get("write", {}).get("io_bytes") or 0
    led_tot = {}
    for t in ts[-1]["tenants"]:
        led_tot[t["cg"]] = led_tot.get(t["cg"], 0) + (t.get("bytes") or 0)
    cgs = sorted(led_tot)
    if len(cgs) != 2:
        return None
    # nearest matching (LC total smaller than BE here, but match by value)
    pairs = []
    for side in ("lc", "be"):
        cg = min(cgs, key=lambda c: abs(led_tot[c] - fio_tot[side]))
        err = abs(led_tot[cg] - fio_tot[side]) / max(fio_tot[side], 1)
        if err > 0.05:
            return None
        pairs.append((side, cg))
    be_cg = dict(pairs)["be"]
    attach = next((r["t_ms"] for r in recs if r.get("event") == "attach"),
                  ts[0]["t_ms"])
    aw = armed_unix(run)
    if aw is None:
        return None
    base = (aw - 1.0) - t1
    ser = []
    prev_t = prev_be = None
    for r in ts:
        be = sum(t.get("bytes") or 0 for t in r["tenants"] if t["cg"] == be_cg)
        t_rel = base + (r["t_ms"] - attach) / 1000.0
        if prev_be is not None and 0 <= t_rel <= 92:
            dt = r["t_ms"] - prev_t
            if dt > 0:
                mib = (be - prev_be) / 1048576.0 / (dt / 1000.0)
                ser.append((round(t_rel, 3), mib))
        prev_t, prev_be = r["t_ms"], be
    return ser


csv_rows = []
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8.6, 7.4), sharex=True,
                               gridspec_kw={"height_ratios": [3, 2]})

have_perio = 0
have_bwl = 0
json_lc = {arm: {} for arm in ARMS_ALL}    # phase -> [(rep, p99_us)]
json_be = {arm: {} for arm in ARMS_ALL}    # phase -> [(rep, mean_bw_mib)]

for arm in ARMS_ALL:
    p99_reps, bw_reps = [], []
    for rep in REPS:
        run = run_dir(ALGO, rep, arm)
        # JSON fallback data
        for ph, t0, t1 in PHASES:
            jl = load_json(os.path.join(run, "fio_%s_lc.json" % ph))
            jb = load_json(os.path.join(run, "fio_%s_be.json" % ph))
            if jl is not None:
                for jo in jl.get("jobs", []):
                    v = jo.get("write", {}).get("clat_ns", {}).get("percentile", {}).get("99.000000")
                    if v:
                        json_lc[arm].setdefault(ph, []).append((rep, v / 1000.0))
            if jb is not None:
                for jo in jb.get("jobs", []):
                    if jo.get("write", {}).get("bw"):
                        json_be[arm].setdefault(ph, []).append(
                            (rep, jo["write"]["bw"] / 1024.0))
        # per-IO LC p99 series
        ser_p99 = []
        ok_all = True
        for ph, t0, t1 in PHASES:
            p = find_log(run, "lat_%s_lc" % ph, "lat")
            j = load_json(os.path.join(run, "fio_%s_lc.json" % ph))
            if p is None or j is None:
                ok_all = False
                break
            rows = parse_fio_log(p)
            if rows is None:
                ok_all = False
                break
            phase_start = j["timestamp"] - 30
            for (t_ms, v, d, bs, off) in rows:
                if d != 1:  # write only
                    continue
                t_rel = phase_start - (load_json(os.path.join(
                    run, "fio_p1_lc.json"))["timestamp"] - 30) + t_ms / 1000.0
                ser_p99.append((t_rel, v))
        if ok_all and ser_p99:
            have_perio += 1
            buckets = {}
            for t, v in ser_p99:
                buckets.setdefault(int(np.floor(t)), []).append(v)
            ser = [(b, float(np.percentile(vals, 99)))
                   for b, vals in sorted(buckets.items()) if len(vals) >= 20]
            p99_reps.append(ser)
            for t, v in ser:
                csv_rows.append(["lc_write_p99", arm, rep, t, v, ""])
        # BE bw 1 s series
        ser_bw = []
        ok_all = True
        for ph, t0, t1 in PHASES:
            p = find_log(run, "lat_%s_be" % ph, "bw")
            j = load_json(os.path.join(run, "fio_%s_be.json" % ph))
            if p is None or j is None:
                ok_all = False
                break
            rows = parse_fio_log(p)
            if rows is None:
                ok_all = False
                break
            phase_start = j["timestamp"] - 30
            p1_start = load_json(os.path.join(run, "fio_p1_lc.json"))["timestamp"] - 30
            for (t_ms, v, d, bs, off) in rows:
                t_rel = phase_start - p1_start + t_ms / 1000.0
                ser_bw.append((t_rel, v / 1024.0))
        if ok_all and ser_bw:
            have_bwl += 1
            bw_reps.append(ser_bw)
            for t, v in ser_bw:
                csv_rows.append(["be_write_bw", arm, rep, t, "", v])

    # top panel: LC p99
    if p99_reps:
        grid = np.arange(0, 91, 1.0)
        mat = np.full((len(p99_reps), len(grid)), np.nan)
        for i, ser in enumerate(p99_reps):
            tt = np.array([t for t, _ in ser]) / 1000.0 if max(t for t, _ in ser) > 120 else np.array([t for t, _ in ser])
            vv = np.array([v / 1000.0 for _, v in ser])
            order = np.argsort(tt)
            mat[i] = np.interp(grid, tt[order], vv[order], left=np.nan, right=np.nan)
            ax1.plot(grid, mat[i], color=ARM_COLOR[arm], alpha=0.22, lw=0.8)
        med = np.nanmedian(mat, axis=0)
        ax1.plot(grid, med, color=ARM_COLOR[arm], lw=2.0,
                 label="%s median (n=%d)" % (ARM_LABEL[arm], len(p99_reps)))
    # bottom panel: BE bw
    if bw_reps:
        grid = np.arange(0, 91, 1.0)
        mat = np.full((len(bw_reps), len(grid)), np.nan)
        for i, ser in enumerate(bw_reps):
            tt = np.array([t for t, _ in ser])
            vv = np.array([v for _, v in ser])
            order = np.argsort(tt)
            mat[i] = np.interp(grid, tt[order], vv[order], left=np.nan, right=np.nan)
            ax2.plot(grid, mat[i], color=ARM_COLOR[arm], alpha=0.22, lw=0.8)
        med = np.nanmedian(mat, axis=0)
        ax2.plot(grid, med, color=ARM_COLOR[arm], lw=2.0,
                 label="%s median (n=%d)" % (ARM_LABEL[arm], len(bw_reps)))

# IOX arbiter-ledger BE bandwidth (1 s, dashed overlay): loader stats carry
# cumulative completed_bytes per (cgroup, hctx); fio bw logs are absent in this
# snapshot, so this is the only 1 s BE series available (iox arm only).
led = ledger_bw_series(run_dir(ALGO, 1, "iox")) if os.path.isdir(
    run_dir(ALGO, 1, "iox")) else None
led_reps = []
for rep in REPS:
    s = ledger_bw_series(run_dir(ALGO, rep, "iox"))
    if s:
        led_reps.append(s)
        for t, v in s:
            csv_rows.append(["be_write_bw_ledger", "iox", rep, t, "", v])
if led_reps:
    grid = np.arange(0, 91, 1.0)
    mat = np.full((len(led_reps), len(grid)), np.nan)
    for i, ser in enumerate(led_reps):
        tt = np.array([t for t, _ in ser])
        vv = np.array([v for _, v in ser])
        mat[i] = np.interp(grid, tt, vv, left=np.nan, right=np.nan)
        ax2.plot(grid, mat[i], color=ARM_COLOR["iox"], alpha=0.20, lw=0.8, ls="--")
    med = np.nanmedian(mat, axis=0)
    ax2.plot(grid, med, color=ARM_COLOR["iox"], lw=1.8, ls="--",
             label="IOX arbiter ledger bw (n=%d)" % len(led_reps))

# fallback: phase-center markers from JSON
if have_perio == 0:
    rng = np.random.default_rng(9)
    for arm in ARMS_ALL:
        for ph, t0, t1 in PHASES:
            vals = json_lc[arm].get(ph, [])
            if not vals:
                continue
            xj = (t0 + t1) / 2.0 + rng.uniform(-1.0, 1.0, len(vals))
            ax1.scatter(xj, [v / 1000.0 for _, v in vals], s=26,
                        color=ARM_COLOR[arm], alpha=0.75,
                        label=("%s per-rep phase p99 (fio JSON)" % ARM_LABEL[arm]) if ph == "p1" else None)
    pass
if have_bwl == 0:
    rng = np.random.default_rng(10)
    for arm in ARMS_ALL:
        for ph, t0, t1 in PHASES:
            vals = json_be[arm].get(ph, [])
            if not vals:
                continue
            xj = (t0 + t1) / 2.0 + rng.uniform(-1.0, 1.0, len(vals))
            ax2.scatter(xj, [v for _, v in vals], s=26,
                        color=ARM_COLOR[arm], alpha=0.75,
                        label=("%s per-rep phase mean bw (fio JSON)" % ARM_LABEL[arm]) if ph == "p1" else None)
    ax2.text(0.99, 0.97, "1 s bw series unavailable: window logs (lat_p*_be_bw.1.log) not in\n"
             "delivered data; shown: per-rep phase mean bw (fio JSON) + IOX ledger 1 s bw",
             transform=ax2.transAxes, ha="right", va="top", fontsize=8.5, color="firebrick")
fig.text(0.01, 0.006,
         "Note: 1 s series unavailable in this snapshot (per-IO logs lat_p*_lc_lat.1.log and window logs "
         "lat_p*_be_bw.1.log were not packaged into the run dirs) —\n"
         "top panel shows per-rep phase p99 from fio JSON (marker x = phase center); "
         "bottom panel additionally shows the IOX arbiter-ledger 1 s BE bandwidth.",
         fontsize=8, color="firebrick", va="bottom")
fig.subplots_adjust(bottom=0.14, top=0.90, hspace=0.12)

for ax in (ax1, ax2):
    for x in (30, 60):
        ax.axvline(x, color="black", ls="--", lw=1.0, alpha=0.7)
ax1.axhline(IOLAT_TARGET_US / 1000.0, color="black", ls=":", lw=1.3)
ax1.text(32.5, 9.0, "LC latency target = 12 ms", ha="left", fontsize=9)

ax1.set_ylabel("LC write p99 (ms)")
ax1.set_title("io_latency: LC 4k randwrite QD 32->64->32 (top) + BE 128k seqwrite QD16 (bottom)\n"
              "native io.latency target=12 ms; IOX arb_iolatency 12 ms; none = uncontrolled")
ax1.grid(alpha=0.3)
ax1.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0.0, 1.02))
ax2.set_xlabel("Time since LC workload start (s)")
ax2.set_ylabel("BE write bandwidth (MiB/s)")
ax2.grid(alpha=0.3)
ax2.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0.0, 1.02))
ax1.set_xlim(-2, 92)

fig.tight_layout(rect=[0, 0.055, 1, 1])
fig.savefig(os.path.join(OUT, "fig.io_latency.pdf"))
fig.savefig(os.path.join(OUT, "fig.io_latency.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["metric", "arm", "rep", "t_s", "lc_p99_us", "be_bw_mib_s"])
    for r in csv_rows:
        w.writerow(r)
    for arm in ARMS_ALL:
        for ph, vals in json_lc[arm].items():
            for rep, v in vals:
                w.writerow(["lc_write_p99_json_%s" % ph, arm, rep, "", "%.1f" % v, ""])
        for ph, vals in json_be[arm].items():
            for rep, v in vals:
                w.writerow(["be_write_bw_json_%s" % ph, arm, rep, "", "", "%.3f" % v])

print("per-IO series:", have_perio, "| bw series:", have_bwl)
print("figure written to", OUT)
