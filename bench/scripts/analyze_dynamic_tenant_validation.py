#!/usr/bin/env python3
"""Paired analysis for the kvmhost validation matrix (NO schedule re-tuning).

For each validation seed offset off in {101,102,103} (predeclared bases
20261103+off = 20261204/205/206):
  dynamic  = DYN_valid_f1_arbfb_s{off}    (the EXECUTED schedule arm)
  bfq_ref  = DYN_valid_n3_native_bfq_s{off} (native whole-run reference)
paired on the SAME workload seed base -- a genuine paired sample (same seed =>
same workload stream).

20261104 iox-only amendment: the dynamic trajectory must stay under an iox
policy the whole run; under that + the same-arbiter-family constraint the
schedule DP over io_ext arms gives a whole-run resident f1_arbfb
(num_transitions == 0) as the optimum (schedule_feasible_iox_only.json).
Therefore the "dynamic vs f1-static" delta is IDENTITY 0 by construction and
is reported as such -- NOT manufactured.  The informative paired comparisons
actually run are:
  1) dynamic (f1 iox resident) vs native bfq whole-run reference, per seed;
  2) validation f1 losses vs the in-sample matrix3 f1 run (loss 5.1805,
     single run, no variance estimate) -- seed stability of that figure.
The native-mixed v3 schedule (bfq at T5, -13.6% in-sample vs the same static)
is kept in the LEDGER as not-executed-by-amendment; never re-ranked post hoc.

Per seed reports:
  - weighted tail loss (7.8) for both runs, paired delta absolute + relative;
  - per-tenant p99 ratio means; tenant A p99 per phase;
  - measured transition records (none executed when num_transitions == 0);
  - guardrail verdicts.

Outputs:
  results/dyn_tenant_kvmhost/validation_matrix.json
  results/dyn_tenant_kvmhost/validation_analysis.json (summary; step 4 path)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys_path = REPO / "scripts"
import sys  # noqa: E402
# env must be set BEFORE importing analyze_dynamic_tenant_static_arms (its
# solo-baseline constant is read at import time).
os.environ.setdefault("DYN_TENANT_CALIB_JSON",
                      str(REPO / "results/dyn_tenant_kvmhost/contract/calibration.json"))
os.environ.setdefault("DYN_TENANT_ARM_RESULTS_ROOT",
                      str(REPO / "results/dyn_tenant_kvmhost"))
sys.path.insert(0, str(sys_path))
import analyze_dynamic_tenant_static_arms as sa  # noqa: E402

RESULTS = Path(os.environ.get("DYN_TENANT_VALIDATION_RESULTS_ROOT",
                              str(REPO / "results/dyn_tenant_kvmhost")))
IOX_SCHEDULE_JSON = Path(os.environ.get(
    "DYN_TENANT_SCHEDULE_JSON",
    str(REPO / "results/dyn_tenant_kvmhost/schedule_feasible_iox_only.json")))
V3_SCHEDULE_JSON = Path(os.environ.get(
    "DYN_TENANT_V3_SCHEDULE_JSON",
    str(REPO / "results/dyn_tenant_kvmhost/schedule_feasible.json")))
SEED_OFFSETS = (101, 102, 103)
SEED_BASE_FMT = 20261103
DYN_SEQ_FMT = "DYN_valid_f1_arbfb_s{off}"
BFQ_SEQ_FMT = "DYN_valid_n3_native_bfq_s{off}"
MICROBENCH_CLASSES = ("switch_native", "attach_io_ext", "detach_io_ext", "swap_q")


def run_analysis(seq: str) -> dict | None:
    path = RESULTS / seq
    if not path.is_dir():
        return None
    try:
        return sa.analyze_sequence(seq, sa.load_solo())
    except Exception as exc:
        return {"sequence_id": seq, "analysis_error": repr(exc)}


def transitions_of(seq: str) -> list[dict]:
    """Executed phase_action records from the run's timeline stat."""
    stat_files = sorted((RESULTS / seq / "step_0").glob("ib_rdyn_*/timeline_*.json"))
    if not stat_files:
        return []
    doc = json.loads(stat_files[0].read_text())
    out = []
    for b in doc.get("boundaries") or []:
        for a in b.get("actions") or []:
            if a.get("type") in MICROBENCH_CLASSES or a.get("type"):
                if a.get("type") in (None, "none"):
                    continue
                out.append({
                    "boundary": b["boundary"], "type": a["type"],
                    "executed": a.get("executed"),
                    "duration_sec": a.get("duration_sec"),
                    "rc": a.get("rc"),
                    "drain": a.get("drain"),
                    "scheduler_after": a.get("scheduler_after"),
                    "loaders_started": a.get("loaders_started"),
                    "loaders_stopped": a.get("loaders_stopped"),
                    "error": a.get("error"),
                })
    return out


