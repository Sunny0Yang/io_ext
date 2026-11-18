#!/usr/bin/env python3
"""m1_cache_cases_bm.py -- P0-1 M1 cache campaign aggregation, BAREMETAL-86 build.

Adapted from results/motivation_m1_cache_20261031/tools/m1_cache_cases.py
(kvmhost build). Differences vs the kvmhost original:

  * in-flight / diskstats device is nvme0n1 (bare-metal raw NVMe), configurable via
    --device (default nvme0n1); the kvmhost original hard-coded "sdb".
  * --results-root defaults to experiments/m1m2_bm/runs/m1_campaign. Case dirs
    produced by run_m1_cache_campaign_bm.py live one level deeper
    (<results-root>/<RUN_ID>/<CASE>; cache-ab products in <RUN_ID>/ab/<CASE>),
    so if no case dirs sit directly under --results-root the walker
    auto-discovers <RUN_ID> subdirectories that contain case dirs.
  * output CSV paths / column schema are IDENTICAL to the kvmhost original
    (m1_cache_cases.csv + m1_cache_cases_detail.json); the downstream
    render_m1_1_behavior_profile script depends on that schema.

Walks the extracted case dirs ([ab/]<CASE>/step_0/...) and emits one CSV row
per MEASURED POINT (case x phase):

  case, campaign, workload, phase, repeat, cache_on, use_direct_io, scheduler
  -- sampler (m1_pattern_sampler, 1-in-16 thinned dump; unbiased estimators):
     req size p50/p90/p99 per direction [bytes], read/write byte share,
     >64KiB byte share, flush/s (exact per-second map), per-second IOPS CV,
     inflight mean/p99/peak @1 Hz diskstats, e2e p50/p99 us (anchored),
     dump rows, anchor coverage
  -- diskstats (ib_system_metric 1 Hz): mean read/write MB/s in window
  -- app metrics: YCSB ops/s + READ/UPDATE P50/P99; db_bench ops/s + P50/P99;
     Shore p50/p99 + deadline miss rate + est QPS
  -- quality gates: result_json_ok / sampler_dump_rows / sysmetric_samples /
     gate_ok (single boolean for the plotting stage to filter on)

Parsing logic (sampler log format, windows, percentile math) is reused from
results/motivation_m1/tools/m1_metrics.py (imported, not duplicated); the
in-flight device here defaults to nvme0n1 (bare-metal raw disk), not sdb.

Usage:
  python3 m1_cache_cases_bm.py \
      --results-root experiments/m1m2_bm/runs/m1_campaign/<RUN_ID> \
      --device nvme0n1 \
      --out-csv <results-root>/m1_cache_cases.csv \
      --out-json <results-root>/m1_cache_cases_detail.json
"""

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
from typing import Dict, List, Optional, Tuple

_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "..", "..", "results", "motivation_m1", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))
import m1_metrics as mm  # noqa: E402  (shared parser: parse_sampler_log, windows, math)

CASE_RE = re.compile(
    r"^M1CACHE_(?:AB_)?(?P<wl>YCSB_a|YCSB_c|DBB_rww|DBB_rr|SHORE_qps50)"
    r"(?P<off>_cachefix_off)?_r_?(?P<rep>probe|\d+)$")

WORKLOAD_NAMES = {
    "YCSB_a": "YCSB workloada (50/50 read/update, 16t closed)",
    "YCSB_c": "YCSB workloadc (100% read, 16t closed)",
    "DBB_rww": "db_bench readwhilewriting (4t read + 1 bg writer)",
    "DBB_rr": "db_bench readrandom (4t)",
    "SHORE_qps50": "Shore QPS50 open loop",
}

# db_bench phases that get their own measured row, mapped from sequence kind
DBB_PHASES = {"DBB_rww": ["fillrandom", "readwhilewriting"],
              "DBB_rr": ["fillrandom", "readrandom"]}


def one(pattern: str) -> Optional[str]:
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


