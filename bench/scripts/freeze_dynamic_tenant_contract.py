#!/usr/bin/env python3
"""Assemble the frozen contract manifest for the dynamic multi-tenant experiment.

Reads results/dynamic_tenant_virtme/contract/calibration.json (produced by
scripts/analyze_dynamic_tenant_calibration.py), verifies the "frozen" block is
complete, and writes results/dynamic_tenant_virtme/contract/frozen_manifest.json
containing:

  - the full frozen parameter set (timeline, workloads, rates, L, metric
    definition, loss weights, guardrail thresholds, candidate arms, seeds,
    tie-break, failure rules);
  - sha256 of every file that constitutes the frozen contract (scene, scene
    config, task YAMLs, generator, analyzer, runner, calibration.json, this
    contract document);
  - the calibration evidence numbers verbatim.

Fail-visible: refuses to write the manifest if a required frozen field is
missing/None, or if any hashed file is missing.

Usage:
  python3 scripts/freeze_dynamic_tenant_contract.py \
      [--contract-dir results/dynamic_tenant_virtme/contract]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CONTRACT_FILES = [
    "src/vm/scene/io_ext/timeline.py",
    "src/vm/configs/task/isol_bench/rig_dynamic_tenant/dynamic_tenant_smoke.yml",
    "src/vm/configs/task/isol_bench/rig_dynamic_tenant/dynamic_tenant_calibration.yml",
    "src/vm/configs/task/isol_bench/rig_dynamic_tenant/dynamic_tenant_virtme.yml",
    "src/vm/configs/scene/io_ext/timeline.yml",
    "scripts/gen_dynamic_tenant_yamls.py",
    "scripts/analyze_dynamic_tenant_calibration.py",
    "scripts/run_dynamic_tenant_virtme.sh",
    "docs/plans/dynamic_tenant_reconfiguration_experiment.md",
]

REQUIRED_FROZEN_FIELDS = [
    "formal_phase_length_sec",
    "rate_b_mib_s",
    "rate_c_mib_s",
    "d_rate",
    "d_pressure_fraction_of_solo",
    "seed_base",
    "service_preference",
    "loss_weights",
    "guardrail_thresholds",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--contract-dir", type=Path,
                    default=REPO_ROOT / "results/dynamic_tenant_virtme/contract")
    args = ap.parse_args()

    calib_path = args.contract_dir / "calibration.json"
    with open(calib_path) as f:
        calib = json.load(f)
    frozen = calib.get("frozen") or {}
    missing = [k for k in REQUIRED_FROZEN_FIELDS if frozen.get(k) in (None, "")]
    if frozen.get("formal_phase_length_sec") in (None, ""):
        missing.append("formal_phase_length_sec (L decision failed?)")
    if missing:
        print(f"REFUSING to freeze: missing frozen fields: {missing}", file=sys.stderr)
        sys.exit(1)
    if frozen.get("formal_phase_length_sec") is None:
        sys.exit(1)

    files = {rel: sha256_file(REPO_ROOT / rel) for rel in CONTRACT_FILES}
    files["results/dynamic_tenant_virtme/contract/calibration.json"] = sha256_file(calib_path)

    manifest = {
        "schema": "dynamic_tenant_frozen_manifest/1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "dynamic multi-tenant reconfiguration (IoExtTimelineScene)",
        "frozen_parameters": frozen,
        "calibration_evidence": {
            "solo": calib.get("solo"),
            "rates": calib.get("rates"),
            "t4_coexistence": calib.get("t4_coexistence"),
            "guardrails": calib.get("guardrails"),
            "L_decision": calib.get("L_decision"),
        },
        "explicitly_unfrozen": {
            "dynamic_arms": "switch_q placeholder; hotswitch gate FAILED "
                            "(results/iox_policy_exploration/hotswitch_gate/); "
                            "dynamic arms are placeholders per contract section 8",
            "scene_score": "IoExtTimelineScene.score() still 0.0 (reported scene gap)",
        },
        "scene_gap_report": "frozen B/C rates are NOT applied by the scene "
                            "(no fio --rate passthrough in _spawn_tenant); "
                            "reported, not changed",
        "contract_files_sha256": files,
        "contract_doc": "docs/plans/dynamic_tenant_reconfiguration_experiment.md",
    }

    out = args.contract_dir / "frozen_manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"wrote {out} ({len(files)} contract files hashed, "
          f"L={frozen['formal_phase_length_sec']}s, "
          f"rate_b={frozen['rate_b_mib_s']}M, rate_c={frozen['rate_c_mib_s']}M)")


if __name__ == "__main__":
    main()
