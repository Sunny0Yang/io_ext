#!/usr/bin/env python3
"""m1_metrics.py — M1 six-axis load-pattern metrics (task B, P0-2).

Parses the raw m1_pattern_sampler bpftrace logs + workload scene result JSONs
under results/motivation_m1/raw/<SEQ>/ and emits one row per PATTERN POINT:

  1 size distribution (read/write p50/p90/p99 bytes)     — from M1P dump
  2 byte bimodality (>64KB byte share, per direction)     — from M1P dump
  3 direction byte ratio (write byte share)              — from M1P dump
  4 concurrency proxy (in-flight mean/p99/peak @1 Hz)    — ib_system_metric
  5 barrier rate (REQ_OP_FLUSH events/s, x16 estimator)  — from M1P dump
  6 arrival non-stationarity (per-second IOPS CV)         — exact @m1_sec_comp
  7 direct-I/O ratio (scene-config metadata column)

Honesty notes baked into the output:
  - dump is a uniform 1-in-16 thinning -> size percentiles / byte shares /
    flush-rate are unbiased estimators (flush rate carries ~sqrt(n)x16
    sampling noise; reported with the raw dump count);
  - e2e latency covers anchored requests only (anchor coverage per row);
    on `none` the anchor is the block_io_start request-creation ts;
  - per-second bins are boot-second aligned (sub-second wall skew <=1 s,
    noted); only full seconds inside a window enter the CV.

Usage:
  python3 m1_metrics.py --results-root results/motivation_m1/raw \
      --out-csv results/motivation_m1/m1_metrics.csv \
      --out-json results/motivation_m1/m1_metrics_detail.json
"""

import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
import statistics
from typing import Dict, List, Optional, Tuple

DUMP_SAMPLE_EVERY = 16


# ---------------------------------------------------------------- log parsing

def parse_sampler_log(path: str) -> dict:
    """Parse one raw m1_pattern_sampler bpftrace log."""
    out = {
        "boot_ns0": None,
        "epoch0": None,
        "scalars": {},
        "lat_stats": {},
        "lhist": {},
        "sec": {},           # map name -> {boot_sec: count} (last wins)
        "dump": [],          # (arr_boot_ns, opcode, nr_bytes, e2e_ns, comp_ns, anchor)
        "lost_lines": 0,
    }
    if not os.path.isfile(path):
        return out
    cur_lhist = None
    _opener = gzip.open if str(path).endswith(".gz") else open
    with _opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("M1BEGIN_BOOTNS"):
                out["boot_ns0"] = int(line.split()[1])
                continue
            if line.startswith("M1BEGIN_EPOCH"):
                out["epoch0"] = int(line.split()[1])
                continue
            if line.startswith("M1P "):
                parts = line.split()
                if len(parts) == 7:
                    out["dump"].append(
                        (int(parts[1]), int(parts[2]), int(parts[3]),
                         int(parts[4]), int(parts[5]), int(parts[6]))
                    )
                continue
            if "Lost events" in line or "map full" in line.lower():
                out["lost_lines"] += 1
            # lhist section header: "@m1_size_r: "
            m = re.match(r"^@(\w+):\s*$", line)
            if m and m.group(1).startswith("m1_size"):
                cur_lhist = m.group(1)
                out["lhist"][cur_lhist] = []
                continue
            if cur_lhist:
                m = re.match(r"^\[(\S+),\s*(\S+)\)\s+(\d+)", line)
                if m:
                    lo = _parse_bucket_label(m.group(1))
                    hi = _parse_bucket_label(m.group(2))
                    out["lhist"][cur_lhist].append((lo, hi, int(m.group(3))))
                    continue
                if line.strip() == "":
                    cur_lhist = None
                    continue
            # per-second keyed counts: "@m1_sec_comp[31]: 123"
            m = re.match(r"^@(\w+)\[(\d+)\]:\s*(\d+)\s*$", line)
            if m:
                name, key, val = m.group(1), int(m.group(2)), int(m.group(3))
                out["sec"].setdefault(name, {})[key] = val
                continue
            # stats line (bpftrace 0.20.2: no braces):
            m = re.match(r"^@(\w+):\s*count\s+(\d+),\s*average\s+([0-9.]+),\s*total\s+(\d+)\s*$", line)
            if m:
                out["lat_stats"] = {
                    "count": int(m.group(2)),
                    "average": float(m.group(3)),
                    "total": int(m.group(4)),
                }
                continue
            # keyless scalar: "@m1_complete_count: 123"
            m = re.match(r"^@(\w+):\s*(\d+)\s*$", line)
            if m:
                out["scalars"][m.group(1)] = int(m.group(2))  # last wins
    return out