def main() -> None:
    (RESULTS / "analysis").mkdir(parents=True, exist_ok=True)
    sa.RESULTS_ROOT = RESULTS
    sched = json.loads(IOX_SCHEDULE_JSON.read_text())
    v3 = json.loads(V3_SCHEDULE_JSON.read_text()) if V3_SCHEDULE_JSON.exists() else None
    dp = sched["candidate_A_dp_schedule"]
    decision = sched.get("decision") or {}
    num_transitions = int(dp["num_transitions"])
    matrix: dict = {}
    rows: list[dict] = []
    for off in SEED_OFFSETS:
        seq_base = SEED_BASE_FMT + off
        dyn_seq, bfq_seq = DYN_SEQ_FMT.format(off=off), BFQ_SEQ_FMT.format(off=off)
        dyn, bfq = run_analysis(dyn_seq), run_analysis(bfq_seq)
        row: dict = {
            "seed_offset": off,
            "seed_base": seq_base,
            "per_tenant_seeds": {t: seq_base + i
                                 for i, t in enumerate("abcd", start=1)},
        }
        for arm, res in (("dynamic", dyn), ("bfq_reference", bfq)):
            if res is None:
                row[arm] = {"missing": True}
                continue
            row[arm] = {
                "sequence_id": res.get("sequence_id"),
                "weighted_tail_loss": res.get("weighted_tail_loss"),
                "guardrail_feasible": (res.get("guardrails") or {}).get("feasible"),
                "guardrail_violations": (res.get("guardrails") or {}).get("violations"),
                "max_boundary_drift_sec": res.get("max_boundary_drift_sec"),
                "scene_errors": res.get("scene_errors"),
                "fio_return_codes": res.get("fio_return_codes"),
                "p99_ratio_mean": {t: (res["per_tenant"][t] or {}).get("p99_ratio_mean")
                                   for t in ("a", "b", "c", "d")
                                   if t in res.get("per_tenant", {})},
                "mib_s_mean": {t: (res["per_tenant"][t] or {}).get("mib_s")
                               for t in ("a", "b", "c", "d")
                               if t in res.get("per_tenant", {})},
                "a_p99_us_by_phase": {str(p["phase"]): p["p99_us"]
                                      for p in (res["per_tenant"].get("a", {}).get("phases") or [])},
            }
        if (row.get("dynamic") or {}).get("weighted_tail_loss") is not None and \
                (row.get("bfq_reference") or {}).get("weighted_tail_loss") is not None:
            ld, lb = row["dynamic"]["weighted_tail_loss"], row["bfq_reference"]["weighted_tail_loss"]
            row["paired_delta_vs_bfq"] = {
                "loss_dynamic": ld, "loss_bfq": lb,
                "delta_abs": ld - lb, "delta_rel": (ld - lb) / lb if lb else None,
                "a_p99_by_phase_dynamic": row["dynamic"]["a_p99_us_by_phase"],
                "a_p99_by_phase_bfq": row["bfq_reference"]["a_p99_us_by_phase"],
            }
        row["transitions_executed"] = transitions_of(dyn_seq) \
            if (row.get("dynamic") or {}).get("sequence_id") else None
        matrix[f"s{off}"] = row
        rows.append(row)
        # per-seed analysis file mirrors the static-arms convention
        dyn_res, bfq_res = run_analysis(dyn_seq), run_analysis(bfq_seq)
        (RESULTS / "analysis" / f"valid_s{off}_analysis.json").write_text(
            json.dumps({"seed_offset": off, "dynamic": dyn_res,
                        "bfq_reference": bfq_res}, indent=2) + "\n")

    # cross-seed synthesis (honest wording, contract-bounded)
    deltas = [r["paired_delta_vs_bfq"]["delta_rel"] for r in rows
              if "paired_delta_vs_bfq" in r]
    synthesis: dict = {}
    if deltas:
        signs = set("neg" if d < 0 else "pos" for d in deltas)
        synthesis = {
            "comparison": "dynamic(iox f1 whole-run) vs native bfq whole-run reference",
            "n_seeds_with_pair": len(deltas),
            "per_seed_rel_delta": deltas,
            "sign_consistent": len(signs) == 1,
            "mean_rel_delta": sum(deltas) / len(deltas),
            "spread_rel_delta": max(deltas) - min(deltas),
        }
        magnitude = abs(synthesis["mean_rel_delta"])
        synthesis["discernible_at_3_seeds"] = bool(
            synthesis["sign_consistent"]
            and magnitude > synthesis["spread_rel_delta"] / 2.0)

    # in-sample stability check: validation f1 seeds vs the matrix3 f1 figure
    matrix3_f1_loss = v3.get("best_static_trajectory_loss") if v3 else None
    f1_val_losses = [r["dynamic"]["weighted_tail_loss"] for r in rows
                     if (r.get("dynamic") or {}).get("weighted_tail_loss") is not None]
    f1_val_mean = sum(f1_val_losses) / len(f1_val_losses) if f1_val_losses else None
    f1_stability = {
        "matrix3_in_sample_dyn_f1_loss": matrix3_f1_loss,
        "validation_f1_losses_per_seed": f1_val_losses,
        "validation_f1_mean": f1_val_mean,
        "relative_drift_vs_matrix3": (f1_val_mean / matrix3_f1_loss - 1.0)
            if (f1_val_mean and matrix3_f1_loss) else None,
        "caveat": "matrix3 figure is a single in-sample run; independent-seed "
                  "drift is reported, not a verdict",
    }

    v3_loss = v3.get("schedule_hypothetical_trajectory_loss_7_8") if v3 else None
    v3_static = v3.get("best_static_trajectory_loss") if v3 else None
    summary = {
        "schema": "dynamic_tenant_validation_summary_kvmhost/1",
        "validation_seed_derivation": (
            f"seed_base_j = {SEED_BASE_FMT} + off; off in "
            "{101,102,103}; per-tenant seed = base + tenant index "
            "(contract 7.11 derivation rule); tuning base 20261103 NOT reused"),
        "schedule_under_validation": {
            "source": str(IOX_SCHEDULE_JSON),
            "arm_sequence": dp["arm_sequence"],
            "num_transitions": num_transitions,
            "decision": decision.get("winner"),
            "iox_only": sched.get("constraint", {}).get("iox_only"),
        },
        "executed_matrix": {
            "note": "num_transitions==0 => the dynamic arm runs as a whole-run "
                    "resident (matrix3-mechanism static arm); only the two "
                    "comparison families were executed per seed",
            "sequences": {
                "dynamic_whole_run": DYN_SEQ_FMT.format(off=101) + " | s102 | s103",
                "bfq_reference": BFQ_SEQ_FMT.format(off=101) + " | s102 | s103",
            },
        },
        "schedule_ledger": {
            "iox_only_executed": {
                "hypothetical_loss_7_8": dp.get("loss_7_8"),
                "best_static_loss_7_8": sched.get("candidate_B_whole_run_arms",
                                                  {}).get("best_loss_7_8"),
                "delta_vs_static": 0.0 if num_transitions == 0 else None,
                "num_transitions": num_transitions,
            },
            "native_mixed_v3_NOT_executed": {
                "reason": "20261104 amendment: dynamic trajectory must stay "
                          "under an iox policy the whole run (bfq at T5 "
                          "violates it).  The claimed -13.6% (in-sample "
                          "single run) was lost by CONSTRAINT, not by "
                          "evidence; not re-ranked post hoc.",
                "hypothetical_loss_7_8": v3_loss,
                "best_static_loss_7_8": v3_static,
                "delta_vs_static": (v3_loss - v3_static)
                    if (v3_loss is not None and v3_static is not None) else None,
                "delta_pct": (100.0 * (v3_loss - v3_static) / v3_static)
                    if (v3_loss is not None and v3_static is not None and v3_static)
                    else None,
                "bfq_T5_robustness": (
                    "the bfq T5 win exists only in the native-mixed model; in "
                    "the validation seeds the bfq whole-run reference is "
                    "measured directly per seed (see paired_delta_vs_bfq)"),
            },
        },
        "seeds": matrix,
        "paired_synthesis": {
            **synthesis,
            "identity_comparison_excluded": "dynamic vs f1-static = identity "
                                            "0 by construction (same arm); "
                                            "not a measurement",
        },
        "f1_stability_vs_matrix3_in_sample": f1_stability,
        "wording_contract": ("dynamic-vs-reference comparison is paired and "
                             "pre-declared; no claim of global optimality or "
                             "online adaptivity is admissible; the executed "
                             "schedule has num_transitions=0 so transition "
                             "overhead is honestly 'none executed'; the "
                             "native-mixed v3 numbers stay in the ledger "
                             "without any post-hoc re-ranking"),
    }
    (RESULTS / "validation_matrix.json").write_text(json.dumps(matrix, indent=2) + "\n")
    (RESULTS / "validation_analysis.json").write_text(
        json.dumps(summary, indent=2) + "\n")
    print(f"matrix -> {RESULTS / 'validation_matrix.json'}")
    print(f"summary -> {RESULTS / 'validation_analysis.json'}")
    for r in rows:
        line = f"s{r['seed_offset']}: "
        if "paired_delta_vs_bfq" in r:
            pd = r["paired_delta_vs_bfq"]
            line += (f"dyn_loss={pd['loss_dynamic']:.4f} bfq_loss={pd['loss_bfq']:.4f} "
                     f"delta={100 * pd['delta_rel']:+.3f}%")
        else:
            line += "INCOMPLETE"
        print(line)


if __name__ == "__main__":
    main()