#!/usr/bin/env python3
"""Generate the kvmhost D3 discovery-v2 yaml (burst-ms sweep + bfq refix).

v1 finding (discovery batch, 2026-11-15): qrl caps deliver BE retention but
the LC P99 tail fails SLO 1.9-17x, monotonically worse with cap -- BE burst
mass drives LC tail on single-queue virtio.  v2 attacks the burst knob:

  none x3                        fresh in-window baseline (v1 denominator 576
                                 was depressed by degraded-window runs)
  bfqf x3                        bfq with FIXED wiring (tenant groups carry
                                 scheduler=bfq; v1 tenants reset it to none --
                                 metadata audit caught it, all 3 runs invalid)
  qrl_k065_burst{050,100,250} x3 cap 141 MiB/s (v1 best BE-ret/LC point),
                                 token-bucket burst 50/100/250 ms (default 500)
  qrl_k075_burst050 x3           the best-BE point (694 MB/s = 76% calib) with
                                 the tightest burst

Watch item (HDD campaign lesson): burst-ms <= 250 triggered fio long stalls on
HDD cg8/cg16 -- here 5 tenants on virtio; the runner timeout + forensics
covers a recurrence.  Cap arithmetic unchanged: M(k) from the Phase-1 anchor.
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


def tenant_group(gid, count, iodepth, dev, out, runtime, ramp, sched):
    iox = sched == "scx_io"
    io_ext = {
        "cleanup_stale": False, "cleanup_bpffs_pins": False,
        "loaders": [], "cgroup_policies": [],
        "cgroup": {"path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                   else f"/sys/fs/cgroup/ib_{gid}",
                   "controllers": ["io"], "io_weight": None, "io_max": None,
                   "io_cost": {"qos": {"enable": False}}},
    } if iox else {
        "cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True,
        "cgroup": {"path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                   else f"/sys/fs/cgroup/ib_{gid}",
                   "controllers": ["io"], "io_weight": 100,
                   "io_max": {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
                   "io_cost": {"qos": {"enable": False}}},
    }
    return {gid: {
        "count": count, "execution_mode": "fio_subjob",
        "scene_id_template": f"ib_{gid}", "collapsed_scene_id": f"ib_{gid}_subjob",
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
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--runtime-sec", type=int, default=60)
    ap.add_argument("--ramp-sec", type=int, default=20)
    args = ap.parse_args()

    anchor = json.loads(Path(args.anchor).read_text())
    if anchor.get("verdict") != "PASS" or not anchor.get("b_none_mb_s"):
        raise SystemExit(f"FATAL: anchor not PASS-validated: {args.anchor}")
    b_none_mib = anchor["b_none_mb_s"] * 1e6 / 2 ** 20
    cap_k065 = round(0.65 * b_none_mib / 4)
    cap_k075 = round(0.75 * b_none_mib / 4)

    dev = args.dev
    raw = "/tmp/d3_kvmhost_disc2_raw"

    def qrl_burst_profile(name: str, cap: int, burst_ms: int) -> dict:
        return {
            "name": (f"D3 kvmhost {name} (LC q_fifo -p0 / BE q_ratelimit -p7 "
                     f"cap {cap} MiB/s per BE job, burst {burst_ms} ms)"),
            "target_device": dev, "scheduler": "scx_io",
            "io_ext": {"cleanup_stale": True, "cleanup_bpffs_pins": True,
                       "stale_loader_patterns": STALE, "root_cgroup": ROOT_CG},
            "cgroup_policies_from_groups": [
                {"tenant_group": "d3x_iox_lc", "name_template": "q_fifo_lc",
                 "path": f"{LOADER_DIR}/loader_q_fifo", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "0"]},
                {"tenant_group": "d3x_iox_be", "name_template": "q_rl_be_t{idx1}",
                 "path": f"{LOADER_DIR}/loader_q_ratelimit", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "7",
                          "--rate-mib-s", str(cap), "--burst-ms", str(burst_ms)]},
            ],
            "output_dir": f"{raw}/policies/{{scene_id}}",
        }

    profiles = {
        "IB_none": {"name": "D3 kvmhost disc2 none (fresh in-window denominator)",
                    "target_device": dev, "scheduler": "none",
                    "io_ext": {"cleanup_stale_when_no_specs": True,
                               "cleanup_bpffs_pins": True},
                    "output_dir": f"{raw}/policies/{{scene_id}}"},
        "IB_bfq": {"name": "D3 kvmhost disc2 bfq (FIXED: tenants carry scheduler=bfq)",
                   "target_device": dev, "scheduler": "bfq",
                   "io_ext": {"cleanup_stale_when_no_specs": True,
                              "cleanup_bpffs_pins": True},
                   "output_dir": f"{raw}/policies/{{scene_id}}"},
        "iox_qrl_k065_b050": qrl_burst_profile("iox_qrl_k065_b050", cap_k065, 50),
        "iox_qrl_k065_b100": qrl_burst_profile("iox_qrl_k065_b100", cap_k065, 100),
        "iox_qrl_k065_b250": qrl_burst_profile("iox_qrl_k065_b250", cap_k065, 250),
        "iox_qrl_k075_b050": qrl_burst_profile("iox_qrl_k075_b050", cap_k075, 50),
    }

    cfg = {
        "benchmark_name": f"isol_bench D3 kvmhost discovery-v2 (bare virtio {dev}, raw device)",
        "description": (
            "D3 kvmhost discovery v2 (burst-ms sweep): none x3 fresh / bfqf x3 "
            f"(fixed wiring) / qrl_k065 burst 50/100/250 x3 / qrl_k075_b050 x3. "
            f"Anchor: SLO={anchor['slo_batch_ms']} ms, B_none={anchor['b_none_mb_s']} MB/s; "
            f"caps {cap_k065}/{cap_k075} MiB/s per BE job. v1 lesson applied: "
            "LC tail on virtio is driven by BE burst mass; bfq tenant groups "
            "carry scheduler=bfq (metadata audit enforced in analysis)."
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
            **tenant_group("d3x_nat_lc", 1, 1, dev, f"{raw}/nat/lc", args.runtime_sec, args.ramp_sec, "none"),
            **tenant_group("d3x_nat_be", 4, 256, dev, f"{raw}/nat/be", args.runtime_sec, args.ramp_sec, "none"),
            **tenant_group("d3x_bfq_lc", 1, 1, dev, f"{raw}/bfq/lc", args.runtime_sec, args.ramp_sec, "bfq"),
            **tenant_group("d3x_bfq_be", 4, 256, dev, f"{raw}/bfq/be", args.runtime_sec, args.ramp_sec, "bfq"),
            **tenant_group("d3x_iox_lc", 1, 1, dev, f"{raw}/iox/lc", args.runtime_sec, args.ramp_sec, "scx_io"),
            **tenant_group("d3x_iox_be", 4, 256, dev, f"{raw}/iox/be", args.runtime_sec, args.ramp_sec, "scx_io"),
        },
        "test_sequences": [],
        "fail_fast_on_scene_error": True,
    }

    arm_profile = [
        ("none", "IB_none", ["d3x_nat_lc", "d3x_nat_be"]),
        ("bfqf", "IB_bfq", ["d3x_bfq_lc", "d3x_bfq_be"]),
        ("qrl_k065_b050", "iox_qrl_k065_b050", ["d3x_iox_lc", "d3x_iox_be"]),
        ("qrl_k065_b100", "iox_qrl_k065_b100", ["d3x_iox_lc", "d3x_iox_be"]),
        ("qrl_k065_b250", "iox_qrl_k065_b250", ["d3x_iox_lc", "d3x_iox_be"]),
        ("qrl_k075_b050", "iox_qrl_k075_b050", ["d3x_iox_lc", "d3x_iox_be"]),
    ]
    seqs = cfg["test_sequences"]
    for run in range(1, args.runs + 1):
        for arm, prof, groups in arm_profile:
            seqs.append({
                "sequence_id": f"D3PV_{arm}_run{run}",
                "name": f"D3 kvmhost disc2 {arm} run{run}",
                "io_ext_policy": prof,
                "steps": [{"name": f"1 LC + 4 BE {arm} run{run}",
                           "tenant_groups": groups,
                           "monitoring_scenes": ["ib_system_metric"],
                           "monitoring_startup_time": 3, "duration": 120}],
            })
    cfg["default_sequence"] = seqs[0]["sequence_id"]

    hdr = (
        "# AUTO-GENERATED by scripts/gen_d3_kvmhost_disc2_yaml.py -- DO NOT EDIT\n"
        f"# device=/dev/{dev} arms={len(arm_profile)} runs_per_arm={args.runs}\n"
        f"# anchor: SLO={anchor['slo_batch_ms']} ms B_none={anchor['b_none_mb_s']} MB/s\n"
        f"# caps: k065={cap_k065} k075={cap_k075} MiB/s per BE job; burst sweep 50/100/250\n"
    )
    Path(args.out).write_text(hdr + yaml.safe_dump(cfg, sort_keys=False, width=100))
    print(f"wrote {args.out}: {len(seqs)} sequences ({len(arm_profile)} arms x {args.runs})")


if __name__ == "__main__":
    main()