def _parse_bucket_label(label: str) -> Optional[int]:
    label = label.strip().strip("[")
    if label in ("+inf", "...", ""):
        return None
    if label.endswith("K"):
        return int(float(label[:-1]) * 1024)
    if label.endswith("M"):
        return int(float(label[:-1]) * 1024 * 1024)
    if label.endswith("G"):
        return int(float(label[:-1]) * 1024 * 1024 * 1024)
    try:
        return int(float(label))
    except ValueError:
        return None


# ------------------------------------------------------------- window sources

def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _one(glob_pattern: str) -> Optional[str]:
    hits = sorted(glob.glob(glob_pattern))
    return hits[-1] if hits else None


def ycsb_run_window(seq_dir: str) -> Optional[Tuple[float, float]]:
    p = _one(os.path.join(seq_dir, "step_0", "*", "ycsb_rocksdb_*.json"))
    if not p:
        return None
    d = _load_json(p)
    snaps = (d.get("collected_data") or d).get("io_stat_snapshots", {})
    b = (snaps.get("before_run") or {}).get("timestamp")
    a = (snaps.get("after_run") or {}).get("timestamp")
    if b and a:
        return float(b), float(a)
    return None


def dbbench_phase_windows(seq_dir: str) -> Dict[str, Tuple[float, float]]:
    p = _one(os.path.join(seq_dir, "step_0", "*", "dbbench_*.json"))
    if not p:
        return {}
    d = _load_json(p)
    snaps = (d.get("collected_data") or d).get("io_stat_snapshots", {})
    wins = {}
    for phase in ("fillrandom", "readrandom", "readwhilewriting", "readrandomwriterandom"):
        b = (snaps.get(f"before_{phase}") or {}).get("timestamp")
        a = (snaps.get(f"after_{phase}") or {}).get("timestamp")
        if b and a:
            wins[phase] = (float(b), float(a))
    return wins


def shore_windows(seq_dir: str) -> Dict[str, Tuple[float, float]]:
    p = _one(os.path.join(seq_dir, "step_0", "*", "tailbench_shore*.json"))
    if not p:
        return {}
    d = _load_json(p)
    cd = d.get("collected_data") or d
    st, et = cd.get("start_time"), cd.get("end_time")
    if not st or not et:
        return {}
    st, et = float(st), float(et)
    return {
        "full": (st, et),
        # ROI approximation: warmup 1000 reqs @ 50 qps = 20 s + 10 s margin;
        # ROI = 30000/50 = 600 s.
        "roi": (st + 30.0, min(st + 630.0, et)),
    }


def fio_windows(seq_dir: str, seq_id: str) -> Dict[str, Tuple[float, float]]:
    p = _one(os.path.join(seq_dir, "step_0", "*", "*_jobs1_fio.json"))
    if not p:
        return {}
    raw = _load_json(p)
    jobs = raw.get("jobs") or []
    if not jobs:
        return {}
    job = jobs[0]
    js_ms = job.get("job_start")
    if not js_ms:
        return {}
    start = float(js_ms) / 1000.0
    if seq_id == "M1_F6":
        # fio job_start is the POST-startdelay start of the first job
        # (verified on M1_F6 raw data: activity span == [job_start,
        # job_start+90]); the timeline origin (idle start) is 60 s earlier.
        origin = start - 60.0
        return {"burst": (origin, origin + 150.0)}
    # F1..F5: ramp 20 s + measured 180 s
    return {"measured": (start + 20.0, start + 200.0)}


