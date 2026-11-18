#!/usr/bin/env python3
"""Generate the M2' task YAML replicating isol-bench paper Figure 2 (virtme).

Replicates "Does Linux Provide Performance Isolation for NVMe SSDs?
Configuring cgroups for I/O Control in the NVMe Era?" (IISWC'25) Fig. 2:
three identical fio batch-apps A/B/C (64 KiB random read, QD=8, direct=1,
io_uring, rate-limited to 1.5 GiB/s), staggered A 0-50s / B 10-70s /
C 20-50s, one arm per cgroup I/O control knob, plus this repo's native
kyber scheduler-only arm (rig_native convention; no iox arms).

Arms (paper order + added native kyber):
  M2F2_a_none            (a)  no knob, scheduler none
  M2F2_b_mqdl_prio       (b)  mq-deadline + io.prio.class rt/idle/be
  M2F2_c_bfq             (c)  bfq + no knob (BFQ low_latency=0, per paper)
  M2F2_d_bfq_weight      (d)  bfq + io.bfq.weight 1000/1/100
  M2F2_e_io_max          (e)  io.max rbps A/3 ratio (paper 1500/500/500 MiB/s)
  M2F2_f_io_latency      (f)  io.latency targets 20/1000/100 us (paper values)
  M2F2_g_io_cost         (g)  io.cost model+qos at cgroup root (calibrated)
  M2F2_h_io_cost_weight  (h)  io.cost + io.weight 1000/1/100
  M2F2_i_native_kyber    (+)  native kyber, no knob (repo native-arm convention)

Device scaling: the paper's absolute numbers are tied to a Samsung 980 PRO.
A calibration JSON (produced in-VM by scripts/run_m2_fig2_virtme.sh) may set
"rate_mib_s" (solo-achievable rate cap) and "iocost_model". When absent, the
paper's raw values are used.

Output: src/vm/configs/task/isol_bench/rig_m2_fig2/m2_fig2_virtme.yml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "src/vm/configs/task/isol_bench/rig_m2_fig2/m2_fig2_virtme.yml"

# Paper constants (§III, §IV-B).
TARGET_DEVICE = "nvme0n1"
PAPER_RATE_MIB_S = 1536.0        # 1.5 GiB/s fio rate limit
PAPER_IOMAX_A_MIB_S = 1500.0     # io.max legend: A @ 1500 MiB/s
PAPER_IOMAX_BC_MIB_S = 500.0     # B, C @ 500 MiB/s
BFQ_TUNABLES = {"low_latency": 0}  # paper: BFQ low_latency disabled; default slice_idle kept
RUNTIMES = {"a": 50, "b": 60, "c": 30}   # A 0-50, B 10-70, C 20-50
START_DELAYS = {"a": 0, "b": 10, "c": 20}
TENANT_ORDER = ("a", "b", "c")

# Calibration defaults used when no calibration JSON is supplied: the paper's
# raw values, so the generated YAML is a verbatim paper config.
DEFAULTS: Dict[str, Any] = {
    "rate_mib_s": PAPER_RATE_MIB_S,
    "solo_mib_s": None,
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

# io.cost arms run last among the io_cost knobs so a wedged io.latency run
# (paper §III: io.latency "frequently required reboots") cannot poison them;
# io.latency itself runs at the very end.
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
    for key in ("rate_mib_s", "solo_mib_s", "iocost_model", "iocost_qos"):
        if data.get(key) is not None:
            calib[key] = data[key]
    return calib


def iomax_values(calib: Dict[str, Any]) -> Dict[str, int]:
    """Scale the paper's 1500/500/500 MiB/s io.max caps to the rate cap.

    Paper ratio: A cap == its own rate limit minus headroom, B/C == A/3.
    """
    cap = float(calib["rate_mib_s"])
    a = int(round(PAPER_IOMAX_A_MIB_S / PAPER_RATE_MIB_S * cap)) * 1024 * 1024
    bc = int(round(PAPER_IOMAX_BC_MIB_S / PAPER_RATE_MIB_S * cap)) * 1024 * 1024
    return {"a": a, "b": bc, "c": bc}


def fio_rate(calib: Dict[str, Any]) -> str:
    return f"{int(round(float(calib['rate_mib_s'])))}M"


def tenant_cgroup(arm_key: str, tenant: str) -> str:
    return f"/sys/fs/cgroup/ib_m2f2_{arm_key}_t_{tenant}"


def build_cgroup(arm: Dict[str, Any], tenant: str, calib: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tenant cgroup spec; every non-io_cost arm scrubs sticky root io.cost."""
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
            "file_name": f"/dev/{TARGET_DEVICE}",
            "scheduler": scheduler,
            "scheduler_tunables": BFQ_TUNABLES if arm.get("bfq") else {},
            "output_dir": f"./test_results/isol_bench/rig_m2_fig2/{arm['key']}",
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
        "name": f"M2F2 fig2 arm {arm['key']}",
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


def build_config(calib: Dict[str, Any]) -> Dict[str, Any]:
    profiles: Dict[str, Any] = {}
    groups: Dict[str, Any] = {}
    by_seq = {arm["seq"]: arm for arm in ARMS}
    sequences: List[Dict[str, Any]] = []
    for seq in SEQUENCE_ORDER:
        arm = by_seq[seq]
        profiles[f"F2_{arm['key']}"] = build_policy_profile(arm)
        groups[f"m2f2_{arm['key']}"] = build_tenant_group(arm, calib)
        sequences.append({
            "sequence_id": seq,
            "name": f"M2F2 fig2 replication {arm['title']}",
            "io_ext_policy": f"F2_{arm['key']}",
            "steps": [{
                "name": f"m2f2 {arm['key']}",
                "tenant_groups": [f"m2f2_{arm['key']}"],
                "duration": 110,
                "tenant_count": 3,
            }],
        })
    return {
        "benchmark_name": "isol_bench rig M2 fig2 replication (virtme)",
        "description": (
            "M2' motivation: near-verbatim replication of isol-bench paper Fig. 2 "
            "(IISWC'25) cgroups I/O knob arms + native kyber arm; three identical "
            "64K randread QD8 fio apps rate-limited to the calibrated cap, staggered "
            "A 0-50s / B 10-70s / C 20-50s."
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
        "--calibration", type=Path, default=None,
        help="calibration JSON (rate_mib_s, iocost_model, iocost_qos) from the in-VM probe",
    )
    parser.add_argument("--output", type=Path, default=OUT_PATH)
    args = parser.parse_args()
    calib = load_calibration(args.calibration)
    config = build_config(calib)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False, width=100)
    print(f"wrote {args.output} (rate={fio_rate(calib)}, iocost_model={'yes' if calib['iocost_model'] else 'paper-default'})")


if __name__ == "__main__":
    main()
