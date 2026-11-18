#!/usr/bin/env python3
"""Analyze a d1_ladder_<runid> results tree (D1' E1-E4, bm) into markdown tables.

Usage: python3 d1_framework_ladder_analyze.py <results_dir> [--out SUMMARY.md]
Run with -I if pointed at untrusted data dirs.  All inputs are fio JSON emitted
by our own d1_framework_ladder_bm.sh plus attach.csv / env.txt from that script.
"""
import csv
import glob
import json
import os
import statistics as st
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = os.path.join(RUN, "SUMMARY.md")


def pctl(pcts, name):
    # fio 3.41 json: {"50.000000": ns, ...}
    if not pcts:
        return None
    v = pcts.get(name)
    return None if v is None else v / 1000.0  # ns -> us


def job_stats(path):
    with open(path) as f:
        d = json.load(f)
    j = d["jobs"][0]
    r = j["read"]
    clat_pcts = r.get("clat_ns", {}).get("percentile")
    slat_pcts = r.get("slat_ns", {}).get("percentile")
    lat_pcts = r.get("lat_ns", {}).get("percentile")
    # cpu_us_per_io = (usr_cpu% + sys_cpu%)/100 * runtime_ms / 1000 / N_ios * 1e6
    #             = (usr+sys)% * runtime_ms * 10 / N_ios   [us per IO]
    runtime_ms = float(j.get("job_runtime", 0)) or j.get("elapsed", 0) * 1000.0
    n_ios = r.get("total_ios", 0) or (r.get("iops", 0) * (runtime_ms / 1000.0))
    cpu_pct = j.get("usr_cpu", 0.0) + j.get("sys_cpu", 0.0)
    cpu_us_per_io = cpu_pct * runtime_ms * 10.0 / n_ios if n_ios else None
    return {
        "iops": r["iops"],
        "clat_mean_us": r["clat_ns"]["mean"] / 1000.0,
        "slat_mean_us": r["slat_ns"]["mean"] / 1000.0,
        "lat_mean_us": r["lat_ns"]["mean"] / 1000.0,
        "clat_p50": pctl(clat_pcts, "50.000000"),
        "clat_p99": pctl(clat_pcts, "99.000000"),
        "clat_p999": pctl(clat_pcts, "99.900000"),
        "slat_p99": pctl(slat_pcts, "99.000000"),
        "lat_p99": pctl(lat_pcts, "99.000000"),
        "cpu_pct": cpu_pct,
        "cpu_us_per_io": cpu_us_per_io,
        "runtime_s": d.get("jobs", [{}])[0].get("elapsed", 0),
    }


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def fmt(x, nd=1):
    return "-" if x is None else f"{x:.{nd}f}"


lines = []
A = lines.append

A("# D1' framework-overhead run — SUMMARY")
A("")
with open(os.path.join(RUN, "env.txt")) as f:
    env = f.read()
A("## Provenance")
A("")
A("```")
A(env.strip())
A("```")
A("")

# ---- device state ----
dp = os.path.join(RUN, "00_device_probe", "probe_fio.json")
if os.path.exists(dp):
    s = job_stats(dp)
    A("## Device state (solo native 4K QD1 probe, same-day baseline)")
    A("")
    A(f"- IOPS: {s['iops']:.0f}, clat p50: {fmt(s['clat_p50'])} us, clat p99: {fmt(s['clat_p99'])} us")
    A(f"- Reference healthy state (2026-11-06): ~55k IOPS, p50 ~10.8 us")
    A(f"- Reference degraded state (2026-11-07): ~16.6-21k IOPS, p50 ~46 us")
    if s["iops"] >= 40000:
        v = "HEALTHY (matches 11-06 level)"
    elif s["iops"] >= 14000:
        v = "DEGRADED (matches 11-07 level)"
    else:
        v = "UNKNOWN/other"
    A(f"- Verdict: **{v}** — all arms below are same-day paired comparisons regardless of level.")
    A("")

# ---- E1 ----
att = {}
att_csv = os.path.join(RUN, "attach.csv")
if os.path.exists(att_csv):
    with open(att_csv) as f:
        for row in csv.reader(f):
            # data order written by rec_attach(): phase,arm,rep,token,attach_ms,spawn,seen
            if len(row) < 7 or row[0] == "phase":
                continue
            if row[4] in ("NA", ""):
                continue
            att.setdefault((row[1], row[3]), []).append(float(row[4]))
