#!/usr/bin/env python3
"""Aggregate converted m2ctrl cases (scripts/convert_m2ctrl_cases.py output)
into m2_controller_summary.csv: one row per (arm, offered) with mean +/- SD
over the n repeats of: achieved MiB/s, READ P99 (ms), UPDATE P99 (ms), ops/s.

Usage: python3 scripts/summarize_m2ctrl.py RUN_DIR [OUT_CSV]
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    run_dir = Path(sys.argv[1])
    out_csv = Path(sys.argv[2]) if len(sys.argv) > 2 else run_dir / "m2_controller_summary.csv"
    groups = defaultdict(list)
    for rj in sorted(run_dir.glob("*/result.json")):
        d = json.loads(rj.read_text())
        if d.get("status") != "success":
            continue
        groups[(d["scheduler"], float(d["offered_mib_s"]))].append(d)

    rows = []
    for (arm, offered), items in sorted(groups.items()):
        def ms(values):
            return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)
        ach_m, ach_s = ms([x["background_achieved_mib_s"] for x in items])
        r_m, r_s = ms([x["application"]["READ"]["99thPercentileLatency(us)"] / 1000.0 for x in items])
        u_m, u_s = ms([x["application"]["UPDATE"]["99thPercentileLatency(us)"] / 1000.0 for x in items])
        o_m, o_s = ms([x["application"]["OVERALL"]["Throughput(ops/sec)"] for x in items])
        cov = statistics.mean([x["background_window_coverage"] for x in items])
        eff = statistics.mean([x["controller_effectiveness_window_fraction"] for x in items])
        rows.append({
            "arm": arm, "offered_mib_s": offered, "n": len(items),
            "achieved_mib_s_mean": round(ach_m, 3), "achieved_mib_s_sd": round(ach_s, 3),
            "read_p99_ms_mean": round(r_m, 3), "read_p99_ms_sd": round(r_s, 3),
            "update_p99_ms_mean": round(u_m, 3), "update_p99_ms_sd": round(u_s, 3),
            "ycsb_ops_s_mean": round(o_m, 2), "ycsb_ops_s_sd": round(o_s, 2),
            "mean_window_coverage": round(cov, 3),
            "mean_controller_effectiveness": round(eff, 3),
            "cases": ";".join(x["case_dir"] for x in items),
        })

    fields = list(rows[0].keys()) if rows else []
    with out_csv.open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out_csv} ({len(rows)} rows)")
    for r in rows:
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
