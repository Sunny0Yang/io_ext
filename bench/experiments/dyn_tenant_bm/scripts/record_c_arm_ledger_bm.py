#!/usr/bin/env python3
"""Safety-ledger recording for the bm C-arm (io controller) runs.

Same mechanism as scripts/record_c_arm_ledger.py (the kvmhost C-arm
enablement ledger): one iox_safety ledger attempt per recorded run, events
appended with evidence, attempt finalized with the outcome.  The campaign
naming follows the kvmhost precedent (dyn_tenant_static_tuning) with the target
machine made explicit -- campaign ``dyn_tenant_static_tuning_bm``, metadata
machine=bm, target_device=nvme0n1 (ROOT DISK: tenant fio targets preallocated
region files only; C-arm io.* controls are applied to the per-tenant child
cgroups /sys/fs/cgroup/rdyn_<seq>_t{a..d} and the root io.cost model/qos for
C4, all read-back verified by the scene).

Called by run_dyn_tenant_bm.sh after every c_arms-stage sequence (completed /
failed).  The 86-relevant facts recorded per arm:
  - C1 io.max caps are DERIVED from the bm frozen contract (rates + 0.8*solo_d),
    never the kvmhost literals (gen prints the derivation into the stage log);
  - C4 io.cost model/qos are measured on bm (iocost_coef_gen testfile mode);
  - the run's effective-filename audit outcome (region files, never the raw
    block device).

Usage:
  python3 record_c_arm_ledger_bm.py --sequence DYN_arm_c1_io_max \
      --outcome completed|failed [--result-dir DIR] [--config FILE] [--note STR]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from lib.iox_safety import ledger  # noqa: E402

ROOT = str(REPO / "results/iox_policy_exploration")
CAMPAIGN = "dyn_tenant_static_tuning_bm"
MACHINE = "bm"

RUNNER = REPO / "experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sequence", required=True,
                    help="e.g. DYN_arm_c1_io_max")
    ap.add_argument("--outcome", required=True, choices=("completed", "failed"))
    ap.add_argument("--result-dir", default=None,
                    help="pulled result dir (evidence)")
    ap.add_argument("--config", default=None,
                    help="the audited task yml used for the run (evidence)")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    seq = args.sequence
    # Reuse an attempt dir left empty by a previous schema-failed run (the
    # runner's first-ever call used event_type "RUN", which the ledger schema
    # rejected AFTER create_attempt had already made the directory); a plain
    # re-run would create duplicate empty attempts.
    import glob as _glob
    prior = sorted(_glob.glob(str(Path(ROOT) / CAMPAIGN /
                                "attempts" / f"carm86_{seq}_*")))
    if len(prior) == 1:
        attempt_id = Path(prior[0]).name
        reuse = True
    elif not prior:
        attempt_id = f"carm86_{seq}_{stamp}"
        reuse = False
    else:
        print(f"ERROR: ambiguous existing attempts for {seq}: {prior}", file=sys.stderr)
        sys.exit(2)
    evidence = []
    if args.result_dir and Path(args.result_dir).is_dir():
        evidence.append(str(Path(args.result_dir).resolve()))
    if args.config and Path(args.config).is_file():
        evidence.append(str(Path(args.config).resolve()))
    if not reuse:
        ledger.create_attempt(
            ROOT, CAMPAIGN,
            metadata={
                "attempt_kind": "c_arm_run",
                "machine": MACHINE,
                "target_device": "nvme0n1",
                "device_mode": "root-disk region files (never raw device)",
                "sequence": seq,
                "outcome": args.outcome,
                "attempt_note": args.note or f"C-arm stage run {seq} on bm",
            },
            attempt_id=attempt_id,
            source_paths=[str(RUNNER)],
        )
    event = {
        "event_type": "POLICY",
        "severity": "info" if args.outcome == "completed" else "error",
        "name": f"c_arm_run_{args.sequence}",
        "evidence": evidence,
        "recovery": "not_needed" if args.outcome == "completed" else "pending",
        "confidence": "observed",
        "notes": (
            f"20261106 bm campaign C-arm run {args.sequence} outcome={args.outcome}. "
            "Machine bm (root disk nvme0n1, kernel 7.3.1-ioext2): tenant fio "
            "targets preallocated region files "
            "/opt/ioext/dyn_tenant_regions/region_<t>.bin only "
            "(post-run effective-filename audit enforced by the runner); C1-C3 "
            "io.* controls land on the per-tenant child cgroups, C4 writes the "
            "root io.cost model/qos (measured on bm via iocost_coef_gen "
            "testfile mode). Constants are DERIVED from the bm frozen contract "
            "(same formulas as kvmhost: rates + 0.8*solo_d + 2*solo-p99)."
            + (f" Note: {args.note}" if args.note else "")),
    }
    ledger.append_event(ROOT, CAMPAIGN, attempt_id, event)
    ledger.finalize_attempt(
        ROOT, CAMPAIGN, attempt_id,
        status="completed" if args.outcome == "completed" else "failed",
        outcome={"build_passed": True, "actually_ran": 1},
        notes=f"bm C-arm run {args.sequence}: {args.outcome}",
    )
    suffix = " (reused empty attempt)" if reuse else ""
    print(f"recorded attempt {attempt_id} in campaign {CAMPAIGN}{suffix}")
    print(ledger.derive_summary(ROOT, CAMPAIGN).get("totals"))


if __name__ == "__main__":
    main()
