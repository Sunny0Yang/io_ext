#!/usr/bin/env python3
"""Held-out stage analysis for the kvmhost dynamic-tenant experiment.

Inputs (real six-phase runs, same frozen contract as matrix3/validation:
L=90s, B=299M/C=198M, guardrails 7.9, loss 7.8, seeds by contract 7.11):

  1) MAIN-FIGURE seed s201 (base 20261304 = 20261103+201): nine static arms,
     one run each  (DYN_held_s201_{arm}); the schedule terminal state is the
     whole-run resident f1_arbfb (num_transitions=0, COINCIDE_DP_IS_WHOLE_RUN),
     so the f1 run carries both the dynamic(=resident) and best-static
     identities of the main figure.
  2) PAIRED seeds s201..s205 (bases 20261304..20261308): f1_arbfb (resident)
     vs native bfq (whole-run reference), same seed base per pair; the s201
     pair is shared with the main-figure group.

Outputs (results/dyn_tenant_kvmhost/):
  analysis/heldout_s201_<arm>_analysis.json  per-arm per-phase table
  analysis/heldout_paired_matrix.json        5-seed paired values
  heldout_matrix.json                        combined matrix of record
  heldout_analysis.json                      summary (step 4 path)

Cross-check target: the 3-seed validation conclusion (validation_matrix.json)
is compared SIGN-LEVEL ONLY (sign consistency / direction of the paired
delta), never by re-running or re-ranking the schedule.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("DYN_TENANT_CALIB_JSON",
                      str(REPO / "results/dyn_tenant_kvmhost/contract/calibration.json"))
os.environ.setdefault("DYN_TENANT_ARM_RESULTS_ROOT",
                      str(REPO / "results/dyn_tenant_kvmhost"))
sys.path.insert(0, str(REPO / "scripts"))
import analyze_dynamic_tenant_static_arms as sa  # noqa: E402

RESULTS = Path(os.environ.get("DYN_TENANT_HELDOUT_RESULTS_ROOT",
                              str(REPO / "results/dyn_tenant_kvmhost")))
# arm-set switch (user 20261106 criterion): kvmhost = 4 static iox arms;
# bm = 4 cgroup controller arms (C1-C4).  bm has only the main-figure seed
# s201 measured (the paired 202-205 extension is optional, not run).
if os.environ.get("DYN_TENANT_HELDOUT_ARM_SET", "kvmhost") == "bm":
    SEED_OFFSETS = (201,)
    NINE_ARMS = ("n1_native_none", "n2_native_mqddl", "n3_native_bfq",
                 "n4_native_kyber", "c1_io_max", "c2_bfq_weight",
                 "c3_io_latency", "c4_io_cost", "f1_arbfb")
    ARM_LABELS = (
        ("n1_native_none", "N1 native none"),
        ("n2_native_mqddl", "N2 native mq-deadline"),
        ("n3_native_bfq", "N3 native bfq"),
        ("n4_native_kyber", "N4 native kyber"),
        ("c1_io_max", "C1 cgroup io.max"),
        ("c2_bfq_weight", "C2 cgroup io.bfq.weight"),
        ("c3_io_latency", "C3 cgroup io.latency"),
        ("c4_io_cost", "C4 cgroup io.cost+io.weight"),
        ("f1_arbfb", "F1 / dynamic resident f1_arbfb"),
    )
else:
    SEED_OFFSETS = (201, 202, 203, 204, 205)
    NINE_ARMS = ("n1_native_none", "n2_native_mqddl", "n3_native_bfq",
                 "n4_native_kyber", "x1_qnone", "x2_qfifo_wdrr",
                 "x3_qddl_wdrr", "x4_spwedf_wdrr", "f1_arbfb")
    ARM_LABELS = (
        ("n1_native_none", "N1 native none"),
        ("n2_native_mqddl", "N2 native mq-deadline"),
        ("n3_native_bfq", "N3 native bfq"),
        ("n4_native_kyber", "N4 native kyber"),
        ("x1_qnone", "X1 io_ext q_none"),
        ("x2_qfifo_wdrr", "X2 io_ext q_fifo+wdrr"),
        ("x3_qddl_wdrr", "X3 io_ext q_ddl+wdrr"),
        ("x4_spwedf_wdrr", "X4 io_ext sp_edf+wedf+wdrr"),
        ("f1_arbfb", "F1 / dynamic resident f1_arbfb"),
    )
SEED_BASE_FMT = 20261103
MAIN_FIGURE_OFF = 201
F1_SEQ_FMT = "DYN_held_s{off}_f1_arbfb"
BFQ_SEQ_FMT = "DYN_held_s{off}_n3_native_bfq"
ARM_SEQ_FMT = "DYN_held_s{off}_{arm}"


def run_analysis(seq: str) -> dict | None:
    path = RESULTS / seq
    if not path.is_dir():
        return None
    try:
        return sa.analyze_sequence(seq, sa.load_solo())
    except Exception as exc:
        return {"sequence_id": seq, "analysis_error": repr(exc)}


def row_from(res: dict | None, seq: str) -> dict:
    if res is None:
        return {"sequence_id": seq, "missing": True}
    out = {
        "sequence_id": seq,
        "weighted_tail_loss": res.get("weighted_tail_loss"),
        "guardrail_feasible": (res.get("guardrails") or {}).get("feasible"),
        "guardrail_violations": (res.get("guardrails") or {}).get("violations"),
        "max_boundary_drift_sec": res.get("max_boundary_drift_sec"),
        "scene_errors": res.get("scene_errors"),
        "analysis_error": res.get("analysis_error"),
    }
    if res.get("per_tenant"):
        out["p99_ratio_mean"] = {
            t: (res["per_tenant"].get(t) or {}).get("p99_ratio_mean")
            for t in ("a", "b", "c", "d") if t in res["per_tenant"]}
        out["a_p99_us_by_phase"] = {
            str(p["phase"]): p["p99_us"]
            for p in (res["per_tenant"].get("a", {}) or {}).get("phases", [])}
    return out


def main() -> None:
    an = RESULTS / "analysis"
    an.mkdir(parents=True, exist_ok=True)

    # ---- 1) main-figure seed: all nine arms at s201 ----------------------
    s201: dict = {"seed_offset": MAIN_FIGURE_OFF,
                  "seed_base": SEED_BASE_FMT + MAIN_FIGURE_OFF}
    arms: dict = {}
    for arm in NINE_ARMS:
        seq = ARM_SEQ_FMT.format(off=MAIN_FIGURE_OFF, arm=arm)
        res = run_analysis(seq)
        row = row_from(res, seq)
        row["arm"] = arm
        if res is not None and res.get("per_tenant"):
            row["per_tenant_phases"] = {
                t: {"phases": (res["per_tenant"][t] or {}).get("phases"),
                    "p99_ratio_mean": (res["per_tenant"][t] or {}).get("p99_ratio_mean")}
                for t in res["per_tenant"]}
        arms[arm] = row
        if res is not None:
            (an / f"heldout_s201_{arm}_analysis.json").write_text(
                json.dumps(res, indent=2) + "\n")
    s201["arms"] = arms
    # native arms are expected infeasible under the iox contract framing; the
    # numbers stay real and fail-visible (figure keeps them)
    s201["infeasible_arms"] = [a for a, r in arms.items()
                               if r.get("guardrail_feasible") is False]

    # ---- 2) paired table: 5 seeds f1 vs bfq ------------------------------
    matrix: dict = {}
    rows: list[dict] = []
    for off in SEED_OFFSETS:
        seed_base = SEED_BASE_FMT + off
        f1_seq, bfq_seq = F1_SEQ_FMT.format(off=off), BFQ_SEQ_FMT.format(off=off)
        f1_res, bfq_res = run_analysis(f1_seq), run_analysis(bfq_seq)
        row: dict = {
            "seed_offset": off,
            "seed_base": seed_base,
            "per_tenant_seeds": {t: seed_base + i
                                 for i, t in enumerate("abcd", start=1)},
            "f1_resident": row_from(f1_res, f1_seq),
            "bfq_reference": row_from(bfq_res, bfq_seq),
            "note": "s201 pair shared with the main-figure run" if off == MAIN_FIGURE_OFF
                    else "held-out paired-only seed",
        }
        lf = row["f1_resident"].get("weighted_tail_loss")
        lb = row["bfq_reference"].get("weighted_tail_loss")
        if lf is not None and lb is not None:
            row["paired_delta_vs_bfq"] = {
                "loss_f1": lf, "loss_bfq": lb,
                "delta_abs": lf - lb,
                "delta_rel": (lf - lb) / lb if lb else None,
                "sign": "f1_better" if lf < lb else ("bfq_better" if lf > lb else "tie"),
            }
        matrix[f"s{off}"] = row
        rows.append(row)

    deltas = [r["paired_delta_vs_bfq"]["delta_rel"] for r in rows
              if "paired_delta_vs_bfq" in r]
    signs = [r["paired_delta_vs_bfq"]["sign"] for r in rows
             if "paired_delta_vs_bfq" in r]
    synthesis: dict = {}
    if deltas:
        synthesis = {
            "comparison": "f1_arbfb (schedule-terminal resident, iox) vs native bfq whole-run",
            "n_paired_seeds": len(deltas),
            "per_seed_rel_delta": deltas,
            "per_seed_abs_delta": [r["paired_delta_vs_bfq"]["delta_abs"] for r in rows
                                   if "paired_delta_vs_bfq" in r],
            "per_seed_signs": signs,
            "f1_better_seeds": signs.count("f1_better"),
            "mean_rel_delta": sum(deltas) / len(deltas),
            "spread_rel_delta": max(deltas) - min(deltas),
            "sign_consistent_f1_better": len(set(signs)) == 1 and signs[0] == "f1_better",
            "stability_note": ("dispersion check only (no inferential claim): with "
                               "5 paired seeds and a consistent sign, a claim that "
                               "the paired difference retains its sign across seeds "
                               "is admissible; magnitude beyond that is not"),
        }
    # sign-level check against the 3-seed VALIDATION conclusion (no re-ranking)
    val_path = RESULTS / "validation_matrix.json"
    val_check: dict = {}
    if val_path.exists() and synthesis:
        val = json.loads(val_path.read_text())
        val_deltas = [v["paired_delta_vs_bfq"]["delta_rel"]
                      for v in val.values() if "paired_delta_vs_bfq" in v]
        val_sign = "f1_better" if all(d < 0 for d in val_deltas) else "mixed"
        val_check = {
            "validation_conclusion": ("3/3 seeds: f1 loss < bfq loss (paired)"),
            "validation_rel_deltas": val_deltas,
            "validation_sign": val_sign,
            "heldout_n_seeds": len(deltas),
            "heldout_sign_consistent_f1_better": synthesis["sign_consistent_f1_better"],
            "heldout_sign": ("f1_better" if synthesis["sign_consistent_f1_better"] else "mixed"),
            "sign_level_agreement": val_sign == (
                "f1_better" if synthesis["sign_consistent_f1_better"] else "mixed"),
            "scope": "SIGN-LEVEL cross-check only; validation numbers are NOT merged into held-out statistics",
        }
        # f1 magnitude drift vs the validation-era f1 figures (informational)
        val_f1 = [v["dynamic"]["weighted_tail_loss"] for v in val.values()]
        held_f1 = [r["f1_resident"]["weighted_tail_loss"] for r in rows
                   if r["f1_resident"].get("weighted_tail_loss") is not None]
        if val_f1 and held_f1:
            val_check["f1_loss_mean_validation"] = sum(val_f1) / len(val_f1)
            val_check["f1_loss_mean_heldout"] = sum(held_f1) / len(held_f1)
            val_check["f1_mean_rel_drift"] = (sum(held_f1) / len(held_f1)) / (sum(val_f1) / len(val_f1)) - 1.0

    summary = {
        "schema": "dynamic_tenant_heldout_summary_kvmhost/1",
        "heldout_seed_derivation": (
            f"seed_base_j = {SEED_BASE_FMT} + off; off in "
            "{201,202,203,204,205}; per-tenant seed = base + tenant "
            "index (contract 7.11); tuning/validation bases NOT reused"),
        "schedule_terminal_state": {
            "arm_sequence": ["f1_arbfb"] * 6, "num_transitions": 0,
            "decision": "COINCIDE_DP_IS_WHOLE_RUN",
            "source": "results/dyn_tenant_kvmhost/schedule_feasible_iox_only.json",
        },
        "main_figure_matrix": {
            "seed_base": s201["seed_base"],
            "arms": {arm: {k: v for k, v in arm_row.items()
                           if k != "per_tenant_phases"}
                     for arm, arm_row in arms.items()},
            "infeasible_arms": s201["infeasible_arms"],
        },
        "paired_seeds": matrix,
        "paired_synthesis": synthesis,
        "validation_sign_level_check": val_check,
        "wording_contract": (
            "held-out comparisons are paired and predeclared; no claim of "
            "global optimality, online adaptivity or transition-robustness "
            "is admissible (the executed schedule has num_transitions=0); "
            "native-infeasible arms remain in the figure with real data "
            "and fail-visible guardrails"),
    }
    (RESULTS / "heldout_matrix.json").write_text(json.dumps({"schema":
        "dynamic_tenant_heldout_matrix/1", "main_figure_s201": s201,
        "paired_seeds": matrix}, indent=2) + "\n")
    (RESULTS / "heldout_analysis.json").write_text(
        json.dumps(summary, indent=2) + "\n")
    print(f"matrix -> {RESULTS / 'heldout_matrix.json'}")
    print(f"summary -> {RESULTS / 'heldout_analysis.json'}")
    for r in rows:
        if "paired_delta_vs_bfq" in r:
            pd = r["paired_delta_vs_bfq"]
            print(f"s{r['seed_offset']}: f1={pd['loss_f1']:.4f} bfq={pd['loss_bfq']:.4f} "
                  f"delta={100 * pd['delta_rel']:+.3f}% [{pd['sign']}]")
        else:
            print(f"s{r['seed_offset']}: INCOMPLETE")


if __name__ == "__main__":
    main()