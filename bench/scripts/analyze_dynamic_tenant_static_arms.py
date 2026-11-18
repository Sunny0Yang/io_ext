#!/usr/bin/env python3
"""Per-arm per-phase metric extraction for the static tuning campaign.

Contract sources (frozen):
  docs/plans/dynamic_tenant_reconfiguration_experiment.md sections 7.3/7.7-7.9
  results/dynamic_tenant_virtme/contract/calibration.json (solo baselines)

For every sequence result dir under results/dynamic_tenant_static_arms/:
  - per-phase merged-clat_hist p99 per active tenant (read dir A/B/C,
    write dir D; 1s windows, coarseness 0 -- contract 7.7)
  - per-phase achieved throughput (hist bytes / phase length) and completion
    fraction vs the tenant's solo bandwidth (guardrail 7.9)
  - max zero-completion run per active tenant (guardrail 7.9)
  - boundary drift, unexpected rc / scene errors (fail-visible, 7.9)
  - weighted tail loss (7.8): loss = 0.6*L_A + 0.3*L_C + 0.1*(L_B + L_D)/2
    with L_x = mean over active phases of (phase p99 / solo p99 steady)

Usage:
  python3 scripts/analyze_dynamic_tenant_static_arms.py [SEQ ...]
  (default: every DYN_arm_* result dir present under the results root)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import analyze_dynamic_tenant_calibration as calib  # noqa: E402

RESULTS_ROOT = Path(__import__("os").environ.get("DYN_TENANT_ARM_RESULTS_ROOT",
    str(REPO / "results/dynamic_tenant_static_arms")))
CALIB_JSON = Path(__import__("os").environ.get("DYN_TENANT_CALIB_JSON",
    str(REPO / "results/dynamic_tenant_virtme/contract/calibration.json")))

# Contract 7.1 / timeline.py CONTRACT_TENANT_WINDOWS (inclusive, 1-based).
TENANT_SPANS = {"a": (1, 6), "b": (2, 3), "c": (3, 4), "d": (4, 5)}
TENANT_DIR = {"a": 0, "b": 0, "c": 0, "d": 1}  # read for A/B/C, write for D

LOSS_WEIGHTS = {"a": 0.6, "c": 0.3, "low_group_bd": 0.1}
GUARD_MIN_COMPLETION = 0.05
GUARD_NO_PROGRESS_SEC = 5.0
GUARD_MAX_DRIFT_SEC = 1.0
BOUNDARY_SIGINT_RCS = {0, 128, 130, 143, -2, -15}


def load_solo() -> Dict[str, Dict[str, float]]:
    data = json.loads(CALIB_JSON.read_text())
    out = {}
    for t in ("a", "b", "c", "d"):
        s = data["solo"][t]
        out[t] = {
            "p99_us_steady": float(s["p99_us_steady"]),
            "steady_mib_s": float(s["steady_mib_s"]),
        }
    return out


def boundary_epochs(stat: Dict[str, Any]) -> List[float]:
    """Absolute epoch seconds of the planned boundaries (0..num_phases)."""
    t0_epoch = float(stat["start_time"])
    t0_mono = float(stat["t0_monotonic"])
    return [t0_epoch + (float(b["planned_monotonic"]) - t0_mono)
            for b in stat["boundaries"]]


def analyze_sequence(seq: str, solo: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    seq_dir = RESULTS_ROOT / seq
    scene_dirs = sorted((seq_dir / "step_0").glob("ib_rdyn_*"))
    if not scene_dirs:
        raise FileNotFoundError(f"no step_0/ib_rdyn_* scene dir under {seq_dir}")
    scene_dir = scene_dirs[0]
    stat = calib.load_timeline_stat(scene_dir, scene_dir.name)
    seq_token = stat["timeline_id"]
    L = float(stat["phase_length_sec"])
    num_phases = int(stat["num_phases"])
    bnds = boundary_epochs(stat)

    errors = list(stat.get("errors") or [])
    # fio return codes of every boundary exit (fail-visible, 7.9)
    rcs: List[Dict[str, Any]] = []
    for rec in stat.get("tenants_runtime", {}).values():
        er = rec.get("exit_record") or {}
        rc = er.get("returncode")
        if rc is None:
            errors.append(f"tenant {rec.get('tenant')}: missing exit_record")
            continue
        if rc not in BOUNDARY_SIGINT_RCS:
            errors.append(f"tenant {rec.get('tenant')}: unexpected rc={rc}")
        rcs.append({"tenant": rec.get("tenant"), "rc": rc,
                    "exited_via": er.get("exited_via")})
    max_drift = max((abs(float(b.get("drift_sec", 0.0))) for b in stat.get("boundaries", [])),
                    default=0.0)
    if max_drift > GUARD_MAX_DRIFT_SEC:
        errors.append(f"max boundary drift {max_drift:.3f}s > {GUARD_MAX_DRIFT_SEC}s")

    per_tenant: Dict[str, Any] = {}
    losses: Dict[str, Optional[float]] = {}
    guardrails: Dict[str, Any] = {"violations": []}
    # Tenant spans come from the scene stat (the smoke sequences use shortened
    # windows; full-contract arms carry the frozen 7.1 spans).
    spans = {t: (int(w[0]), int(w[1]))
             for t, w in (stat.get("tenant_windows") or TENANT_SPANS).items()}
    for tenant in sorted(spans):
        start_p, end_p = spans[tenant]
        if end_p > num_phases:
            continue  # tenant not selected in this sequence
        direction = TENANT_DIR[tenant]
        try:
            hist = calib.read_hist_log(calib.hist_log_for(scene_dir, tenant, seq_token))
        except FileNotFoundError as exc:
            errors.append(str(exc))
            continue
        phases = []
        p99_ratios = []
        for p in range(start_p, min(end_p, num_phases) + 1):
            w0, w1 = bnds[p - 1], bnds[p]
            merged, n_win = calib.merge_hist_window(hist, direction, w0, w1)
            st = calib.hist_stats(merged)
            mib_s = st["ios"] * _bs_of(hist, direction) / 2**20 / (w1 - w0)
            solo_mib = solo[tenant]["steady_mib_s"]
            completion = mib_s / solo_mib if solo_mib > 0 else 0.0
            # zero-completion runs within the phase window
            series = calib.per_second_series(hist, direction, w0, w1)
            worst_zero, cur = 0, 0
            for item in series:
                if int(item.get("ios", 0)) == 0:
                    cur += 1
                    worst_zero = max(worst_zero, cur)
                else:
                    cur = 0
            phases.append({
                "phase": p,
                "hist_windows": n_win,
                "ios": st["ios"],
                "p50_us": st.get("p50_us"),
                "p99_us": st.get("p99_us"),
                "mib_s": round(mib_s, 3),
                "completion_vs_solo": round(completion, 4),
                "max_zero_completion_sec": worst_zero,
            })
            p99_ratios.append(st["p99_us"] / solo[tenant]["p99_us_steady"])
            if completion < GUARD_MIN_COMPLETION:
                guardrails["violations"].append(
                    f"tenant {tenant} phase {p}: completion {completion:.3f} < {GUARD_MIN_COMPLETION}")
            if worst_zero >= GUARD_NO_PROGRESS_SEC:
                guardrails["violations"].append(
                    f"tenant {tenant} phase {p}: {worst_zero}s zero-completion run")
        losses[tenant] = (sum(p99_ratios) / len(p99_ratios)) if p99_ratios else None
        per_tenant[tenant] = {
            "direction": "read" if direction == 0 else "write",
            "phases_active": [start_p, min(end_p, num_phases)],
            "phases": phases,
            "solo_p99_us_steady": solo[tenant]["p99_us_steady"],
            "solo_mib_s": solo[tenant]["steady_mib_s"],
            "p99_ratio_mean": losses[tenant],
        }

    loss = None
    if all(losses.get(t) is not None for t in ("a", "c")) and \
            losses.get("b") is not None and losses.get("d") is not None:
        loss = (LOSS_WEIGHTS["a"] * losses["a"] + LOSS_WEIGHTS["c"] * losses["c"]
                + LOSS_WEIGHTS["low_group_bd"] * (losses["b"] + losses["d"]) / 2.0)
    guardrails["feasible"] = (not guardrails["violations"]) and not errors

    return {
        "schema": "dynamic_tenant_static_arm_analysis/1",
        "sequence_id": seq,
        "scene_id": scene_dir.name,
        "seq_token": seq_token,
        "phase_length_sec": L,
        "num_phases": num_phases,
        "scheduler": stat.get("scheduler"),
        "fio_return_codes": rcs,
        "max_boundary_drift_sec": round(max_drift, 4),
        "scene_errors": errors,
        "per_tenant": per_tenant,
        "weighted_tail_loss": loss,
        "guardrails": guardrails,
    }


def _bs_of(hist: List, direction: int) -> int:
    for _ts, d, bs, _bins in hist:
        if d == direction:
            return bs
    return 0


def main() -> None:
    args = sys.argv[1:]
    seqs = args or sorted(p.name for p in RESULTS_ROOT.glob("DYN_arm_*")
                          if p.is_dir() and not p.name.endswith("_run1_noevidence")
                          and not p.name.endswith("_batch1_ratebug"))
    solo = load_solo()
    matrix: Dict[str, Any] = {}
    out_dir = RESULTS_ROOT / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    for seq in seqs:
        try:
            res = analyze_sequence(seq, solo)
        except Exception as exc:  # fail-visible per-arm, keep going
            res = {"schema": "dynamic_tenant_static_arm_analysis/1",
                   "sequence_id": seq, "analysis_error": repr(exc)}
        out = out_dir / f"{seq}_analysis.json"
        out.write_text(json.dumps(res, indent=2) + "\n")
        matrix[seq] = {
            "loss": res.get("weighted_tail_loss"),
            "guardrail_feasible": res.get("guardrails", {}).get("feasible"),
            "max_drift_sec": res.get("max_boundary_drift_sec"),
            "errors": res.get("scene_errors"),
            "analysis_error": res.get("analysis_error"),
        }
        print(f"{seq}: loss={res.get('weighted_tail_loss')} "
              f"feasible={res.get('guardrails', {}).get('feasible')} -> {out}")
    (out_dir / "tuning_matrix.json").write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
    print(f"matrix -> {out_dir / 'tuning_matrix.json'}")


if __name__ == "__main__":
    main()
