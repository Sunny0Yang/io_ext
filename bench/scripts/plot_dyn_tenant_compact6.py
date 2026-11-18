#!/usr/bin/env python3
"""Compact 6-panel figure: 4 native schedulers + best io_ext static + F1.

Uses the same held-out seed (s201) data as plot_dyn_tenant_main.py but renders
phase-level median p99 (per tenant) so trends are readable without per-window
scatter markers.

Outputs: results/dyn_tenant_kvmhost/figures/dyn_tenant_compact6.{png,pdf}
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from plot_dyn_tenant_main import (  # noqa: E402
    COLORS,
    LPHASE,
    NUM_PHASES,
    RESULTS,
    SEED_OFF,
    TENANTS,
    collect_arm,
)

# x1_qnone has the lowest weighted_tail_loss among X1–X4 on held-out s201.
COMPACT_ARMS = [
    ("n1_native_none", "N1 native none"),
    ("n2_native_mqddl", "N2 native mq-deadline"),
    ("n3_native_bfq", "N3 native bfq"),
    ("n4_native_kyber", "N4 native kyber"),
    ("x1_qnone", "X1 io_ext q_none (best among X1–X4)"),
    ("f1_arbfb", "F1 arb_feedback (best static & resident dynamic)"),
]


def phase_median_p99(rows: list[dict], phase: int) -> float | None:
    lo = (phase - 1) * LPHASE
    hi = phase * LPHASE
    vals = [
        r["p99_clat_us"]
        for r in rows
        if r["window_start_s"] >= lo - 0.01
        and r["window_end_s"] <= hi + 0.01
        and r["p99_clat_us"] > 0
    ]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def render(per_arm: dict, missing: dict, out_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    phase_x = [(k - 0.5) * LPHASE for k in range(1, NUM_PHASES + 1)]
    all_vals = []
    for arm, _ in COMPACT_ARMS:
        data = per_arm.get(arm)
        if not data:
            continue
        for tenant in TENANTS:
            rows = data["rows"].get(tenant) or []
            for k in range(1, NUM_PHASES + 1):
                v = phase_median_p99(rows, k)
                if v is not None:
                    all_vals.append(v)
    if not all_vals:
        raise SystemExit("no phase medians to plot")
    minimum, maximum = min(all_vals), max(all_vals)
    low = 0.85 * 10 ** math.floor(math.log10(minimum))
    high = 1.15 * 10 ** math.ceil(math.log10(maximum))

    fig, axes = plt.subplots(2, 3, figsize=(12.5, 7.2), sharex=True, sharey=True)
    x_max = LPHASE * NUM_PHASES
    for (arm, title), ax in zip(COMPACT_ARMS, axes.flat):
        data = per_arm.get(arm)
        if data is None:
            ax.text(
                0.5,
                0.5,
                f"MISSING: {missing.get(arm, 'run absent')}",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=9,
                color="crimson",
            )
        else:
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
                    ax.plot(xs, yplot, color=COLORS[tenant], lw=2.2, marker="o",
                            ms=4.5, zorder=2)
                elif len(xs) == 1:
                    ax.plot(xs, yplot, color=COLORS[tenant], marker="o", ms=5,
                            ls="none", zorder=2)
            loss = None
            held = REPO / "results/dyn_tenant_kvmhost/heldout_analysis.json"
            if held.exists():
                loss = (
                    json.loads(held.read_text())
                    .get("main_figure_matrix", {})
                    .get("arms", {})
                    .get(arm, {})
                    .get("weighted_tail_loss")
                )
            if loss is not None:
                ax.text(
                    0.02,
                    0.98,
                    f"loss={loss:.2f}",
                    transform=ax.transAxes,
                    ha="left",
                    va="top",
                    fontsize=7.5,
                    color=".35",
                )
        for k in range(1, NUM_PHASES + 1):
            ax.axvline(LPHASE * k, color=".78", ls="--", lw=0.55, zorder=0)
        ax.set_title(title, fontsize=9, pad=6)
        ax.set_yscale("log")
        ax.set_xlim(0, x_max)
        ax.set_ylim(low, high)
        ax.grid(True, alpha=0.22, lw=0.5)
        ax.tick_params(labelsize=7.5)
        for k in range(1, NUM_PHASES):
            ax.axvspan(LPHASE * (k - 1), LPHASE * k, color=".94", alpha=0.35, zorder=0)

    for ax in axes[1, :]:
        ax.set_xlabel("Time from t0 (s)", fontsize=8.5)
    for ax in axes[:, 0]:
        ax.set_ylabel("Phase median clat p99 (µs)", fontsize=8.5)

    phase_labels = " → ".join(
        f"T{k}" for k in range(1, NUM_PHASES + 1)
    )
    handles = [
        Line2D([0], [0], color=COLORS[t], lw=2, marker="o", label=lab)
        for t, lab in zip(
            TENANTS,
            (
                "A 4K read QD1 (LC)",
                "B 64K read QD8",
                "C 64K read QD8",
                "D 4K write QD64",
            ),
        )
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.02),
               ncol=4, frameon=False, fontsize=8.5)
    fig.suptitle(
        "Dynamic multi-tenant reconfiguration (kvmhost, held-out s201): "
        "phase-median completion p99\n"
        f"Six phases {phase_labels} (L=90s); curves connect medians within each arm",
        fontsize=10.5,
        y=1.08,
    )
    fig.text(
        0.5,
        0.01,
        "F1 is both the tuning-best static io_ext arm and the frozen dynamic schedule "
        "(whole-run resident f1_arbfb, num_transitions=0). "
        "X1–X4 are fixed io_ext queue configs; X1 (q_none) had the lowest loss among them.",
        ha="center",
        va="bottom",
        fontsize=7.5,
        linespacing=1.4,
    )
    fig.subplots_adjust(left=0.08, right=0.98, bottom=0.12, top=0.88, hspace=0.38, wspace=0.12)
    for ext in ("png", "pdf"):
        fig.savefig(out_dir / f"dyn_tenant_compact6.{ext}", dpi=200)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--result-root", type=Path, default=RESULTS)
    ap.add_argument("--seed-off", type=int, default=SEED_OFF)
    args = ap.parse_args()
    out_dir = args.result_root / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    per_arm: dict = {}
    missing: dict[str, str] = {}
    for arm, _ in COMPACT_ARMS:
        seq = f"DYN_held_s{args.seed_off}_{arm}"
        seq_dir = args.result_root / seq
        if not seq_dir.is_dir():
            missing[arm] = "run dir absent"
            continue
        try:
            data, _aud = collect_arm(seq_dir)
            if not data["rows"]:
                missing[arm] = f"no tenant artifacts; errs={data['errors']}"
            else:
                per_arm[arm] = data
        except Exception as exc:
            missing[arm] = f"parsing failed: {exc!r}"

    if not per_arm:
        raise SystemExit("no arm data loaded")
    render(per_arm, missing, out_dir)
    print(f"rendered {out_dir}/dyn_tenant_compact6.png|pdf; missing={missing or 'none'}")


if __name__ == "__main__":
    main()
