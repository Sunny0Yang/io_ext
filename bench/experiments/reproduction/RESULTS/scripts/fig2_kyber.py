#!/usr/bin/env python3
# fig2_kyber.py — LC read p99 1 s-bucketed time series (top panel) + read-domain
# depth time series (bottom panel).
#   p99:    per-IO log lat_p*_lc_lat.1.log (write_lat_log, mode=full), dir==0 (read),
#           1 s windows across p1(0-30)/p2(30-60)/p3(60-90), median of 5 reps per arm.
#   depth:  IOX = loader_q_kyber.stdout hctx[*].dl[0] (read-domain depth limit)
#           native = kyber_depth_native/rep{N}/native/depth_trace.log read= column
#           (debugfs sbitmap depth = allowed depth for the read domain).
# Alignment: fio JSON timestamp = job END; p1 start = ts(p1_lc) - 30.
#            loader wall-clock anchor = state_snapshot "armed" - 1.0 s.
import csv
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CAMPAIGN, RESULTS, ARM_COLOR, ARM_LABEL, ARM_LS, ARMS_ALL, REPS,
                    find_log, load_json, load_jsonl, parse_fio_log, read_text_maybe_gz,
                    run_dir)

ALGO = "kyber"
OUT = os.path.join(RESULTS, ALGO)
os.makedirs(OUT, exist_ok=True)

SRC_ROOT = os.path.dirname(CAMPAIGN)  # .../results (companion dirs live here)
DEPTH_LOG = os.path.join(SRC_ROOT, "kyber_depth_native", "rep%d", "native", "depth_trace.log.gz")
PHASE_MS = 30000
PHASES = [("p1", 0), ("p2", 30), ("p3", 60)]


def phase_anchor(run, ts_key="p1"):
    """Return (p1_start_unix, per-phase (start,end) unix) for a run."""
    tss = {}
    for ph, _ in PHASES:
        j = load_json(os.path.join(run, "fio_%s_lc.json" % ph))
        if j is None or not j.get("timestamp"):
            return None, None
        tss[ph] = j["timestamp"]
    p1_start = tss["p1"] - 30
    spans = {}
    for ph, off in PHASES:
        spans[ph] = (tss[ph] - 30 - p1_start, tss["p2" if ph == "p1" else ph] - p1_start)
    return p1_start, spans


def armed_unix(run):
    txt = read_text_maybe_gz(os.path.join(run, "state_snapshot.log.gz"))
    if txt is None:
        txt = read_text_maybe_gz(os.path.join(run, "state_snapshot.log"))
    if not txt:
        return None
    m = re.search(r"^== armed ([\d.]+)", txt, re.M)
    return float(m.group(1)) if m else None


def lc_p99_series(run, t1_start):
    """1 s-bucket p99 of LC read per-IO latencies, buckets relative to p1 start.
    Returns dict t_s -> p99_us (pooled across the whole run window)."""
    rows_all = []
    spans, ok = [], True
    for ph, off in PHASES:
        p = find_log(run, "lat_%s_lc" % ph, "lat")
        if p is None:
            return None
        rows = parse_fio_log(p)
        if rows is None:
            return None
        j = load_json(os.path.join(run, "fio_%s_lc.json" % ph))
        phase_start = j["timestamp"] - 30
        for (t_ms, v, d, bs, off_) in rows:
            if d != 0:  # read direction only
                continue
            t_rel = phase_start - t1_start + t_ms / 1000.0
            rows_all.append((t_rel, v))  # v in us
    if not rows_all:
        return None
    buckets = {}
    for t, v in rows_all:
        b = int(np.floor(t))
        buckets.setdefault(b, []).append(v)
    out = {}
    for b, vals in sorted(buckets.items()):
        if len(vals) < 20:  # near-empty boundary buckets: unreliable p99
            continue
        out[b] = float(np.percentile(vals, 99))
    return out


