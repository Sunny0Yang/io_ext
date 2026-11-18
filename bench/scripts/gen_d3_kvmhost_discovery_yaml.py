#!/usr/bin/env python3
"""Generate the kvmhost D3 discovery yaml (campaign Phase 2).

Arms (campaign plan §3.2, calibration-gated):
  none / bfq / iox_qrl_k065..k100 (5 cap points) / iox_arbonly  -- 8 arms.
io_cost is deferred to a v2 discovery batch (its io.cost linear model
constants must be re-probed on the rolled-back device first; stale constants
would be a silent confounder).

Cap derivation (single source of truth -- SLO_and_B_none.json from Phase 1):
  B_none_mib = b_none_mb_s * 1e6 / 2**20
  M(k)       = round(k * B_none_mib / 4)   # per-BE-job cap, binary MiB/s

Policy wiring is copied verbatim-adapted from the frozen bm D3 batch ymls
(results/isol_d2d4_bm_n3_d3_arbwdrrfb/d3_lc_bm.yml):
  qrl : LC loader_q_fifo -p0 (pass-through) + BE loader_q_ratelimit -p7 --rate-mib-s M
  arb : arb_wdrr arbiter (--allow-fallback 1) + q_fifo enable layer LC -p0 / BE -p7
Loader paths map to the kvmhost deployment /opt/io-ext/userspace/.

Sequence order is arm-interleaved (plan §3.4): run-major round robin.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

PERCENTILES = "20:25:30:35:40:45:50:55:60:65:70:75:80:85:90:95:99:99.9:99.99:100"
LOADER_DIR = "/opt/io-ext/userspace"
KS = (0.65, 0.75, 0.85, 0.95, 1.00)


def fio_shape(iodepth: int, runtime: int, ramp: int) -> dict:
    return {
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
        "write_bw_log": "bw",
        "write_lat_log": "lat",
        "log_avg_msec": 1000,
    }


def tenant_group(gid: str, count: int, iodepth: int, dev: str, out: str,
                 runtime: int, ramp: int, iox: bool, sched: str | None = None) -> dict:
    """iox=True -> neutral cgroup (policy governs); else native weight=100.
    sched overrides the tenant scene's device scheduler -- it MUST match the
    arm (a bfq profile with none-scheduler tenants gets its elevator reset by
    the tenant scene; the discovery-v1 bfq-arm failure)."""
    io_ext = {
        "cleanup_stale": False,
        "cleanup_bpffs_pins": False,
        "loaders": [],
        "cgroup_policies": [],
        "cgroup": {
            "path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                    else f"/sys/fs/cgroup/ib_{gid}",
            "controllers": ["io"],
            "io_weight": None,
            "io_max": None,
            "io_cost": {"qos": {"enable": False}},
        },
    } if iox else {
        "cleanup_stale_when_no_specs": True,
        "cleanup_bpffs_pins": True,
        "cgroup": {
            "path": f"/sys/fs/cgroup/ib_{gid}_t{{idx1}}" if count > 1
                    else f"/sys/fs/cgroup/ib_{gid}",
            "controllers": ["io"],
            "io_weight": 100,
            "io_max": {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
            "io_cost": {"qos": {"enable": False}},
        },
    }
    return {gid: {
        "count": count,
        "execution_mode": "fio_subjob",
        "scene_id_template": f"ib_{gid}",
        "collapsed_scene_id": f"ib_{gid}_subjob",
        "template": {"base_scene": "scene/io_ext/fio.yml", "parameters": {
            "target_device": dev,
            "file_name": f"/dev/{dev}",
            "scheduler": sched or ("scx_io" if iox else "none"),
            "scheduler_tunables": {},
            "output_dir": out,
            "duration": 120,
            "drop_caches_before_run": False,
            "cooldown_sec": 1,
            "fio_global": fio_shape(iodepth, runtime, ramp),
            "io_ext": io_ext,
        }},
    }}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", default="sdb")
    ap.add_argument("--anchor", required=True,
                    help="Phase-1 SLO_and_B_none.json (single source of truth)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--runtime-sec", type=int, default=60)
    ap.add_argument("--ramp-sec", type=int, default=20)
    args = ap.parse_args()

    anchor = json.loads(Path(args.anchor).read_text())
    if anchor.get("verdict") != "PASS" or not anchor.get("b_none_mb_s"):
        raise SystemExit(f"FATAL: anchor not PASS-validated: {args.anchor}")
    b_none_mib = anchor["b_none_mb_s"] * 1e6 / 2 ** 20
    caps = {f"k{int(k * 100):03d}": round(k * b_none_mib / 4) for k in KS}

    dev = args.dev
    raw = "/tmp/d3_kvmhost_disc_raw"

    # ---- policy profiles (bare-metal-frozen wiring, kvmhost loader paths) -------------
    stale = ["loader_arb_", "loader_q_", "loader_minimal"]
    root_cg = {"path": "/sys/fs/cgroup", "controllers": ["io"]}

    def qrl_profile(name: str, cap: int) -> dict:
        return {
            "name": f"D3 kvmhost {name} (LC q_fifo -p0 pass-through / BE q_ratelimit -p7 cap {cap} MiB/s per BE job)",
            "target_device": dev,
            "scheduler": "scx_io",
            "io_ext": {"cleanup_stale": True, "cleanup_bpffs_pins": True,
                       "stale_loader_patterns": stale, "root_cgroup": root_cg},
            "cgroup_policies_from_groups": [
                {"tenant_group": "d3x_iox_lc", "name_template": "q_fifo_lc",
                 "path": f"{LOADER_DIR}/loader_q_fifo", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "0"]},
                {"tenant_group": "d3x_iox_be", "name_template": "q_rl_be_t{idx1}",
                 "path": f"{LOADER_DIR}/loader_q_ratelimit", "scope": "cgroup_policy",
                 "args": ["-c", "{policy_cgroup_path}", "-p", "7",
                          "--rate-mib-s", str(cap)]},
            ],
            "output_dir": f"{raw}/policies/{{scene_id}}",
        }

    profiles = {
        "IB_none": {
            "name": "D3 kvmhost discovery none (same-batch B_none denominator)",
            "target_device": dev, "scheduler": "none",
            "io_ext": {"cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True},
            "output_dir": f"{raw}/policies/{{scene_id}}",
        },
        "IB_bfq": {
            "name": "D3 kvmhost discovery bfq (native scheduler baseline)",
            "target_device": dev, "scheduler": "bfq",
            "io_ext": {"cleanup_stale_when_no_specs": True, "cleanup_bpffs_pins": True},
            "output_dir": f"{raw}/policies/{{scene_id}}",
        },
    }
    for kname, cap in caps.items():
        profiles[f"iox_qrl_{kname}"] = qrl_profile(f"iox_qrl_{kname}", cap)
    profiles["iox_arbonly"] = {
        "name": "D3 kvmhost iox_arbonly (arb_wdrr --allow-fallback 1 + q_fifo enable: LC -p0 / BE -p7)",
        "target_device": dev,
        "scheduler": "scx_io",
        "io_ext": {"cleanup_stale": True, "cleanup_bpffs_pins": True,
                   "stale_loader_patterns": stale, "root_cgroup": root_cg,
                   "arbiters": [{"name": "arb_wdrr",
                                 "path": f"{LOADER_DIR}/loader_arb_wdrr",
                                 "scope": "arbiter",
                                 "args": ["--allow-fallback", "1"]}]},
        "cgroup_policies_from_groups": [
            {"tenant_group": "d3x_iox_lc", "name_template": "q_fifo_lc",
             "path": f"{LOADER_DIR}/loader_q_fifo", "scope": "cgroup_policy",
             "args": ["-c", "{policy_cgroup_path}", "-p", "0"]},
            {"tenant_group": "d3x_iox_be", "name_template": "q_fifo_be_t{idx1}",
             "path": f"{LOADER_DIR}/loader_q_fifo", "scope": "cgroup_policy",
             "args": ["-c", "{policy_cgroup_path}", "-p", "7"]},
        ],
        "output_dir": f"{raw}/policies/{{scene_id}}",
    }

    cfg = {
        "benchmark_name": f"isol_bench D3 kvmhost discovery (bare virtio {dev}, raw device)",
        "description": (
            "D3 kvmhost discovery batch (campaign Phase 2): 8 arms x "
            f"{args.runs} runs, arm-interleaved. Arms: none / bfq / "
            "iox_qrl_k065..k100 (cap grid from Phase-1 anchor) / iox_arbonly. "
            f"Anchor: SLO={anchor['slo_batch_ms']} ms, B_none={anchor['b_none_mb_s']} MB/s "
            f"(calibration batch, kernel {anchor.get('kernel')}); caps MiB/s per BE job "
            f"= {json.dumps(caps)}. io_cost deferred to v2 (model re-probe). "
            "Same-batch none re-measures the denominator; SLO screening reuses the "
            "calibration anchor until the confirmation batch re-anchors."
        ),
        "scenes": {
            "ib_system_metric": {
                "base_scene": "scene/system/system_metric.yml",
                "parameters": {
                    "collection_mode": "proc", "sample_interval": 1.0,
                    "max_samples": 120, "require_samples": True,
                    "disk_devices": [dev],
                    "output_dir": f"{raw}/system_metric",
                    "agent_output_file": f"{raw}/system_metric/binary_output.bin",
                },
            },
        },
        "io_ext_policy_profiles": profiles,
        "tenant_groups": {
            **tenant_group("d3x_nat_lc", 1, 1, dev, f"{raw}/nat/lc", args.runtime_sec, args.ramp_sec, iox=False),
            **tenant_group("d3x_nat_be", 4, 256, dev, f"{raw}/nat/be", args.runtime_sec, args.ramp_sec, iox=False),
            **tenant_group("d3x_bfq_lc", 1, 1, dev, f"{raw}/bfq/lc", args.runtime_sec, args.ramp_sec,
                           iox=False, sched="bfq"),
            **tenant_group("d3x_bfq_be", 4, 256, dev, f"{raw}/bfq/be", args.runtime_sec, args.ramp_sec,
                           iox=False, sched="bfq"),
            **tenant_group("d3x_iox_lc", 1, 1, dev, f"{raw}/iox/lc", args.runtime_sec, args.ramp_sec, iox=True),
            **tenant_group("d3x_iox_be", 4, 256, dev, f"{raw}/iox/be", args.runtime_sec, args.ramp_sec, iox=True),
        },
        "test_sequences": [],
        "fail_fast_on_scene_error": True,
    }

    arm_profile = [("none", "IB_none", ["d3x_nat_lc", "d3x_nat_be"]),
                   ("bfq", "IB_bfq", ["d3x_bfq_lc", "d3x_bfq_be"])]
    for kname in caps:
        arm_profile.append((f"qrl_{kname}", f"iox_qrl_{kname}", ["d3x_iox_lc", "d3x_iox_be"]))
    arm_profile.append(("arbonly", "iox_arbonly", ["d3x_iox_lc", "d3x_iox_be"]))

    seqs = cfg["test_sequences"]
    for run in range(1, args.runs + 1):          # run-major interleave (plan §3.4)
        for arm, prof, groups in arm_profile:
            seqs.append({
                "sequence_id": f"D3PV_{arm}_run{run}",
                "name": f"D3 kvmhost discovery {arm} run{run}",
                "io_ext_policy": prof,
                "steps": [{
                    "name": f"1 LC + 4 BE {arm} run{run}",
                    "tenant_groups": groups,
                    "monitoring_scenes": ["ib_system_metric"],
                    "monitoring_startup_time": 3,
                    "duration": 120,
                }],
            })
    cfg["default_sequence"] = seqs[0]["sequence_id"]

    hdr = (
        "# AUTO-GENERATED by scripts/gen_d3_kvmhost_discovery_yaml.py -- DO NOT EDIT\n"
        f"# device=/dev/{dev} arms={len(arm_profile)} runs_per_arm={args.runs}\n"
        f"# anchor: SLO={anchor['slo_batch_ms']} ms B_none={anchor['b_none_mb_s']} MB/s "
        f"(b_none_mib={b_none_mib:.2f})\n"
        f"# caps MiB/s per BE job: {json.dumps(caps)}\n"
    )
    body = yaml.safe_dump(cfg, sort_keys=False, width=100)
    Path(args.out).write_text(hdr + body)
    print(f"wrote {args.out}: {len(seqs)} sequences "
          f"({len(arm_profile)} arms x {args.runs}), caps={caps}")


if __name__ == "__main__":
    main()