A("## E1 attach latency (per loader start, ladder reps; poller = `bpftool struct_ops show` every ~2ms)")
A("")
A("Note: granularity is bounded by bpftool process startup (~5-15 ms per poll), so values are upper bounds on true attach latency.")
A("")
A("| arm | struct_ops token | n | median ms | min ms | max ms |")
A("|---|---|---|---|---|---|")
for (arm, tok), xs in sorted(att.items()):
    A(f"| {arm} | {tok} | {len(xs)} | {fmt(med(xs))} | {fmt(min(xs))} | {fmt(max(xs))} |")
A("")

# ---- E3/E2 ladder ----
A("## E2+E3 ladder (5 arms x 5 reps, interleaved round-robin, 4K QD1 solo, seed 4242)")
A("")
A("| arm | rep | IOPS | slat mean us | slat p99 us | clat mean us | clat p50 us | clat p99 us | clat p99.9 us | lat p99 us | cpu% | cpu us/IO |")
A("|---|---|---|---|---|---|---|---|---|---|---|---|")
ladder_rows = {}
for d in sorted(glob.glob(os.path.join(RUN, "ladder", "*"))):
    base = os.path.basename(d)
    arm, rep = base.rsplit("_r", 1)
    f = os.path.join(d, "ta_fio.json")
    if not os.path.exists(f):
        ladder_rows.setdefault(arm, {})[rep] = None
        continue
    s = job_stats(f)
    ladder_rows.setdefault(arm, {})[rep] = s
    A(f"| {arm} | {rep} | {s['iops']:.0f} | {fmt(s['slat_mean_us'])} | {fmt(s['slat_p99'])} | "
      f"{fmt(s['clat_mean_us'])} | {fmt(s['clat_p50'])} | {fmt(s['clat_p99'])} | {fmt(s['clat_p999'])} | "
      f"{fmt(s['lat_p99'])} | {fmt(s['cpu_pct'], 2)} | {fmt(s['cpu_us_per_io'], 3)} |")
A("")
A("Per-arm aggregates (median over reps):")
A("")
A("| arm | median IOPS | median clat mean us | median clat p50 us | median clat p99 us | median cpu us/IO |")
A("|---|---|---|---|---|---|")
for arm in ["native", "bare", "q_noop", "q_fifo", "qfifo_arb"]:
    rows = [v for v in ladder_rows.get(arm, {}).values() if v]
    if not rows:
        A(f"| {arm} | - | - | - | - | - |")
        continue
    A(f"| {arm} | {fmt(med([r['iops'] for r in rows]), 0)} | {fmt(med([r['clat_mean_us'] for r in rows]))} | "
      f"{fmt(med([r['clat_p50'] for r in rows]))} | {fmt(med([r['clat_p99'] for r in rows]))} | "
      f"{fmt(med([r['cpu_us_per_io'] for r in rows]), 3)} |")
A("")
A("cpu_us_per_io formula: `(usr_cpu% + sys_cpu%) / 100 * runtime_ms / 1000 / N_ios * 1e6`, i.e. fio-reported CPU fraction of one core over the job runtime converted to CPU-seconds, divided by total read IOs, in microseconds per IO.")
A("")

# ---- E4a ----
A("## E4a fixed load (4 QD1 jobs total, layouts 1/2/4 cgroups, native vs q_noop attach-all)")
A("")
A("| layout | stack | rep | agg IOPS | per-job IOPS | mean per-job clat p99 us | worst per-job clat p99 us |")
A("|---|---|---|---|---|---|---|")
e4a = {}
import re as _re
for d in sorted(glob.glob(os.path.join(RUN, "e4a", "*"))):
    base = os.path.basename(d)
    m = _re.match(r"(4on1cg|twox2|fourx1)_(native|q_noop)_r(\d+)$", base)
    if not m:
        continue
    layout, stack, rep = m.group(1), m.group(2), m.group(3)
    iops = 0.0
    p99s = []
    jiops = []
    ok = True
    for j in (1, 2, 3, 4):
        f = os.path.join(d, f"cg{j}", f"j{j}_fio.json")
        if not os.path.exists(f):
            ok = False
            continue
        s = job_stats(f)
        iops += s["iops"]
        jiops.append(s["iops"])
        p99s.append(s["clat_p99"])
    if not ok:
        A(f"| {layout} | {stack} | {rep} | INCOMPLETE | - | - | - |")
        continue
    e4a.setdefault((layout, stack), []).append({"iops": iops, "p99mean": med(p99s), "p99worst": max(p99s)})
    A(f"| {layout} | {stack} | {rep} | {iops:.0f} | {'/'.join(f'{x:.0f}' for x in jiops)} | "
      f"{fmt(med(p99s))} | {fmt(max(p99s))} |")
