#!/usr/bin/env python3
"""Compact 6-panel dual-axis: stacked phase IOPS bars + phase-median p99 lines.

Left Y: log p99 (µs); right Y: stacked mean IOPS (A/B/C/D) from clat_hist (§7.7).

Outputs: results/dyn_tenant_kvmhost/figures/dyn_tenant_compact6_combo.{png,pdf}
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import analyze_dynamic_tenant_calibration as calib  # noqa: E402
from analyze_dynamic_tenant_static_arms import (  # noqa: E402
    TENANT_DIR,
    TENANT_SPANS,
    boundary_epochs,
)
from plot_dyn_tenant_compact6 import COMPACT_ARMS, phase_median_p99  # noqa: E402
from plot_dyn_tenant_main import (  # noqa: E402
    COLORS,
    LPHASE,
    NUM_PHASES,
    RESULTS,
    SEED_OFF,
    TENANTS,
    collect_arm,
)

STACK_ORDER = TENANTS  # bottom -> top: a, b, c, d


def collect_phase_iops(seq_dir: Path) -> dict[str, list[float | None]]:
    """Mean IOPS per contract phase for each tenant (None if inactive)."""
    scene_dirs = sorted((seq_dir / "step_0").glob("ib_rdyn_*"))
    if not scene_dirs:
        raise FileNotFoundError(f"no scene under {seq_dir}")
    scene_dir = scene_dirs[0]
    stat = calib.load_timeline_stat(scene_dir, scene_dir.name)
    seq_token = stat["timeline_id"]
    bnds = boundary_epochs(stat)
    num_phases = int(stat["num_phases"])
    spans = {
        t: (int(w[0]), int(w[1]))
        for t, w in (stat.get("tenant_windows") or TENANT_SPANS).items()
    }
    out: dict[str, list[float | None]] = {
        t: [None] * num_phases for t in TENANTS
    }
    for tenant in TENANTS:
        start_p, end_p = spans[tenant]
        direction = TENANT_DIR[tenant]
        hist = calib.read_hist_log(calib.hist_log_for(scene_dir, tenant, seq_token))
        for p in range(1, num_phases + 1):
            if p < start_p or p > end_p:
                continue
            w0, w1 = bnds[p - 1], bnds[p]
            merged, _ = calib.merge_hist_window(hist, direction, w0, w1)
            st = calib.hist_stats(merged)
            dur = w1 - w0
            out[tenant][p - 1] = (st["ios"] / dur) if dur > 0 else 0.0
    return out


def render(
    per_arm: dict,
    iops_arm: dict[str, dict[str, list[float | None]]],
    missing: dict,
    out_dir: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    phase_x = [(k - 0.5) * LPHASE for k in range(1, NUM_PHASES + 1)]
    all_p99 = []
    for arm, _ in COMPACT_ARMS:
        data = per_arm.get(arm)
        if not data:
            continue
        for tenant in TENANTS:
            rows = data["rows"].get(tenant) or []
            for k in range(1, NUM_PHASES + 1):
                v = phase_median_p99(rows, k)
                if v is not None:
                    all_p99.append(v)
    if not all_p99:
        raise SystemExit("no p99 data")
    p99_lo = 0.85 * 10 ** math.floor(math.log10(min(all_p99)))
    p99_hi = 1.15 * 10 ** math.ceil(math.log10(max(all_p99)))

    max_stack = 1.0
    for arm, _ in COMPACT_ARMS:
        iops = iops_arm.get(arm)
        if not iops:
            continue
        for pi in range(NUM_PHASES):
            max_stack = max(
                max_stack,
                sum((iops[t][pi] or 0.0) for t in TENANTS),
            )
    iops_hi = max_stack * 1.08

    fig, axes = plt.subplots(2, 3, figsize=(12.8, 7.8), sharex=True)
    x_max = LPHASE * NUM_PHASES
    bar_w = LPHASE * 0.78

    held_path = REPO / "results/dyn_tenant_kvmhost/heldout_analysis.json"
    held = json.loads(held_path.read_text()) if held_path.exists() else {}

    for idx, (arm, title) in enumerate(COMPACT_ARMS):
        ax = axes.flat[idx]
        ax2 = ax.twinx()

        data = per_arm.get(arm)
        iops = iops_arm.get(arm)
        if data is None or iops is None:
            ax.text(
                0.5, 0.5, f"MISSING: {missing.get(arm, '?')}",
                transform=ax.transAxes, ha="center", va="center", color="crimson",
            )
            ax.set_title(title, fontsize=9)
            continue

        bottoms = [0.0] * NUM_PHASES
        for tenant in STACK_ORDER:
            heights = [
                float(iops[tenant][pi]) if iops[tenant][pi] is not None else 0.0
                for pi in range(NUM_PHASES)
            ]
            ax2.bar(
                phase_x,
                heights,
                width=bar_w,
                bottom=bottoms,
                color=COLORS[tenant],
                edgecolor="white",
                linewidth=0.4,
                alpha=0.42,
                zorder=1,
                align="center",
            )
            bottoms = [b + h for b, h in zip(bottoms, heights)]

        for tenant in TENANTS:
            rows = data["rows"].get(tenant) or []
            ys = [phase_median_p99(rows, k) for k in range(1, NUM_PHASES + 1)]
            xs, yplot = [], []
            for x, y in zip(phase_x, ys):
                if y is None:
                    continue
                xs.append(x)
                yplot.append(y)
            if len(xs) >= 2:
                ax.plot(xs, yplot, color=COLORS[tenant], lw=2.1, marker="o",
                        ms=4.5, zorder=4)
            elif len(xs) == 1:
                ax.plot(xs, yplot, color=COLORS[tenant], marker="o", ms=5,
                        ls="none", zorder=4)

        loss = (
            held.get("main_figure_matrix", {}).get("arms", {}).get(arm, {}).get(
                "weighted_tail_loss"
            )
        )
        if loss is not None:
            ax.text(0.02, 0.97, f"loss={loss:.2f}", transform=ax.transAxes,
                    fontsize=7, va="top", color=".35", zorder=5)

        for k in range(1, NUM_PHASES + 1):
            ax.axvline(LPHASE * k, color=".78", ls="--", lw=0.45, zorder=0)
        for k in range(1, NUM_PHASES):
            ax.axvspan(LPHASE * (k - 1), LPHASE * k, color=".94", alpha=0.28, zorder=0)

        ax.set_title(title, fontsize=8.8, pad=5)
        ax.set_yscale("log")
        ax.set_xlim(0, x_max)
        ax.set_ylim(p99_lo, p99_hi)
        ax.grid(True, alpha=0.22, lw=0.45, zorder=0)
        ax.tick_params(labelsize=7.5, zorder=5)
        ax.set_zorder(2)
        ax.patch.set_alpha(0.0)

        ax2.set_ylim(0, iops_hi)
        ax2.tick_params(labelsize=7.5, colors=".45")
        ax2.yaxis.label.set_color(".45")
        if idx % 3 == 2:
            ax2.set_ylabel("stacked IOPS", fontsize=8, color=".45")
        else:
            ax2.set_yticklabels([])

        if idx >= 3:
            ax.set_xlabel("Time from t0 (s)", fontsize=8.5)
        if idx % 3 == 0:
            ax.set_ylabel("clat p99 (µs)", fontsize=8.5)

    line_handles = [
        Line2D([0], [0], color=COLORS[t], lw=2.2, marker="o", label=f"{t.upper()} p99")
        for t in TENANTS
    ]
    bar_handles = [
        Patch(facecolor=COLORS[t], edgecolor="white", alpha=0.42,
              label=f"{t.upper()} IOPS (stack)")
        for t in STACK_ORDER
    ]
    all_handles = line_handles + bar_handles
    fig.legend(
        handles=all_handles,
        labels=[h.get_label() for h in all_handles],
        loc="upper center",
        bbox_to_anchor=(0.5, 1.01),
        ncol=4,
        frameon=False,
        fontsize=8,
    )
    fig.suptitle(
        "Held-out s201: dual axis — lines = phase-median p99 (left, log); "
        "bars = stacked mean IOPS per phase (right)\n"
        "Stack height = total active throughput (work conservation across schedulers)",
        fontsize=10,
        y=1.06,
    )
    fig.subplots_adjust(left=0.08, right=0.96, bottom=0.09, top=0.88, hspace=0.32, wspace=0.22)
    for ext in ("png", "pdf"):
        fig.savefig(out_dir / f"dyn_tenant_compact6_combo.{ext}", dpi=200)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--result-root", type=Path, default=RESULTS)
    ap.add_argument("--seed-off", type=int, default=SEED_OFF)
    args = ap.parse_args()
    out_dir = args.result_root / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    per_arm: dict = {}
    iops_arm: dict = {}
    missing: dict[str, str] = {}
    for arm, _ in COMPACT_ARMS:
        seq_dir = args.result_root / f"DYN_held_s{args.seed_off}_{arm}"
        if not seq_dir.is_dir():
            missing[arm] = "run dir absent"
            continue
        try:
            data, _ = collect_arm(seq_dir)
            if not data["rows"]:
                missing[arm] = "no p99 rows"
                continue
            per_arm[arm] = data
            iops_arm[arm] = collect_phase_iops(seq_dir)
        except Exception as exc:
            missing[arm] = repr(exc)

    if not per_arm:
        raise SystemExit("no data")
    render(per_arm, iops_arm, missing, out_dir)
    print(f"rendered {out_dir}/dyn_tenant_compact6_combo.png|pdf; missing={missing or 'none'}")


if __name__ == "__main__":
    main()
