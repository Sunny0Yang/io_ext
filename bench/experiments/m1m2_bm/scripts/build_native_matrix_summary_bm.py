#!/usr/bin/env python3
"""Build per-case + summary CSVs for the m1matrix (native scheduler x real-app,
cache=on) campaign. kvmhost original: /tmp/build_native_matrix_summary.py (run
under results/motivation_native_matrix_cacheon_20261031/); this bm build is for
the baremetal-86 campaign written by run_m1_cache_campaign_bm.py under
experiments/m1m2_bm/runs/m1_campaign/<RUN_ID>/.

The parser is device-agnostic: it reads run_results.json + step_0 scene result
JSONs only (no diskstats), so the only 86-facing change vs the kvmhost original
is this header. Case dir naming, CSV schemas and the quality gate are IDENTICAL
to the kvmhost original (native_matrix_cases.csv / native_matrix_summary.csv);
render_m1_2_policy_conflict depends on that schema. The optional --sampler-log
keeps the kvmhost line format ("<control_ts> <guest_ts> | [scheduler]" lines).

Usage:
  python3 -I build_native_matrix_summary_bm.py \
      experiments/m1m2_bm/runs/m1_campaign/<RUN_ID> [--sampler-log LOG]

Outputs (inside <results_dir>):
  native_matrix_cases.csv     one row per (case, repeat)
  native_matrix_summary.csv   mean +- sd per (scenario, scheduler)
Also prints a quality-gate table.
"""
import argparse
import csv
import glob
import json
import math
import re
import sys
from pathlib import Path

ARMS = ["none", "kyber", "mq_deadline", "bfq"]
ARM_LABEL = {"none": "none", "kyber": "kyber", "mq_deadline": "mq-deadline", "bfq": "bfq"}

CASE_RE = re.compile(
    r"^M1MATRIX_(YCSB_A|YCSB_C|DBB_RWW|SHORE_QPS50)_(NONE|KYBER|MQ_DEADLINE|BFQ)_r(\d+|r_probe)$"
)
SCEN_LABEL = {
    "YCSB_A": "YCSB-A",
    "YCSB_C": "YCSB-C",
    "DBB_RWW": "db_bench-rww",
    "SHORE_QPS50": "Shore-QPS50",
}


def one(pattern):
    hits = sorted(glob.glob(pattern))
    return hits[0] if hits else None


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"_parse_error": str(exc)}


