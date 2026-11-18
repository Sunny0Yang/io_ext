#!/usr/bin/env python3
"""Analyze the kvmhost D3 discovery batch (campaign Phase 2, plan §3.5).

Screening order (frozen):
  1. clean gate (manifest + parseable LC/BE windows; mechanical LC flip
     p99 > 5 ms; device-phase via proportional busy-second floors)
  2. LC p99 <= SLO (from the Phase-1 anchor file -- screening reference until
     the confirmation batch re-anchors)
  3. BE retention >= 60% of the SAME-BATCH none median (never the calibration
     B_none -- same-batch discipline)
  4. run-to-run consistency + per-BE fairness (reported, not gated)

Verdicts per arm: PASS (n>=3 valid, every clean run passes the double gate) /
FAIL(measured) (any clean run violates) / NOT-EVALUABLE (n<3 or denominator
invalid).  No run is silently dropped; incomplete runs stay in the CSV.

Outputs under <results-root>/analysis:
  discovery_runs.csv     per-run LC p99 / BE agg / per-BE / busy / gates
  discovery_pareto.csv   per-arm medians + double-gate verdicts
  discovery_summary.json machine-readable screening result
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_d3_kvmhost_calib import (  # noqa: E402
    jobs_from_result_dir, classify, p99_ms, pct_ms, bw_mb_s,
    busy_seconds, load_manifest, med, LC_FLIP_MS,
)


def parse_seq(name: str) -> tuple[str, int]:
    body = name[len("D3PV_"):]
    arm, run = body.rsplit("_run", 1)
    return arm, int(run)


def policy_engaged(run_dir: Path, arm: str) -> bool | None:
    """Audit the ACTUAL device scheduler recorded by the executor
    (metadata.json io_schedulers) against the arm's expectation.
    Catches the 'policy profile overridden by tenant scene' class of bug
    (plan §1.2 -- the bfq-arm discovery-v1 failure mode)."""
    try:
        m = json.loads((run_dir / "metadata.json").read_text())
        actual = ((m.get("io_schedulers") or [{}])[0] or {}).get("scheduler")
    except Exception:
        return None
    expect = {"none": "none", "bfq": "bfq", "bfqf": "bfq"}.get(arm, "scx_io")
    return actual == expect


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-root", type=Path, default=Path("results/d3_kvmhost/discovery"))
    ap.add_argument("--anchor", type=Path,
                    default=Path("results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"))
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--device", default="sdb")
    ap.add_argument("--floor-frac", type=float, default=0.20)
    ap.add_argument("--retention-line", type=float, default=60.0)
    args = ap.parse_args()

    anchor = json.loads(args.anchor.read_text())
    slo_ms = anchor["slo_batch_ms"]
    root = args.results_root
    out_dir = args.out_dir or (root / "analysis")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- per-run extraction ---------------------------------------------------
    runs = []
    for d in sorted(root.glob("D3PV_*_run*")):
        if not d.is_dir():
            continue
        arm, run_idx = parse_seq(d.name)
        recs = jobs_from_result_dir(d)
        lc, be, errors = classify(recs)
        lc_p99 = p99_ms(lc[0]["job"]) if len(lc) == 1 else None
        be_jobs = [{"label": f"be{i + 1}", "bw": bw_mb_s(j["job"])} for i, j in enumerate(be)]
        be_vals = [j["bw"] for j in be_jobs if j["bw"] is not None]
        agg = sum(be_vals) if len(be) == len(be_vals) and be_vals else None
        bs = busy_seconds(d, args.device)
        flip = bool(lc_p99 and lc_p99 > LC_FLIP_MS)
        engaged = policy_engaged(d, arm)
        status = "ok" if (len(lc) == 1 and agg is not None and not errors) else "incomplete"
        if status == "ok" and engaged is False:
            status = "policy-not-engaged"
        runs.append({
            "run_id": d.name, "arm": arm, "run": run_idx,
            "status": status,
            "policy_engaged": engaged,
            "lc_p99_ms": lc_p99,
            "lc_p50_ms": pct_ms(lc[0]["job"], "50.000000") if len(lc) == 1 else None,
            "be_agg_mb_s": agg,
            "be_min_mb_s": min(be_vals) if be_vals else None,
            "be_max_mb_s": max(be_vals) if be_vals else None,
            "be_per_job_mb_s": json.dumps({j["label"]: j["bw"] for j in be_jobs},
                                          sort_keys=True) if be_jobs else None,
            "busy_seconds": len(bs) or None,
            "lc_flip": flip,
            "manifest_ok": bool(load_manifest(d)),
            "errors": "; ".join(errors) or None,
        })

    # ---- data validity & same-batch none denominator --------------------------
    # Device-pause signature on kvmhost = BE-side collapse (the 41 MB/s class);
    # an LC p99 above the 5 ms flag is a PERFORMANCE result (e.g. the none arm's
    # honest no-protection latency), NOT invalid data -- the bm absolute LC-flip
    # exclusion must not transplant here (preflight §0 fix).
    be_floor = args.floor_frac * anchor["b_none_mb_s"]
    for r in runs:
        r["be_valid"] = bool(r["be_agg_mb_s"] is not None
                             and r["be_agg_mb_s"] >= be_floor)

    none_agg = [r["be_agg_mb_s"] for r in runs
                if r["arm"] == "none" and r["status"] == "ok" and r["be_valid"]]
    b_none_batch = med(none_agg) if none_agg else None

    for r in runs:
        r["retention_pct"] = (round(r["be_agg_mb_s"] / b_none_batch * 100.0, 2)
                              if (b_none_batch and r["be_agg_mb_s"] is not None) else None)
        r["ret_pct_calib"] = (round(r["be_agg_mb_s"] / anchor["b_none_mb_s"] * 100.0, 2)
                              if r["be_agg_mb_s"] is not None else None)
        r["slo_pass"] = (r["lc_p99_ms"] is not None and r["lc_p99_ms"] <= slo_ms) or None
        r["ret_pass"] = (r["retention_pct"] is not None
                         and r["retention_pct"] >= args.retention_line) or None
        if r["status"] == "ok" and b_none_batch:
            bs = [v for v in busy_seconds(root / r["run_id"], args.device)]
            r["busy_under_floor20"] = sum(v < args.floor_frac * b_none_batch for v in bs) or None

    # ---- per-arm screening ------------------------------------------------------
    arms = sorted({r["arm"] for r in runs})
    pareto = []
    for arm in arms:
        rs = [r for r in runs if r["arm"] == arm]
        valid = [r for r in rs if r["status"] == "ok"]
        clean = [r for r in valid if r["be_valid"]]
        paused = [r for r in valid if not r["be_valid"]]
        not_engaged = [r for r in rs if r["status"] == "policy-not-engaged"]
        p99s = [r["lc_p99_ms"] for r in clean if r["lc_p99_ms"] is not None]
        aggs = [r["be_agg_mb_s"] for r in clean if r["be_agg_mb_s"] is not None]
        rets = [r["retention_pct"] for r in clean if r["retention_pct"] is not None]
        slo_pass = sum(1 for r in clean if r["slo_pass"])
        ret_pass = sum(1 for r in clean if r["ret_pass"])
        double_gate = sum(1 for r in clean if r["slo_pass"] and r["ret_pass"])
        if len(clean) < 3 or b_none_batch is None:
            verdict = "NOT-EVALUABLE"
        elif double_gate == len(clean) and len(clean) >= 3:
            verdict = "PASS"
        elif clean and double_gate < len(clean):
            verdict = "FAIL(measured)"
        else:
            verdict = "NOT-EVALUABLE"
        notes = []
        if paused:
            notes.append(f"{len(paused)} device-pause run(s) excluded from clean pool "
                         f"(BE < {be_floor:.0f} MB/s = 20% x calib B_none)")
        if not_engaged:
            notes.append(f"{len(not_engaged)} policy-not-engaged run(s) excluded "
                         "(metadata scheduler audit)")
        if b_none_batch is None and arm == "none":
            notes.append("no valid none runs — batch denominator invalid")
        pareto.append({
            "arm": arm, "n_total": len(rs), "n_valid": len(valid), "n_clean": len(clean),
            "lc_p99_ms_med": round(med(p99s), 4) if p99s else None,
            "lc_p99_ms_min": round(min(p99s), 4) if p99s else None,
            "lc_p99_ms_max": round(max(p99s), 4) if p99s else None,
            "be_agg_mb_s_med": round(med(aggs), 2) if aggs else None,
            "be_agg_mb_s_min": round(min(aggs), 2) if aggs else None,
            "be_agg_mb_s_max": round(max(aggs), 2) if aggs else None,
            "retention_pct_med": round(med(rets), 2) if rets else None,
            "retention_pct_min": round(min(rets), 2) if rets else None,
            "slo_pass_runs": f"{slo_pass}/{len(clean)}",
            "ret_pass_runs": f"{ret_pass}/{len(clean)}",
            "double_gate_runs": f"{double_gate}/{len(clean)}",
            "be_fairness_min_max": (f"{min(r['be_min_mb_s'] for r in clean):.0f}-"
                                     f"{max(r['be_max_mb_s'] for r in clean):.0f}") if clean else None,
            "verdict": verdict,
            "notes": "; ".join(notes) or None,
        })

    # ---- outputs ---------------------------------------------------------------
    run_cols = ["run_id", "arm", "run", "status", "policy_engaged", "lc_p50_ms", "lc_p99_ms",
                "be_agg_mb_s", "be_min_mb_s", "be_max_mb_s", "be_per_job_mb_s",
                "be_valid", "retention_pct", "ret_pct_calib", "slo_pass", "ret_pass",
                "busy_seconds", "busy_under_floor20", "lc_flip", "manifest_ok", "errors"]
    with open(out_dir / "discovery_runs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=run_cols)
        w.writeheader()
        for r in runs:
            w.writerow({k: r.get(k) for k in run_cols})

    p_cols = ["arm", "n_total", "n_valid", "n_clean", "lc_p99_ms_med", "lc_p99_ms_min",
              "lc_p99_ms_max", "be_agg_mb_s_med", "be_agg_mb_s_min", "be_agg_mb_s_max",
              "retention_pct_med", "retention_pct_min", "slo_pass_runs", "ret_pass_runs",
              "double_gate_runs", "be_fairness_min_max", "verdict", "notes"]
    with open(out_dir / "discovery_pareto.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=p_cols)
        w.writeheader()
        for p in pareto:
            w.writerow({k: p.get(k) for k in p_cols})

    passing = [p for p in pareto if p["verdict"] == "PASS"]
    best = max(passing, key=lambda p: (p["retention_pct_med"] or 0), default=None)
    summary = {
        "batch": root.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slo_ms_reference": slo_ms,
        "b_none_batch_mb_s": round(b_none_batch, 2) if b_none_batch else None,
        "b_none_calibration_mb_s": anchor.get("b_none_mb_s"),
        "retention_line_pct": args.retention_line,
        "pareto": pareto,
        "best_slo_passing_arm": best["arm"] if best else None,
        "candidates_for_confirmation": [p["arm"] for p in pareto
                                        if p["verdict"] == "PASS"
                                        or (p["verdict"] == "FAIL(measured)"
                                            and (p["slo_pass_runs"] or "0/0").split("/")[0].isdigit()
                                            and int(p["slo_pass_runs"].split("/")[0]) >= 2)],
        "rule": ("discovery screens; confirmation (Phase 3) decides. "
                 "Never promote discovery numbers to the paper figure."),
    }
    (out_dir / "discovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # ---- console ----------------------------------------------------------------
    print("=== D3 kvmhost discovery screening ===")
    print(f"same-batch B_none: {summary['b_none_batch_mb_s']} MB/s "
          f"(calibration anchor: {summary['b_none_calibration_mb_s']}) | "
          f"SLO reference: {slo_ms} ms")
    print(f"{'arm':<14}{'n':>4}{'p99_med':>10}{'p99_max':>10}{'BE_med':>10}{'ret%':>8}"
          f"{'SLO':>7}{'ret':>7}{'2x2':>7}  verdict")
    for p in pareto:
        print(f"{p['arm']:<14}{p['n_valid']:>4}"
              f"{p['lc_p99_ms_med'] if p['lc_p99_ms_med'] is not None else '-':>10}"
              f"{p['lc_p99_ms_max'] if p['lc_p99_ms_max'] is not None else '-':>10}"
              f"{p['be_agg_mb_s_med'] if p['be_agg_mb_s_med'] is not None else '-':>10}"
              f"{p['retention_pct_med'] if p['retention_pct_med'] is not None else '-':>8}"
              f"{p['slo_pass_runs']:>7}{p['ret_pass_runs']:>7}{p['double_gate_runs']:>7}"
              f"  {p['verdict']}")
    if best:
        print(f"\nbest SLO-passing arm by BE retention: {best['arm']} "
              f"(ret med {best['retention_pct_med']}%)")
    print(f"confirmation candidates: {summary['candidates_for_confirmation']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
