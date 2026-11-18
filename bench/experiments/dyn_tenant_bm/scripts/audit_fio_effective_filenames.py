#!/usr/bin/env python3
"""Post-run audit for the bm root-disk rule: tenant fio must target REGION
FILES — never the raw block device /dev/nvme0n1.

Two evidence layers over the pulled sequence directory:

  1. timeline stat JSONs (timeline_*.json, written by the scene's stat()):
     every tenants_runtime.<t>.spawn_record.argv must contain EXACTLY ONE
     --filename and it must be <region_dir>/region_<t>.bin.  This is the
     authoritative check: the scene consumes the reserved tenant-spec key
     "filename" as the single fio --filename (20261106 fix; fio treats
     multiple --filename as a multi-file STRIPE, and the fio JSON echo only
     keeps the last option string, so the fio JSON alone is NOT sufficient
     evidence).
  2. tenant fio JSONs (*_fio.json): the parsed "job options".filename must be
     the region file (secondary, cross-checks layer 1 at the fio level).

fio JSONs may be prefixed with "fio: terminating on signal N" (boundary
SIGINT, scene-documented); parsing starts at the first '{'.

Usage:
  python3 audit_fio_effective_filenames.py \
      --region-dir /opt/ioext/dyn_tenant_regions \
      --result-dir <pulled sequence dir>
Exit 0 only when every found artifact passes.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path


def load_json_lenient(path: Path) -> dict:
    text = path.read_text(errors="replace")
    brace = text.find("{")
    if brace < 0:
        raise ValueError("no JSON object in file")
    return json.loads(text[brace:])


def filenames_in_argv(argv: list) -> list:
    out = []
    for arg in argv or []:
        if str(arg).startswith("--filename="):
            out.append(str(arg)[len("--filename="):])
    return out


def audit_stat_json(path: Path, region_dir: str, failures: list) -> int:
    """Layer 1: spawn argv single --filename check. Returns tenants checked."""
    doc = load_json_lenient(path)
    tenants = doc.get("tenants_runtime") or {}
    checked = 0
    for tenant, rec in sorted(tenants.items()):
        spawn = (rec or {}).get("spawn_record") or {}
        argv = spawn.get("argv") or []
        rel = f"{path.name}:{tenant}"
        got = filenames_in_argv(argv)
        expected = f"{region_dir}/region_{tenant}.bin"
        checked += 1
        if len(got) != 1:
            failures.append(f"{rel}: argv has {len(got)} --filename entries "
                            f"({got!r}); expected exactly 1")
        elif got[0] != expected:
            failures.append(f"{rel}: argv --filename {got[0]!r} != {expected!r}")
        if str(spawn.get("fio_target") or "") and str(spawn["fio_target"]) != expected:
            failures.append(f"{rel}: fio_target {spawn['fio_target']!r} != {expected!r}")
    return checked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region-dir", required=True)
    ap.add_argument("--result-dir", required=True)
    args = ap.parse_args()
    region_dir = args.region_dir.rstrip("/")
    result_dir = Path(args.result_dir)

    failures: list[str] = []

    # Layer 1: scene stat JSONs (authoritative)
    stat_jsons = sorted(glob.glob(str(result_dir / "**" / "timeline_*.json"), recursive=True))
    tenants_checked = 0
    for stat_path in stat_jsons:
        try:
            tenants_checked += audit_stat_json(Path(stat_path), region_dir, failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{Path(stat_path).name}: stat parse failed: {exc}")
    if stat_jsons and tenants_checked == 0:
        failures.append("timeline stat JSONs found but no tenants_runtime entries")

    # Layer 2: tenant fio JSONs (secondary cross-check)
    fio_jsons = sorted(result_dir.rglob("*_fio.json"))
    for path in fio_jsons:
        try:
            doc = load_json_lenient(path)
            jobs = doc.get("jobs") or []
            if not jobs:
                raise ValueError("no jobs array")
            for job in jobs:
                eff = str((job.get("job options") or {}).get("filename") or "")
                if not eff.startswith(region_dir + "/") or eff.startswith("/dev/"):
                    raise ValueError(f"effective filename {eff!r} is not a region file")
                if job.get("error"):
                    raise ValueError(f"fio job error {job.get('error')}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.relative_to(result_dir)}: {exc}")

    if not stat_jsons and not fio_jsons:
        print(f"[fio-audit] FAIL {result_dir}: no timeline_*.json and no *_fio.json "
              "found (pull missing?)")
        sys.exit(1)
    for line in failures:
        print(f"[fio-audit] FAIL {line}")
    status = "ALL OK" if not failures else f"{len(failures)} FAILURES"
    print(f"[fio-audit] {status} "
          f"({len(stat_jsons)} stat JSONs / {tenants_checked} tenant argvs, "
          f"{len(fio_jsons)} fio JSONs under {result_dir})")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
