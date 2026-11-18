#!/usr/bin/env python3
"""Build the RQ1 real-app campaign CSVs (Phase D, fig.4 input contract).

Per scenario directory (results/rq1_realapps_<scn>/) this produces:
  rq1_<scn>_cases.csv    one row per case: scenario, arm, repeat, run_dir, rc,
                         gate_ok + app KPI columns (exact column names below).
  rq1_<scn>_summary.csv  arm x metric: mean, sd, min, max, n.

KPI mapping (from the extracted case tars):
  ycsb    scene JSON ycsb_rocksdb_*.json:
            latency_read_p{50,95,99}_us -> read_p*_us
            latency_update_p{50,95,99}_us -> update_p*_us
            overall_Throughput(ops/sec) -> ops_per_sec
  dbbench scene JSON dbbench_*.json (readwhilewriting phase):
            histograms['read'].p50/p99_us -> fg_read_p50/p99_us
            p95 interpolated from the histogram bucket table in the
            *_readwhilewriting.log (db_bench prints P50/75/99/99.9/99.99 only)
            ops_per_sec -> fg_ops_per_sec
            io_stat_snapshots delta wbytes over the rww window / seconds
            -> bg_write_mibs (estimated, buffered writeback)
  shore   scene JSON tailbench_shore_*.json:
            p{50,95,99}_latency_us -> lat_p*_us
            deadline_miss_rate -> deadline_miss_rate
            latency_count / duration_wall -> qps_est

gate_ok = app rc==0 AND KPIs parseable AND sidecar arm-state samples
consistent over the whole case window. Sidecar source: the single
--sidecar-log file when given (whole-scenario sampler log, older layout);
otherwise the per-case snapshot <case_dir>.sidecar.log stored next to the
case dir by scripts/rq1_drive_campaign.py (case-granular layout). With
neither source present the "samples" check FAILS with a reason in the gate
detail -- it never silently passes.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Sequence-id substring -> result arm label. Order matters: specific patterns
# before the generic NONE. The RQ1D_* sequences (cache=off direct-I/O caliber,
# configs rig_rq1_directio/) reuse the same token set, so they map identically.
ARM_FROM_SEQ = [
    ("IOX_ARB_Q", "iox_arb_q"),
    ("IOX_Q_DDL", "iox_q_only"),
    ("IO_COST", "io_cost"),
    ("MQ_DEADLINE", "mq_deadline"),
    ("KYBER", "kyber"),
    ("BFQ", "bfq"),
    ("NONE", "none"),
]

ARM_ORDER = ["none", "kyber", "mq_deadline", "bfq", "io_cost", "iox_q_only", "iox_arb_q"]

COLUMNS = {
    "ycsb": ["scenario", "arm", "repeat", "run_dir", "rc", "gate_ok",
             "read_p50_us", "read_p95_us", "read_p99_us",
             "update_p50_us", "update_p95_us", "update_p99_us", "ops_per_sec"],
    "dbbench": ["scenario", "arm", "repeat", "run_dir", "rc", "gate_ok",
                "fg_read_p50_us", "fg_read_p95_us", "fg_read_p99_us",
                "fg_ops_per_sec", "bg_write_mibs"],
    "shore": ["scenario", "arm", "repeat", "run_dir", "rc", "gate_ok",
              "lat_p50_us", "lat_p95_us", "lat_p99_us",
              "deadline_miss_rate", "qps_est"],
}


def parse_case_name(case: str) -> tuple:
    m = re.match(r"^(.+)_r(\d+)$", case)
    if not m:
        return None, None
    seq, rep = m.group(1), int(m.group(2))
    for pat, arm in ARM_FROM_SEQ:
        if pat in seq:
            return arm, rep
    return None, rep


def find_scene_json(case_dir: Path, scenario: str) -> Path | None:
    pattern = {"ycsb": "ycsb_rocksdb_*.json",
               "dbbench": "dbbench_*.json",
               "shore": "tailbench_shore_*.json"}[scenario]
    hits = sorted(case_dir.glob(f"scene/*/{pattern}"))
    return hits[0] if hits else None


def parse_hist_buckets_p95(log_path: Path) -> float | None:
    """Interpolate P95 from the db_bench 'Microseconds per read' bucket table."""
    if not log_path.is_file():
        return None
    text = log_path.read_text(errors="replace")
    m = re.search(r"Microseconds per read:\s*\n(.*?)(?:\n\s*\n-----|\Z)",
                  text, re.DOTALL)
    if not m:
        return None
    rows = []
    for line in m.group(1).splitlines():
        bm = re.match(r"[\(\[]\s*([\d.]+)\s*,\s*([\d.]+)\s*\]\s+(\d+)\s+([\d.]+)%\s+([\d.]+)%", line.strip())
        if bm:
            rows.append((float(bm.group(1)), float(bm.group(2)), int(bm.group(3)), float(bm.group(5))))
    if not rows:
        return None
    # cumulative percent is the running column in db_bench output
    prev_pct, prev_hi = 0.0, 0.0
    for lo, hi, _count, cum_pct in rows:
        if cum_pct >= 95.0:
            span = hi - lo
            frac = 0.0 if span <= 0 else (95.0 - prev_pct) / (cum_pct - prev_pct)
            return prev_hi + span * min(max(frac, 0.0), 1.0)
        prev_pct, prev_hi = cum_pct, hi
    return None


def extract_kpis(scenario: str, case_dir: Path) -> dict:
    out = {"rc": None, "_parse_ok": False, "_window": None}
    sj = find_scene_json(case_dir, scenario)
    if sj is None:
        out["_error"] = "no scene json"
        return out
    try:
        d = json.loads(sj.read_text())
    except json.JSONDecodeError as exc:
        out["_error"] = f"scene json unparsable: {exc}"
        return out
    out["_window"] = (d.get("start_time"), d.get("end_time"))
    if scenario == "ycsb":
        out["rc"] = d.get("run_returncode")
        ls = d.get("ycsb_latency_summary") or {}
        for k_src, k_dst in (("latency_read_p50_us", "read_p50_us"),
                             ("latency_read_p95_us", "read_p95_us"),
                             ("latency_read_p99_us", "read_p99_us"),
                             ("latency_update_p50_us", "update_p50_us"),
                             ("latency_update_p95_us", "update_p95_us"),
                             ("latency_update_p99_us", "update_p99_us")):
            if ls.get(k_src) is not None:
                out[k_dst] = ls[k_src]
        thr = ls.get("overall_Throughput(ops/sec)")
        if thr is None:
            thr = d.get("throughput_ops_per_sec")
        if thr is not None:
            out["ops_per_sec"] = thr
        kpis = [k for k in out if not k.startswith("_") and k not in ("rc",)]
        out["_parse_ok"] = out["rc"] == 0 and len(kpis) == 7 and all(
            isinstance(out.get(k), (int, float)) for k in kpis)
    elif scenario == "dbbench":
        phases = d.get("phase_results") or {}
        rww = phases.get("readwhilewriting")
        if not rww:
            out["_error"] = "no readwhilewriting phase_result"
            return out
        rcs = [p.get("returncode") for p in phases.values() if p.get("returncode") is not None]
        out["rc"] = max(rcs) if rcs else None
        hist = (rww.get("histograms") or {}).get("read") or {}
        if hist.get("p50_us") is not None:
            out["fg_read_p50_us"] = hist["p50_us"]
        if hist.get("p99_us") is not None:
            out["fg_read_p99_us"] = hist["p99_us"]
        p95 = parse_hist_buckets_p95(case_dir / "scene" / sj.parent.name / f"{sj.parent.name}_t1_readwhilewriting.log")
        if p95 is None:
            # fall back: glob any *_readwhilewriting.log under the case
            for cand in case_dir.glob("scene/*/*_readwhilewriting.log"):
                p95 = parse_hist_buckets_p95(cand)
                if p95 is not None:
                    break
        if p95 is not None:
            out["fg_read_p95_us"] = p95
        if rww.get("ops_per_sec") is not None:
            out["fg_ops_per_sec"] = rww["ops_per_sec"]
        snaps = d.get("io_stat_snapshots") or {}
        after_key = next((k for k in snaps if k.startswith("after_") and "readwhilewriting" in k), None)
        before_key = next((k for k in snaps if k.startswith("before_") and "readwhilewriting" in k), None) or \
            next((k for k in snaps if "settle" in k), None) or \
            next((k for k in snaps if k.startswith("after_") and "fillrandom" in k), None)
        seconds = rww.get("seconds")
        if after_key and before_key and seconds:
            wb_after = (snaps[after_key].get("totals") or {}).get("wbytes")
            wb_before = (snaps[before_key].get("totals") or {}).get("wbytes")
            if wb_after is not None and wb_before is not None and seconds > 0:
                out["bg_write_mibs"] = max(0.0, (wb_after - wb_before) / 2**20 / seconds)
        kpis = [k for k in out if not k.startswith("_") and k not in ("rc",)]
        out["_parse_ok"] = out["rc"] == 0 and all(
            isinstance(out.get(k), (int, float)) for k in ("fg_read_p50_us", "fg_read_p99_us", "fg_ops_per_sec")) \
            and len(kpis) >= 4
    elif scenario == "shore":
        out["rc"] = d.get("returncode")
        for k_src, k_dst in (("p50_latency_us", "lat_p50_us"),
                             ("p95_latency_us", "lat_p95_us"),
                             ("p99_latency_us", "lat_p99_us"),
                             ("deadline_miss_rate", "deadline_miss_rate")):
            if d.get(k_src) is not None:
                out[k_dst] = d[k_src]
        cnt, wall = d.get("latency_count"), d.get("duration_wall")
        if cnt and wall:
            out["qps_est"] = cnt / wall
        kpis = [k for k in out if not k.startswith("_") and k not in ("rc",)]
        out["_parse_ok"] = out["rc"] == 0 and len(kpis) == 5 and all(
            isinstance(out.get(k), (int, float)) for k in kpis)
    return out


# ---------------- sidecar arm-state consistency ----------------

def parse_sidecar_log(path: Path) -> list:
    """Return [(ts, {scheduler, qos_enable, pins, loaders})]."""
    samples = []
    if not path.is_file():
        return samples
    cur = None
    section = None
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("=== T="):
            if cur:
                samples.append(cur)
            cur = {"ts": float(line.rsplit("=", 1)[1]), "scheduler": None,
                   "qos_enable": None, "pins": [], "loaders": [], "scx": []}
            section = None
        elif cur is not None:
            if line.startswith("-- "):
                section = line[3:].strip()
            elif section == "sdb scheduler":
                cur["scheduler"] = line.strip()
            elif section == "io.cost.qos 8:16":
                m = re.search(r"enable=(\d)", line)
                cur["qos_enable"] = int(m.group(1)) if m else None
            elif section == "bpffs pins" and line.strip() not in ("(none)",):
                cur["pins"].append(line.strip())
            elif section == "loaders" and line.strip() not in ("(none)",):
                cur["loaders"].append(line.strip())
            elif section == "scx procs" and line.strip() not in ("(none)",):
                cur["scx"].append(line.strip())
    if cur:
        samples.append(cur)
    return samples


def expected_state(arm: str) -> dict:
    if arm == "none":
        return {"scheduler": "none", "qos_enable": 0, "pins": False,
                "q_ddl": False, "arb": False, "q_fifo": False}
    if arm == "kyber":
        return {"scheduler": "kyber", "qos_enable": 0, "pins": False,
                "q_ddl": False, "arb": False, "q_fifo": False}
    if arm == "mq_deadline":
        return {"scheduler": "mq-deadline", "qos_enable": 0, "pins": False,
                "q_ddl": False, "arb": False, "q_fifo": False}
    if arm == "bfq":
        return {"scheduler": "bfq", "qos_enable": 0, "pins": False,
                "q_ddl": False, "arb": False, "q_fifo": False}
    if arm == "io_cost":
        return {"scheduler": "none", "qos_enable": 1, "pins": False,
                "q_ddl": False, "arb": False, "q_fifo": False}
    if arm == "iox_q_only":
        return {"scheduler": "scx_io", "qos_enable": 0, "pins": True,
                "q_ddl": True, "arb": False, "q_fifo": False}
    if arm == "iox_arb_q":
        return {"scheduler": "scx_io", "qos_enable": 0, "pins": True,
                "q_ddl": False, "arb": True, "q_fifo": True}
    raise ValueError(arm)


def loader_pid(line: str) -> tuple:
    m = re.match(r"^(\d+)\s+(.*)$", line)
    return (m.group(1), m.group(2)) if m else ("", line)


def active_scheduler(sched_line: str) -> str:
    """Extract the ACTIVE scheduler from a /sys/block/<dev>/queue/scheduler
    contents line: the option wrapped in square brackets. Plain substring
    matching is unsafe once the 7-arm set includes native schedulers (the
    options list of e.g. bfq also contains the words 'mq-deadline', 'kyber'
    and 'none'). Returns '' when no bracket entry is present."""
    import re as _re
    m = _re.search(r"\[([^\]]+)\]", sched_line)
    return m.group(1) if m else ""


def sample_matches(sample: dict, arm: str) -> bool:
    exp = expected_state(arm)
    sched = sample["scheduler"] or ""
    active = active_scheduler(sched)
    if active:
        if active != exp["scheduler"]:
            return False
    else:
        # bracketless fallback (scx_io attachments can drop the bracket list):
        # keep the old substring behaviour only for unambiguous schedulers.
        if exp["scheduler"] not in ("scx_io", "none") or exp["scheduler"] not in sched:
            return False
    if sample["qos_enable"] != exp["qos_enable"]:
        return False
    has_pins = bool(sample["pins"])
    if has_pins != exp["pins"]:
        return False
    procs = " ".join(cmd for _pid, cmd in (loader_pid(l) for l in sample["loaders"]))
    if exp["q_ddl"] != ("loader_q_ddl" in procs):
        return False
    if exp["q_fifo"] != ("loader_q_fifo" in procs):
        return False
    if exp["arb"] != ("loader_arb_" in procs):
        return False
    return True


def window_gate(window: tuple, arm: str, samples: list, edge_tol: float = 0.0) -> dict:
    """All samples strictly inside the window (minus edge_tol at both ends)
    must match the expected arm state."""
    out = {"sample_n": 0, "bad_n": 0, "ok": False}
    if not window or None in window:
        out["error"] = "no window"
        return out
    start, end = window
    inside = [s for s in samples if start + edge_tol <= s["ts"] <= max(start + edge_tol, end - edge_tol)]
    out["sample_n"] = len(inside)
    if not inside:
        out["error"] = "no samples in window"
        return out
    bad = [s for s in inside if not sample_matches(s, arm)]
    out["bad_n"] = len(bad)
    times = [s["ts"] for s in inside]
    out["coverage_ok"] = (times[0] <= start + edge_tol + 4 and
                          times[-1] >= end - edge_tol - 4 and
                          all(b - a <= 6 for a, b in zip(times, times[1:])))
    out["ok"] = not bad and out["coverage_ok"]
    if bad:
        out["first_bad_ts"] = bad[0]["ts"]
        s = bad[0]
        out["first_bad_state"] = {"scheduler": s["scheduler"], "qos_enable": s["qos_enable"],
                                  "pins": s["pins"][:3], "loaders": s["loaders"][:4]}
    return out


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", required=True, choices=sorted(COLUMNS))
    ap.add_argument("--root", required=True, type=Path,
                    help="results/rq1_realapps_<scn>/ containing extracted case dirs")
    ap.add_argument("--sidecar-log", type=Path, default=None)
    ap.add_argument("--probe-prefix", default="probe",
                    help="dir-name prefix to exclude (probe runs)")
    args = ap.parse_args()

    sidecar = parse_sidecar_log(args.sidecar_log) if args.sidecar_log else []
    # Case-granular layout fallback (scripts/rq1_drive_campaign.py): each case
    # has its own sidecar snapshot at <case_dir>.sidecar.log (a sibling of the
    # case dir). Used only when --sidecar-log is not given; an explicit
    # --sidecar-log always wins so the whole-scenario behaviour is unchanged.
    per_case_samples: dict = {}
    rows = []
    candidates = []
    for entry in sorted(args.root.iterdir()):
        if entry.is_dir() and not entry.name.startswith(args.probe_prefix):
            candidates.append(entry)
            if not (entry / "run_results.json").is_file():
                # formal_<arm>/ batch dirs: descend one more level
                candidates.extend(sorted(p for p in entry.iterdir() if p.is_dir()))
    for case_dir in candidates:
        if not (case_dir / "run_results.json").is_file():
            continue
        arm, rep = parse_case_name(case_dir.name)
        if arm is None:
            print(f"SKIP {case_dir.name}: cannot map to an arm", file=sys.stderr)
            continue
        kpis = extract_kpis(args.scenario, case_dir)
        row = {"scenario": args.scenario, "arm": arm, "repeat": rep,
               "run_dir": str(case_dir), "rc": kpis.get("rc")}
        for col in COLUMNS[args.scenario][6:]:
            v = kpis.get(col)
            row[col] = "" if v is None else (f"{v:.6g}" if isinstance(v, float) else v)
        checks = {
            "rc": kpis.get("rc") == 0,
            "parse": bool(kpis.get("_parse_ok")),
        }
        if sidecar:
            gate = window_gate(kpis.get("_window"), arm, sidecar)
            checks["samples"] = gate.get("ok", False)
            row["_sample_detail"] = json.dumps({k: v for k, v in gate.items() if k != "ok"})
        else:
            case_sidecar = case_dir.parent / f"{case_dir.name}.sidecar.log"
            if case_sidecar.is_file() and case_sidecar not in per_case_samples:
                per_case_samples[case_sidecar] = parse_sidecar_log(case_sidecar)
            samples = per_case_samples.get(case_sidecar, [])
            if samples:
                gate = window_gate(kpis.get("_window"), arm, samples)
                checks["samples"] = gate.get("ok", False)
                detail = {k: v for k, v in gate.items() if k != "ok"}
                detail["sidecar"] = case_sidecar.name
                row["_sample_detail"] = json.dumps(detail)
            else:
                checks["samples"] = False
                row["_sample_detail"] = "sidecar log missing or empty"
        persisted_gate = case_dir.parent / f"{case_dir.name}.gate.json"
        if persisted_gate.is_file():
            checks["case_gate"] = bool(json.loads(persisted_gate.read_text()).get("ok"))
        row["gate_ok"] = all(checks.values())
        row["_checks"] = ",".join(k for k, v in checks.items() if not v) or "all"
        rows.append(row)

    rows.sort(key=lambda r: (ARM_ORDER.index(r["arm"]),
                             r["repeat"]))

    cases_path = args.root / f"rq1_{args.scenario}_cases.csv"
    cols = COLUMNS[args.scenario]
    with open(cases_path, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
    print(f"wrote {cases_path} ({len(rows)} rows)")
    detail_path = args.root / f"rq1_{args.scenario}_gate_detail.csv"
    import csv as _csv
    with open(detail_path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["arm", "repeat", "run_dir", "checks_failed", "sample_detail"])
        for r in rows:
            w.writerow([r["arm"], r["repeat"], r["run_dir"], r.get("_checks", ""),
                        r.get("_sample_detail", "")])
    print(f"wrote {detail_path}")

    # summary: arm x metric
    metric_cols = COLUMNS[args.scenario][6:]
    summary_rows = []
    for arm in ARM_ORDER:
        arm_rows = [r for r in rows if r["arm"] == arm and r["gate_ok"]]
        for metric in metric_cols:
            vals = []
            for r in arm_rows:
                v = r.get(metric)
                if v not in ("", None):
                    try:
                        vals.append(float(v))
                    except ValueError:
                        pass
            if not vals:
                summary_rows.append([arm, metric, "NA", "NA", "NA", "NA", 0])
                continue
            mean = statistics.fmean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            summary_rows.append([arm, metric, f"{mean:.6g}", f"{sd:.6g}",
                                 f"{min(vals):.6g}", f"{max(vals):.6g}", len(vals)])
    summary_path = args.root / f"rq1_{args.scenario}_summary.csv"
    with open(summary_path, "w") as f:
        f.write("arm,metric,mean,sd,min,max,n\n")
        for r in summary_rows:
            f.write(",".join(str(x) for x in r) + "\n")
    print(f"wrote {summary_path}")
    bad = [r for r in rows if not r["gate_ok"]]
    if bad:
        print(f"GATE FAILURES: {[(r['arm'], r['repeat'], r['_checks']) for r in bad]}")
    return 1 if bad or not rows else 0


if __name__ == "__main__":
    sys.exit(main())
