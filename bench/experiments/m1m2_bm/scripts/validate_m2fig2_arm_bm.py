#!/usr/bin/env python3
"""Per-arm PASS validator for the bare-metal bm M2' Fig.2 replication run.

Validates one retrieved sequence directory (the layout produced by
scripts/run_m2fig2_bm.sh after tar+scp retrieval:

    <results>/<SEQUENCE>/step_0/ib_m2f2_<arm>_t_{1,2,3}/
        fio_<scene>_<ts>.json   (scene collected_data with start_time)
        <scene>_jobs1_fio.json   (raw fio json)
        <scene>_jobs1_..._bw_bw.1.log  (fio bw timeline, log_avg_msec=1000)

which is the exact layout scripts/plot_m2_fig2.py consumes (kvmhost
results/m2_fig2_virtme parity).

PASS criteria (docs/plans/m2_fig2_replication_experiment.md + the kvmhost
pipeline's executor+validate gate), checked fail-visibly per arm:

  hard (FAIL):
    - metadata.json / run_results.json present; sequence_id matches;
      metadata reports no failed scenes
    - all 3 tenant scene dirs present, each with a raw fio json reporting
      > 0 completed read IOs, and a parsable bw timeline with >= 1 sample
    - (e) io.max arm: per-tenant max <= 1.10 x its io.max cap
      (1500/500/500 MiB/s at the bm calibration) -- a violation means the
      knob did not apply and the arm's data is invalid
  soft (WARN, reported but not failing; qualitative shapes are observed
    evidence per the plan doc, not gates):
    - timeline coverage below 80% of the tenant runtime (50/60/30 samples
      at log_avg_msec=1000) -- expected for suppressed tenants on the
      b/d/f knob arms (io.prio idle, bfq weight=1, io.latency), whose
      fio workers legitimately stop issuing
    - per-tenant peak above 1.15 x rate_mib_s (fio rate-pacing burst
      artifacts after throttle starvation, seen in accepted kvmhost data)
    - A-alone plateau (first 8 samples) below 60% of the rate cap
    - (b)/(f): A not dominating B/C
    - (d)/(h): no visible weight separation between A and C
    - (e): a tenant stalling below 25% of its cap while contended

Usage:
  python3 experiments/m1m2_bm/scripts/validate_m2fig2_arm_bm.py \
      --results experiments/m1m2_bm/runs/m2fig2/<RUN_ID> \
      --calibration experiments/m1m2_bm/configs/m2fig2/calibration_bm.json \
      [--sequence M2F2_e_io_max | --all]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from lib.benchmark.fio_time_log import parse_fio_time_log  # noqa: E402

TENANT_IDX = {"A": 1, "B": 2, "C": 3}
TENANT_RUNTIME = {"A": 50, "B": 60, "C": 30}   # A 0-50, B 10-70, C 20-50
PAPER_IOMAX = {"A": 1500, "B": 500, "C": 500}   # MiB/s legend, scaled by cap/1536
COVERAGE_FRACTION = 0.8
RATE_MARGIN = 1.15
IOMAX_MARGIN = 1.10

SOFT_CHECKS = {
    "b_mqdl_prio": "A(promote-to-rt) should dominate B/C",
    "f_io_latency": "A(20us target) should be protected over B/C",
    "d_bfq_weight": "A(1000) should separate from C(100)",
    "h_io_cost_weight": "A(1000) should separate from C(100)",
}


def detect_scale(values: List[float], rate_mib_s: float) -> float:
    """Same heuristic as scripts/plot_m2_fig2.py: fio logs KiB/s or B/s."""
    if not values:
        return 1024.0
    reference = sorted(v for v in values if v > 0)
    if not reference:
        return 1024.0
    plateau = reference[int(len(reference) * 0.9) - 1]
    if plateau / 1024.0 > rate_mib_s * 1.5:
        return 1024.0 * 1024.0  # bytes/s
    return 1024.0


def load_tenant_series(scene_dir: Path, rate_mib_s: float) -> Tuple[Optional[Dict[str, object]], List[str]]:
    errors: List[str] = []
    stat_files = sorted(scene_dir.glob(f"fio_{scene_dir.name}_*.json"))
    if not stat_files:
        errors.append(f"missing scene stat json (fio_{scene_dir.name}_*.json)")
    start_time = None
    for stat_file in stat_files:
        try:
            data = json.loads(stat_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data.get("start_time"), (int, float)):
            start_time = float(data["start_time"])
            break
    if start_time is None:
        errors.append("no start_time in scene stat json")

    raw_fio = sorted(scene_dir.glob(f"{scene_dir.name}_jobs*_fio.json"))
    read_ios = 0
    for path in raw_fio:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for job in data.get("jobs") or []:
            read = job.get("read") or {}
            read_ios += float(read.get("total_ios") or 0) or float(read.get("io_bytes") or 0)
    if read_ios <= 0:
        errors.append("no completed read IOs in raw fio json")

    logs = sorted(
        p for p in scene_dir.glob("*bw*.log")
        if not any(token in p.name for token in ("iops", "clat", "slat", "lat."))
    )
    if not logs:
        errors.append("missing bw timeline log")
        return None, errors
    samples: List[float] = []
    scale = None
    for log_path in logs:
        parsed = parse_fio_time_log(log_path)
        if scale is None:
            scale = detect_scale([s.value for s in parsed], rate_mib_s)
        for sample in parsed:
            if sample.direction is not None and sample.direction != 0:
                continue
            samples.append(sample.value / scale)
    if not samples:
        errors.append("bw timeline log has no read samples")
        return None, errors
    return {"start_time": start_time, "samples": samples, "bw_log": logs[0].name}, errors


def validate_arm(results: Path, seq: str, rate_mib_s: float, iomax_caps: Dict[str, float]) -> Tuple[bool, List[str], Dict[str, Dict[str, float]]]:
    arm = seq[len("M2F2_"):]
    result_dir = results / seq
    errors: List[str] = []
    stats: Dict[str, Dict[str, float]] = {}

    if not result_dir.is_dir():
        return False, [f"missing sequence dir: {result_dir}"], stats

    for name, key in (("metadata.json", "metadata"), ("run_results.json", "run_results")):
        if not (result_dir / name).is_file():
            errors.append(f"missing {name}")
    if errors:
        return False, errors, stats

    metadata = json.loads((result_dir / "metadata.json").read_text())
    if metadata.get("sequence_id") != seq:
        errors.append(f"metadata sequence_id {metadata.get('sequence_id')!r} != {seq!r}")
    failed_scenes = metadata.get("failed_scenes") or {}
    if failed_scenes:
        errors.append(f"metadata contains {len(failed_scenes)} failed scene(s): {sorted(failed_scenes)[:3]}")

    step_dir = result_dir / "step_0"
    if not step_dir.is_dir():
        errors.append(f"missing step_0: {step_dir}")
        return False, errors, stats

    tenants: Dict[str, Dict[str, object]] = {}
    for tenant, idx in TENANT_IDX.items():
        matches = sorted(step_dir.glob(f"ib_m2f2_{arm}_t_{idx}"))
        if not matches:
            errors.append(f"missing tenant scene ib_m2f2_{arm}_t_{idx}")
            continue
        series, tenant_errors = load_tenant_series(matches[0], rate_mib_s)
        errors.extend(f"{tenant}: {e}" for e in tenant_errors)
        if series is not None:
            tenants[tenant] = series

    for tenant, series in tenants.items():
        samples = [float(v) for v in series["samples"]]  # type: ignore
        runtime = TENANT_RUNTIME[tenant]
        if len(samples) < COVERAGE_FRACTION * runtime:
            # suppressed tenants on b/d/f arms legitimately stop issuing;
            # coverage is reported evidence, not a validity gate
            print(f"  WARN {seq} {tenant}: timeline coverage {len(samples)} samples "
                  f"< {COVERAGE_FRACTION:.0%} of runtime {runtime}s")
        peak = max(samples) if samples else 0.0
        mean = sum(samples) / max(1, len(samples))
        stats[tenant] = {
            "samples": float(len(samples)),
            "mean_mib_s": round(mean, 1),
            "max_mib_s": round(peak, 1),
        }
        if peak > RATE_MARGIN * rate_mib_s:
            # fio rate-pacing bursts after starvation (seen in accepted
            # kvmhost data) -- evidence, not a gate
            print(f"  WARN {seq} {tenant}: peak {peak:.0f} MiB/s exceeds fio rate cap "
                  f"x{RATE_MARGIN} ({rate_mib_s:.0f})")
        if arm == "e_io_max":
            cap = iomax_caps[tenant]
            if peak > IOMAX_MARGIN * cap:
                errors.append(
                    f"{tenant}: peak {peak:.0f} MiB/s exceeds io.max cap {cap:.0f} x{IOMAX_MARGIN} -- knob did not apply"
                )
            if peak < 0.25 * cap:
                print(f"  WARN {seq} {tenant}: peak {peak:.0f} MiB/s far below io.max cap {cap:.0f}")

    # soft shape checks (evidence reports, not PASS gates)
    if len(tenants) == 3:
        means = {t: stats[t]["mean_mib_s"] for t in TENANT_IDX}
        a_alone = [float(v) for v in tenants["A"]["samples"][:8]]  # type: ignore
        if a_alone and sum(a_alone) / len(a_alone) < 0.6 * rate_mib_s:
            print(f"  WARN {seq}: A-alone plateau {sum(a_alone)/len(a_alone):.0f} MiB/s "
                  f"< 60% of rate cap {rate_mib_s:.0f} (contention/pacing anomaly?)")
        if arm in ("b_mqdl_prio", "f_io_latency") and not (means["A"] > means["B"] and means["A"] > means["C"]):
            print(f"  WARN {seq}: expected A dominance not observed (means {means})")
        if arm in ("d_bfq_weight", "h_io_cost_weight") and abs(means["A"] - means["C"]) < 0.15 * means["A"]:
            print(f"  WARN {seq}: no visible weight separation A vs C (means {means})")

    return not errors, errors, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True,
                        help="run dir containing <SEQUENCE>/ subdirs (kvmhost m2_fig2 layout)")
    parser.add_argument("--sequence", default=None, help="e.g. M2F2_e_io_max")
    parser.add_argument("--all", action="store_true", help="validate every sequence present under --results")
    parser.add_argument("--calibration", type=Path,
                        default=Path(__file__).resolve().parents[1] / "configs" / "m2fig2" / "calibration_bm.json")
    args = parser.parse_args()

    rate_mib_s = 1536.0
    if args.calibration.is_file():
        calib = json.loads(args.calibration.read_text())
        rate_mib_s = float(calib.get("rate_mib_s") or calib.get("rate_mb_s") or 1536.0)
    else:
        print(f"WARN: calibration not found ({args.calibration}); using rate=1536")
    scale = rate_mib_s / 1536.0
    iomax_caps = {t: max(1.0, round(v * scale)) for t, v in PAPER_IOMAX.items()}

    if bool(args.sequence) == bool(args.all):
        print("specify exactly one of --sequence or --all", file=sys.stderr)
        return 2
    seqs = [args.sequence] if args.sequence else sorted(
        p.name for p in args.results.iterdir()
        if p.is_dir() and p.name.startswith("M2F2_")
    )
    if not seqs:
        print(f"no M2F2_* sequence dirs under {args.results}", file=sys.stderr)
        return 2

    failures = 0
    for seq in seqs:
        ok, errors, stats = validate_arm(args.results, seq, rate_mib_s, iomax_caps)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {seq}")
        for tenant in TENANT_IDX:
            s = stats.get(tenant)
            if s:
                cap = f" cap={iomax_caps[tenant]:.0f}" if seq == "M2F2_e_io_max" else ""
                print(f"    {tenant}: samples={s['samples']:.0f} mean={s['mean_mib_s']:.1f} "
                      f"max={s['max_mib_s']:.1f} MiB/s{cap}")
        for e in errors:
            print(f"    - {e}")
        if not ok:
            failures += 1
    print(f"{'PASS' if failures == 0 else 'FAIL'}: {len(seqs) - failures}/{len(seqs)} sequences valid")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