def iox_depth_series(run):
    """IOX read-domain depth limit series [(t_s, max, median, min across hctx)].
    loader t_ms = monotonic uptime ms; wall anchor = attach event ~1 s before
    state_snapshot 'armed'."""
    recs = load_jsonl(os.path.join(run, "loader_q_kyber.stdout"))
    if not recs:
        return None
    aw = armed_unix(run)
    attach = None
    for r in recs:
        if r.get("event") == "attach":
            attach = r["t_ms"]
            break
    if attach is None:
        attach = recs[0]["t_ms"]
    recs = [r for r in recs if r.get("pol") == "q_kyber" and r.get("hctx")]
    if not recs or aw is None:
        return None
    j = load_json(os.path.join(run, "fio_p1_lc.json"))
    if j is None or not j.get("timestamp"):
        return None
    t1 = j["timestamp"] - 30
    base = (aw - 1.0) - t1  # wall offset of loader attach relative to p1 start
    ser = []
    for r in recs:
        h = r["hctx"]
        if not h:
            continue
        dl0 = [x["dl"][0] for x in h]
        t_rel = base + (r["t_ms"] - attach) / 1000.0
        if t_rel < -3 or t_rel > 95:
            continue
        ser.append((round(t_rel, 3), float(np.max(dl0)),
                    float(np.median(dl0)), float(np.min(dl0))))
    return ser


def native_depth_series(rep):
    """native read depth series [(t_s, read_depth)] from depth_trace.log.gz.
    Format: 'epoch_ts read write' numeric triple (1 s sampling); rows like
    'read_tokens=READ_ERR:...' at teardown tail are skipped. Aligned with the
    companion run's own fio_p1_lc.json (timestamp = job END; p1 start = ts-30)."""
    path = DEPTH_LOG % rep
    if not os.path.exists(path):
        return None
    run = os.path.join(SRC_ROOT, "kyber_depth_native", "rep%d", "native") % rep
    j = load_json(os.path.join(run, "fio_p1_lc.json"))
    if j is None or not j.get("timestamp"):
        return None
    t1 = j["timestamp"] - 30
    ser = []
    rx_num = re.compile(r"^\s*([\d.]+)\s+(\d+)\s+(\d+)\s*$")
    rx_kv = re.compile(r"^\s*([\d.]+)\s+read=(\d+)\s+write=(\d+)")
    txt = read_text_maybe_gz(path)
    for line in txt.splitlines():
        m = rx_num.match(line) or rx_kv.match(line)
        if not m:
            continue  # includes READ_ERR tail rows
        t_rel = float(m.group(1)) - t1
        if -3 <= t_rel <= 95:
            ser.append((round(t_rel, 3), int(m.group(2))))
    return ser


csv_rows = []
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8.6, 7.2), sharex=True,
                               gridspec_kw={"height_ratios": [3, 2]})

# ---- top panel: LC read p99 series -----------------------------------------
have_p99 = {"none": 0, "native": 0, "iox": 0}
for arm in ARMS_ALL:
    per_rep = []
    for rep in REPS:
        run = run_dir(ALGO, rep, arm)
        _, spans = phase_anchor(run)
        if spans is None:
            continue
        ser = lc_p99_series(run, 0.0)
        if ser is None:
            print("p99 %s rep%d: per-IO log missing" % (arm, rep))
            continue
        have_p99[arm] += 1
        per_rep.append(ser)
        for t, v in sorted(ser.items()):
            csv_rows.append(["p99_lc_read", arm, rep, t, v, "", "", ""])
    if per_rep:
        grid = np.arange(0, 91, 1.0)
        mat = np.full((len(per_rep), len(grid)), np.nan)
        for i, ser in enumerate(per_rep):
            tt = np.array(sorted(ser))
            vv = np.array([ser[t] for t in tt])
            mat[i] = np.interp(grid, tt, vv, left=np.nan, right=np.nan)
        med = np.nanmedian(mat, axis=0)
        ax1.plot(grid, med / 1000.0, color=ARM_COLOR[arm], lw=2.0,
                 label="%s median (n=%d)" % (ARM_LABEL[arm], len(per_rep)))
if sum(have_p99.values()) == 0:
    ax1.text(0.5, 0.5,
             "LC read p99 1 s series unavailable:\n"
             "the surviving fio 'lat' logs are 1 s-window AVERAGES (global log_avg_msec=1000),\n"
             "not per-IO samples, and none of the survivors is attributable to kyber\n"
             "(fio log CWD defect, EXPERIMENT_MATRIX appendix D1) — see DATA_SUMMARY",
             transform=ax1.transAxes, ha="center", va="center", fontsize=9.5, color="firebrick")