def parse_case(case_dir: Path):
    """Return dict of per-case KPIs for whichever app this case ran."""
    name = case_dir.name
    m = CASE_RE.match(name)
    if not m:
        return None
    scen, arm, rep = m.group(1), m.group(2), m.group(3)
    arm = arm.lower()
    row = {
        "case": name,
        "scenario": SCEN_LABEL[scen],
        "scheduler": ARM_LABEL[arm],
        "repeat": rep,
        "gate_ok": True,
        "gate_notes": "",
    }
    notes = []

    def gate(cond, msg):
        if not cond:
            row["gate_ok"] = False
            notes.append(msg)

    # ---- run_results.json: configured scheduler + sequence identity ----
    rr_path = case_dir / "run_results.json"
    rr = load_json(rr_path)
    gate("_parse_error" not in rr, f"run_results.json:{rr.get('_parse_error')}")
    configured_sched = None
    seq_id = None
    drop_caches = None
    start_time = end_time = None
    if "_parse_error" not in rr:
        for _seq_key, seq_block in rr.items():
            if not isinstance(seq_block, dict):
                continue
            sc = seq_block.get("sequence_config") or {}
            seq_id = sc.get("sequence_id", _seq_key)
            steps = seq_block.get("steps") or []
            if steps:
                for scene_name, res in (steps[0].get("results") or {}).items():
                    if isinstance(res, dict) and "scheduler" in res:
                        configured_sched = res.get("scheduler")
                        drop_caches = res.get("drop_caches_before_run")
                        start_time = res.get("start_time")
                        end_time = res.get("end_time")
        gate(configured_sched == ARM_LABEL[arm],
             f"scheduler-config-mismatch cfg={configured_sched}")

    # ---- app-specific result JSON ----
    step0 = case_dir / "step_0"
    if scen in ("YCSB_A", "YCSB_C"):
        p = one(str(step0 / "*" / "ycsb_rocksdb_*.json"))
        gate(p is not None, "no ycsb result json")
        if p:
            d = load_json(p)
            gate("_parse_error" not in d, f"ycsb json:{d.get('_parse_error')}")
            ls = d.get("ycsb_latency_summary") or {}
            row.update({
                "kpi_ops_per_sec": d.get("throughput_ops_per_sec"),
                "kpi_read_p99_us": ls.get("latency_read_p99_us"),
                "kpi_update_p99_us": ls.get("latency_update_p99_us"),
                "kpi_read_p50_us": ls.get("latency_read_p50_us"),
                "kpi_update_p50_us": ls.get("latency_update_p50_us"),
                "kpi_read_ops": ls.get("latency_read_ops"),
                "kpi_update_ops": ls.get("latency_update_ops"),
                "kpi_runtime_s": (ls.get("overall_RunTime(ms)") or 0) / 1000.0,
                "rc_load": d.get("load_returncode"),
                "rc_run": d.get("run_returncode"),
                "use_direct_io": d.get("use_direct_io"),
                "drop_caches_before_run": d.get("drop_caches_before_run"),
                "scheduler_configured": d.get("scheduler"),
                "app_start_time": d.get("start_time"),
                "app_end_time": d.get("end_time"),
            })
            gate(d.get("run_returncode") == 0 and d.get("load_returncode") == 0,
                 f"ycsb rc load={d.get('load_returncode')} run={d.get('run_returncode')}")
            gate(ls.get("latency_read_p99_us") is not None, "no READ p99")
            if scen == "YCSB_A":
                # workloadc (YCSB-C) is 100% read: UPDATE p99 is empty by design,
                # not a missing metric -- never gate on it there.
                gate(ls.get("latency_update_p99_us") is not None, "no UPDATE p99")
            gate(d.get("throughput_ops_per_sec") is not None, "no ops/s")
            gate(d.get("use_direct_io") is False, "use_direct_io is not False")
            gate(d.get("drop_caches_before_run") is True, "drop_caches not true")
    elif scen == "DBB_RWW":
        p = one(str(step0 / "*" / "dbbench_*.json"))
        gate(p is not None, "no dbbench result json")
        if p:
            d = load_json(p)
            gate("_parse_error" not in d, f"dbbench json:{d.get('_parse_error')}")
            pr = d.get("phase_results") or {}
            fill = pr.get("fillrandom") or {}
            rww = pr.get("readwhilewriting") or {}
            read_hist = (rww.get("histograms") or {}).get("read") or {}
            row.update({
                "kpi_fg_read_p99_us": read_hist.get("p99_us"),
                "kpi_fg_ops_per_sec": rww.get("ops_per_sec"),
                "kpi_bg_write_mib_s": rww.get("mb_per_sec"),
                "kpi_fg_read_p50_us": read_hist.get("p50_us"),
                "kpi_fg_found": rww.get("found"),
                "kpi_fg_found_of": rww.get("found_of"),
                "kpi_fill_ops_per_sec": fill.get("ops_per_sec"),
                "kpi_fill_mib_s": fill.get("mb_per_sec"),
                "rc_rww": rww.get("returncode"),
                "rc_fill": fill.get("returncode"),
                "use_direct_io": d.get("use_direct_io"),
                "drop_caches_before_run": d.get("drop_caches_before_run"),
                "scheduler_configured": d.get("scheduler"),
                "app_start_time": d.get("start_time"),
                "app_end_time": d.get("end_time"),
            })
            gate(rww.get("returncode") == 0, f"rww rc={rww.get('returncode')}")
            gate(fill.get("returncode") == 0, f"fill rc={fill.get('returncode')}")
            gate(read_hist.get("p99_us") is not None, "no fg READ p99")
            gate(rww.get("ops_per_sec") is not None, "no fg ops/s")
            gate(rww.get("mb_per_sec") is not None, "no bg write rate")
    else:  # SHORE_QPS50
        p = one(str(step0 / "*" / "tailbench_shore_*.json"))
        gate(p is not None, "no shore result json")
        if p:
            d = load_json(p)
            gate("_parse_error" not in d, f"shore json:{d.get('_parse_error')}")
            lat_count = d.get("latency_count")
            wall = d.get("duration_wall")
            row.update({
                "kpi_p99_us": d.get("p99_latency_us"),
                "kpi_p50_us": d.get("p50_latency_us"),
                "kpi_p95_us": d.get("p95_latency_us"),
                "kpi_deadline_miss_rate": d.get("deadline_miss_rate"),
                "kpi_deadline_miss_count": d.get("deadline_miss_count"),
                "kpi_latency_count": lat_count,
                "kpi_duration_wall_s": wall,
                "kpi_qps_est": (lat_count / wall) if (lat_count and wall) else None,
                "rc": d.get("returncode"),
                "drop_caches_before_run": d.get("drop_caches_before_run"),
                "scheduler_configured": d.get("scheduler"),
                "slo_deadline_us": d.get("slo_deadline_us"),
                "app_start_time": d.get("start_time"),
                "app_end_time": d.get("end_time"),
            })
            gate(d.get("returncode") == 0, f"shore rc={d.get('returncode')}")
            gate(d.get("p99_latency_us") is not None, "no p99 latency")
            gate(d.get("deadline_miss_rate") is not None, "no deadline miss rate")
            gate(lat_count == 30000, f"latency_count={lat_count} (expect 30000)")

    # ---- sysfs sampler cross-check ----
    if start_time and end_time:
        row["app_start_time"] = start_time
        row["app_end_time"] = end_time

    row["gate_notes"] = "; ".join(notes)
    return row


