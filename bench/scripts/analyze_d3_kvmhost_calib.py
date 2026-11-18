#!/usr/bin/env python3
"""Analyze the kvmhost D3 calibration batch (campaign Phase 1).

Inputs  --results-root (default results/d3_kvmhost/calibration)
Outputs (under <results-root>/analysis unless --out-dir):
  lc_solo_runs.csv          per-run LC solo p99 (+manifest completeness)
  none_contention_runs.csv  per-run LC p99 + BE per-job/aggregate bw
  SLO_and_B_none.json       the campaign's single anchor source of truth
  calibration_summary.json  per-second busy telemetry stats + verdict block

Semantics (frozen, campaign plan §2):
  SLO_batch = 2 x median(solo LC p99);  B_none = median(contention BE agg).
  Units: latency ms (fio clat_ns percentile / 1e6); bandwidth decimal MB/s
  (bw_bytes / 1e6). No bm anchor is read. No run is silently dropped: runs
  with missing/unparseable data are reported with status != ok and excluded
  from the medians while staying in the CSVs.

Busy-second audit (bare-metal aggregate-gate blind-spot fix): device-level 1 s
samples (util >= 80% and ios_in_progress >= 1) from
system_metric_summary.json; counts under 50 MB/s and under the PROPORTIONAL
floors 20% / 60% of B_none (the bm absolute 600 MB/s floor is deliberately
not reused -- see D3_kvmhost_PREFLIGHT.md §0).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

SOLO_RE = "D3PV_lcsolo_none_run*"
CONT_RE = "D3PV_none_run*"
BUSY_UTIL_MIN = 80
BUSY_IOS_MIN = 1
LC_FLIP_MS = 5.0   # frozen bm mechanical LC flip gate (p99 window > 5 ms)


def per_second_lc_profile(seq_dir: Path) -> dict | None:
    """Per-second median LC clat profile from fio lat logs (values are ns in
    fio 3.41 with clat_percentiles).  Returns first-10s vs rest medians (ms)
    and the count of seconds whose median exceeds half the anchor scale —
    the shape evidence for warm-up vs sustained-drift classification."""
    import statistics
    best = None
    for lg in sorted(seq_dir.rglob("*lat_clat*.log")):
        secs: dict[int, list[int]] = {}
        try:
            lines = lg.read_text().splitlines()
        except Exception:
            continue
        base = None
        for line in lines:
            p = line.split(",")
            if len(p) < 2 or not p[1].strip():
                continue
            try:
                t, v = int(p[0]), int(p[1])
            except ValueError:
                continue
            if base is None:
                base = t
            secs.setdefault((t - base) // 1000, []).append(v)
        if len(secs) >= 10:
            meds = [statistics.median(v) / 1e6 for _, v in sorted(secs.items())]
            scale = meds and statistics.median(meds) or 0
            best = {"first10": statistics.median(meds[:10]),
                    "rest": statistics.median(meds[10:]),
                    "slow_secs": sum(1 for x in meds if x > max(scale * 4, 0.12)),
                    "n_secs": len(meds)}
            break
    return best


def jobs_from_result_dir(seq_dir: Path) -> list[dict]:
    """Collect every fio job record under one sequence dir.

    Handles both one-json-per-tenant and multi-job group_reporting layouts.
    """
    records = []
    for p in sorted(seq_dir.rglob("*fio.json")):
        try:
            doc = json.loads(p.read_text())
        except Exception as e:
            records.append({"path": str(p), "error": f"unparseable: {e}"})
            continue
        for job in doc.get("jobs") or []:
            records.append({"path": str(p), "job": job})
    return records


def classify(records: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Split job records into LC / BE by scene-id suffix; collect errors."""
    lc, be, errors = [], [], []
    for rec in records:
        if "error" in rec:
            errors.append(f"{rec['path']}: {rec['error']}")
            continue
        name = str(rec["job"].get("jobname") or "")
        if name.endswith("_lc") or "_lc_" in name or name == "ib_d3cal_lc":
            lc.append(rec)
        elif "_be" in name:
            be.append(rec)
        else:
            errors.append(f"{rec['path']}: unclassified jobname {name!r}")
    return lc, be, errors


