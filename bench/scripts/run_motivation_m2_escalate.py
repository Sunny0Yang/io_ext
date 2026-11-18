#!/usr/bin/env python3
"""Escalate unlimited FIO background pressure until YCSB P99/throughput thresholds are met."""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path


DEFAULT_LADDER = [
    (32, 4),
    (64, 4),
    (128, 2),
    (128, 4),
    (64, 8),
    (128, 8),
    (256, 4),
    (128, 16),
    (256, 8),
]


def parse_ladder(text: str):
    out = []
    for token in text.split(","):
        token = token.strip().lower()
        if not token:
            continue
        depth_s, jobs_s = token.split("x", 1)
        out.append((int(depth_s), int(jobs_s)))
    return out


def load_metrics(campaign_dir: Path):
    rows = []
    for path in sorted(campaign_dir.glob("*/result.json")):
        data = json.loads(path.read_text())
        if data.get("status") != "success":
            raise RuntimeError(f"case failed: {path.parent.name} {data.get('error')}")
        app = data.get("application") or {}

        def metric(op, key):
            return (app.get(op) or {}).get(key)

        rows.append(
            {
                "case": path.parent.name,
                "read_p99_us": metric("READ", "99thPercentileLatency(us)"),
                "update_p99_us": metric("UPDATE", "99thPercentileLatency(us)"),
                "ycsb_ops_s": (app.get("OVERALL") or {}).get("Throughput(ops/sec)"),
                "achieved_mib_s": float(data.get("background_achieved_mib_s") or 0.0),
                "fio_iodepth": data.get("fio_iodepth"),
                "fio_numjobs": data.get("fio_numjobs"),
            }
        )
    if not rows:
        raise RuntimeError(f"no successful cases under {campaign_dir}")
    return rows


def aggregate(rows):
    return {
        "n": len(rows),
        "read_p99_us": statistics.mean(r["read_p99_us"] for r in rows),
        "update_p99_us": statistics.mean(r["update_p99_us"] for r in rows),
        "ycsb_ops_s": statistics.mean(r["ycsb_ops_s"] for r in rows),
        "achieved_mib_s": statistics.mean(r["achieved_mib_s"] for r in rows),
        "cases": [r["case"] for r in rows],
    }


def ratios(pressure, baseline):
    return {
        "read_p99": pressure["read_p99_us"] / baseline["read_p99_us"],
        "update_p99": pressure["update_p99_us"] / baseline["update_p99_us"],
        "throughput": pressure["ycsb_ops_s"] / baseline["ycsb_ops_s"],
        "achieved_mib_s": pressure.get("achieved_mib_s", 0.0),
    }


def criteria_met(r, args):
    p99_ok = r["read_p99"] >= args.read_p99_factor or r["update_p99"] >= args.update_p99_factor
    tp_ok = r["throughput"] <= args.throughput_factor
    return p99_ok and tp_ok


def run_campaign(runner: Path, bundle: Path, out: Path, rates: str, profiles: str, repeats: int, seconds: int):
    if out.exists():
        shutil.rmtree(out)
    cmd = [
        sys.executable,
        str(runner),
        "--bundle",
        str(bundle),
        "--output",
        str(out),
        "--allow-format",
        "--schedulers",
        "none",
        "--rates",
        rates,
        "--repeats",
        str(repeats),
        "--seconds",
        str(seconds),
    ]
    if profiles:
        cmd.extend(["--fio-profiles", profiles])
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runner", type=Path, default=Path(__file__).with_name("run_motivation_m2_campaign.py"))
    parser.add_argument("--read-p99-factor", type=float, default=1.30, help="READ P99 must reach baseline × this")
    parser.add_argument("--update-p99-factor", type=float, default=1.20, help="UPDATE P99 must reach baseline × this")
    parser.add_argument("--throughput-factor", type=float, default=0.88, help="YCSB ops/s must fall to baseline × this")
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--search-repeats", type=int, default=1)
    parser.add_argument("--confirm-repeats", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--ladder", default="", help="Override comma list like 32x4,64x4,...")
    args = parser.parse_args()

    ladder = parse_ladder(args.ladder) if args.ladder else DEFAULT_LADDER
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "thresholds": {
            "read_p99_factor": args.read_p99_factor,
            "update_p99_factor": args.update_p99_factor,
            "throughput_factor": args.throughput_factor,
            "rule": "(read_p99 >= baseline×read OR update_p99 >= baseline×update) AND throughput <= baseline×tp",
        },
        "ladder": [f"{d}x{j}" for d, j in ladder],
        "steps": [],
    }

    baseline_dir = args.output / "baseline"
    print(f"=== baseline x{args.baseline_repeats} ===", flush=True)
    run_campaign(args.runner, args.bundle, baseline_dir, "0", "", args.baseline_repeats, args.seconds)
    baseline = aggregate(load_metrics(baseline_dir))
    report["baseline"] = baseline
    print(f"baseline READ P99={baseline['read_p99_us']:.1f}us UPDATE P99={baseline['update_p99_us']:.1f}us ops/s={baseline['ycsb_ops_s']:.0f}", flush=True)

    winner = None
    for depth, jobs in ladder:
        profile = f"{depth}x{jobs}"
        step_dir = args.output / f"search_{profile}"
        print(f"=== search max {profile} x{args.search_repeats} ===", flush=True)
        run_campaign(args.runner, args.bundle, step_dir, "max", profile, args.search_repeats, args.seconds)
        pressure = aggregate(load_metrics(step_dir))
        r = ratios(pressure, baseline)
        met = criteria_met(r, args)
        step = {
            "phase": "search",
            "profile": profile,
            "metrics": pressure,
            "ratios": r,
            "criteria_met": met,
        }
        report["steps"].append(step)
        print(
            f"  achieved={pressure['achieved_mib_s']:.1f} MiB/s "
            f"read×={r['read_p99']:.3f} update×={r['update_p99']:.3f} tp×={r['throughput']:.3f} met={met}",
            flush=True,
        )
        if met:
            winner = profile
            break

    if winner:
        confirm_dir = args.output / f"confirm_{winner}"
        print(f"=== confirm {winner} x{args.confirm_repeats} ===", flush=True)
        run_campaign(args.runner, args.bundle, confirm_dir, "max", winner, args.confirm_repeats, args.seconds)
        confirmed = aggregate(load_metrics(confirm_dir))
        r = ratios(confirmed, baseline)
        report["winner"] = {
            "profile": winner,
            "metrics": confirmed,
            "ratios": r,
            "criteria_met": criteria_met(r, args),
        }
        print(
            f"CONFIRMED {winner}: achieved={confirmed['achieved_mib_s']:.1f} MiB/s "
            f"read×={r['read_p99']:.3f} update×={r['update_p99']:.3f} tp×={r['throughput']:.3f}",
            flush=True,
        )
    else:
        report["winner"] = None
        print("STOP: ladder exhausted without meeting thresholds", flush=True)

    (args.output / "escalation_report.json").write_text(json.dumps(report, indent=2))
    return 0 if report.get("winner") else 2


if __name__ == "__main__":
    raise SystemExit(main())
