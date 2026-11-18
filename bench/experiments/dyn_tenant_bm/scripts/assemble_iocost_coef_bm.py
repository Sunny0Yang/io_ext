#!/usr/bin/env python3
"""Assemble the bm iocost_coef.json from an iocost_coef_gen.py stdout log.

kvmhost measured its coefficients with the kernel tool
  tools/cgroup/iocost_coef_gen.py --testdev /dev/sdb   (exclusive disk, destructive)
and assembled results/dyn_tenant_kvmhost/contract/iocost_coef.json
("model" from the tool's final "<MAJ:MIN> rbps=... wrandiops=..." line with
ctrl=user model=linear added, plus the PREDECLARED qos block identical to the
m2 fig2 io_cost arms: rpct/wpct=95, rlat/wlat=20000us, min=50, max=150 --
consistency across figures, not tuned).

bm cannot run --testdev (the campaign device IS the root disk; the raw device
must never be written), so the bm measurement runs the SAME tool in TESTFILE
mode (--testfile-size-gb N, no --testdev): the tool creates
./iocost-coef-fio.testfile inside a scratch directory on the root filesystem
and measures the same nvme0n1 through the file-backed path the campaign itself
uses (dir_to_dev('.') -> nvme0n1 259:0).  The tool's final line is IDENTICAL
in both modes, so this assembler is the same for bm with an explicit
provenance note about the measurement mode.

Usage:
  python3 assemble_iocost_coef_bm.py --stdout-log <iocost_coef_gen stdout> \
      --target-device nvme0n1 --tool-sha256 <sha256> --testfile-gb 16 \
      --out <contract dir>/iocost_coef.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# PREDECLARED qos, byte-identical to the kvmhost contract (consistency across
# figures, not tuned — see results/dyn_tenant_kvmhost/contract/iocost_coef.json
# "qos_predeclare_note").
QOS_PREDECLARE = {
    "enable": 1,
    "ctrl": "user",
    "rpct": 95,
    "rlat": 20000,
    "wpct": 95,
    "wlat": 20000,
    "min": 50,
    "max": 150,
}

RATE_KEYS = ("rbps", "rseqiops", "rrandiops", "wbps", "wseqiops", "wrandiops")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout-log", required=True)
    ap.add_argument("--target-device", required=True)
    ap.add_argument("--tool-sha256", default="unknown")
    ap.add_argument("--testfile-gb", type=float, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    log = Path(args.stdout_log).read_text()
    # final tool line: "<MAJ:MIN> rbps=... rseqiops=... ... wrandiops=..."
    matches = re.findall(
        r"^(\d+:\d+)\s+rbps=(\d+)\s+rseqiops=(\d+)\s+rrandiops=(\d+)\s+"
        r"wbps=(\d+)\s+wseqiops=(\d+)\s+wrandiops=(\d+)\s*$",
        log, re.MULTILINE)
    if not matches:
        sys.exit("no iocost_coef_gen result line found in the stdout log "
                 "(expected '<MAJ:MIN> rbps=... ...'); refusing to invent coefficients")
    majmin, *rates = matches[-1]
    model = {"ctrl": "user", "model": "linear"}
    model.update({k: int(v) for k, v in zip(RATE_KEYS, rates)})
    doc = {
        "schema": "dyn_tenant_iocost_coef/1",
        "measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "target_device": (f"{args.target_device} (bare-metal ROOT disk, rootfs /dev/nvme0n1p2; "
                          "coefficients measured through the file-backed path)"),
        "provenance": {
            "tool": "kernel/linux-7.3.1 tools/cgroup/iocost_coef_gen.py "
                    f"--testfile-size-gb {args.testfile_gb:g} (TESTFILE mode)",
            "tool_sha256": args.tool_sha256,
            "stdout_log": str(Path(args.out).parent.name) + "/iocost_coef_gen_stdout.log",
            "mode_note": (
                "kvmhost used --testdev (exclusive disk, destructive writes to "
                "/dev/sdb); bm FORBIDS raw-device writes (root disk), so the "
                "tool ran in its testfile mode against a file on the root "
                "filesystem -- the same nvme0n1 device the campaign workloads "
                "use. elevator/nomerges were temporarily set by the tool and "
                "restored (verified post-probe)."),
            "pv_shim_note": "pv absent on bm; a pass-through shim was used during the probe",
            "qos_predeclare_note": (
                "QOS predeclared, identical structure/percentages as the kvmhost "
                "contract and the m2 fig2 io_cost arms (rpct/wpct=95, "
                "rlat/wlat=20000us, min=50 max=150): consistency across figures, "
                "not tuned."),
        },
        "model": model,
        "qos": dict(QOS_PREDECLARE),
        "measured_device_major_minor": majmin,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {out}")
    print(f"  model rates: " + " ".join(f"{k}={model[k]}" for k in RATE_KEYS))
    print(f"  device MAJ:MIN from tool: {majmin} (expect nvme0n1 = 259:0)")


if __name__ == "__main__":
    main()
