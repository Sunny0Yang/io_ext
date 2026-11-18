#!/usr/bin/env python3
"""Generate the kvmhost D3 calibration yaml (Phase 1 of the D3 kvmhost campaign).

Shape is copied from the frozen bm template
``src/vm/configs/task/isol_bench/d2d4_bm_tmpfs_probe/d3_lc_bm.yml`` (1 LC 4k
randread QD1 60s+ramp20 + 4 BE QD256, none policy) with the kvmhost
adjustments:

  1. raw-device addressing ``file_name=/dev/<dev>`` (D2-rerun precedent --
     the 96G experiment disk is an unmounted dedicated virtio disk; the bm
     region-file layout does not apply);
  2. calibration-only sequence set: n solo runs (LC only, same-batch
     ``lcsolo`` anchor) + n contention runs (LC + 4 BE, the ``B_none``
     denominator arm);
  3. per-second telemetry: the system_metric scene (1 s diskstats sampling)
     plus fio ``write_bw_log``/``write_lat_log``/``log_avg_msec`` so the
     analyzer can rebuild busy-second distributions (the bm aggregate-gate
     blind spot);
  4. no io_ext policy beyond ``IB_none`` -- calibration never loads a
     scheduler.

Usage:
  gen_d3_kvmhost_calib_yaml.py --dev sdb --out /tmp/d3_kvmhost/d3_kvmhost_calib.yml

The SLO / B_none values are *outputs* of this batch, never inputs: do not
thread any bm anchor into this yaml.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

PERCENTILES = "20:25:30:35:40:45:50:55:60:65:70:75:80:85:90:95:99:99.9:99.99:100"


def tenant_group(gid: str, count: int, iodepth: int, dev: str, out: str,
                 runtime: int, ramp: int) -> dict:
    """One none-policy tenant group (LC or BE shape), bm d3 template verbatim."""
    return {
        gid: {
            "count": count,
            "execution_mode": "fio_subjob",
            "scene_id_template": f"ib_{gid}",
            "collapsed_scene_id": f"ib_{gid}_subjob",
            "template": {
                "base_scene": "scene/io_ext/fio.yml",
                "parameters": {
                    "target_device": dev,
                    "file_name": f"/dev/{dev}",
                    "scheduler": "none",
                    "scheduler_tunables": {},
                    "output_dir": out,
                    "duration": 120,
                    "drop_caches_before_run": False,
                    "cooldown_sec": 1,
                    "fio_global": {
                        "ioengine": "libaio",
                        "direct": 1,
                        "bs": "4k",
                        "rw": "randread",
                        "iodepth": iodepth,
                        "numjobs": 1,
                        "runtime_sec": runtime,
                        "time_based": True,
                        "group_reporting": True,
                        "ramp_time": ramp,
                        "percentile_list": PERCENTILES,
                        # per-second logs (busy-second audit; bm blind-spot fix)
                        "write_bw_log": "bw",
                        "write_lat_log": "lat",
                        "log_avg_msec": 1000,
                    },
                    "io_ext": {
                        "cleanup_stale_when_no_specs": True,
                        "cleanup_bpffs_pins": True,
                        "cgroup": {
                            "path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                                    else f"/sys/fs/cgroup/ib_{gid}",
                            "controllers": ["io"],
                            "io_weight": 100,
                            "io_max": {"rbps": "max", "wbps": "max",
                                       "riops": "max", "wiops": "max"},
                            "io_cost": {"qos": {"enable": False}},
                        },
                    },
                },
            },
        }
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", default="sdb", help="experiment device letter (96G disk)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--solo-runs", type=int, default=5)
    ap.add_argument("--cont-runs", type=int, default=5)
    ap.add_argument("--runtime-sec", type=int, default=60)
    ap.add_argument("--ramp-sec", type=int, default=20)
    args = ap.parse_args()

    dev = args.dev
    raw = "/tmp/d3_kvmhost_calib_raw"

    cfg = {
        "benchmark_name": f"isol_bench D3 kvmhost calibration (bare virtio {dev}, raw device)",
        "description": (
            "D3 kvmhost calibration batch (campaign plan Phase 1): LC solo anchor "
            f"(4k randread QD1, {args.runtime_sec}s + ramp{args.ramp_sec}) x{args.solo_runs} "
            f"and none contention (same LC + 4 BE QD256) x{args.cont_runs}, all none "
            "policy on the raw experiment device. Outputs SLO_batch=2x median solo "
            "LC p99 and B_none=median contention BE aggregate. No bm anchors are "
            "consumed; per-second telemetry (system_metric + fio bw/lat logs) is "
            "mandatory for the busy-second audit."
        ),
        "scenes": {
            "ib_system_metric": {
                "base_scene": "scene/system/system_metric.yml",
                "parameters": {
                    "collection_mode": "proc",
                    "sample_interval": 1.0,
                    "max_samples": 120,
                    "require_samples": True,
                    "disk_devices": [dev],
                    "output_dir": f"{raw}/system_metric",
                    "agent_output_file": f"{raw}/system_metric/binary_output.bin",
                },
            },
        },
        "io_ext_policy_profiles": {
            "IB_none": {
                "name": "D3 kvmhost calibration none (solo anchor + B_none denominator)",
                "target_device": dev,
                "scheduler": "none",
                "io_ext": {
                    "cleanup_stale_when_no_specs": True,
                    "cleanup_bpffs_pins": True,
                },
                "output_dir": f"{raw}/policies/{{scene_id}}",
            },
        },
        "tenant_groups": {
            **tenant_group("d3cal_lc", 1, 1, dev, f"{raw}/lc", args.runtime_sec, args.ramp_sec),
            **tenant_group("d3cal_be", 4, 256, dev, f"{raw}/be", args.runtime_sec, args.ramp_sec),
        },
        "test_sequences": [],
        "fail_fast_on_scene_error": True,
    }

    seqs = cfg["test_sequences"]
    for i in range(1, args.solo_runs + 1):
        seqs.append({
            "sequence_id": f"D3PV_lcsolo_none_run{i}",
            "name": f"D3 kvmhost calib LC solo anchor run{i}",
            "io_ext_policy": "IB_none",
            "steps": [{
                "name": f"LC solo (no BE) run{i}",
                "tenant_groups": ["d3cal_lc"],
                "monitoring_scenes": ["ib_system_metric"],
                "monitoring_startup_time": 3,
                "duration": 120,
            }],
        })
    for i in range(1, args.cont_runs + 1):
        seqs.append({
            "sequence_id": f"D3PV_none_run{i}",
            "name": f"D3 kvmhost calib none contention (B_none) run{i}",
            "io_ext_policy": "IB_none",
            "steps": [{
                "name": f"1 LC + 4 BE none run{i}",
                "tenant_groups": ["d3cal_lc", "d3cal_be"],
                "monitoring_scenes": ["ib_system_metric"],
                "monitoring_startup_time": 3,
                "duration": 120,
            }],
        })
    cfg["default_sequence"] = seqs[0]["sequence_id"]

    hdr = (
        "# AUTO-GENERATED by scripts/gen_d3_kvmhost_calib_yaml.py -- DO NOT EDIT\n"
        f"# device=/dev/{dev} solo_runs={args.solo_runs} cont_runs={args.cont_runs}\n"
        f"# fio shape: LC 4k randread QD1 {args.runtime_sec}s+ramp{args.ramp_sec}; BE 4k randread QD256\n"
        "# anchors: NONE consumed -- SLO/B_none are outputs of this batch\n"
    )
    body = yaml.safe_dump(cfg, sort_keys=False, width=100)
    Path(args.out).write_text(hdr + body)
    print(f"wrote {args.out}: {len(seqs)} sequences "
          f"({args.solo_runs} solo + {args.cont_runs} contention), device=/dev/{dev}")


if __name__ == "__main__":
    main()