# ------------------------------------------------------------------- helpers

def boot_ns_to_epoch(ns: int, boot_ns0: int, epoch0: int) -> float:
    return epoch0 + (ns - boot_ns0) / 1e9


def slice_dump(dump, boot_ns0, epoch0, ws, we):
    """Slice M1P rows by arrival epoch inside [ws, we]."""
    rows = []
    for (arr, op, nb, e2e, comp, anchor) in dump:
        t = boot_ns_to_epoch(arr, boot_ns0, epoch0)
        if ws <= t < we:
            rows.append((t, op, nb, e2e, comp, anchor))
    return rows


def per_second_counts(sec_map: Dict[int, int], boot_ns0: int, epoch0: int,
                      ws: float, we: float) -> List[int]:
    """Exact per-second completion counts for full seconds inside [ws, we).

    boot key k covers boot time [k*1e9, (k+1)*1e9). A full epoch second
    [s, s+1) maps to boot key k = floor(boot_ns0/1e9) + (s - epoch0) plus a
    sub-second skew (<= 1 s, noted in the report).
    """
    k_off = math.floor(boot_ns0 / 1e9) - epoch0
    s_first = math.ceil(ws)
    s_last = math.floor(we)
    counts = []
    for s in range(s_first, s_last):
        k = s + k_off
        counts.append(sec_map.get(k, 0))
    return counts


def inflight_stats(samples: List[dict], dev: str, ws: float, we: float) -> dict:
    vals = []
    for smp in samples:
        ts = smp.get("timestamp")
        if ts is None or not (ws <= ts <= we):
            continue
        v = ((smp.get("disk") or {}).get("devices") or {}).get(dev, {}).get("ios_in_progress")
        if isinstance(v, (int, float)):
            vals.append(float(v))
    if not vals:
        return {"inflight_mean": None, "inflight_p99": None, "inflight_peak": None,
                "inflight_samples": 0}
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    p99_idx = max(0, min(n - 1, int(math.ceil(0.99 * n)) - 1))
    return {
        "inflight_mean": statistics.fmean(vals),
        "inflight_p99": vals_sorted[p99_idx],
        "inflight_peak": vals_sorted[-1],
        "inflight_samples": n,
    }


def percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def load_system_metric(seq_dir: str) -> List[dict]:
    p = _one(os.path.join(seq_dir, "step_0", "ib_system_metric",
                          "ib_system_metric_summary.json"))
    if not p:
        return []
    return _load_json(p).get("samples") or []


