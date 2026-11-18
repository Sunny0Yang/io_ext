#!/usr/bin/env python3
"""Audit the generated dyn-tenant-86 task YAMLs for the root-disk safety rule.

bm hard safety rule: tenant fio NEVER targets the raw block device; every
scene's per-tenant spec must carry filename=<REGION_DIR>/region_<t>.bin (the
scene's tenant-spec passthrough appends --filename AFTER its built-in
--filename=/dev/nvme0n1 and fio applies the LAST occurrence).  A scene whose
tenant spec lacks the filename override would silently fall back to the RAW
DEVICE -- this audit turns that into a hard failure BEFORE anything is
pushed or run.

Checks per YAML (all five/six pieces):
  1. yaml.safe_load parses the file (fail on parse errors);
  2. every scene based on scene/io_ext/timeline.yml that selects tenants
     has parameters.tenants.<t>.filename == <REGION_DIR>/region_<t>.bin
     for EVERY tenant key (the Mapping selects tenants; missing key = the
     scene default would run = RAW DEVICE = FAIL);
  3. the filename is a regular-file path, not /dev/* (and target_device is
     allowed to be the device name for scheduler/cgroup purposes);
  4. optional --expect-arms for the heldout config: the main-figure arm ids
     and the C-arm io_ext extras sanity (cgroup_tenant_controls/io_cost
     present for c1-c4);
  5. prints a per-file PASS/FAIL summary line (grep-able "[audit]").

Usage:
  python3 experiments/dyn_tenant_bm/scripts/audit_dyn_tenant_bm_yamls.py \
      --region-dir /opt/ioext/dyn_tenant_regions \
      --files <yml> [<yml> ...] [--expect-arms c1_io_max,c2_bfq_weight,...]
Exit 0 only when every file passes every check.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

TIMELINE_BASE = "scene/io_ext/timeline.yml"
TENANTS = ("a", "b", "c", "d")


def audit_file(path: Path, region_dir: str) -> tuple[bool, list[str]]:
    problems: list[str] = []
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        return False, [f"{path.name}: not a mapping"]
    scenes = doc.get("scenes") or {}
    checked = 0
    for scene_id, scene in sorted(scenes.items()):
        base = str((scene or {}).get("base_scene") or "")
        if base != TIMELINE_BASE:
            continue
        params = (scene or {}).get("parameters") or {}
        tenants = params.get("tenants")
        if not isinstance(tenants, dict) or not tenants:
            problems.append(
                f"{path.name}/{scene_id}: parameters.tenants is not a non-empty "
                "mapping -- tenant fio would target the RAW DEVICE")
            continue
        for tenant, spec in sorted(tenants.items()):
            checked += 1
            spec = spec if isinstance(spec, dict) else {}
            filename = str(spec.get("filename") or "")
            expected = f"{region_dir}/region_{tenant}.bin"
            if not filename:
                problems.append(
                    f"{path.name}/{scene_id}/t{tenant}: no filename override -- "
                    "RAW DEVICE fallback")
            elif filename != expected:
                problems.append(
                    f"{path.name}/{scene_id}/t{tenant}: filename {filename!r} != "
                    f"expected {expected!r}")
            elif filename.startswith("/dev/"):
                problems.append(
                    f"{path.name}/{scene_id}/t{tenant}: filename targets a device "
                    f"node {filename!r}")
            layout = spec.get("disk_layout") or {}
            if not (str(layout.get("offset")) == "0" and layout.get("size")):
                problems.append(
                    f"{path.name}/{scene_id}/t{tenant}: disk_layout not the region "
                    f"file span {{offset: 0, size: ...}}: {layout!r}")
    return (not problems), [f"{path.name}: {p}" for p in problems] + (
        [] if problems else [f"{path.name}: {checked} tenant specs OK"])


def audit_heldout_arms(path: Path, expect_arms: list[str]) -> tuple[bool, list[str]]:
    doc = yaml.safe_load(path.read_text())
    stage = doc.get("heldout_stage") or {}
    arms = list(stage.get("nine_arms") or [])
    if arms != expect_arms:
        return False, [f"{path.name}: heldout_stage.nine_arms {arms} != expected {expect_arms}"]
    seqs = {s.get("sequence_id") for s in doc.get("test_sequences") or []}
    for arm in expect_arms:
        if f"DYN_held_s201_{arm}" not in seqs:
            return False, [f"{path.name}: main-figure sequence DYN_held_s201_{arm} missing"]
    # C arms must carry their io.* extras copied from the static config
    c_arms = [a for a in expect_arms if a.startswith("c")]
    if c_arms:
        scenes = doc.get("scenes") or {}
        for arm in c_arms:
            scene = scenes.get(f"ib_rdyn_held_s201_{arm}") or {}
            io_ext = ((scene.get("parameters") or {}).get("io_ext")) or {}
            has = bool(io_ext.get("cgroup_tenant_controls") or io_ext.get("io_cost"))
            if not has:
                return False, [
                    f"{path.name}: held_s201_{arm} carries no cgroup_tenant_controls/"
                    "io_cost extras (unthrottled C arm!)"]
    return True, [f"{path.name}: heldout arms {len(expect_arms)} OK "
                 f"(incl. {len(c_arms)} C arms with io.* extras)"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region-dir", required=True,
                    help="expected region file dir (absolute, on bm)")
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--expect-arms", default=None,
                    help="comma-separated arm ids the heldout main figure must run")
    args = ap.parse_args()

    ok = True
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"[audit] FAIL {path}: missing")
            ok = False
            continue
        file_ok, notes = audit_file(path, args.region_dir.rstrip("/"))
        if path.name.endswith("heldout.yml") and args.expect_arms:
            arms_ok, arm_notes = audit_heldout_arms(
                path, [a.strip() for a in args.expect_arms.split(",") if a.strip()])
            file_ok = file_ok and arms_ok
            notes.extend(arm_notes)
        for note in notes:
            print(f"[audit] {'PASS' if file_ok else 'FAIL'} {note}")
        ok = ok and file_ok
    print(f"[audit] {'ALL OK' if ok else 'AUDIT FAILED'} "
          f"({len(args.files)} files, region_dir={args.region_dir})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
