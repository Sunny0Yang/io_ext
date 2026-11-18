#!/usr/bin/env python3
"""Convert m2ctrl campaign case dirs into render_m2_interference.py input format.

The m2ctrl campaign runs through scripts/run_m1_cache_campaign.py (vm.executor),
whose per-case outputs (run_results.json + scene/ tree) differ from the legacy
m2_curve_formal harness format that results/motivation_paper_figs/
render_m2_interference.py consumes (result.json + background_bw*.log in each
case dir). This converter is the bridge; it re-derives exactly the fields the
renderer needs and adds controller-effectiveness evidence from the sidecar
sysfs sampler (scripts/m2ctrl_sidecar_sample.py).

Per case dir (M2CTRL_IO_COST_8M_r2 ...):
  * arm    = "io.cost" | "io.latency"  (from sequence id in the case name)
  * offered= 8 | 16 MiB/s              (from sequence id)
  * repeat = int(_r<k>)
  * foreground window = YCSB scene io_stat_snapshots before_run/after_run
    timestamps (taken immediately before/after the YCSB `run` process)
  * application metrics = YCSB scene ycsb_latency_summary + throughput
  * background_bw.1.log = copied fio scene *_bw*.log (log_avg_msec=1000,
    log_unix_epoch=1, so the renderer's window-integral math applies)
  * controller_effectiveness = fraction of sidecar samples inside the window
    that show the arm's knob in force (io.cost: RECAL model line + qos
    enable=1; io.latency: ycsb cgroup io.latency target=12000)

Quality gate (prints PASS/FAIL per case, exits 1 on any FAIL):
  rc==0, coverage >= 0.70, controller effectiveness == 1.0,
  drop_caches_before_run == false (comparability caliber with m2_none_pressure).

Usage:
  python3 scripts/convert_m2ctrl_cases.py RUN_DIR
  (sampler log expected at RUN_DIR/logs/m2ctrl_iocost_state.log)
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

COVERAGE_GATE = 0.70
STATE_GATE = 1.00
RECAL_RBPS = "rbps=335544320"
LAT_TARGET = "target=12000"


def parse_case_name(name: str):
    m = re.match(r"M2CTRL_(IO_COST|IO_LAT)_(8|16)M_r(?:_probe|(\d+))$", name)
    if not m:
        return None
    arm = "io.cost" if m.group(1) == "IO_COST" else "io.latency"
    return arm, float(m.group(2)), m.group(3) or "probe"


def window_integral(log_path: Path, begin_ms: float, end_ms: float):
    """Mirror of render_m2_interference._log_window_avg_mibs."""
    integral = duration = 0.0
    count = 0
    prev = None
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        f = [float(x) for x in line.split(",")]
        ts, rate = f[0], f[1]
        if prev is not None and ts <= prev:
            raise ValueError(f"nonmonotonic fio log {log_path}")
        if prev is not None and prev >= begin_ms and ts <= end_ms:
            iv = (ts - prev) / 1000.0
            integral += (rate / 1024.0) * iv
            duration += iv
            count += 1
        prev = ts
    return integral / duration if duration > 0 else 0.0, duration, count


def load_sampler_blocks(sampler_log: Path):
    text = sampler_log.read_text()
    blocks = re.split(r"=== T=([0-9.]+)\n", text)
    out = []
    for i in range(1, len(blocks) - 1, 2):
        out.append((float(blocks[i]), blocks[i + 1]))
    return out


def state_in_force(arm: str, body: str) -> bool:
    if arm == "io.cost":
        m = re.search(r"-- io.cost.model\n(.*)", body)
        q = re.search(r"-- io.cost.qos\n(.*)", body)
        return bool(m and q and RECAL_RBPS in m.group(1) and "enable=1" in q.group(1))
    m = re.search(r"-- ycsb io.latency\n([^\n]*)", body)
    return bool(m and LAT_TARGET in m.group(1))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    run_dir = Path(sys.argv[1])
    sampler_log = run_dir / "logs" / "m2ctrl_iocost_state.log"
    samples = load_sampler_blocks(sampler_log) if sampler_log.is_file() else []
    if not samples:
        print(f"BLOCKED: no sampler samples in {sampler_log}")
        return 2

    cases = sorted(d for d in run_dir.iterdir() if d.is_dir() and parse_case_name(d.name))
    if not cases:
        print(f"BLOCKED: no case dirs in {run_dir}")
        return 2

    failures = []
    for case_dir in cases:
        parsed = parse_case_name(case_dir.name)
        arm, offered, rep = parsed
        problems = []

        ycsb_jsons = sorted(case_dir.glob("scene/*/ycsb_rocksdb_*.json"))
        if not ycsb_jsons:
            problems.append("missing ycsb scene json")
        else:
            yd = json.loads(ycsb_jsons[0].read_text())
            snaps = yd["io_stat_snapshots"]
            fg0 = snaps["before_run"]["timestamp"]
            fg1 = snaps["after_run"]["timestamp"]
            lat = yd["ycsb_latency_summary"]
            thr = yd.get("throughput_ops_per_sec")
            rc_run = yd.get("run_returncode")
            rc_load = yd.get("load_returncode")
            if rc_run != 0 or rc_load != 0:
                problems.append(f"ycsb rc load={rc_load} run={rc_run}")
            if not thr or thr <= 0:
                problems.append(f"bad throughput {thr}")
            if yd.get("drop_caches_before_run") is not False:
                problems.append("drop_caches_before_run is not false (caliber break)")
            if lat.get("latency_read_p99_us", 0) <= 0 or lat.get("latency_update_p99_us", 0) <= 0:
                problems.append("missing READ/UPDATE p99")

            in_win = [(ts, b) for ts, b in samples if fg0 <= ts <= fg1]
            n_force = sum(1 for _, b in in_win if state_in_force(arm, b))
            eff = (n_force / len(in_win)) if in_win else 0.0

            # fio side
            bw_logs = sorted(case_dir.glob("scene/*/*_bw*.log"))
            if not bw_logs:
                problems.append("missing fio bw logs")
                achieved = cov = 0.0
            else:
                begin_ms, end_ms = fg0 * 1000.0, fg1 * 1000.0
                total, max_dur, nsamp = 0.0, 0.0, 0
                for lg in bw_logs:
                    avg, dur, cnt = window_integral(lg, begin_ms, end_ms)
                    total += avg
                    max_dur = max(max_dur, dur)
                    nsamp += cnt
                achieved = total
                cov = max_dur / ((end_ms - begin_ms) / 1000.0) if end_ms > begin_ms else 0.0
                shutil.copyfile(bw_logs[0], case_dir / "background_bw.1.log")
            if cov < COVERAGE_GATE:
                problems.append(f"coverage {cov:.3f} < {COVERAGE_GATE}")
            if eff < STATE_GATE:
                problems.append(f"controller state in force only {eff:.3f} of window")

            result = {
                "scheduler": arm,
                "offered_mib_s": offered,
                "repeat": int(rep) if rep.isdigit() else 0,
                "status": "success" if not problems else "failed",
                "case_dir": case_dir.name,
                "sequence": case_dir.name.rsplit("_r", 1)[0],
                "foreground_start_epoch": fg0,
                "foreground_end_epoch": fg1,
                "application": {
                    "OVERALL": {"Throughput(ops/sec)": thr},
                    "READ": {"99thPercentileLatency(us)": lat.get("latency_read_p99_us"),
                             "95thPercentileLatency(us)": lat.get("latency_read_p95_us"),
                             "Operations": lat.get("latency_read_ops")},
                    "UPDATE": {"99thPercentileLatency(us)": lat.get("latency_update_p99_us"),
                               "95thPercentileLatency(us)": lat.get("latency_update_p95_us"),
                               "Operations": lat.get("latency_update_ops")},
                },
                "background_achieved_mib_s": achieved,
                "background_window_coverage": cov,
                "background_window_samples": nsamp,
                "controller_effectiveness_window_fraction": eff,
                "controller_samples_in_window": len(in_win),
                "io_ext_cgroup_controls": yd.get("io_ext_cgroup_controls"),
                "comparability_caliber": {
                    "drop_caches_before_run": yd.get("drop_caches_before_run"),
                    "block_cache_size": yd.get("block_cache_size"),
                    "use_direct_io": yd.get("use_direct_io"),
                    "scheduler": yd.get("scheduler"),
                },
            }
            (case_dir / "result.json").write_text(json.dumps(result, indent=2))

        verdict = "PASS" if not problems else "FAIL"
        print(f"[{verdict}] {case_dir.name} arm={arm} offered={offered:g} "
              + ("; ".join(problems) if problems else
                 f"achieved={achieved:.2f} MiB/s cov={cov:.3f} state_eff={eff:.3f}"))
        if problems:
            failures.append(case_dir.name)

    print(f"{len(cases) - len(failures)}/{len(cases)} cases converted+gated")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
