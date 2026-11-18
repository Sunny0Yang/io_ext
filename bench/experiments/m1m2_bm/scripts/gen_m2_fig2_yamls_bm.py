#!/usr/bin/env python3
"""Generate the M2' Fig.2-replication task YAMLs for bare-metal bm (nvme0n1).

bm adaptation of scripts/gen_m2_fig2_yamls.py (the kvmhost/virtme pipeline):
same 9 arms, same sequence ids (M2F2_*), same per-tenant cgroups, same
staggering (A 0-50s / B 10-70s / C 20-50s), same fio shape (64K randread
QD8, io_uring, direct, log_avg_msec=1000, write_bw_log). Deltas forced by
the bare-metal bm environment (experiments/m1m2_bm/LEDGER.md):

  1. target device nvme0n1 stays (scheduler switch + io.max MAJ:MIN are
     resolved by the scene from /dev/nvme0n1 dynamically - never hardcoded).
  2. the fio target is NOT the raw device: LEDGER decision 1 gives a single
     root-partition NVMe, so the workload reads a preallocated 32G file
     with direct=1 (/opt/ioext/m2fig2/data/m2f2_data.bin,
     one-time filled by scripts/run_m2fig2_bm.sh; probe/solo_data is the
     calibration evidence and is never touched).
  3. rate cap is fixed by the already-done solo calibration
     (configs/m2fig2/calibration_bm.json): solo 3305 MiB/s ->
     rate = min(1536, 0.8*solo) = 1536 MiB/s (paper cap, same truncation
     value as kvmhost). Recalibration is out of scope (LEDGER section 5).
  4. io.max is written on per-tenant CHILD cgroups (bare-metal kernel semantics:
     io.* control files exist only below the root cgroup).
  5. the io.cost linear model is the solo-64K-derived one recorded in
     calibration_bm.json (kvmhost deviation 2), not iocost_coef_gen's
     4K-based model.

Outputs (9+1 yamls) under experiments/m1m2_bm/configs/m2fig2/:
  m2_fig2_bm_all.yml                 all 9 sequences (canonical artifact)
  m2_fig2_bm_<SEQUENCE_ID>.yml       one-sequence config per arm (executor
                                     --config for run_m2fig2_bm.sh)

Usage:
  python3 experiments/m1m2_bm/scripts/gen_m2_fig2_yamls_bm.py \
      [--calibration experiments/m1m2_bm/configs/m2fig2/calibration_bm.json] \
      [--output-dir experiments/m1m2_bm/configs/m2fig2]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

EXP_DIR = Path(__file__).resolve().parent.parent          # experiments/m1m2_bm
REPO_ROOT = EXP_DIR.parent
DEFAULT_OUTPUT_DIR = EXP_DIR / "configs" / "m2fig2"
DEFAULT_CALIBRATION = DEFAULT_OUTPUT_DIR / "calibration_bm.json"

# ---- bm bare-metal constants -------------------------------------------------
TARGET_DEVICE = "nvme0n1"
# LEDGER decision 1: single root-partition NVMe -> preallocated file + direct=1.
# The file is created/filled ONE TIME by run_m2fig2_bm.sh (marker
# .m2f2_data_filled); probe/solo_data (the calibration evidence) is untouched.
DATA_FILE = "/opt/ioext/m2fig2/data/m2f2_data.bin"
DATA_SIZE_GB = 32                 # same size as the solo probe file (32G)

# Paper constants (IISWC'25 Fig. 2) - identical to the kvmhost generator.
PAPER_RATE_MIB_S = 1536.0         # 1.5 GiB/s fio rate limit
PAPER_IOMAX_A_MIB_S = 1500.0
PAPER_IOMAX_BC_MIB_S = 500.0
BFQ_TUNABLES = {"low_latency": 0}
RUNTIMES = {"a": 50, "b": 60, "c": 30}     # A 0-50, B 10-70, C 20-50
START_DELAYS = {"a": 0, "b": 10, "c": 20}
TENANT_ORDER = ("a", "b", "c")

# bm solo calibration (LEDGER section 5, 2026-11-04): solo 64K/QD8 direct
# randread = 3305 MiB/s -> rate = min(1536, 0.8*3305) = 1536 MiB/s.
DEFAULTS: Dict[str, Any] = {
    "rate_mib_s": 1536.0,
    "solo_mib_s": 3305.0,
    "iocost_model": None,
    "iocost_qos": {
        "enable": True, "ctrl": "user",
        "rpct": 95.0, "rlat": 20000, "wpct": 95.0, "wlat": 20000,
        "min": 50.0, "max": 150.0,
    },
}

ARMS: List[Dict[str, Any]] = [
    {
        "key": "a_none", "seq": "M2F2_a_none",
        "title": "(a) No knob or I/O scheduler",
        "scheduler": "none", "knob": None, "knob_values": None,
    },
    {
        "key": "b_mqdl_prio", "seq": "M2F2_b_mqdl_prio",
        "title": "(b) MQ-DL + io.prio.class",
        "scheduler": "mq-deadline", "knob": "io_prio_class",
        "knob_values": {"a": "promote-to-rt", "b": "idle", "c": "restrict-to-be"},
    },
    {
        "key": "c_bfq", "seq": "M2F2_c_bfq",
        "title": "(c) BFQ + no knob",
        "scheduler": "bfq", "knob": None, "knob_values": None, "bfq": True,
    },
    {
        "key": "d_bfq_weight", "seq": "M2F2_d_bfq_weight",
        "title": "(d) BFQ + io.bfq.weight",
        "scheduler": "bfq", "knob": "io_bfq_weight",
        "knob_values": {"a": 1000, "b": 1, "c": 100}, "bfq": True,
    },
    {
        "key": "e_io_max", "seq": "M2F2_e_io_max",
        "title": "(e) io.max",
        "scheduler": "none", "knob": "io_max", "knob_values": "iomax",
    },
    {
        "key": "f_io_latency", "seq": "M2F2_f_io_latency",
        "title": "(f) io.latency",
        "scheduler": "none", "knob": "io_latency",
        "knob_values": {"a": 20, "b": 1000, "c": 100},  # usec targets, paper legend
    },
    {
        "key": "g_io_cost", "seq": "M2F2_g_io_cost",
        "title": "(g) io.cost",
        "scheduler": "none", "knob": "io_cost", "knob_values": None,
    },
    {
        "key": "h_io_cost_weight", "seq": "M2F2_h_io_cost_weight",
        "title": "(h) io.cost + io.weight",
        "scheduler": "none", "knob": "io_cost",
        "knob_values": {"a": 1000, "b": 1, "c": 100},
    },
    {
        "key": "i_native_kyber", "seq": "M2F2_i_native_kyber",
        "title": "(i) Native kyber (added arm)",
        "scheduler": "kyber", "knob": None, "knob_values": None,
    },
]

# io.cost arms run before io.latency (which goes last) so a wedged io.latency
# run cannot poison them - identical ordering to the kvmhost generator.
SEQUENCE_ORDER = [
    "M2F2_a_none", "M2F2_b_mqdl_prio", "M2F2_c_bfq", "M2F2_d_bfq_weight",
    "M2F2_e_io_max", "M2F2_g_io_cost", "M2F2_h_io_cost_weight",
    "M2F2_i_native_kyber", "M2F2_f_io_latency",
]


def load_calibration(path: Optional[Path]) -> Dict[str, Any]:
    calib = dict(DEFAULTS)
    if path is None:
        return calib
    with open(path) as f:
        data = json.load(f)
    for key in ("rate_mib_s", "rate_mb_s", "solo_mib_s", "solo_mb_s",
                "iocost_model", "iocost_qos"):
        if data.get(key) is not None:
            calib[key] = data[key]
    # calibration_bm.json uses the task-mandated *_mb_s spellings; normalize.
    if data.get("rate_mb_s") is not None and data.get("rate_mib_s") is None:
        calib["rate_mib_s"] = float(data["rate_mb_s"])
    if data.get("solo_mb_s") is not None and data.get("solo_mib_s") is None:
        calib["solo_mib_s"] = float(data["solo_mb_s"])
    if calib.get("iocost_model") is None:
        # kvmhost fallback formula (run_m2_fig2_virtme.sh): a linear model at
        # the 64K workload scale derived from the solo probe - deviation 2.
        solo = float(calib["solo_mib_s"])
        rbps = int(solo * 1024 * 1024)
        calib["iocost_model"] = {
            "ctrl": "user", "model": "linear",
            "rbps": rbps, "rseqiops": max(1, rbps // 4096), "rrandiops": max(1, rbps // 65536),
            "wbps": rbps, "wseqiops": max(1, rbps // 4096), "wrandiops": max(1, rbps // 65536),
        }
    return calib


def iomax_values(calib: Dict[str, Any]) -> Dict[str, int]:
    """Scale the paper's 1500/500/500 MiB/s io.max caps to the rate cap."""
    cap = float(calib["rate_mib_s"])
    a = int(round(PAPER_IOMAX_A_MIB_S / PAPER_RATE_MIB_S * cap)) * 1024 * 1024
    bc = int(round(PAPER_IOMAX_BC_MIB_S / PAPER_RATE_MIB_S * cap)) * 1024 * 1024
    return {"a": a, "b": bc, "c": bc}


