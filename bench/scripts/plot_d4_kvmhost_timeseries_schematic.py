#!/usr/bin/env python3
"""Standalone D4 LC burst time-series (kvmhost PD4 rerun, n=3 median).

New schematic — does not modify fig_d3_d4_rq13_combo. Reads clat logs from
paper_sdb_D4LC_*_r{1..N}.tar.gz, 250 ms window p99, median across reps.
Optional translucent min–max band per arm (n>=2 windows).

Output: <rerun-dir>/figures/fig_d4_kvmhost_lc_burst_timeseries.{png,pdf}
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter, NullLocator

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import plot_d3_kvmhost_pareto_merged as d3  # noqa: E402

RERUN_DEFAULT = ROOT / "results/isol_bench/paper/d4_kvmhost_rerun_kvmhost_d4_rerun_20261116"
SOLO_JSON = ROOT / "results/isol_bench/paper/paper_sdb_d4_lc/solo_lc_none_fio.json"
STEM = "fig_d4_kvmhost_lc_burst_timeseries"

# Match combo D4 panel geometry (right half aspect).
FIG_W = 2.55
FIG_H = 2.05
WIN_S = 0.25
MIN_N = 4
REPS = 3
TICK_PAD = 1.0
D4_YLABEL_PAD = 2.5
BURST = "#b8860b"
SOLO = "#c62828"
D4_DASH = (0, (5, 2.5))
IO_EXT_COLOR = d3.FR_COLORS["iox"]

plt.rcParams.update(d3.MPL_RC)

# (sequence suffix, legend label, color, linestyle, linewidth) — palette = combo D3/D4
ARMS: list[tuple[str, str, str, str | tuple, float]] = [
    ("none", "none", d3.REH_COLORS["none"], D4_DASH, 0.9),
    ("mq_deadline", d3.LEGEND_REH["mq_deadline"], d3.REH_COLORS["mq_deadline"], D4_DASH, 0.9),
    ("kyber", "kyber", d3.REH_COLORS["io_latency"], D4_DASH, 0.9),
    ("bfq", d3.LEGEND_REH["bfq"], d3.REH_COLORS["bfq"], D4_DASH, 0.9),
    ("io_max", d3.LEGEND_REH["io_max"], d3.REH_COLORS["io_max"], D4_DASH, 0.9),
    ("io_latency", d3.LEGEND_REH["io_latency"], d3.REH_COLORS["io_latency"], D4_DASH, 0.9),
    ("io_cost", d3.LEGEND_REH["io_cost"], d3.REH_COLORS["io_cost"], D4_DASH, 0.9),
    ("iox_q_ddl", "iox q_ddl", IO_EXT_COLOR, "-", 1.05),
    ("iox_q_wedf", "iox q_wedf", IO_EXT_COLOR, "-", 0.95),
    ("iox_q_sp_edf", "iox q_sp_edf", IO_EXT_COLOR, "-", 1.0),
]


def _d4_y_formatter(v: float, _p: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def _clat_from_tar(tar: Path) -> tuple[np.ndarray, np.ndarray]:
    with tarfile.open(tar, "r:gz") as tf:
        member = next(
            m for m in tf.getmembers()
            if m.isfile() and m.name.endswith("_clat.1.log") and "_lc" in m.name
        )
        raw = tf.extractfile(member).read().decode()  # type: ignore[union-attr]
    ts, vs = [], []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            ts.append(float(parts[0]))
            vs.append(float(parts[1]))
        except ValueError:
            continue
    if not ts:
        raise ValueError(f"{tar}: empty clat log")
    return np.asarray(ts) / 1000.0, np.asarray(vs) / 1000.0


def win_p99(t: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    edges = np.arange(0.0, t.max() + WIN_S, WIN_S)
    mids, vals = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (t >= a) & (t < b)
        if sel.sum() < MIN_N:
            continue
        mids.append(0.5 * (a + b))
        vals.append(np.percentile(v[sel], 99))
    return np.asarray(mids), np.asarray(vals)


def aggregate_arm(
    arm: str, rerun_dir: Path, reps: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    seq = f"D4LC_{arm}"
    by_m: dict[float, list[float]] = defaultdict(list)
    n_run = 0
    for rep in range(1, reps + 1):
        tar = rerun_dir / f"paper_sdb_{seq}_r{rep}.tar.gz"
        if not tar.is_file():
            continue
        t, v = _clat_from_tar(tar)
        m, y = win_p99(t, v)
        n_run += 1
        for mi, yi in zip(m, y):
            by_m[float(mi)].append(float(yi))
    if not by_m:
        raise FileNotFoundError(f"missing tars for {seq}")
    xs = sorted(by_m)
    med = np.asarray([float(np.median(by_m[x])) for x in xs])
    lo = np.asarray([float(min(by_m[x])) for x in xs])
    hi = np.asarray([float(max(by_m[x])) for x in xs])
    return np.asarray(xs), med, hi, lo, n_run


def solo_us() -> float:
    if SOLO_JSON.is_file():
        j = json.loads(SOLO_JSON.read_text())
        return j["jobs"][0]["read"]["clat_ns"]["percentile"]["99.000000"] / 1000.0
    return 506.0


def render(rerun_dir: Path, out_dir: Path, *, band: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.axvspan(-1.3, 0.0, color="0.93", zorder=0)
    ax.axvline(0.0, color=BURST, lw=1.1, zorder=2)
    ref = solo_us()
    ax.plot([-1.3, 10.0], [ref, ref], color=SOLO, lw=1.0, ls=D4_DASH, alpha=0.85, zorder=2)

    arm_meta = []
    for arm, label, color, ls, lw in ARMS:
        try:
            xs, med, hi, lo, n_run = aggregate_arm(arm, rerun_dir, REPS)
        except FileNotFoundError:
            print(f"skip {arm}: no tar")
            continue
        if band and n_run >= 2:
            ax.fill_between(xs, lo, hi, color=color, alpha=0.12, lw=0, zorder=2)
        ax.plot(xs, med, color=color, ls=ls, lw=lw, label=label, zorder=3)
        arm_meta.append({"arm": arm, "n_runs": n_run})

    ax.set_yscale("log")
    ax.set_ylim(45, 1.2e6)
    ax.set_yticks([1e2, 1e3, 1e4, 1e5, 1e6])
    ax.yaxis.set_major_formatter(FuncFormatter(_d4_y_formatter))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xlim(-1.3, 10.0)
    ax.set_xticks([0, 2, 4, 6, 8, 10])
    ax.set_xlabel("Time since burst (s)")
    ax.set_ylabel("LC p99 (µs)", labelpad=D4_YLABEL_PAD)
    ax.yaxis.set_label_position("right")
    ax.yaxis.tick_right()
    ax.tick_params(
        axis="y",
        left=False,
        right=True,
        labelleft=False,
        labelright=True,
        pad=TICK_PAD,
        length=2.5,
    )
    ax.tick_params(axis="x", pad=TICK_PAD, length=2.5)
    ax.grid(True, which="major", alpha=0.22, lw=0.55)
    ax.set_axisbelow(True)
    ax.legend(
        loc="lower left",
        bbox_to_anchor=(0.0, 1.02),
        ncol=5,
        frameon=False,
        handlelength=1.6,
        columnspacing=0.55,
        fontsize=5.0,
    )
    fig.subplots_adjust(left=0.11, right=0.86, bottom=0.15, top=0.76)

    meta = {
        "figure": STEM,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rerun_dir": str(rerun_dir),
        "reps": REPS,
        "solo_us": ref,
        "arms": arm_meta,
        "band": band,
    }
    for ext in ("png", "pdf"):
        fig.savefig(out_dir / f"{STEM}.{ext}", dpi=400 if ext == "png" else None)
    (out_dir / f"{STEM}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    plt.close(fig)
    print(f"Wrote {out_dir}/{STEM}.png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerun-dir", type=Path, default=RERUN_DEFAULT)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--band", action="store_true", help="min–max band across reps")
    args = ap.parse_args()
    out = args.out_dir or (args.rerun_dir / "figures")
    if not args.rerun_dir.is_dir():
        raise SystemExit(f"missing {args.rerun_dir}")
    render(args.rerun_dir, out, band=args.band)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())