def compute_point(point_id: str, workload: str, phase: str, window,
                  seq_dir: str, sampler: dict, samples: List[dict],
                  meta: dict) -> dict:
    ws, we = window
    win_s = we - ws
    boot_ns0, epoch0 = sampler["boot_ns0"], sampler["epoch0"]
    rows = slice_dump(sampler["dump"], boot_ns0, epoch0, ws, we)

    read_sizes, write_sizes, e2es = [], [], []
    r_bytes = w_bytes = o_bytes = 0
    r_gt64k = w_gt64k = 0
    r_count = w_count = f_count = 0
    for (_t, op, nb, e2e, _comp, _anchor) in rows:
        if op == 0:
            r_count += 1
            r_bytes += nb
            if nb > 65536:
                r_gt64k += nb
            read_sizes.append(nb)
            e2es.append(e2e)
        elif op in (1, 5):
            w_count += 1
            w_bytes += nb
            if nb > 65536:
                w_gt64k += nb
            write_sizes.append(nb)
            e2es.append(e2e)
        elif op == 2:
            f_count += 1
        else:
            o_bytes += nb
    read_sizes.sort()
    write_sizes.sort()
    e2es.sort()

    total_bytes = r_bytes + w_bytes + o_bytes
    gt64k_share = (r_gt64k + w_gt64k) / total_bytes if total_bytes else None
    write_share = w_bytes / total_bytes if total_bytes else None

    sec_map = sampler["sec"].get("m1_sec_comp", {})
    sec_counts = per_second_counts(sec_map, boot_ns0, epoch0, ws, we)
    if len(sec_counts) >= 2:
        mean_iops = statistics.fmean(sec_counts)
        iops_cv = (statistics.pstdev(sec_counts) / mean_iops) if mean_iops > 0 else None
    else:
        mean_iops = None
        iops_cv = None

    # Barrier rate: EXACT per-second flush map (flush sequence requests fire
    # neither insert nor io_start, so they never appear in the dump — see
    # m1_pattern_sampler.bt header; the dump-based estimator is not used).
    flush_map = sampler["sec"].get("m1_sec_flush", {})
    flush_counts = per_second_counts(flush_map, boot_ns0, epoch0, ws, we)
    flush_total_exact = sum(flush_counts)
    flush_per_s = (flush_total_exact / win_s) if flush_counts else 0.0

    sc = sampler["scalars"]
    anchored = sc.get("m1_anchored_count", 0)
    missing = sc.get("m1_anchor_missing_count", 0)
    comp_total = sc.get("m1_complete_count", 0)
    flush_whole = sc.get("m1_flush_issue_count", 0)
    anchor_cov = (anchored / comp_total) if comp_total else None
    # Flush sequence requests structurally carry no anchor; coverage over the
    # anchorable population = anchored / (completes - flushes issued).
    anchor_cov_nonflush = (anchored / (comp_total - flush_whole)) if (comp_total - flush_whole) > 0 else None

    ifl = inflight_stats(samples, "nvme0n1", ws, we)

    # Small-sample guard: suppress direction percentiles when the direction
    # has < MIN_PCT_SAMPLES dump rows (e.g. stray system reads in a pure-write
    # run: 7 rows x 16 = ~112 requests out of 33.8GB writes) — honest NA
    # instead of meaningless percentiles.
    MIN_PCT_SAMPLES = 100

    def pct(vals, p):
        return percentile(vals, p) if len(vals) >= MIN_PCT_SAMPLES else None

    row = {
        "point_id": point_id,
        "workload": workload,
        "phase": phase,
        "window_s": round(win_s, 1),
        "read_p50_bytes": pct(read_sizes, 0.50),
        "read_p90_bytes": pct(read_sizes, 0.90),
        "read_p99_bytes": pct(read_sizes, 0.99),
        "write_p50_bytes": pct(write_sizes, 0.50),
        "write_p90_bytes": pct(write_sizes, 0.90),
        "write_p99_bytes": pct(write_sizes, 0.99),
        "read_req_est": r_count * DUMP_SAMPLE_EVERY,
        "write_req_est": w_count * DUMP_SAMPLE_EVERY,
        "read_dump_rows": r_count,
        "write_dump_rows": w_count,
        "read_bytes_est": r_bytes * DUMP_SAMPLE_EVERY,
        "write_bytes_est": w_bytes * DUMP_SAMPLE_EVERY,
        "gt64k_byte_share": gt64k_share,
        "write_byte_share": write_share,
        "fg_read_byte_share": (r_bytes / total_bytes) if total_bytes else None,
        "inflight_mean": ifl["inflight_mean"],
        "inflight_p99": ifl["inflight_p99"],
        "inflight_peak": ifl["inflight_peak"],
        "inflight_samples": ifl["inflight_samples"],
        "flush_per_s_exact": flush_per_s,
        "flush_window_exact": flush_total_exact,
        "flush_dump_count": f_count,
        "flush_wholerun_count": flush_whole,
        "iops_per_s_mean": mean_iops,
        "iops_cv_sec": iops_cv,
        "e2e_p50_us": (percentile(e2es, 0.50) / 1000.0) if len(e2es) >= MIN_PCT_SAMPLES else None,
        "e2e_p99_us": (percentile(e2es, 0.99) / 1000.0) if len(e2es) >= MIN_PCT_SAMPLES else None,
        "dump_samples": len(rows),
        "anchor_coverage_wholerun": anchor_cov,
        "anchor_cov_nonflush": anchor_cov_nonflush,
        "sampler_lost_lines": sampler["lost_lines"],
        **meta,
    }
    return row


