#!/usr/bin/env python3
"""Generate the kvmhost D2 rerun yaml from the checked-in paper_sdb d2_fairness.yml.

Patches applied (everything else is preserved verbatim so scene semantics do
not drift from the paper track):
  1. target_device / file_name           -> discovered experiment device
  2. io.cost linear model bps/iops       -> freshly measured constants
  3. io.max per-tenant share caps        -> share = ref_agg(cg) * w / sum(w)
     where ref_agg(cg) is the freshly measured contention aggregate for that
     tenant count (the old yaml used one stale 320 MiB/s divisor for all cg).
  4. test_sequences                      -> filtered to the 7 curated arms
     (none, bfq, io_max, io_cost, iox_arb_wdrr, iox_arb_slo, iox_arb_cost)

Usage:
  gen_d2_kvmhost_rerun_yaml.py --dev sdb --calib calib.json --out /tmp/x.yml

calib.json (produced by the runner's calibration stage):
  {"model_bps": int, "model_iops": int,
   "solo_mb_s": float, "contention_ref_mb_s": {"2": .., "4": .., "8": .., "16": ..}}
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SRC_YAML = REPO / "src/vm/configs/task/isol_bench/paper_sdb/d2_fairness.yml"

ARMS = ("none", "bfq", "io_bfq_weight", "io_max", "io_cost", "iox_arb_wdrr", "iox_arb_slo", "iox_arb_cost")
CGS = (2, 4, 8, 16)
ITERS = (1, 2, 3)

# 2026-11-15: q_ratelimit arm (io.max wbps mirror) with fresh-anchor shares.
QRL_CGS = (4,)

WANT_IDS = {
    f"D2_saturated_weighted_{arm}_cg{cg}_iter{it}"
    for arm in ARMS for cg in CGS for it in ITERS
} | {
    f"D2_saturated_weighted_iox_q_rl_cg{cg}_iter{it}"
    for cg in QRL_CGS for it in ITERS
}


def patch_cgroup(cg: dict, dev: str, model_bps: int, model_iops: int, sat_bytes: int | None) -> None:
    """Rewrite device + io.cost model + numeric io_max caps inside one cgroup map."""
    if not isinstance(cg, dict):
        return
    if cg.get("io_max") and isinstance(cg["io_max"], dict):
        for k in ("rbps", "wbps"):
            v = cg["io_max"].get(k)
            if isinstance(v, int) and sat_bytes:
                cg["io_max"][k] = sat_bytes
    if cg.get("io_cost") and isinstance(cg["io_cost"], dict):
        model = cg["io_cost"].get("model")
        if isinstance(model, dict) and model.get("model") == "linear":
            for k in ("rbps", "wbps"):
                model[k] = model_bps
            for k in ("rseqiops", "rrandiops", "wseqiops", "wrandiops"):
                model[k] = model_iops


def walk_params_io_ext(node, dev: str, model_bps: int, model_iops: int) -> None:
    """Apply device/model patch to every io_ext.cgroup / root_cgroup found."""
    if not isinstance(node, dict):
        return
    io_ext = node.get("io_ext")
    if isinstance(io_ext, dict):
        for key in ("cgroup", "root_cgroup"):
            cgc = io_ext.get(key)
            if isinstance(cgc, dict):
                cg = dict(cgc)
                cg.pop("path", None)
                cg.pop("controllers", None)
                patch_cgroup(cg, dev, model_bps, model_iops, None)
    for v in node.values():
        if isinstance(v, (dict, list)):
            walk_params_io_ext(v, dev, model_bps, model_iops)
    if isinstance(node, list):
        for v in node:
            walk_params_io_ext(v, dev, model_bps, model_iops)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True, help="experiment device letter, e.g. sdb")
    ap.add_argument("--calib", required=True, help="calibration json from the runner")
    ap.add_argument("--out", required=True)
    ap.add_argument("--runtime-sec", type=int, default=0,
                    help="override tenant fio runtime_sec (0 = keep source; 2026-11-14 contract: 20)")
    ap.add_argument("--ramp-sec", type=int, default=0,
                    help="override tenant fio ramp_time (0 = keep source; 2026-11-14 contract: 10)")
    args = ap.parse_args()

    calib = json.loads(Path(args.calib).read_text())
    model_bps = int(calib["model_bps"])
    model_iops = int(calib["model_iops"])
    ref = {int(k): float(v) for k, v in calib["contention_ref_mb_s"].items()}

    cfg = yaml.safe_load(SRC_YAML.read_text())
    dev = args.dev

    # 1. device rewrite (profiles + scene params + template params)
    def rewrite_dev(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "target_device" and v == "sdb":
                    node[k] = dev
                elif k == "file_name" and v == "/dev/sdb":
                    node[k] = f"/dev/{dev}"
                elif k == "disk_devices" and isinstance(v, list):
                    node[k] = [dev if x == "sdb" else x for x in v]
                rewrite_dev(v)
        elif isinstance(node, list):
            for v in node:
                rewrite_dev(v)

    rewrite_dev(cfg)

    # 2. io.cost model constants everywhere (templates + root profiles)
    def rewrite_model(node):
        if isinstance(node, dict):
            if node.get("model") == "linear" and "rbps" in node:
                for k in ("rbps", "wbps"):
                    node[k] = model_bps
                for k in ("rseqiops", "rrandiops", "wseqiops", "wrandiops"):
                    node[k] = model_iops
            for v in node.values():
                rewrite_model(v)
        elif isinstance(node, list):
            for v in node:
                rewrite_model(v)

    rewrite_model(cfg)

    # 3. per-tenant io_max shares per cg from fresh contention refs
    tg = cfg.get("tenant_groups", {})
    for gid, group in tg.items():
        if "io_max" not in gid:
            continue
        ncg = int(gid.rsplit("ncg", 1)[1])
        ref_bytes = ref.get(ncg, 0.0) * 1e6
        if ref_bytes <= 0:
            print(f"WARN: no contention ref for ncg={ncg}, leaving shares", file=sys.stderr)
            continue
        total_w = sum(range(1, ncg + 1))
        for idx, entry in enumerate(group.get("per_tenant") or [], start=1):
            cgmap = (((entry or {}).get("parameters") or {}).get("io_ext") or {}).get("cgroup") or {}
            iomax = cgmap.get("io_max")
            if isinstance(iomax, dict) and isinstance(iomax.get("rbps"), int):
                share = max(1, int(ref_bytes * idx / total_w))
                iomax["rbps"] = share
                iomax["wbps"] = share
    # root profile IB_io_max may also pin a share via template cgroup io_max
    # (numeric caps elsewhere are "max" neutrals; those stay untouched).

    # 2b. q_ratelimit arm (2026-11-15): per-tenant token-bucket rate caps at
    #     the fresh-anchor shares (io.max wbps mirror, q_ratelimit.bpf.c).
    #     rate_i = ref_agg(cg) * w_i / sum(w); bucket = rate * 0.5s (default).
    ref_mb = {int(k): float(v) for k, v in calib["contention_ref_mb_s"].items()}
    profs = cfg.setdefault("io_ext_policy_profiles", {})
    tg_all = cfg.setdefault("tenant_groups", {})
    for cg in QRL_CGS:
        src = profs.get(f"iox_arb_wdrr_cg{cg}")
        if not src:
            print(f"WARN: no iox_arb_wdrr_cg{cg} profile to mirror for q_rl", file=sys.stderr)
            continue
        rl = copy.deepcopy(src)
        rl["name"] = f"paper iox q_rl per-cgroup cg{cg} (fresh-anchor shares)"
        rl["io_ext"].pop("arbiters", None)
        n = cg
        total_w = sum(range(1, n + 1))
        rate_scale = float(os.environ.get("D2_QRL_RATE_SCALE", "1.0"))
        burst_ms = os.environ.get("D2_QRL_BURST_MS", "500")
        for entry in rl.get("cgroup_policies_from_groups") or []:
            mw = re.search(r"_w(\d+)_ncg", str(entry.get("tenant_group") or ""))
            if not mw:
                continue
            w = int(mw.group(1))
            share = max(1, int(round(ref_mb.get(cg, 0.0) * w / total_w * rate_scale)))
            entry["name_template"] = f"q_rl_w{w}"
            entry["path"] = "/opt/io-ext/userspace/loader_q_ratelimit"
            entry["args"] = ["-c", "{policy_cgroup_path}",
                             "--rate-mib-s", str(share),
                             "--burst-ms", burst_ms]
        profs[f"iox_q_rl_cg{cg}"] = rl
        # sequences: clone the iox arb sequences for this cg, swap policy
        for s in list(cfg.get("test_sequences") or []):
            m = re.match(r"^D2_saturated_weighted_iox_arb_wdrr_cg(%d)_iter(\d+)$" % cg,
                         s.get("sequence_id") or "")
            if not m:
                continue
            ns = copy.deepcopy(s)
            ns["sequence_id"] = f"D2_saturated_weighted_iox_q_rl_cg{cg}_iter{m.group(2)}"
            ns["name"] = f"D2 saturated weighted q_rl cg={cg} iter{m.group(2)}"
            ns["io_ext_policy"] = f"iox_q_rl_cg{cg}"
            cfg["test_sequences"].append(ns)

    # 3a. optional arbiter diagnostics: D2_ARB_DIAG_MS=<ms> adds
    #     --diag-interval-ms to every arbiter loader (ledger dump to the
    #     loader's stdout file inside the sequence result dir).
    diag_ms = os.environ.get("D2_ARB_DIAG_MS", "")
    if diag_ms:
        for prof in (cfg.get("io_ext_policy_profiles") or {}).values():
            for arb in ((prof.get("io_ext") or {}).get("arbiters") or []):
                arb["args"] = ["--diag-interval-ms", str(diag_ms)]
        print(f"arbiter diagnostics enabled: --diag-interval-ms {diag_ms}")

    # 3b. optional fio shape override (20s/10s read-budget contract, 2026-11-14)
    if args.runtime_sec or args.ramp_sec:
        for group in tg.values():
            fg = (((group or {}).get("template") or {}).get("parameters") or {}).get("fio_global") or {}
            if args.runtime_sec:
                fg["runtime_sec"] = args.runtime_sec
            if args.ramp_sec:
                fg["ramp_time"] = args.ramp_sec

    # 4. sequence filter
    seqs = cfg.get("test_sequences") or []
    kept = [s for s in seqs if s.get("sequence_id") in WANT_IDS]
    missing = WANT_IDS - {s.get("sequence_id") for s in kept}
    if missing:
        print(f"ERROR: {len(missing)} wanted sequences missing from source yaml", file=sys.stderr)
        sys.exit(1)
    cfg["test_sequences"] = kept

    hdr = (
        "# AUTO-GENERATED by scripts/gen_d2_kvmhost_rerun_yaml.py -- DO NOT EDIT\n"
        f"# device={dev} model_bps={model_bps} model_iops={model_iops}\n"
        f"# contention_ref_mb_s={json.dumps(calib['contention_ref_mb_s'], sort_keys=True)}\n"
        f"# arms={','.join(ARMS)} n_sequences={len(kept)}\n"
    )
    body = yaml.safe_dump(cfg, sort_keys=False, width=100)
    Path(args.out).write_text(hdr + body)
    print(f"wrote {args.out}: {len(kept)} sequences, device={dev}")


if __name__ == "__main__":
    main()