def annotate_sampler(rows, sampler_log):
    """Add scheduler_sysfs_samples / scheduler_sysfs_ok from the sampler log.

    Sampler line format: "<control_ts> <guest_ts> | <sysfs value>" (guest clock
    is skewed vs the control host, so windows in guest epoch are matched with
    the guest_ts column). Legacy "<ts> | <value>" lines are treated as guest
    epoch only if they were recorded that way -- we accept both and note it.
    """
    samples = []
    try:
        with open(sampler_log) as fh:
            for line in fh:
                try:
                    left, val = line.strip().split(" | ", 1)
                except ValueError:
                    continue
                if val == "SSH_FAIL" or not val:
                    continue
                act = re.search(r"\[([^\]]+)\]", val)
                if not act:
                    continue
                parts = left.split()
                if len(parts) < 2:
                    continue  # legacy one-clock line (control epoch) -- skip
                ts = float(parts[1])
                samples.append((ts, act.group(1)))
    except FileNotFoundError:
        for r in rows:
            r["scheduler_sysfs_ok"] = ""
            r["scheduler_sysfs_samples"] = "sampler-log-missing"
        return
    for r in rows:
        st, en = r.get("app_start_time"), r.get("app_end_time")
        if not st or not en:
            r["scheduler_sysfs_ok"] = ""
            r["scheduler_sysfs_samples"] = "no-window"
            continue
        win = [s for t, s in samples if t >= st - 5 and t <= en + 5]
        if not win:
            r["scheduler_sysfs_ok"] = ""
            r["scheduler_sysfs_samples"] = "no-samples"
            continue
        counts = {}
        for s in win:
            counts[s] = counts.get(s, 0) + 1
        modal = max(counts, key=counts.get)
        r["scheduler_sysfs_samples"] = ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        r["scheduler_sysfs_ok"] = str(modal == r["scheduler"]).lower()


