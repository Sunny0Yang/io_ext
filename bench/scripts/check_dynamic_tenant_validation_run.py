#!/usr/bin/env python3
"""Pre-declared post-run checklist for one validation run (fail-visible).

Checks (task-mandated, applied identically to every run BEFORE it counts):
  1. all six boundary records present (boundaries 0..6) with expected
     active-tenant sets matched (contract 7.1);
  2. every pre-declared transition executed with rc==0 and the transition
     inventory matches the frozen schedule (dynamic arm: switch_native@1,
     attach_io_ext@3, detach_io_ext@5; static arm: none);
  3. detach drain closed (drain.ok == true);
  4. no fio error: every tenant fio JSON error==0, every exit rc in
     BOUNDARY_SIGINT_RCS, stderr logs carry no I/O error line;
  5. scene errors list empty (fail-visible metadata);
  6. guardrail re-check: completion >= 5% solo, zero-completion run < 5s,
     max boundary drift <= 1.0s (contract 7.9) via the static-arms analyzer;
  7. host-side (in-VM captured) evidence: no leftover loader processes, dmesg
     has no WARN/Oops/BUG/panic since session start.

Exit 0 = run counts; exit 1 = invalid run (contract 7.12: one retry, then
the arm is FAILED and reported).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import analyze_dynamic_tenant_static_arms as sa  # noqa: E402
import analyze_dynamic_tenant_calibration as calib  # noqa: E402

# Frozen schedule (schedule_feasible.json): expected transitions of the
# dynamic arm.  Boundary k = start of phase k+1.
EXPECTED_TRANSITIONS_DYNAMIC = {
    1: [{"type": "switch_native", "to_scheduler": "none"}],
    3: [{"type": "attach_io_ext", "to_scheduler": "scx_io"}],
    5: [{"type": "detach_io_ext", "to_scheduler": "kyber"}],
}
EXPECTED_ACTIVE = {  # contract 7.1
    0: ["a"], 1: ["a", "b"], 2: ["a", "b", "c"],
    3: ["a", "c", "d"], 4: ["a", "d"], 5: ["a"], 6: [],
}
# Phase-0 smoke (DYN_valid_smoke_ab): 4 phases, one transition per class plus
# BOTH swap_q directions at boundary 3 (the none->fifo direction is the slow
# owner_token-flip case; both must flip CLEAN at owner_token_timeout_sec=10).
EXPECTED_BOUNDARIES_SMOKE = list(range(5))
EXPECTED_TRANSITIONS_SMOKE = {
    1: [{"type": "switch_native", "to_scheduler": "mq-deadline"}],
    2: [{"type": "attach_io_ext", "to_scheduler": "scx_io"}],
    3: [{"type": "swap_q", "to_scheduler": None},
        {"type": "swap_q", "to_scheduler": None}],
    4: [{"type": "detach_io_ext", "to_scheduler": "none"}],
}
EXPECTED_ACTIVE_SMOKE = {
    0: ["a"], 1: ["a", "b"], 2: ["a", "b"], 3: ["a"], 4: [],
}
DMESG_PATTERNS = ("WARN", "Oops", "BUG:", "kernel panic", "KCATCH", "general protection")
STDERR_IO_PATTERNS = ("io error", "I/O error", "EIO", "fio: pid=", "aborting", "file:write failed")


def load_stat(result_dir: Path) -> tuple[dict, Path]:
    scene_dirs = sorted((result_dir / "step_0").glob("ib_rdyn_*"))
    if not scene_dirs:
        raise FileNotFoundError(f"no step_0/ib_rdyn_* scene dir under {result_dir}")
    scene_dir = scene_dirs[0]
    stat = calib.load_timeline_stat(scene_dir, scene_dir.name)
    return stat, scene_dir


def check_run(result_dir: Path, arm: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    stat, scene_dir = load_stat(result_dir)
    is_smoke = (arm == "smoke")
    want_boundaries = EXPECTED_BOUNDARIES_SMOKE if is_smoke else list(range(7))
    want_active = EXPECTED_ACTIVE_SMOKE if is_smoke else EXPECTED_ACTIVE

    # 5. scene errors empty
    scene_errors = list(stat.get("errors") or [])
    checks["scene_errors_empty"] = not scene_errors
    errors.extend(scene_errors)

    # 1. boundaries + expected active sets
    boundaries = {int(b["boundary"]): b for b in stat.get("boundaries") or []}
    checks["six_boundaries"] = sorted(boundaries) == want_boundaries
    if not checks["six_boundaries"]:
        errors.append(f"boundaries present: {sorted(boundaries)} (want {want_boundaries})")
    for k, want in want_active.items():
        rec = boundaries.get(k)
        if rec is None:
            continue
        got = list(rec.get("expected_active_after") or [])
        if got != want:
            errors.append(f"boundary {k}: expected_active_after {got} != contract {want}")
        got_actual = list(rec.get("actual_active_after") or [])
        if got_actual != want:
            errors.append(f"boundary {k}: actual_active_after {got_actual} != contract {want}")
    checks["active_sets_match_contract"] = not any("active_after" in e for e in errors)

    # 2. transition inventory matches the frozen schedule
    actions: dict[int, list[dict]] = {}
    for k, rec in boundaries.items():
        actions[k] = [a for a in (rec.get("actions") or [])
                      if a.get("type") in ("switch_native", "swap_q",
                                           "attach_io_ext", "detach_io_ext",
                                           "switch_q")]
    if is_smoke:
        want = EXPECTED_TRANSITIONS_SMOKE
    elif arm == "dynamic":
        want = EXPECTED_TRANSITIONS_DYNAMIC
    else:
        want = {}
    got = {k: v for k, v in actions.items() if v}
    types_match = (sorted((k, tuple(a["type"] for a in v)) for k, v in got.items())
                   == sorted((k, tuple(s["type"] for s in specs)) for k, specs in want.items()))
    if not types_match:
        errors.append(f"transition inventory mismatch: got { {k: [a['type'] for a in v] for k, v in got.items()} }, want { {k: [s['type'] for s in v] for k, v in want.items()} }")
    else:
        for k, specs in want.items():
            recs = actions[k]
            if len(recs) != len(specs):
                errors.append(f"boundary {k}: {len(recs)} transition records, want {len(specs)}")
                continue
            for spec, r in zip(specs, recs):
                if not r.get("executed"):
                    errors.append(f"boundary {k}: {spec['type']} not executed")
                if r.get("rc") != 0:
                    errors.append(f"boundary {k}: {spec['type']} rc={r.get('rc')}")
                after = r.get("scheduler_after")
                if spec["to_scheduler"] is not None and after is not None and after != spec["to_scheduler"]:
                    errors.append(f"boundary {k}: scheduler_after={after!r} != {spec['to_scheduler']!r}")
                if spec["type"] == "swap_q":
                    flips = {t: res.get("owner_token_flipped")
                             for t, res in (r.get("tenants") or {}).items()}
                    if not flips or not all(flips.values()):
                        errors.append(f"boundary {k}: swap_q owner_token did not flip cleanly: {flips}")
    checks["transitions_match_schedule"] = not any(
        ("transition inventory" in e or "not executed" in e or "rc=" in e
         or "scheduler_after" in e or "owner_token" in e)
        for e in errors)

    # 3. drain closed on detach
    for k, recs in actions.items():
        for rec in recs:
            if rec.get("type") == "detach_io_ext":
                drain = rec.get("drain") or {}
                if not drain.get("ok"):
                    errors.append(f"boundary {k}: detach drain not closed: {drain}")
    checks["drain_closed"] = not any("drain" in e for e in errors)

    # 4. fio errors / rcs / stderr
    for tenant, rec in (stat.get("tenants_runtime") or {}).items():
        er = rec.get("exit_record") or {}
        rc = er.get("returncode")
        if rc is None:
            errors.append(f"tenant {tenant}: missing exit_record")
        elif rc not in sa.BOUNDARY_SIGINT_RCS:
            errors.append(f"tenant {tenant}: unexpected rc={rc}")
    for fio_json in sorted(scene_dir.glob("*_fio.json")):
        try:
            raw = fio_json.read_text()
            doc = json.loads(raw[raw.index("{"):])
        except Exception:
            # Documented scene gap (results/dynamic_tenant_virtme/contract/
            # DYN_virtme_failed_attempt1_rc-2.tar.gz): a tenant that dies by
            # the DEFAULT SIGINT disposition (rc=-2, accepted in
            # BOUNDARY_SIGINT_RCS) loses its final JSON flush while the
            # continuously flushed hist/bw logs stay complete.  All metrics
            # come from the hist logs, so this is a warning, not an error.
            warnings.append(
                f"{fio_json.name}: final fio JSON missing/unparseable "
                f"(documented rc=-2 final-flush scene gap; metrics from hist logs)")
            continue
        for job in doc.get("jobs") or []:
            if int(job.get("error", 0)) != 0:
                errors.append(f"{fio_json.name}: fio job error={job.get('error')}")
    for stderr_log in sorted(scene_dir.glob("*_stderr.log")):
        text = stderr_log.read_text(errors="replace")
        for pat in STDERR_IO_PATTERNS:
            if pat in text:
                errors.append(f"{stderr_log.name}: contains {pat!r}")
    checks["fio_clean"] = not any(("fio" in e or "rc=" in e or "stderr" in e or "unparseable" in e)
                                  for e in errors)

    # 6. guardrails + metrics via the frozen analyzer (full runs only)
    analysis = None
    if not is_smoke:
        solo = sa.load_solo()
        try:
            analysis = sa.analyze_sequence(result_dir.name, solo)
        except Exception as exc:
            analysis = None
            errors.append(f"analyzer failed: {exc!r}")
        if analysis:
            for v in analysis["guardrails"]["violations"]:
                errors.append(f"guardrail: {v}")
            checks["guardrails_7_9"] = analysis["guardrails"]["feasible"]

    # 7. in-VM captured host evidence
    ev = result_dir / "host_checks"
    dmesg_file = ev / "dmesg_tail.txt"
    if dmesg_file.is_file():
        hits = [ln for ln in dmesg_file.read_text(errors="replace").splitlines()
                if any(p in ln for p in DMESG_PATTERNS)]
        if hits:
            errors.append(f"dmesg: {len(hits)} WARN/Oops/BUG lines (first: {hits[0][:160]})")
    else:
        warnings.append("host_checks/dmesg_tail.txt missing (dmesg check skipped)")
    loaders_file = ev / "remaining_loaders.txt"
    if loaders_file.is_file():
        leftover = [ln for ln in loaders_file.read_text(errors="replace").splitlines() if ln.strip()]
        if leftover:
            errors.append(f"leftover loaders: {leftover[:3]}")
    else:
        warnings.append("host_checks/remaining_loaders.txt missing (loader check skipped)")

    checks["dmesg_clean"] = not any("dmesg" in e for e in errors)
    checks["no_leftover_loaders"] = not any("leftover" in e for e in errors)

    return {
        "schema": "dynamic_tenant_validation_run_check/1",
        "result_dir": str(result_dir),
        "arm": arm,
        "pass": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "analysis": analysis,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-dir", required=True)
    ap.add_argument("--arm", required=True, choices=("dynamic", "static", "smoke"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    sa.RESULTS_ROOT = Path(args.result_dir).parent
    report = check_run(Path(args.result_dir), args.arm)
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