A("")
A("E4a aggregates (median over reps):")
A("")
A("| layout | stack | median agg IOPS | median mean-per-job p99 us | median worst-per-job p99 us |")
A("|---|---|---|---|---|")
for layout in ["4on1cg", "twox2", "fourx1"]:
    for stack in ["native", "q_noop"]:
        rows = e4a.get((layout, stack), [])
        if not rows:
            continue
        A(f"| {layout} | {stack} | {fmt(med([r['iops'] for r in rows]), 0)} | "
          f"{fmt(med([r['p99mean'] for r in rows]))} | {fmt(med([r['p99worst'] for r in rows]))} |")
A("")

# ---- E4b ----
A("## E4b registered-idle (1 active job; idle q_noop-registered cgroups vs plain native cgroups)")
A("")
A("N_idle=0 q_noop baseline = the E3 q_noop reps (not rerun).")
A("")
A("| N_registered | stack | rep | active IOPS | active clat mean us | active clat p99 us |")
A("|---|---|---|---|---|---|")
e4b = {}
for d in sorted(glob.glob(os.path.join(RUN, "e4b", "*"))):
    base = os.path.basename(d)
    parts = base.rsplit("_r", 1)
    n_stack, rep = parts[0], parts[1]
    n_idle = int(n_stack[1:n_stack.index("_")])
    stack = n_stack[n_stack.index("_") + 1:]
    f = os.path.join(d, "act_fio.json")
    if not os.path.exists(f):
        A(f"| {n_idle + 1} | {stack} | {rep} | INCOMPLETE | - | - |")
        continue
    s = job_stats(f)
    e4b.setdefault((n_idle + 1, stack), []).append(s)
    A(f"| {n_idle + 1} | {stack} | {rep} | {s['iops']:.0f} | {fmt(s['clat_mean_us'])} | {fmt(s['clat_p99'])} |")
# append E3 q_noop as N_registered=1
qn = [v for v in ladder_rows.get("q_noop", {}).values() if v]
if qn:
    A("")
    A("E3 q_noop reps (N_registered=1 baseline, single registered loader):")
    A("")
    A("| N_registered | stack | rep | active IOPS | active clat mean us | active clat p99 us |")
    A("|---|---|---|---|---|---|")
    for rep, s in sorted(ladder_rows.get("q_noop", {}).items(), key=lambda kv: int(kv[0])):
        if not s:
            continue
        A(f"| 1 | q_noop | {rep} | {s['iops']:.0f} | {fmt(s['clat_mean_us'])} | {fmt(s['clat_p99'])} |")
A("")
A("E4b aggregates (median over reps):")
A("")
A("| N_registered | stack | median active IOPS | median clat p99 us | min p99 | max p99 | median clat mean us |")
A("|---|---|---|---|---|---|---|")
for n in (1, 2, 4, 8, 16):
    for stack in ["native", "q_noop"]:
        if n == 1 and stack == "native":
            src = ladder_rows.get("native", {})
            rows = [v for v in src.values() if v]
        elif n == 1:
            rows = qn
        else:
            rows = e4b.get((n, stack), [])
        if not rows:
            continue
        A(f"| {n} | {stack} | {fmt(med([r['iops'] for r in rows]), 0)} | "
          f"{fmt(med([r['clat_p99'] for r in rows]))} | {fmt(min(r['clat_p99'] for r in rows))} | "
          f"{fmt(max(r['clat_p99'] for r in rows))} | {fmt(med([r['clat_mean_us'] for r in rows]))} |")
A("")

# ---- skipped / failed ----
A("## Skipped / failed reps")
A("")
n_att_na = 0
if os.path.exists(att_csv):
    with open(att_csv) as f:
        rows = list(csv.DictReader(f))
        n_att_na = sum(1 for r in rows if r["attach_ms"] in ("NA", ""))
A(f"- Attach samples NA (loader failed to appear within poll deadline): {n_att_na}")
A("- Full list of expected run dirs is in `summary.txt`; incomplete dirs would show INCOMPLETE rows above.")
A("")
with open(os.path.join(RUN, "env.txt")) as f:
    tail = f.read().strip().splitlines()[-7:]
A("## Final restore verification (from env.txt tail)")
A("")
A("```")
A("\n".join(tail))
A("```")
A("")

with open(OUT, "w") as f:
    f.write("\n".join(lines))
print(f"wrote {OUT}")