def jload(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def scene_result(case_dir: str, prefix: str) -> Optional[dict]:
    p = one(os.path.join(case_dir, "step_0", "*", f"{prefix}_*.json"))
    return jload(p) if p else None


def load_sysmetric(case_dir: str) -> List[dict]:
    p = one(os.path.join(case_dir, "step_0", "ib_system_metric",
                         "ib_system_metric_summary.json"))
    if not p:
        return []
    return jload(p).get("samples") or []


def load_sampler(case_dir: str) -> Optional[dict]:
    p = one(os.path.join(case_dir, "step_0", "m1_pattern_sampler",
                         "m1_pattern_sampler.bpftrace.log"))
    if not p:
        return None
    return mm.parse_sampler_log(p)


def disk_mb_s(samples: List[dict], dev: str, ws: float, we: float) -> Tuple[Optional[float], Optional[float]]:
    """Mean read/write MB/s inside [ws, we] from 1 Hz sector deltas."""
    rs = ws_sum = 0
    n = 0
    for smp in samples:
        ts = smp.get("timestamp")
        if ts is None or not (ws <= ts <= we):
            continue
        d = ((smp.get("disk") or {}).get("devices") or {}).get(dev) or {}
        r, w = d.get("read_sectors_delta"), d.get("write_sectors_delta")
        if isinstance(r, (int, float)) and isinstance(w, (int, float)) and r >= 0 and w >= 0:
            rs += r
            ws_sum += w
            n += 1
    if not n:
        return None, None
    return rs * 512.0 / 1e6 / n, ws_sum * 512.0 / 1e6 / n


def scene_duration_wall(case_dir: str, kind: str) -> Optional[float]:
    prefix = {"YCSB": "ycsb_rocksdb", "DBB": "dbbench", "SHORE": "tailbench_shore"}[kind.split("_")[0]]
    res = scene_result(case_dir, prefix)
    if not res:
        return None
    cd = res.get("collected_data") or res
    st, et = cd.get("start_time"), cd.get("end_time")
    if st and et:
        return float(et) - float(st)
    return cd.get("duration_wall")


def app_metrics(kind: str, res: Optional[dict], phase: str) -> dict:
    out = {}
    if not res:
        return out
    cd = res.get("collected_data") or res
    if kind.startswith("YCSB"):
        lat = cd.get("ycsb_latency_summary") or {}
        out.update({
            "app_ops_per_sec": cd.get("throughput_ops_per_sec"),
            "app_read_p50_us": lat.get("latency_read_p50_us"),
            "app_read_p99_us": lat.get("latency_read_p99_us"),
            "app_read_ops": lat.get("latency_read_ops"),
            "app_update_p50_us": lat.get("latency_update_p50_us"),
            "app_update_p99_us": lat.get("latency_update_p99_us"),
            "app_update_ops": lat.get("latency_update_ops"),
            "app_rc_run": cd.get("run_returncode"),
            "app_rc_load": cd.get("load_returncode"),
        })
    elif kind.startswith("DBB"):
        pr = cd.get("phase_results") or {}
        r = pr.get(phase) or {}
        hist = (r.get("histograms") or {}).get("read") or {}
        out.update({
            "app_ops_per_sec": r.get("ops_per_sec"),
            "app_p50_us": hist.get("p50_us") or r.get("micros_per_op"),
            "app_p99_us": hist.get("p99_us"),
            "app_operations": r.get("operations"),
            "app_mb_per_sec": r.get("mb_per_sec"),
            "app_rc": r.get("returncode"),
        })
        if phase == "readrandom":
            out["app_found"] = r.get("found")
            out["app_found_of"] = r.get("found_of")
    elif kind.startswith("SHORE"):
        out.update({
            "app_p50_us": cd.get("p50_latency_us"),
            "app_p99_us": cd.get("p99_latency_us"),
            "app_deadline_miss_rate": cd.get("deadline_miss_rate"),
            "app_latency_count": cd.get("latency_count"),
            "app_rc": cd.get("returncode"),
            "app_duration_wall": cd.get("duration_wall"),
        })
        if cd.get("latency_count") and cd.get("duration_wall"):
            out["app_qps_est"] = cd["latency_count"] / cd["duration_wall"]
    return out


def compute_point(case: str, campaign: str, kind: str, phase: str, rep: str,
                  cache_on: int, window: Tuple[float, float], case_dir: str,
                  use_direct_io, scheduler: str, device: str = "nvme0n1") -> Tuple[dict, List[str]]:
    problems: List[str] = []
    ws, we = window
    win_s = we - ws

    res = scene_result(case_dir, {"YCSB": "ycsb_rocksdb", "DBB": "dbbench",
                                  "SHORE": "tailbench_shore"}[kind.split("_")[0]])
    samples = load_sysmetric(case_dir)
    sampler = load_sampler(case_dir)

    gates = {
        "result_json_ok": bool(res),
        "sysmetric_samples": len(samples),
        "sampler_log_present": bool(sampler),
    }
    if not res:
        problems.append(f"{case}/{phase}: no scene result json")
    if not samples:
        problems.append(f"{case}/{phase}: no ib_system_metric samples")
    if not sampler:
        problems.append(f"{case}/{phase}: no sampler log")

    row = {
        "case": case, "campaign": campaign,
        "workload": WORKLOAD_NAMES[kind], "kind": kind, "phase": phase,
        "repeat": rep, "cache_on": cache_on,
        "use_direct_io": use_direct_io if use_direct_io is not None else "",
        "scheduler": scheduler,
        "window_start": ws, "window_end": we, "window_s": round(win_s, 1),
    }
    row.update(app_metrics(kind, res, phase))

    # in-flight / diskstats-derived columns come from ib_system_metric (1 Hz)
    # and are INDEPENDENT of the sampler -- they must also populate the A/B
    # arm (which intentionally runs without m1_pattern_sampler).
    ifl = mm.inflight_stats(samples, device, ws, we)
    row.update({
        "inflight_mean": ifl["inflight_mean"],
        "inflight_p99": ifl["inflight_p99"],
        "inflight_peak": ifl["inflight_peak"],
    })

    # sampler-based pattern metrics (only over the measured window)
    if sampler and sampler.get("epoch0") and sampler.get("boot_ns0") is not None:
        rows = mm.slice_dump(sampler["dump"], sampler["boot_ns0"], sampler["epoch0"], ws, we)
        read_sizes, write_sizes, e2es = [], [], []
        r_bytes = w_bytes = o_bytes = 0
        r_gt = w_gt = 0
        r_c = w_c = f_c = 0
        for (_t, op, nb, e2e, _comp, _anchor) in rows:
            if op == 0:
                r_c += 1; r_bytes += nb
                if nb > 65536: r_gt += nb
                read_sizes.append(nb); e2es.append(e2e)
            elif op in (1, 5):
                w_c += 1; w_bytes += nb
                if nb > 65536: w_gt += nb
                write_sizes.append(nb); e2es.append(e2e)
            elif op == 2:
                f_c += 1
            else:
                o_bytes += nb
        read_sizes.sort(); write_sizes.sort(); e2es.sort()
        total_bytes = r_bytes + w_bytes + o_bytes
        sec_map = sampler["sec"].get("m1_sec_comp", {})
        sec_counts = mm.per_second_counts(sec_map, sampler["boot_ns0"], sampler["epoch0"], ws, we)
        if len(sec_counts) >= 2:
            mean_iops = statistics.fmean(sec_counts)
            iops_cv = (statistics.pstdev(sec_counts) / mean_iops) if mean_iops > 0 else None
        else:
            mean_iops = iops_cv = None
        flush_counts = mm.per_second_counts(sampler["sec"].get("m1_sec_flush", {}),
                                            sampler["boot_ns0"], sampler["epoch0"], ws, we)
        flush_total = sum(flush_counts)
        # @m1_sec_flush can be lost when the scene kills bpftrace mid
        # exit-auto-print; the whole-run scalar @m1_flush_issue_count usually
        # survives. Emit an honest whole-scene estimate instead of silently
        # reporting 0 (same behavior as the kvmhost original).
        flush_whole = sampler["scalars"].get("m1_flush_issue_count", 0)
        scene_wall = scene_duration_wall(case_dir, kind)
        # bm 的 .bt（assets 最新版）产出 @m1_sec_flush 逐秒 map；kvmhost 老 .bt
        # 没有（只有全场景 scalar）。flush/s 一律优先测量窗 exact（逐秒 map），
        # 老 .bt 才回退 scalar/scene_wall 老口径 —— 两代 .bt 同列语义统一。
        flush_scene_est = (
            (flush_total / win_s) if (flush_counts and win_s) else
            (flush_whole / scene_wall) if (scene_wall and "m1_sec_flush" not in sampler["sec"]) else None)
        sc = sampler["scalars"]
        comp_total = sc.get("m1_complete_count", 0)
        anchored = sc.get("m1_anchored_count", 0)
        flush_whole = sc.get("m1_flush_issue_count", 0)

        MIN_PCT = 100  # honest NA below this (m1_metrics.py convention)

        def pct(vals, p):
            return mm.percentile(vals, p) if len(vals) >= MIN_PCT else None

        row.update({
            "read_p50_bytes": pct(read_sizes, 0.50),
            "read_p90_bytes": pct(read_sizes, 0.90),
            "read_p99_bytes": pct(read_sizes, 0.99),
            "write_p50_bytes": pct(write_sizes, 0.50),
            "write_p90_bytes": pct(write_sizes, 0.90),
            "write_p99_bytes": pct(write_sizes, 0.99),
            "read_dump_rows": r_c, "write_dump_rows": w_c,
            "read_req_est": r_c * mm.DUMP_SAMPLE_EVERY,
            "write_req_est": w_c * mm.DUMP_SAMPLE_EVERY,
            "read_bytes_est": r_bytes * mm.DUMP_SAMPLE_EVERY,
            "write_bytes_est": w_bytes * mm.DUMP_SAMPLE_EVERY,
            "gt64k_byte_share": ((r_gt + w_gt) / total_bytes) if total_bytes else None,
            "write_byte_share": (w_bytes / total_bytes) if total_bytes else None,
            "fg_read_byte_share": (r_bytes / total_bytes) if total_bytes else None,
            "flush_per_s_exact": (flush_total / win_s) if flush_counts else None,
            "flush_window_exact": flush_total,
            "flush_wholerun_count": flush_whole,
            "flush_per_s_scene_est": flush_scene_est,
            "iops_per_s_mean": mean_iops,
            "iops_cv_sec": iops_cv,
            "e2e_p50_us": (mm.percentile(e2es, 0.50) / 1000.0) if len(e2es) >= MIN_PCT else None,
            "e2e_p99_us": (mm.percentile(e2es, 0.99) / 1000.0) if len(e2es) >= MIN_PCT else None,
            "dump_samples": len(rows),
            "anchor_coverage_wholerun": (anchored / comp_total) if comp_total else None,
            "sampler_lost_lines": sampler["lost_lines"],
        })
        gates["sampler_dump_rows"] = len(rows)
        gates["anchor_coverage_wholerun"] = row["anchor_coverage_wholerun"]
        if len(rows) == 0:
            problems.append(f"{case}/{phase}: sampler dump empty in window")
    else:
        for k in ("read_p50_bytes", "write_p99_bytes", "iops_cv_sec", "flush_per_s_exact"):
            row[k] = None

    rmb, wmb = disk_mb_s(samples, device, ws, we)
    row["disk_read_mb_s"] = rmb
    row["disk_write_mb_s"] = wmb

    # ---- quality gate (per task spec) ----
    gate_ok = bool(gates.get("result_json_ok")) and gates["sysmetric_samples"] > 0
    if kind.startswith("SHORE"):
        gate_ok = gate_ok and bool(row.get("app_p99_us")) and (row.get("app_rc") == 0)
    elif kind.startswith("YCSB"):
        gate_ok = gate_ok and bool(row.get("app_ops_per_sec")) and bool(row.get("app_read_p99_us"))
    elif kind.startswith("DBB"):
        gate_ok = gate_ok and bool(row.get("app_ops_per_sec")) and (row.get("app_rc") == 0)
    # sampler presence is required for pattern columns; A/B arm has no sampler
    # by design (m1_cache_ab.yml: monitoring = ib_system_metric only), so it does
    # not fail the gate -- recorded in sampler_log_present instead.
    if campaign == "on":
        gate_ok = gate_ok and bool(sampler) and gates.get("sampler_dump_rows", 0) > 0
    row.update({
        "sampler_dump_rows_gate": gates.get("sampler_dump_rows", ""),
        "sysmetric_samples": gates["sysmetric_samples"],
        "sampler_log_present": gates["sampler_log_present"],
        "gate_ok": gate_ok,
    })
    if not gate_ok:
        problems.append(f"{case}/{phase}: GATE FAILED")
    return row, problems


def discover_analysis_roots(rr: str, quiet: bool = False) -> List[str]:
    """Return case-holding results roots under rr.

    Case dirs sit at <rr>/<CASE> (kvmhost layout) or <rr>/<RUN_ID>/<CASE>
    (bare-metal layout: run_m1_cache_campaign_bm.py writes one run dir per RUN_ID).
    If rr itself holds case dirs, return [rr]; otherwise return every direct
    subdirectory that holds case dirs (sorted, newest last).
    """
    def has_cases(d: str) -> bool:
        if not os.path.isdir(d):
            return False
        return any(os.path.isdir(os.path.join(d, e)) and CASE_RE.match(e)
                   for e in os.listdir(d))

    if has_cases(rr):
        return [rr]
    subs = sorted(glob.glob(os.path.join(rr, "*")))
    roots = [d for d in subs if os.path.isdir(d) and has_cases(d)]
    if roots and not quiet:
        print("results-root holds no case dirs directly; "
              f"auto-discovered RUN_ID dirs: "
              f"{[os.path.relpath(d, rr) for d in roots]}")
    return roots


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", default=None,
                    help="m1 cache campaign results root (default: "
                         "experiments/m1m2_bm/runs/m1_campaign; if it holds no "
                         "case dirs, <RUN_ID> subdirs with case dirs are "
                         "auto-discovered)")
    ap.add_argument("--device", default="nvme0n1",
                    help="diskstats/inflight device as keyed in "
                         "ib_system_metric samples (bare-metal: nvme0n1, kvmhost: sdb)")
    ap.add_argument("--out-csv", default=None)
    ap.add_argument("--out-json", default=None)
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    default_rr = os.path.join(here, "..", "runs", "m1_campaign")
    rr = args.results_root or os.path.normpath(default_rr)
    out_csv = args.out_csv or os.path.join(rr, "m1_cache_cases.csv")
    out_json = args.out_json or os.path.join(rr, "m1_cache_cases_detail.json")
    dev = args.device

    points: List[dict] = []
    problems: List[str] = []
    case_dirs = []
    roots = discover_analysis_roots(rr)
    if not roots:
        print(f"ERROR: no M1CACHE_* case dirs under {rr} (directly or in "
              f"<RUN_ID> subdirs); has the campaign data landed?", file=sys.stderr)
        return 2
    for root in roots:
        for root_main in (root, os.path.join(root, "ab")):
            if not os.path.isdir(root_main):
                continue
            for entry in sorted(os.listdir(root_main)):
                p = os.path.join(root_main, entry)
                if os.path.isdir(p) and (m := CASE_RE.match(entry)):
                    # tag from the CASE NAME (M1CACHE_AB_*_cachefix_off_* = ab),
                    # not from the directory level -- passing a RUN_ID dir or its
                    # /ab subdir as --results-root used to mislabel ab cases "on"
                    # and fail every gate on the (by-design missing) sampler.
                    case_dirs.append((p, entry, "ab" if m.group("off") else "on"))

    for case_dir, case, campaign in case_dirs:
        m = CASE_RE.match(case)
        kind, off, rep = m.group("wl"), m.group("off"), m.group("rep")
        cache_on = 0 if off else 1

        run_res_p = one(os.path.join(case_dir, "run_results.json"))
        use_direct_io = scheduler = None
        if run_res_p:
            rrd = jload(run_res_p)
            s = list(rrd.values())[0]
            failed = s.get("failed_scenes")
            if failed:
                problems.append(f"{case}: failed_scenes={failed}")
            try:
                sres = list(s["steps"][0]["results"].values())[0]
                use_direct_io = sres.get("use_direct_io")
                scheduler = sres.get("scheduler")
            except (KeyError, IndexError, TypeError):
                pass

        if kind.startswith("YCSB"):
            win = mm.ycsb_run_window(case_dir)
            if win is None:
                problems.append(f"{case}: no ycsb run window")
                continue
            row, prob = compute_point(case, campaign, kind, "run", rep, cache_on,
                                      win, case_dir, use_direct_io, scheduler, dev)
            points.append(row); problems += prob
        elif kind.startswith("DBB"):
            wins = mm.dbbench_phase_windows(case_dir)
            if not wins:
                problems.append(f"{case}: no dbbench phase windows")
                continue
            for ph in DBB_PHASES[kind]:
                if ph not in wins:
                    problems.append(f"{case}: missing window for {ph}")
                    continue
                row, prob = compute_point(case, campaign, kind, ph, rep, cache_on,
                                          wins[ph], case_dir, use_direct_io, scheduler, dev)
                points.append(row); problems += prob
        else:  # SHORE
            wins = mm.shore_windows(case_dir)
            if not wins.get("roi"):
                problems.append(f"{case}: no shore windows")
                continue
            row, prob = compute_point(case, campaign, kind, "roi", rep, cache_on,
                                      wins["roi"], case_dir, use_direct_io, scheduler, dev)
            row["window_note"] = "ROI approx [start+30, start+630]"
            points.append(row); problems += prob

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    base_cols = ["case", "campaign", "workload", "kind", "phase", "repeat", "cache_on",
                 "use_direct_io", "scheduler", "window_start", "window_end", "window_s",
                 "window_note",
                 "read_p50_bytes", "read_p90_bytes", "read_p99_bytes",
                 "write_p50_bytes", "write_p90_bytes", "write_p99_bytes",
                 "read_dump_rows", "write_dump_rows", "read_req_est", "write_req_est",
                 "read_bytes_est", "write_bytes_est",
                 "gt64k_byte_share", "write_byte_share", "fg_read_byte_share",
                 "flush_per_s_exact", "flush_window_exact",
                 "flush_wholerun_count", "flush_per_s_scene_est",
                 "iops_per_s_mean", "iops_cv_sec",
                 "inflight_mean", "inflight_p99", "inflight_peak",
                 "e2e_p50_us", "e2e_p99_us", "dump_samples",
                 "anchor_coverage_wholerun", "sampler_lost_lines",
                 "disk_read_mb_s", "disk_write_mb_s",
                 "app_ops_per_sec", "app_read_p50_us", "app_read_p99_us", "app_read_ops",
                 "app_update_p50_us", "app_update_p99_us", "app_update_ops",
                 "app_p50_us", "app_p99_us", "app_operations", "app_mb_per_sec",
                 "app_found", "app_found_of", "app_deadline_miss_rate",
                 "app_latency_count", "app_qps_est", "app_duration_wall",
                 "app_rc", "app_rc_run", "app_rc_load",
                 "sampler_dump_rows_gate", "sysmetric_samples", "sampler_log_present",
                 "gate_ok"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base_cols, extrasaction="ignore")
        w.writeheader()
        for row in points:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in row.items()})
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"points": points, "problems": problems}, f, indent=2, ensure_ascii=False)

    ncases = len({p["case"] for p in points})
    nbad = len({p["case"] for p in points if not p["gate_ok"]})
    print(f"points: {len(points)} ({ncases} cases, {nbad} cases failing gate)  "
          f"problems: {len(problems)}")
    for p in problems:
        print("PROBLEM:", p)
    print("wrote:", out_csv, "and", out_json)
    return 0 if points else 1


if __name__ == "__main__":
    raise SystemExit(main())