def p99_ms(job: dict) -> float | None:
    clat = ((job.get("read") or {}).get("clat_ns") or {}).get("percentile") or {}
    v = clat.get("99.000000")
    return None if v is None else v / 1e6


def pct_ms(job: dict, pct: str) -> float | None:
    clat = ((job.get("read") or {}).get("clat_ns") or {}).get("percentile") or {}
    v = clat.get(pct)
    return None if v is None else v / 1e6


def bw_mb_s(job: dict) -> float | None:
    v = (job.get("read") or {}).get("bw_bytes")
    return None if v is None else v / 1e6


def busy_seconds(seq_dir: Path, device: str) -> list[float]:
    """Device-level busy-second read throughput samples (MB/s)."""
    samples = []
    for p in sorted(seq_dir.rglob("*system_metric_summary*.json")):
        try:
            doc = json.loads(p.read_text())
        except Exception:
            continue
        for s in doc.get("samples") or []:
            disk = ((s.get("disk") or {}).get("devices") or {}).get(device, {})
            if not disk.get("usage_valid"):
                continue
            if (disk.get("util_percent") or 0) < BUSY_UTIL_MIN:
                continue
            if (disk.get("ios_in_progress") or 0) < BUSY_IOS_MIN:
                continue
            sectors = disk.get("read_sectors_delta")
            if sectors is not None:
                samples.append(sectors * 512 / 1e6)
    return samples