# --------------------------------------------------------------------- main

META = {
    "YCSB_wa": {"sync_model": "closed_16t_adaptive", "direct_meta": "O_DIRECT data files (scene cfg); WAL buffered",
                "class": "real"},
    "YCSB_wc": {"sync_model": "closed_16t_adaptive", "direct_meta": "O_DIRECT data files (scene cfg); WAL buffered",
                "class": "real"},
    "DBB_fillrandom": {"sync_model": "closed_bulk_write", "direct_meta": "O_DIRECT (use_direct_io=true)",
                       "class": "real"},
    "DBB_readrandom": {"sync_model": "closed_4t", "direct_meta": "O_DIRECT (use_direct_io=true)", "class": "real"},
    "DBB_rww": {"sync_model": "closed_4t_read+1writer_bg", "direct_meta": "O_DIRECT (use_direct_io=true)",
                "class": "real"},
    "DBB_rrwr": {"sync_model": "closed_8t_mixed", "direct_meta": "O_DIRECT (use_direct_io=true)", "class": "real"},
    "Shore_qps50": {"sync_model": "open_50qps_fixed_arrival", "direct_meta": "WAL direct; db pages buffered (cfg)",
                    "class": "real"},
    "F1_randread_qd1": {"sync_model": "closed_qd1", "direct_meta": "direct=1 raw device", "class": "fio"},
    "F2_randread_qd256": {"sync_model": "closed_qd256", "direct_meta": "direct=1 raw device", "class": "fio"},
    "F3_seqread_128k_qd32": {"sync_model": "closed_qd32", "direct_meta": "direct=1 raw device", "class": "fio"},
    "F4_randwrite_qd256": {"sync_model": "closed_qd256", "direct_meta": "direct=1 raw device", "class": "fio"},
    "F5_randrw_70_30_qd64": {"sync_model": "closed_qd64", "direct_meta": "direct=1 raw device", "class": "fio"},
    "F6_burst_d4shape": {"sync_model": "burst_60idle+10LC_qd1+80BE_qd256", "direct_meta": "direct=1 raw device",
                         "class": "fio"},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", default="results/motivation_m1/raw")
    ap.add_argument("--out-csv", default="results/motivation_m1/m1_metrics.csv")
    ap.add_argument("--out-json", default="results/motivation_m1/m1_metrics_detail.json")
    args = ap.parse_args()

    rr = args.results_root
    points: List[dict] = []
    problems: List[str] = []

    def sampler_for(seq_dir):
        p = _one(os.path.join(seq_dir, "step_0", "m1_pattern_sampler",
                             "m1_pattern_sampler.bpftrace.log"))
        if not p:
            return None
        return parse_sampler_log(p)

    def add_point(point_id, workload, phase, window, seq_dir, sampler, samples, meta_extra=None):
        if window is None:
            problems.append(f"{point_id}: no window")
            return
        sampler = sampler or {}
        if not sampler.get("epoch0"):
            problems.append(f"{point_id}: sampler anchors missing (bpftrace failed?)")
        meta = dict(META.get(point_id, {}))
        if meta_extra:
            meta.update(meta_extra)
        points.append(compute_point(point_id, workload, phase, window,
                                    seq_dir, sampler, samples, meta))

    # YCSB a / c
    for seq, point, wl in (("M1_YCSB_a", "YCSB_wa", "YCSB workloada (16t, 600s run)"),
                           ("M1_YCSB_c", "YCSB_wc", "YCSB workloadc (16t, 600s run)")):
        seq_dir = os.path.join(rr, seq)
        sm = load_system_metric(seq_dir)
        add_point(point, wl, "run", ycsb_run_window(seq_dir), seq_dir,
                  sampler_for(seq_dir), sm)

    # db_bench chain
    seq = "M1_DBB_chain"
    seq_dir = os.path.join(rr, seq)
    dbb_wins = dbbench_phase_windows(seq_dir)
    dbb_sampler = sampler_for(seq_dir)
    dbb_samples = load_system_metric(seq_dir)
    for phase, point in (("fillrandom", "DBB_fillrandom"),
                         ("readrandom", "DBB_readrandom"),
                         ("readwhilewriting", "DBB_rww"),
                         ("readrandomwriterandom", "DBB_rrwr")):
        add_point(point, f"db_bench {phase}", phase, dbb_wins.get(phase),
                  seq_dir, dbb_sampler, dbb_samples)

    # Shore QPS50
    seq = "M1_SHORE_qps50"
    seq_dir = os.path.join(rr, seq)
    shore_wins = shore_windows(seq_dir)
    shore_sampler = sampler_for(seq_dir)
    shore_samples = load_system_metric(seq_dir)
    add_point("Shore_qps50", "Shore QPS50 open loop", "roi_approx",
              shore_wins.get("roi"), seq_dir, shore_sampler, shore_samples,
              meta_extra={"window_note": "ROI approx [start+30,start+630]"})
    add_point("Shore_qps50_full", "Shore QPS50 open loop", "full_scene",
              shore_wins.get("full"), seq_dir, shore_sampler, shore_samples,
              meta_extra={"window_note": "full scene incl warmup+startup"})

    # fio family
    for seq, point, wl in (
            ("M1_F1", "F1_randread_qd1", "fio randread 4K QD1"),
            ("M1_F2", "F2_randread_qd256", "fio randread 4K QD256"),
            ("M1_F3", "F3_seqread_128k_qd32", "fio seqread 128K QD32"),
            ("M1_F4", "F4_randwrite_qd256", "fio randwrite 4K QD256"),
            ("M1_F5", "F5_randrw_70_30_qd64", "fio randrw 70/30 QD64"),
            ("M1_F6", "F6_burst_d4shape", "fio burst 60s idle+10s LC QD1+80s BE QD256")):
        seq_dir = os.path.join(rr, seq)
        wins = fio_windows(seq_dir, seq) or {}
        key = "burst" if "burst" in wins else "measured"
        add_point(point, wl, key, wins.get(key), seq_dir,
                  sampler_for(seq_dir), load_system_metric(seq_dir))

    # ---- write CSV
    os.makedirs(os.path.dirname(args.out_csv) or ".", exist_ok=True)
    cols = ["point_id", "workload", "phase", "class", "sync_model", "direct_meta",
            "window_s", "window_note",
            "read_p50_bytes", "read_p90_bytes", "read_p99_bytes",
            "write_p50_bytes", "write_p90_bytes", "write_p99_bytes",
            "read_dump_rows", "write_dump_rows", "read_req_est", "write_req_est", "read_bytes_est", "write_bytes_est",
            "gt64k_byte_share", "write_byte_share", "fg_read_byte_share",
            "inflight_mean", "inflight_p99", "inflight_peak", "inflight_samples",
            "flush_per_s_exact", "flush_window_exact", "flush_dump_count", "flush_wholerun_count",
            "iops_per_s_mean", "iops_cv_sec",
            "e2e_p50_us", "e2e_p99_us",
            "dump_samples", "anchor_coverage_wholerun", "anchor_cov_nonflush", "sampler_lost_lines"]
    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in points:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in row.items()})

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump({"points": points, "problems": problems}, f, indent=2,
                  ensure_ascii=False)

    print(f"points: {len(points)}  problems: {len(problems)}")
    for p in problems:
        print("PROBLEM:", p)
    print("wrote:", args.out_csv, "and", args.out_json)
    return 0 if points else 1


if __name__ == "__main__":
    raise SystemExit(main())