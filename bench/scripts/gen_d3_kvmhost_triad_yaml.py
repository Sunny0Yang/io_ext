#!/usr/bin/env python3
"""D3 kvmhost triad: none / q_none (scx_io bypass) / bfq — same workload as discovery2.

Executor-integrated; workload = 1×LC QD1 + 4×BE QD256 randread on raw device.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

LOADER_DIR = "/opt/io-ext/userspace"
STALE = ["loader_arb_", "loader_q_", "loader_minimal"]
ROOT_CG = {"path": "/sys/fs/cgroup", "controllers": ["io"]}
PERCENTILES = "20:25:30:35:40:45:50:55:60:65:70:75:80:85:90:95:99:99.9:99.99:100"


def fio_shape(iodepth: int, runtime: int, ramp: int) -> dict:
    return {
        "ioengine": "libaio", "direct": 1, "bs": "4k", "rw": "randread",
        "iodepth": iodepth, "numjobs": 1, "runtime_sec": runtime,
        "time_based": True, "group_reporting": True, "ramp_time": ramp,
        "percentile_list": PERCENTILES,
        "write_bw_log": "bw", "write_lat_log": "lat", "log_avg_msec": 1000,
    }


def tenant_group(gid, count, iodepth, dev, out, runtime, ramp, sched, iox: bool):
    io_ext = {
        "cleanup_stale": False, "cleanup_bpffs_pins": False,
        "loaders": [], "cgroup_policies": [],
        "cgroup": {
            "path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                    else f"/sys/fs/cgroup/ib_{gid}",
            "controllers": ["io"], "io_weight": None, "io_max": None,
            "io_cost": {"qos": {"enable": False}},
        },
    } if iox else {
        "cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True,
        "cgroup": {
            "path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                    else f"/sys/fs/cgroup/ib_{gid}",
            "controllers": ["io"], "io_weight": 100,
            "io_max": {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
            "io_cost": {"qos": {"enable": False}},
        },
    }
    return {gid: {
        "count": count, "execution_mode": "fio_subjob",
        "scene_id_template": f"ib_{gid}",
        "collapsed_scene_id": f"ib_{gid}_subjob",
        "template": {"base_scene": "scene/io_ext/fio.yml", "parameters": {
            "target_device": dev, "file_name": f"/dev/{dev}", "scheduler": sched,
            "scheduler_tunables": {}, "output_dir": out, "duration": 120,
            "drop_caches_before_run": False, "cooldown_sec": 1,
            "fio_global": fio_shape(iodepth, runtime, ramp), "io_ext": io_ext,
        }},
    }}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", default="sdb")
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--runtime-sec", type=int, default=60)
    ap.add_argument("--ramp-sec", type=int, default=20)
    args = ap.parse_args()

    anchor = json.loads(Path(args.anchor).read_text())
    if anchor.get("verdict") != "PASS" or not anchor.get("b_none_mb_s"):
        raise SystemExit(f"FATAL: anchor not PASS-validated: {args.anchor}")

    dev = args.dev
    raw = "/tmp/d3_kvmhost_triad_raw"

    profiles = {
        "IB_none": {
            "name": "D3 kvmhost triad none (native elevator)",
            "target_device": dev, "scheduler": "none",
            "io_ext": {"cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True},
            "output_dir": f"{raw}/policies/{{scene_id}}",
        },
        "IB_bfq": {
            "name": "D3 kvmhost triad bfq (tenants carry scheduler=bfq)",
            "target_device": dev, "scheduler": "bfq",
            "io_ext": {"cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True},
            "output_dir": f"{raw}/policies/{{scene_id}}",
        },
        "IB_qnone": {
            "name": "D3 kvmhost triad q_none (scx_io + loader_q_none LC/BE bypass)",
            "target_device": dev, "scheduler": "scx_io",
            "io_ext": {
                "cleanup_stale": True, "cleanup_bpffs_pins": True,
                "stale_loader_patterns": STALE, "root_cgroup": ROOT_CG,
            },
            "cgroup_policies_from_groups": [
                {"tenant_group": "d3x_qnone_lc", "name_template": "q_none_lc",
                 "path": f"{LOADER_DIR}/loader_q_none", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "0", "-W", "100"]},
                {"tenant_group": "d3x_qnone_be", "name_template": "q_none_be_t{idx1}",
                 "path": f"{LOADER_DIR}/loader_q_none", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "7", "-W", "400"]},
            ],
            "output_dir": f"{raw}/policies/{{scene_id}}",
        },
    }

    cfg = {
        "benchmark_name": f"isol_bench D3 kvmhost triad none/q_none/bfq ({dev})",
        "description": (
            f"Triad throughput/LC coloc: none vs q_none vs bfq. "
            f"Anchor B_none={anchor['b_none_mb_s']} MB/s SLO={anchor['slo_batch_ms']} ms."
        ),
        "scenes": {"ib_system_metric": {
            "base_scene": "scene/system/system_metric.yml",
            "parameters": {"collection_mode": "proc", "sample_interval": 1.0,
                           "max_samples": 120, "require_samples": True,
                           "disk_devices": [dev],
                           "output_dir": f"{raw}/system_metric",
                           "agent_output_file": f"{raw}/system_metric/binary_output.bin"}}},
        "io_ext_policy_profiles": profiles,
        "tenant_groups": {
            **tenant_group("d3x_nat_lc", 1, 1, dev, f"{raw}/nat/lc", args.runtime_sec, args.ramp_sec, "none", False),
            **tenant_group("d3x_nat_be", 4, 256, dev, f"{raw}/nat/be", args.runtime_sec, args.ramp_sec, "none", False),
            **tenant_group("d3x_bfq_lc", 1, 1, dev, f"{raw}/bfq/lc", args.runtime_sec, args.ramp_sec, "bfq", False),
            **tenant_group("d3x_bfq_be", 4, 256, dev, f"{raw}/bfq/be", args.runtime_sec, args.ramp_sec, "bfq", False),
            **tenant_group("d3x_qnone_lc", 1, 1, dev, f"{raw}/qnone/lc", args.runtime_sec, args.ramp_sec, "scx_io", True),
            **tenant_group("d3x_qnone_be", 4, 256, dev, f"{raw}/qnone/be", args.runtime_sec, args.ramp_sec, "scx_io", True),
        },
        "test_sequences": [],
        "fail_fast_on_scene_error": True,
    }

    arm_profile = [
        ("none", "IB_none", ["d3x_nat_lc", "d3x_nat_be"]),
        ("qnone", "IB_qnone", ["d3x_qnone_lc", "d3x_qnone_be"]),
        ("bfqf", "IB_bfq", ["d3x_bfq_lc", "d3x_bfq_be"]),
    ]
    seqs = cfg["test_sequences"]
    for run in range(1, args.runs + 1):
        for arm, prof, groups in arm_profile:
            seqs.append({
                "sequence_id": f"D3PV_{arm}_run{run}",
                "name": f"D3 kvmhost triad {arm} run{run}",
                "io_ext_policy": prof,
                "steps": [{"name": f"1 LC + 4 BE {arm} run{run}",
                           "tenant_groups": groups,
                           "monitoring_scenes": ["ib_system_metric"],
                           "monitoring_startup_time": 3, "duration": 120}],
            })
    cfg["default_sequence"] = seqs[0]["sequence_id"]

    hdr = (
        "# AUTO-GENERATED by scripts/gen_d3_kvmhost_triad_yaml.py\n"
        f"# device=/dev/{dev} arms=none,qnone,bfqf runs_per_arm={args.runs}\n"
    )
    Path(args.out).write_text(hdr + yaml.safe_dump(cfg, sort_keys=False, width=100))
    print(f"wrote {args.out}: {len(seqs)} sequences")


if __name__ == "__main__":
    main()