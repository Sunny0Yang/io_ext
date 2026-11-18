#!/usr/bin/env python3
"""Build traceable native-vs-IOX tables across homo-v2 bm campaigns."""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULT_ROOT = ROOT / "results" / "homo_v2_bm"
OUT_DIR = RESULT_ROOT / os.environ.get("HOMO_V2_REPAIR_TABLES_OUT", "repair_tables_20261109")

# Later repair runs supersede the same arm/rep from the pilot. Keep the pilot
# only where a repair campaign has no sample for that arm/rep.
CAMPAIGNS = [
    "homo_v2_bm_tune_iomax1_20261109",
    "homo_v2_bm_repair_core1_20261108",
    "homo_v2_bm_repair_linnos4_20261108",
    "homo_v2_bm_repair_frontier3_20261108",
    "homo_v2_bm_repair_frontier2_20261108",
    "homo_v2_bm_repair_frontier_20261108",
    "homo_v2_bm_pilot_r1_retry_20261108",
]

PAIR_GROUPS = {
    "elev_mq": ("n_mq_deadline", "x_mq_deadline_q"),
    "elev_kyber": ("n_kyber", "x_kyber_q"),
    "ctrl_iomax": ("n_io_max", "x_iomax_q"),
    "ctrl_weight": ("n_io_weight", "x_weight_arb"),
    "ctrl_cost": ("n_io_cost", "x_cost_arb"),
    "ctrl_latency": ("n_io_latency", "x_latency_arb"),
    # native arm since 2026-11-10: the ioextd2fq kernel port
    "front_d2fq": ("front_n_d2fq", "x_d2fq_arb"),
    "front_linnos": ("front_n_none", "x_linnos_q"),
}

HEADLINE_METRICS = {
    "elev_read_p99_us",
    "lc_read_p99_us",
    "be_track_err_pct",
    "share_ratio_dev",
    "jain_index",
}


def read_campaign(name: str) -> tuple[list[dict], list[dict]]:
    base = RESULT_ROOT / name / "analysis"
    cases = base / "homo_v2_cases.csv"
    gates = base / "homo_v2_gate_detail.csv"
    if not cases.exists() or not gates.exists():
        return [], []
    gate_ok = {}
    for row in csv.DictReader(gates.open()):
        gate_ok[(row["seq"], row["rep"])] = row["checks_failed"] == "all"
    rows = []
    for row in csv.DictReader(cases.open()):
        if row["gate_ok"] != "1" or not gate_ok.get((row["seq"], row["rep"]), False):
            continue
        row = dict(row)
        row["campaign"] = name
        row["value_num"] = float(row["value"])
        rows.append(row)
    return rows, list(csv.DictReader(gates.open()))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected: dict[tuple[str, str, str], dict] = {}
    all_gates = []
    for campaign in CAMPAIGNS:
        rows, gates = read_campaign(campaign)
        all_gates.extend([{**g, "campaign": campaign} for g in gates])
        for row in rows:
            key = (row["arm"], row["rep"], row["metric"])
            if key not in selected:
                selected[key] = row

    valid_rows = list(selected.values())
    with (OUT_DIR / "valid_pair_cases.csv").open("w", newline="") as f:
        fields = ["campaign", "seq", "rep", "arm", "group", "family", "metric", "value", "gate_ok"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(valid_rows)

    by_arm_metric = defaultdict(list)
    for row in valid_rows:
        by_arm_metric[(row["arm"], row["metric"])].append(row)
    summary = []
    for (arm, metric), rows in sorted(by_arm_metric.items()):
        vals = sorted(r["value_num"] for r in rows)
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        summary.append({
            "arm": arm, "metric": metric, "n": n,
            "median": f"{median:.8g}", "min": f"{vals[0]:.8g}",
            "max": f"{vals[-1]:.8g}",
            "reps": ",".join(sorted({r["rep"] for r in rows}, key=int)),
            "campaigns": ",".join(sorted({r["campaign"] for r in rows})),
        })
    with (OUT_DIR / "repair_summary.csv").open("w", newline="") as f:
        fields = ["arm", "metric", "n", "median", "min", "max", "reps", "campaigns"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(summary)

    # Only compare arms when both sides are valid in the same campaign and rep.
    valid_keys = {(r["campaign"], r["rep"], r["arm"], r["metric"]): r for r in valid_rows}
    pair_rows = []
    for pair_id, (native, iox) in PAIR_GROUPS.items():
        keys = sorted({(c, rep) for c, rep, arm, metric in valid_keys if arm == native for metric in HEADLINE_METRICS}
                      & {(c, rep) for c, rep, arm, metric in valid_keys if arm == iox for metric in HEADLINE_METRICS})
        # Use common metrics per common campaign/rep; one row per pair/metric.
        for campaign, rep in keys:
            metrics = sorted({m for c, r, a, m in valid_keys if c == campaign and r == rep and a in (native, iox)} & HEADLINE_METRICS)
            for metric in metrics:
                n = valid_keys.get((campaign, rep, native, metric))
                x = valid_keys.get((campaign, rep, iox, metric))
                if n and x:
                    nv, xv = n["value_num"], x["value_num"]
                    pair_rows.append({
                        "pair_id": pair_id, "campaign": campaign, "rep": rep,
                        "metric": metric, "native_arm": native,
                        "iox_arm": iox, "native": f"{nv:.8g}",
                        "iox": f"{xv:.8g}",
                        "iox_over_native": f"{xv / nv:.8g}" if nv else "NA",
                    })
    with (OUT_DIR / "native_vs_iox_pairs.csv").open("w", newline="") as f:
        fields = ["pair_id", "campaign", "rep", "metric", "native_arm", "iox_arm", "native", "iox", "iox_over_native"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(pair_rows)

    valid_arms = defaultdict(set)
    for r in valid_rows:
        valid_arms[r["arm"]].add(r["rep"])
    md = ["# Homo-v2 bm Repair Tables", "", "Only gate-valid samples are included. Native/IOX ratios require the same campaign and rep.", "", "## Valid samples", "", "| arm | reps | n(metric rows) |", "|---|---|---:|"]
    for arm in sorted(valid_arms):
        n = sum(1 for r in valid_rows if r["arm"] == arm)
        md.append(f"| `{arm}` | {','.join(sorted(valid_arms[arm], key=int))} | {n} |")
    md += ["", "## Native vs IOX", "", "| pair | rep | metric | native | IOX | IOX/native |", "|---|---:|---|---:|---:|---:|"]
    for r in pair_rows:
        md.append(f"| {r['pair_id']} | {r['rep']} | {r['metric']} | {r['native']} | {r['iox']} | {r['iox_over_native']} |")
    md += ["", "## Exclusions", "", "Phase-invalid samples remain in source campaign directories and are excluded by gate. See each campaign's `homo_v2_gate_detail.csv` and `state/events` for the exact reason."]
    (OUT_DIR / "native_vs_iox_table.md").write_text("\n".join(md) + "\n")

    with (OUT_DIR / "excluded_pairs.csv").open("w", newline="") as f:
        fields = ["campaign", "seq", "rep", "arm", "checks_failed", "detail"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader()
        for row in all_gates:
            if row.get("checks_failed") != "all": w.writerow(row)
    print(f"valid_rows={len(valid_rows)} pair_rows={len(pair_rows)} output={OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