ax1.axvline(30, color="black", ls="--", lw=1.0, alpha=0.7)
ax1.axvline(60, color="black", ls="--", lw=1.0, alpha=0.7)
ax1.set_ylabel("LC read p99 (ms)")
ax1.set_title("kyber: LC read p99 (1 s buckets, top) and read-domain depth (bottom)\n"
              "p1: LC QD32 | p2: LC QD64 | p3: LC QD32; native+IOX depth target 80 us read / 1 ms write")
ax1.grid(alpha=0.3)
ax1.legend(frameon=False, fontsize=9, loc="upper left")

# ---- bottom panel: depth ----------------------------------------------------
have_d = {"native": 0, "iox": 0}
for arm in ("native", "iox"):
    all_ser = []
    for rep in REPS:
        ser = (native_depth_series(rep) if arm == "native"
               else iox_depth_series(run_dir(ALGO, rep, arm)))
        if not ser:
            print("depth %s rep%d: missing" % (arm, rep))
            continue
        have_d[arm] += 1
        all_ser.append(ser)
        for s in ser:
            if arm == "iox":
                t, mx, md, mn = s
                csv_rows.append(["depth_read", arm, rep, t, "", md, mn, mx])
            else:
                t, dv = s
                csv_rows.append(["depth_read", arm, rep, t, "", dv, dv, dv])
    if all_ser:
        grid = np.arange(0, 91, 1.0)
        med_m = np.full((len(all_ser), len(grid)), np.nan)
        mn_m = med_m.copy()
        mx_m = med_m.copy()
        for i, ser in enumerate(all_ser):
            tt = np.array([t for t, *_ in ser])
            md = np.array([s[2] if arm == "iox" else s[1] for s in ser])
            mn = np.array([s[3] if arm == "iox" else s[1] for s in ser])
            mx = np.array([s[1] if arm == "iox" else s[1] for s in ser])
            order = np.argsort(tt)
            med_m[i] = np.interp(grid, tt[order], md[order], left=np.nan, right=np.nan)
            mn_m[i] = np.interp(grid, tt[order], mn[order], left=np.nan, right=np.nan)
            mx_m[i] = np.interp(grid, tt[order], mx[order], left=np.nan, right=np.nan)
        med = np.nanmedian(med_m, axis=0)
        mn = np.nanmedian(mn_m, axis=0)
        mx = np.nanmedian(mx_m, axis=0)
        if arm == "iox":
            lbl = "IOX q_kyber dl[0] median (n=%d) [min-max band]" % len(all_ser)
            ax2.fill_between(grid, mn, mx, color=ARM_COLOR[arm], alpha=0.15)
            ax2.plot(grid, med, color=ARM_COLOR[arm], lw=2.0, label=lbl)
        else:
            lbl = "native read depth (debugfs read_tokens, median n=%d)" % len(all_ser)
            ax2.plot(grid, med, color=ARM_COLOR[arm], lw=2.0, ls=ARM_LS[arm], label=lbl)
if sum(have_d.values()) == 0:
    ax2.text(0.5, 0.5, "depth series unavailable in delivered data",
             transform=ax2.transAxes, ha="center", va="center", fontsize=10, color="firebrick")
ax2.axvline(30, color="black", ls="--", lw=1.0, alpha=0.7)
ax2.axvline(60, color="black", ls="--", lw=1.0, alpha=0.7)
ax2.set_xlabel("Time since LC workload start (s)")
ax2.set_ylabel("read depth")
ax2.grid(alpha=0.3)
ax2.legend(frameon=False, fontsize=9, loc="upper left")

fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig.kyber.pdf"))
fig.savefig(os.path.join(OUT, "fig.kyber.png"), dpi=160)
plt.close(fig)

with open(os.path.join(OUT, "data.%s.csv" % ALGO), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["metric", "arm", "rep", "t_s", "p99_us",
                "depth_median_hctx", "depth_min_hctx", "depth_max_hctx"])
    for r in csv_rows:
        w.writerow(r)

print("p99 availability:", have_p99, " depth availability:", have_d)
print("figure written to", OUT)