def fio_rate(calib: Dict[str, Any]) -> str:
    return f"{int(round(float(calib['rate_mib_s'])))}M"


def tenant_cgroup(arm_key: str, tenant: str) -> str:
    return f"/sys/fs/cgroup/ib_m2f2_{arm_key}_t_{tenant}"


def build_cgroup(arm: Dict[str, Any], tenant: str, calib: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tenant child cgroup spec; every non-io_cost arm scrubs sticky root io.cost."""
    cgroup: Dict[str, Any] = {
        "path": tenant_cgroup(arm["key"], tenant),
        "controllers": ["io"],
        "io_cost": {"qos": {"enable": False}},
    }
    knob, values = arm.get("knob"), arm.get("knob_values")
    if knob == "io_max":
        values = iomax_values(calib)
    if knob == "io_cost":
        cgroup["io_cost"] = {
            "model": calib["iocost_model"],
            "qos": calib["iocost_qos"],
        }
        if arm["knob_values"] is not None:
            cgroup["io_weight"] = values[tenant]
    elif knob == "io_max":
        # bm kernel semantics: io.max only exists on child cgroups; the scene
        # writes "<MAJ:MIN> rbps=..." there with the major:minor resolved
        # dynamically from /dev/nvme0n1 (never hardcoded 8:0).
        cgroup["io_max"] = {
            "rbps": values[tenant], "wbps": "max", "riops": "max", "wiops": "max",
        }
    elif knob is not None:
        cgroup[knob] = values[tenant]
    return cgroup


def build_tenant_group(arm: Dict[str, Any], calib: Dict[str, Any]) -> Dict[str, Any]:
    group_id = f"m2f2_{arm['key']}"
    scheduler = arm["scheduler"]
    rate = fio_rate(calib)

    def tenant_override(tenant: str) -> Dict[str, Any]:
        return {
            "parameters": {
                "job_name": f"m2f2_{arm['key']}_{tenant}",
                "fio_global": {
                    "runtime_sec": RUNTIMES[tenant],
                    "startdelay": START_DELAYS[tenant],
                },
                "io_ext": {
                    "cgroup": build_cgroup(arm, tenant, calib),
                },
            }
        }

    template = {
        "base_scene": "scene/io_ext/fio.yml",
        "parameters": {
            "target_device": TARGET_DEVICE,
            # bm delta (LEDGER decision 1): preallocated file + direct=1 on
            # the root fs instead of the raw device.
            "file_name": DATA_FILE,
            "data_file_size_gb": DATA_SIZE_GB,
            "scheduler": scheduler,
            "scheduler_tunables": BFQ_TUNABLES if arm.get("bfq") else {},
            "output_dir": f"./test_results/isol_bench/rig_m2_fig2_bm/{arm['key']}",
            "duration": 110,
            "drop_caches_before_run": False,
            "cooldown_sec": 1,
            "fio_global": {
                "ioengine": "io_uring",
                "direct": 1,
                "bs": "64k",
                "rw": "randread",
                "iodepth": 8,
                "numjobs": 1,
                "rate": rate,
                "runtime_sec": RUNTIMES["a"],
                "startdelay": START_DELAYS["a"],
                "time_based": True,
                "group_reporting": True,
                "log_avg_msec": 1000,
                "write_bw_log": "bw",
            },
            "io_ext": {
                "cleanup_stale_when_no_specs": True,
                "cleanup_bpffs_pins": True,
                "cgroup": build_cgroup(arm, "a", calib),
                **(
                    {"root_cgroup": {"path": "/sys/fs/cgroup", "controllers": ["io"]}}
                    if arm.get("knob") == "io_cost" else {}
                ),
            },
        },
    }
    return {
        "count": 3,
        "scene_id_template": f"ib_{group_id}_t_{{idx1}}",
        "name_template": f"M2F2 {arm['title']} tenant {{idx1}}",
        "template": template,
        "per_tenant": [tenant_override(t) for t in TENANT_ORDER],
    }


def build_policy_profile(arm: Dict[str, Any]) -> Dict[str, Any]:
    profile: Dict[str, Any] = {
        "name": f"M2F2 fig2 arm {arm['key']} (bare-metal)",
        "target_device": TARGET_DEVICE,
        "scheduler": arm["scheduler"],
        "io_ext": {
            "cleanup_stale_when_no_specs": True,
            "cleanup_bpffs_pins": True,
        },
    }
    if arm.get("knob") == "io_cost":
        profile["io_ext"]["root_cgroup"] = {"path": "/sys/fs/cgroup", "controllers": ["io"]}
    return profile


def build_arm_config(arm: Dict[str, Any], calib: Dict[str, Any]) -> Dict[str, Any]:
    profiles = {f"F2_{arm['key']}": build_policy_profile(arm)}
    groups = {f"m2f2_{arm['key']}": build_tenant_group(arm, calib)}
    sequences = [{
        "sequence_id": arm["seq"],
        "name": f"M2F2 fig2 replication {arm['title']} (bare-metal bare metal)",
        "io_ext_policy": f"F2_{arm['key']}",
        "steps": [{
            "name": f"m2f2 {arm['key']}",
            "tenant_groups": [f"m2f2_{arm['key']}"],
            "duration": 110,
            "tenant_count": 3,
        }],
    }]
    return {
        "benchmark_name": "isol_bench rig M2 fig2 replication (bare-metal bare metal)",
        "description": (
            "M2' motivation on bare-metal bm: near-verbatim replication of the "
            "isol-bench paper Fig. 2 (IISWC'25) cgroups I/O knob arms + native "
            "kyber arm; three identical 64K randread QD8 fio apps rate-limited "
            f"to {fio_rate(calib)} MiB/s (bare-metal solo calibration), staggered "
            "A 0-50s / B 10-70s / C 20-50s, reading a preallocated "
            f"{DATA_SIZE_GB}G direct=1 file ({DATA_FILE}) on {TARGET_DEVICE}; "
            "io.* knobs applied on per-tenant child cgroups. Generated by "
            "experiments/m1m2_bm/scripts/gen_m2_fig2_yamls_bm.py."
        ),
        "fail_fast_on_scene_error": True,
        "io_ext_policy_profiles": profiles,
        "tenant_groups": groups,
        "test_sequences": sequences,
        "default_sequence": arm["seq"],
    }


def build_all_config(calib: Dict[str, Any]) -> Dict[str, Any]:
    profiles: Dict[str, Any] = {}
    groups: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []
    by_seq = {arm["seq"]: arm for arm in ARMS}
    for seq in SEQUENCE_ORDER:
        arm = by_seq[seq]
        profiles[f"F2_{arm['key']}"] = build_policy_profile(arm)
        groups[f"m2f2_{arm['key']}"] = build_tenant_group(arm, calib)
        sequences.append({
            "sequence_id": seq,
            "name": f"M2F2 fig2 replication {arm['title']} (bare-metal bare metal)",
            "io_ext_policy": f"F2_{arm['key']}",
            "steps": [{
                "name": f"m2f2 {arm['key']}",
                "tenant_groups": [f"m2f2_{arm['key']}"],
                "duration": 110,
                "tenant_count": 3,
            }],
        })
    return {
        "benchmark_name": "isol_bench rig M2 fig2 replication (bare-metal bare metal)",
        "description": (
            "M2' motivation on bare-metal bm: near-verbatim replication of the "
            "isol-bench paper Fig. 2 (IISWC'25) cgroups I/O knob arms + native "
            "kyber arm; three identical 64K randread QD8 fio apps rate-limited "
            f"to {fio_rate(calib)} MiB/s (bare-metal solo calibration), staggered "
            "A 0-50s / B 10-70s / C 20-50s, reading a preallocated "
            f"{DATA_SIZE_GB}G direct=1 file ({DATA_FILE}) on {TARGET_DEVICE}; "
            "io.* knobs applied on per-tenant child cgroups. Generated by "
            "experiments/m1m2_bm/scripts/gen_m2_fig2_yamls_bm.py."
        ),
        "fail_fast_on_scene_error": True,
        "io_ext_policy_profiles": profiles,
        "tenant_groups": groups,
        "test_sequences": sequences,
        "default_sequence": SEQUENCE_ORDER[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--calibration", type=Path, default=DEFAULT_CALIBRATION,
        help=("calibration JSON (rate_mb_s/rate_mib_s, solo_mb_s, iocost_model); "
              f"default: {DEFAULT_CALIBRATION} (bare-metal solo calibration, do not recalibrate)"),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    calib = load_calibration(args.calibration if args.calibration.is_file() else None)
    if args.calibration and not args.calibration.is_file():
        raise SystemExit(f"calibration file not found: {args.calibration}")
    if not args.calibration:
        print(f"WARN: no calibration file; using built-in bm defaults "
              f"(solo={calib['solo_mib_s']}, rate={calib['rate_mib_s']})", flush=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    by_seq = {arm["seq"]: arm for arm in ARMS}
    written: List[Path] = []

    for seq in SEQUENCE_ORDER:
        arm = by_seq[seq]
        out = args.output_dir / f"m2_fig2_bm_{seq}.yml"
        with open(out, "w") as f:
            yaml.safe_dump(build_arm_config(arm, calib), f, sort_keys=False,
                           default_flow_style=False, width=100)
        written.append(out)

    out_all = args.output_dir / "m2_fig2_bm_all.yml"
    with open(out_all, "w") as f:
        yaml.safe_dump(build_all_config(calib), f, sort_keys=False,
                       default_flow_style=False, width=100)
    written.append(out_all)

    print(f"calibration: {args.calibration}")
    print(f"data file:   {DATA_FILE} ({DATA_SIZE_GB}G, preallocated, direct=1)")
    print(f"rate cap:    {fio_rate(calib)} MiB/s "
          f"(solo={calib['solo_mib_s']} MiB/s, rule min(1536, 0.8*solo))")
    print(f"io.max caps: " + ", ".join(
        f"{t.upper()}={v // (1024 * 1024)}MiB/s" for t, v in iomax_values(calib).items()))
    print(f"wrote {len(written)} yamls:")
    for p in written:
        print(f"  {p}")


if __name__ == "__main__":
    main()