def summarize(rows):
    KPI_BY_SCEN = {
        "YCSB-A": ["kpi_read_p99_us", "kpi_update_p99_us", "kpi_ops_per_sec"],
        "YCSB-C": ["kpi_read_p99_us", "kpi_read_p50_us", "kpi_ops_per_sec"],
        "db_bench-rww": ["kpi_fg_read_p99_us", "kpi_fg_ops_per_sec", "kpi_bg_write_mib_s"],
        "Shore-QPS50": ["kpi_p99_us", "kpi_deadline_miss_rate", "kpi_qps_est", "kpi_p50_us"],
    }
    out = []
    scens = sorted({r["scenario"] for r in rows})
    for scen in scens:
        for arm in ARMS:
            sched = ARM_LABEL[arm]
            sel = [r for r in rows if r["scenario"] == scen and r["scheduler"] == sched]
            if not sel:
                continue
            ok = [r for r in sel if r.get("gate_ok")]
            rec = {
                "scenario": scen,
                "scheduler": sched,
                "n_total": len(sel),
                "n_gate_ok": len(ok),
            }
            for kpi in KPI_BY_SCEN[scen]:
                vals = [r.get(kpi) for r in ok
                        if isinstance(r.get(kpi), (int, float)) and not isinstance(r.get(kpi), bool)]
                if vals:
                    mean = sum(vals) / len(vals)
                    sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 if len(vals) > 1 else 0.0
                    rec[kpi + "_mean"] = round(mean, 3)
                    rec[kpi + "_sd"] = round(sd, 3)
                    rec[kpi + "_min"] = round(min(vals), 3)
                    rec[kpi + "_max"] = round(max(vals), 3)
                else:
                    rec[kpi + "_mean"] = ""
                    rec[kpi + "_sd"] = ""
            out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--sampler-log", default="")
    args = ap.parse_args()
    root = Path(args.results_dir)

    case_dirs = sorted(p for p in root.glob("M1MATRIX_*")
                       if p.is_dir() and CASE_RE.match(p.name))
    if not case_dirs:
        print(f"no M1MATRIX_* case dirs under {root}", file=sys.stderr)
        return 2
    rows = [r for r in (parse_case(p) for p in case_dirs) if r]

    if args.sampler_log:
        annotate_sampler(rows, args.sampler_log)
    else:
        for r in rows:
            r["scheduler_sysfs_ok"] = ""
            r["scheduler_sysfs_samples"] = ""

    # ---- cases csv ----
    all_keys = []
    for r in rows:
        for k in r:
            if k not in all_keys:
                all_keys.append(k)
    cases_path = root / "native_matrix_cases.csv"
    with open(cases_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=all_keys)
        w.writeheader()
        w.writerows(rows)

    # ---- summary csv ----
    summ = summarize(rows)
    summ_path = root / "native_matrix_summary.csv"
    if summ:
        sk = []
        for r in summ:
            for k in r:
                if k not in sk:
                    sk.append(k)
        with open(summ_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=sk)
            w.writeheader()
            w.writerows(summ)

    # ---- quality gate printout ----
    print(f"cases parsed: {len(rows)}  -> {cases_path}")
    for r in rows:
        status = "OK " if r["gate_ok"] else "FAIL"
        sys_ok = r.get("scheduler_sysfs_ok", "")
        print(f"[{status}] {r['case']:38s} sched={r['scheduler']:12s} "
              f"sysfs_ok={sys_ok:5s} {r['gate_notes']}")
    if summ:
        print(f"\nsummary -> {summ_path}")
        for r in summ:
            print(r)
    bad = [r["case"] for r in rows if not r["gate_ok"]]
    print(f"\ngate failures: {len(bad)}{': ' + ','.join(bad) if bad else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