def load_manifest(seq_dir: Path) -> dict | None:
    p = seq_dir / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def med(vals: list[float]) -> float:
    return statistics.median(vals)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-root", type=Path, default=Path("results/d3_kvmhost/calibration"))
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--device", default="sdb")
    ap.add_argument("--floor-frac", type=float, default=0.20,
                    help="proportional busy-second floor as fraction of B_none")
    args = ap.parse_args()

    root = args.results_root
    out_dir = args.out_dir or (root / "analysis")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- LC solo -------------------------------------------------------------
    solo_rows, solo_p99 = [], []
    for d in sorted(root.glob(SOLO_RE)):
        if not d.is_dir():
            continue
        recs = jobs_from_result_dir(d)
        lc, _be, errors = classify(recs)
        p99 = p99_ms(lc[0]["job"]) if len(lc) == 1 else None
        row = {
            "run_id": d.name,
            "status": "ok" if (len(lc) == 1 and p99 is not None and not errors) else "incomplete",
            "n_lc_jobs": len(lc),
            "lc_p50_ms": pct_ms(lc[0]["job"], "50.000000") if len(lc) == 1 else None,
            "lc_p95_ms": pct_ms(lc[0]["job"], "95.000000") if len(lc) == 1 else None,
            "lc_p99_ms": p99,
            "lc_p99_9_ms": pct_ms(lc[0]["job"], "99.900000") if len(lc) == 1 else None,
            "lc_bw_mb_s": bw_mb_s(lc[0]["job"]) if len(lc) == 1 else None,
            "busy_seconds": len(busy_seconds(d, args.device)) or None,
            "manifest_ok": bool(load_manifest(d)),
            "errors": "; ".join(errors) or None,
        }
        solo_rows.append(row)
        if row["status"] == "ok":
            solo_p99.append(p99)

    # ---- none contention ------------------------------------------------------
    cont_rows, cont_be, cont_p99 = [], [], []
    for d in sorted(root.glob(CONT_RE)):
        if not d.is_dir():
            continue
        recs = jobs_from_result_dir(d)
        lc, be, errors = classify(recs)
        lc_p99 = p99_ms(lc[0]["job"]) if len(lc) == 1 else None
        # BE jobs may share one collapsed jobname (single json, 4 jobs) — keep
        # positional labels so per-BE splits survive name collisions.
        be_jobs = [{"label": f"be{i + 1}", "jobname": str(j["job"].get("jobname")),
                    "bw": bw_mb_s(j["job"])} for i, j in enumerate(be)]
        be_vals = [j["bw"] for j in be_jobs if j["bw"] is not None]
        agg = sum(be_vals) if len(be) == len(be_vals) and be_vals else None
        row = {
            "run_id": d.name,
            "status": "ok" if (len(lc) == 1 and len(be) >= 1 and agg is not None and not errors) else "incomplete",
            "n_lc_jobs": len(lc),
            "n_be_jobs": len(be),
            "lc_p99_ms": lc_p99,
            "be_agg_mb_s": agg,
            "be_per_job_mb_s": json.dumps(
                {j["label"]: j["bw"] for j in be_jobs}, sort_keys=True) if be_jobs else None,
            "be_min_mb_s": min(be_vals) if be_vals else None,
            "be_max_mb_s": max(be_vals) if be_vals else None,
            "manifest_ok": bool(load_manifest(d)),
            "errors": "; ".join(errors) or None,
        }
        cont_rows.append(row)
        if row["status"] == "ok":
            cont_be.append(agg)
            cont_p99.append(lc_p99)

    # ---- anchors --------------------------------------------------------------
    verdict, notes = "NOT-EVALUABLE", []
    slo_ms = b_none = None
    anchor_med = med(solo_p99) if solo_p99 else None
    band = (0.5 * anchor_med, 1.5 * anchor_med) if anchor_med else None
    if len(solo_p99) >= 3:
        slo_ms = 2 * med(solo_p99)
    else:
        notes.append(f"LC solo valid runs {len(solo_p99)} < 3")
    if len(cont_be) >= 3:
        b_none = med(cont_be)
    else:
        notes.append(f"none contention valid runs {len(cont_be)} < 3")

    # solo healthy band (±50% of median) + per-second LC profile evidence
    out_of_band = 0
    if band:
        for row in solo_rows:
            if row["status"] != "ok":
                continue
            row["within_band"] = band[0] <= row["lc_p99_ms"] <= band[1]
            if not row["within_band"]:
                out_of_band += 1
            prof = per_second_lc_profile(root / row["run_id"])
            row["persec_first10_med_ms"] = round(prof["first10"], 4) if prof else None
            row["persec_rest_med_ms"] = round(prof["rest"], 4) if prof else None
            row["persec_slow_secs"] = prof["slow_secs"] if prof else None
            if prof and prof["slow_secs"]:
                notes.append(f"{row['run_id']}: {prof['slow_secs']} sec(s) median "
                             f"> anchor/2 — inspect per-second profile")

    # busy-second floors, proportional to B_none (plan §1.5 adjusted);
    # computed whenever B_none exists (not only on PASS)
    if b_none:
        for row in cont_rows:
            if row["status"] != "ok":
                continue
            bs = busy_seconds(root / row["run_id"], args.device)
            row["busy_seconds"] = len(bs) or None
            row["busy_under_floor20"] = sum(v < args.floor_frac * b_none for v in bs) or None
            row["busy_under_floor60"] = sum(v < 0.60 * b_none for v in bs) or None
            row["busy_median_mb_s"] = round(med(bs), 2) if bs else None
            if row["busy_under_floor20"]:
                notes.append(f"{row['run_id']}: {row['busy_under_floor20']} busy sec "
                             f"< {args.floor_frac:.0%} of B_none (phase audit required)")

    # verdict: mechanical flip gate first, then band-systematic check
    if slo_ms and b_none:
        if any(r["lc_p99_ms"] and r["lc_p99_ms"] > LC_FLIP_MS
               for r in solo_rows if r["status"] == "ok"):
            verdict = "FAIL"
            notes.append(f"solo run above mechanical LC flip threshold {LC_FLIP_MS} ms")
        elif out_of_band >= 3:
            verdict = "FAIL"
            notes.append(f"{out_of_band}/5 solo runs outside ±50% band — systematic "
                         "drift, do not freeze the anchor")
        else:
            verdict = "PASS"

    # ---- outputs ---------------------------------------------------------------
    def write_csv(name: str, rows: list[dict], cols: list[str]) -> None:
        with open(out_dir / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})

    write_csv("lc_solo_runs.csv", solo_rows,
              ["run_id", "status", "n_lc_jobs", "lc_p50_ms", "lc_p95_ms", "lc_p99_ms",
               "lc_p99_9_ms", "lc_bw_mb_s", "busy_seconds", "within_band",
               "persec_first10_med_ms", "persec_rest_med_ms", "persec_slow_secs",
               "manifest_ok", "errors"])
    write_csv("none_contention_runs.csv", cont_rows,
              ["run_id", "status", "n_lc_jobs", "n_be_jobs", "lc_p99_ms", "be_agg_mb_s",
               "be_per_job_mb_s", "be_min_mb_s", "be_max_mb_s", "busy_seconds",
               "busy_under_floor20", "busy_under_floor60", "busy_median_mb_s",
               "manifest_ok", "errors"])

    env = {}
    env_p = root / "batch_env.json"
    if env_p.exists():
        env = json.loads(env_p.read_text())

    solo_spread = (max(solo_p99) / med(solo_p99)) if solo_p99 else None
    anchor = {
        "calibration_batch_id": root.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kernel": env.get("uname_r"),
        "hostname": env.get("hostname"),
        "device": env.get("device"),
        "fio_version": env.get("fio_version"),
        "solo_p99_ms_median": round(med(solo_p99), 4) if solo_p99 else None,
        "solo_p99_ms_min": round(min(solo_p99), 4) if solo_p99 else None,
        "solo_p99_ms_max": round(max(solo_p99), 4) if solo_p99 else None,
        "solo_p99_mad_ms": round(statistics.median(
            [abs(x - med(solo_p99)) for x in solo_p99]), 4) if solo_p99 else None,
        "solo_max_over_median": round(solo_spread, 3) if solo_spread else None,
        "solo_band_ms": [round(band[0], 4), round(band[1], 4)] if band else None,
        "solo_out_of_band_runs": out_of_band,
        "slo_batch_ms": round(slo_ms, 4) if slo_ms else None,
        "b_none_mb_s": round(b_none, 2) if b_none else None,
        "b_none_floor_frac": args.floor_frac,
        "valid_solo_run_ids": [r["run_id"] for r in solo_rows if r["status"] == "ok"],
        "valid_cont_run_ids": [r["run_id"] for r in cont_rows if r["status"] == "ok"],
        "clean_gate_version": "d3-kvmhost-calib-v1",
        "verdict": verdict,
        "notes": notes,
        "rule": ("SLO_batch = 2 x median(solo LC p99); B_none = median(contention BE "
                 "aggregate); decimal MB/s; ms; no bm anchor consumed; later cap and "
                 "retention computations MUST read this file (no hand-copied values)."),
    }
    (out_dir / "SLO_and_B_none.json").write_text(json.dumps(anchor, indent=2) + "\n")
    (out_dir / "calibration_summary.json").write_text(json.dumps({
        "verdict": verdict, "notes": notes,
        "solo_rows": solo_rows, "cont_rows": cont_rows,
    }, indent=2) + "\n")

    # ---- console verdict --------------------------------------------------------
    print("=== D3 kvmhost calibration analysis ===")
    print(f"solo runs      : {len(solo_rows)} total, {len(solo_p99)} valid -> "
          f"median p99 {anchor['solo_p99_ms_median']} ms, "
          f"max/median {anchor['solo_max_over_median']}")
    print(f"cont runs      : {len(cont_rows)} total, {len(cont_be)} valid -> "
          f"B_none {anchor['b_none_mb_s']} MB/s")
    print(f"SLO_batch      : {anchor['slo_batch_ms']} ms")
    print(f"VERDICT        : {verdict}")
    for n in notes:
        print(f"  note: {n}")
    if verdict == "PASS":
        print("next: Phase 2 cap grid M(k)=round(k*B_none_mib/4), k in "
              "{0.65,0.75,0.85,0.95,1.00} from SLO_and_B_none.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
