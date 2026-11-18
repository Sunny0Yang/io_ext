#!/usr/bin/env python3
"""Preview figures from available dynamic-tenant campaign data (contract §7.7–7.8).

Figure A: 1s-window per tenant over T1..T6 (gaps when inactive or no completions):
  - p99 / solo_p99 (default, aligns with §7.8 loss)
  - mib_s / solo_mib_s (bandwidth completion vs solo steady)

Figure B: weighted tail loss (§7.8) across static arms (kvmhost or virtme).

Usage:
  python3 scripts/plot_dynamic_tenant_preview.py --all-arms
  python3 scripts/plot_dynamic_tenant_preview.py --arm DYN_arm_f1_arbfb
  python3 scripts/plot_dynamic_tenant_preview.py --all-arms --grid
  python3 scripts/plot_dynamic_tenant_preview.py --all-arms --grid --metric bw
  python3 scripts/plot_dynamic_tenant_preview.py --all-arms --grid --metric both
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import analyze_dynamic_tenant_calibration as calib  # noqa: E402

TENANT_SPANS = {"a": (1, 6), "b": (2, 3), "c": (3, 4), "d": (4, 5)}
TENANT_DIR = {"a": 0, "b": 0, "c": 0, "d": 1}
TENANT_LABEL = {
    "a": "A (4K read QD1)",
    "b": "B (64K read QD8)",
    "c": "C (64K read QD8)",
    "d": "D (4K write QD64)",
}
TENANT_COLOR = {"a": "#1f77b4", "b": "#ff7f0e", "c": "#2ca02c", "d": "#d62728"}
PHASE_NAMES = ["T1", "T2", "T3", "T4", "T5", "T6"]

STATIC_ARM_ORDER = [
    "DYN_arm_n1_native_none",
    "DYN_arm_n2_native_mqddl",
    "DYN_arm_n3_native_bfq",
    "DYN_arm_n4_native_kyber",
    "DYN_arm_x1_qnone",
    "DYN_arm_x2_qfifo_wdrr",
    "DYN_arm_x3_qddl_wdrr",
    "DYN_arm_x4_spwedf_wdrr",
    "DYN_arm_f1_arbfb",
    "DYN_arm_c1_io_max",
    "DYN_arm_c2_bfq_weight",
    "DYN_arm_c3_io_latency",
    "DYN_arm_c4_io_cost",
]

ARM_DISPLAY = {
    "DYN_arm_n1_native_none": "N1 none",
    "DYN_arm_n2_native_mqddl": "N2 mq-deadline",
    "DYN_arm_n3_native_bfq": "N3 bfq",
    "DYN_arm_n4_native_kyber": "N4 kyber",
    "DYN_arm_x1_qnone": "X1 q_none+wdrr",
    "DYN_arm_x2_qfifo_wdrr": "X2 q_fifo+wdrr",
    "DYN_arm_x3_qddl_wdrr": "X3 q_ddl+wdrr",
    "DYN_arm_x4_spwedf_wdrr": "X4 sp_edf+wdrr",
    "DYN_arm_f1_arbfb": "F1 arb_feedback",
    "DYN_arm_c1_io_max": "C1 io.max",
    "DYN_arm_c2_bfq_weight": "C2 bfq+weight",
    "DYN_arm_c3_io_latency": "C3 io.latency",
    "DYN_arm_c4_io_cost": "C4 io.cost",
}


METRIC_SPECS = {
    "p99": {
        "hist_key": "p99_us",
        "solo_key": "p99_us_steady",
        "ylabel": "p99 / solo p99",
        "file_tag": "p99",
        "ylim_cap_default": 200.0,
    },
    "bw": {
        "hist_key": "mib_s",
        "solo_key": "steady_mib_s",
        "ylabel": "MiB/s / solo MiB/s",
        "file_tag": "bw",
        "ylim_cap_default": 1.35,
    },
}


def load_solo(calib_path: Path) -> Dict[str, Dict[str, float]]:
    data = json.loads(calib_path.read_text())
    out = {}
    for t in ("a", "b", "c", "d"):
        s = data["solo"][t]
        out[t] = {
            "p99_us_steady": float(s["p99_us_steady"]),
            "steady_mib_s": float(s["steady_mib_s"]),
        }
    return out


def tenant_active_sec(phase: int, tenant: str) -> bool:
    lo, hi = TENANT_SPANS[tenant]
    return lo <= phase <= hi


def ratio_timeseries(
    scene_dir: Path,
    seq_token: str,
    stat: Dict[str, Any],
    solo: Dict[str, Dict[str, float]],
    metric: str,
) -> Tuple[List[float], Dict[str, List[Optional[float]]], List[float]]:
    spec = METRIC_SPECS[metric]
    hist_key = spec["hist_key"]
    solo_key = spec["solo_key"]
    t0_epoch = float(stat["start_time"])
    t0_mono = float(stat["t0_monotonic"])
    bnds = [t0_epoch + (float(b["planned_monotonic"]) - t0_mono) for b in stat["boundaries"]]
    t_end = bnds[-1]
    n_sec = max(1, int(math.ceil(t_end - t0_epoch)))
    time_axis = [float(i) for i in range(n_sec)]
    series: Dict[str, List[Optional[float]]] = {t: [math.nan] * n_sec for t in TENANT_SPANS}

    for tenant in TENANT_SPANS:
        direction = TENANT_DIR[tenant]
        hist = calib.read_hist_log(calib.hist_log_for(scene_dir, tenant, seq_token))
        per_sec = calib.per_second_series(hist, direction, t0_epoch, t_end)
        solo_val = solo[tenant][solo_key]
        for item in per_sec:
            idx = int(item["t_sec"])
            if idx < 0 or idx >= n_sec:
                continue
            phase = 1
            for pi in range(len(bnds) - 1):
                if bnds[pi] <= t0_epoch + idx < bnds[pi + 1]:
                    phase = pi + 1
                    break
            if not tenant_active_sec(phase, tenant):
                continue
            if item.get("missing") or int(item.get("ios", 0)) == 0:
                continue
            val = float(item.get(hist_key) or 0.0)
            if val <= 0 or solo_val <= 0:
                continue
            series[tenant][idx] = val / solo_val
    return time_axis, series, bnds


def list_static_arms(results_root: Path) -> List[str]:
    present = {p.name for p in results_root.glob("DYN_arm_*") if p.is_dir()}
    arms = [a for a in STATIC_ARM_ORDER if a in present]
    extra = sorted(present - set(arms) - {n for n in present if "invalidated" in n or n == "DYN_arm_smoke_ab"})
    return arms + extra


def load_arm_series(
    results_root: Path,
    arm: str,
    calib_path: Path,
    metric: str = "p99",
) -> Tuple[Dict[str, Any], List[float], Dict[str, List[Optional[float]]], List[float]]:
    solo = load_solo(calib_path)
    seq_dir = results_root / arm
    scene_dirs = sorted((seq_dir / "step_0").glob("ib_rdyn_*"))
    if not scene_dirs:
        raise FileNotFoundError(f"no scene under {seq_dir}")
    scene_dir = scene_dirs[0]
    stat = calib.load_timeline_stat(scene_dir, scene_dir.name)
    seq_token = stat["timeline_id"]
    time_axis, series, bnds = ratio_timeseries(scene_dir, seq_token, stat, solo, metric)
    return stat, time_axis, series, bnds


def _series_ymax(series: Dict[str, List[Optional[float]]], cap: float = 200.0) -> float:
    vals = [y for row in series.values() for y in row if y is not None and not math.isnan(y)]
    if not vals:
        return 10.0
    return min(max(vals) * 1.08, cap)


def draw_timeseries_ax(
    ax: plt.Axes,
    arm: str,
    stat: Dict[str, Any],
    time_axis: List[float],
    series: Dict[str, List[Optional[float]]],
    bnds: List[float],
    *,
    metric: str = "p99",
    ylim_top: Optional[float] = None,
    show_legend: bool = True,
    title_suffix: str = "",
) -> None:
    t0 = float(stat["start_time"])
    for tenant in ("a", "b", "c", "d"):
        ax.plot(
            time_axis,
            series[tenant],
            label=TENANT_LABEL[tenant],
            color=TENANT_COLOR[tenant],
            linewidth=1.0,
            alpha=0.9,
        )
    ymax = ylim_top if ylim_top is not None else _series_ymax(series)
    ax.set_ylim(0, ymax)
    for i, b in enumerate(bnds):
        rel = b - t0
        ax.axvline(rel, color="#888888", linewidth=0.6, linestyle="--", alpha=0.7)
        if i < len(PHASE_NAMES):
            ax.text(rel + 0.5, ymax * 0.92, PHASE_NAMES[i], fontsize=7, color="#555555", va="top")
    ax.axhline(1.0, color="#aaaaaa", linewidth=0.5, linestyle=":")
    ax.set_xlabel("Time (s)", fontsize=8)
    ax.set_ylabel(METRIC_SPECS[metric]["ylabel"], fontsize=8)
    sched = stat.get("scheduler") or "?"
    ax.set_title(f"{ARM_DISPLAY.get(arm, arm)} ({sched}){title_suffix}", fontsize=9)
    if show_legend:
        ax.legend(loc="upper left", fontsize=6, framealpha=0.85, ncol=2)


def plot_arm_timeseries(
    results_root: Path,
    arm: str,
    calib_path: Path,
    out_path: Path,
    *,
    metric: str = "p99",
    ylim_top: Optional[float] = None,
) -> None:
    stat, time_axis, series, bnds = load_arm_series(results_root, arm, calib_path, metric)
    fig, ax = plt.subplots(figsize=(11, 4.2), layout="constrained")
    draw_timeseries_ax(
        ax, arm, stat, time_axis, series, bnds,
        metric=metric, ylim_top=ylim_top, show_legend=True,
        title_suffix=f"  L={stat.get('phase_length_sec')}s",
    )
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def plot_all_arm_timeseries(
    results_root: Path,
    calib_path: Path,
    fig_dir: Path,
    *,
    metric: str = "p99",
    grid: bool = False,
    ylim_cap: Optional[float] = None,
) -> None:
    cap = ylim_cap if ylim_cap is not None else METRIC_SPECS[metric]["ylim_cap_default"]
    file_tag = METRIC_SPECS[metric]["file_tag"]
    arms = list_static_arms(results_root)
    if not arms:
        raise FileNotFoundError(f"no DYN_arm_* under {results_root}")

    loaded = []
    global_ymax = 10.0 if metric == "p99" else 0.5
    for arm in arms:
        try:
            stat, time_axis, series, bnds = load_arm_series(
                results_root, arm, calib_path, metric,
            )
        except FileNotFoundError as exc:
            print(f"skip {arm}: {exc}", file=sys.stderr)
            continue
        ymax = _series_ymax(series, cap=cap)
        global_ymax = max(global_ymax, ymax)
        loaded.append((arm, stat, time_axis, series, bnds))

    shared_ylim = min(global_ymax * 1.05, cap)
    ts_dir = fig_dir / "timeseries"
    ts_dir.mkdir(parents=True, exist_ok=True)

    for arm, stat, time_axis, series, bnds in loaded:
        out = ts_dir / f"{arm}_{file_tag}_timeseries.png"
        plot_arm_timeseries(
            results_root, arm, calib_path, out,
            metric=metric, ylim_top=shared_ylim,
        )
        print(f"wrote {out}")

    if grid and loaded:
        n = len(loaded)
        ncols = 3
        nrows = (n + ncols - 1) // ncols
        fig, axes = plt.subplots(nrows, ncols, figsize=(14, 3.2 * nrows), layout="constrained")
        axes_flat = axes.flatten() if n > 1 else [axes]
        for ax, (arm, stat, time_axis, series, bnds) in zip(axes_flat, loaded):
            draw_timeseries_ax(
                ax, arm, stat, time_axis, series, bnds,
                metric=metric, ylim_top=shared_ylim, show_legend=False,
            )
        for ax in axes_flat[len(loaded):]:
            ax.axis("off")
        leg_handles = [
            plt.Line2D([0], [0], color=TENANT_COLOR[t], lw=2, label=TENANT_LABEL[t])
            for t in ("a", "b", "c", "d")
        ]
        fig.legend(handles=leg_handles, loc="upper center", ncol=4, fontsize=8, frameon=False)
        grid_out = fig_dir / f"static_arms_{file_tag}_timeseries_grid.png"
        fig.savefig(grid_out, dpi=160)
        plt.close(fig)
        print(f"wrote {grid_out}")


def plot_tuning_matrix(results_root: Path, out_path: Path) -> None:
    matrix_path = results_root / "analysis" / "tuning_matrix.json"
    if not matrix_path.is_file():
        raise FileNotFoundError(f"run analyze_dynamic_tenant_static_arms.py first: {matrix_path}")
    matrix = json.loads(matrix_path.read_text())
    rows = [
        (k, v)
        for k, v in matrix.items()
        if k.startswith("DYN_arm_") and "invalidated" not in k and k != "DYN_arm_smoke_ab"
    ]
    rows = [(k, v) for k, v in rows if v.get("loss") is not None]
    rows.sort(key=lambda kv: kv[1]["loss"])
    labels = [ARM_DISPLAY.get(k, k.replace("DYN_arm_", "")) for k, _ in rows]
    losses = [float(v["loss"]) for _, v in rows]
    feasible = [bool(v.get("guardrail_feasible")) for _, v in rows]
    colors = ["#2ca02c" if f else "#c7c7c7" for f in feasible]
    best_idx = 0

    fig, ax = plt.subplots(figsize=(10, 4.5), layout="constrained")
    bars = ax.barh(range(len(labels)), losses, color=colors, edgecolor="#333333", linewidth=0.4)
    bars[best_idx].set_edgecolor("#000000")
    bars[best_idx].set_linewidth(1.5)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("Weighted tail loss (§7.8, lower is better)")
    ax.set_title("Static arms — kvmhost campaign (available data)")
    leg = [
        mpatches.Patch(color="#2ca02c", label="guardrails feasible"),
        mpatches.Patch(color="#c7c7c7", label="guardrail / scene error"),
    ]
    # upper right is the open quadrant (sorted bars put the shortest loss rows
    # at the top); lower right would sit on the longest bar.
    ax.legend(handles=leg, loc="upper right", fontsize=8)
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", type=Path, default=REPO / "results/dyn_tenant_kvmhost")
    ap.add_argument("--calib", type=Path, default=None)
    ap.add_argument("--arm", default="DYN_arm_f1_arbfb")
    ap.add_argument("--all-arms", action="store_true", help="plot every static arm")
    ap.add_argument("--grid", action="store_true", help="with --all-arms, also write 3-column grid")
    ap.add_argument(
        "--metric",
        choices=("p99", "bw", "both"),
        default="p99",
        help="timeseries y: tail latency ratio, bandwidth completion ratio, or both",
    )
    ap.add_argument(
        "--ylim-cap",
        type=float,
        default=None,
        help="shared y-axis cap (defaults: p99=200, bw=1.35)",
    )
    ap.add_argument("--no-bar", action="store_true", help="skip weighted-loss bar chart")
    args = ap.parse_args()
    calib_path = args.calib or (args.results_root / "contract/calibration.json")
    fig_dir = args.results_root / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    metrics = ("p99", "bw") if args.metric == "both" else (args.metric,)
    if args.all_arms:
        for m in metrics:
            plot_all_arm_timeseries(
                args.results_root, calib_path, fig_dir,
                metric=m, grid=args.grid, ylim_cap=args.ylim_cap,
            )
    else:
        for m in metrics:
            tag = METRIC_SPECS[m]["file_tag"]
            ts_out = fig_dir / f"{args.arm}_{tag}_timeseries_preview.png"
            cap = args.ylim_cap
            if cap is None:
                cap = METRIC_SPECS[m]["ylim_cap_default"]
            plot_arm_timeseries(
                args.results_root, args.arm, calib_path, ts_out,
                metric=m, ylim_top=cap,
            )
            print(f"wrote {ts_out}")

    if not args.no_bar:
        bar_out = fig_dir / "static_arms_weighted_loss_kvmhost.png"
        plot_tuning_matrix(args.results_root, bar_out)
        print(f"wrote {bar_out}")


if __name__ == "__main__":
    main()
