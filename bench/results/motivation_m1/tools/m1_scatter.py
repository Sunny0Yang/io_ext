#!/usr/bin/env python3
"""m1_scatter.py — M1 pattern-space scatter (task B stage 4).

pattern_scatter.png: byte bimodality (x: >64KB byte share) x barrier rate
(y: REQ_OP_FLUSH events/s, log scale) with per-point labels, per design
docs/plans/paper_motivation_design.md §3.4 ("模式空间散点图") and prompt B.
Point classes (real / fio) are styled separately; missing data points are
honestly annotated, never dropped silently.
"""

import argparse
import csv
import math


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics-csv", default="results/motivation_m1/m1_metrics.csv")
    ap.add_argument("--out-png", default="results/motivation_m1/pattern_scatter.png")
    args = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = []
    with open(args.metrics_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    fig, ax = plt.subplots(figsize=(11, 7.5))
    styles = {
        "real": {"marker": "o", "color": "tab:red"},
        "fio": {"marker": "s", "color": "tab:blue"},
    }
    placed = []
    for row in rows:
        try:
            x = float(row["gt64k_byte_share"] or "nan")
        except ValueError:
            x = float("nan")
        try:
            y = float(row["flush_per_s_exact"] or "nan")
        except ValueError:
            y = float("nan")
        if not (isfinite_check(x) and isfinite_check(y)):
            continue
        # log-scale y: flush rate can span 0..~100/s
        yplot = math.log10(y) if y > 0 else -1.0  # floor for 0-rate points
        cls = row.get("class") or "real"
        st = styles.get(cls, styles["real"])
        ax.scatter([x * 100], [yplot], s=90, marker=st["marker"],
                   color=st["color"], edgecolors="black", zorder=3,
                   label=None)
        placed.append((x * 100, yplot, row["point_id"], row.get("window_note") or ""))

    # de-overlap labels (simple iterative nudge)
    def nudge(px, py, taken):
        for attempt in range(12):
            hit = False
            for (tx, ty) in taken:
                if abs(px - tx) < 2.2 and abs(py - ty) < 0.18:
                    hit = True
                    break
            if not hit:
                break
            py += 0.16
            px += 1.0
        taken.add((px, py))
        return px, py

    taken = set()
    for (px, py, pid, note) in placed:
        lx, ly = nudge(px, py, taken)
        ax.annotate(pid, (lx, ly), fontsize=8.5,
                    xytext=(4, 4), textcoords="offset points")

    # legend proxies
    ax.scatter([], [], marker="o", color="tab:red", edgecolors="black", label="real workload (DB/OLTP)")
    ax.scatter([], [], marker="s", color="tab:blue", edgecolors="black", label="fio pattern family")
    ax.legend(loc="upper right", fontsize=9)

    ax.set_xlabel("byte bimodality: share of bytes in >64KB requests (%)", fontsize=11)
    ax.set_ylabel("barrier rate: REQ_OP_FLUSH events/s (log10; floor -1 for 0)", fontsize=11)
    ax.set_title("M1 pattern space: byte bimodality x barrier rate (solo, none, zero io.*)\n"
                 "per docs/plans/paper_motivation_design.md §3.4 / §7; 2026-10-29 rig_m1",
                 fontsize=11)
    ax.grid(True, alpha=0.35)
    fig.tight_layout()
    fig.savefig(args.out_png, dpi=160)
    print("wrote", args.out_png, f"({len(placed)} points plotted)")
    return 0


def isfinite_check(v: float) -> bool:
    return v == v and abs(v) != float("inf")


if __name__ == "__main__":
    raise SystemExit(main())