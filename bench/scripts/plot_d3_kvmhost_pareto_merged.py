#!/usr/bin/env python3
"""Merged D3 Pareto: align none anchors, preserve within-batch shape.

Step 1 — per batch, same as standalone figures:
  x = BE / B_none × 100
  y = LC_p99 / solo

Step 2 — per batch, multiply only (relative positions fixed):
  x' = x × (100 / x_none)   [usually x_none ≈ 100, ≈ no-op]
  y' = y / y_none           where y_none = LC_p99_none / solo

Both batches' none land on (100, 1). Raw solo (LC/solo=1) → y' = 1/y_none per batch;
red hline(s) at those y' (not at none).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, LogLocator

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = ROOT / "results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"
LEDGER = ROOT / "results/d3_kvmhost/analysis/d3_kvmhost_all_runs_ledger.csv"
PAPER_CSV = ROOT / "results/isol_bench/paper/analysis/d3_lc_pareto.csv"

# Quarter-page width (~7″ line); square plot; legend band below axes.
PAGE_TEXT_WIDTH_IN = 7.0
FIG_W_IN = PAGE_TEXT_WIDTH_IN / 4.0
_MARGIN_L_IN = 0.34
_MARGIN_R_IN = 0.09
_MARGIN_T_IN = 0.06
_LEGEND_BAND_IN = 0.20
_XLABEL_BAND_IN = 0.14
_BOTTOM_EXTRA_IN = _LEGEND_BAND_IN + _XLABEL_BAND_IN
PLOT_SIDE_IN = FIG_W_IN - _MARGIN_L_IN - _MARGIN_R_IN
FIG_H_IN = _BOTTOM_EXTRA_IN + PLOT_SIDE_IN + _MARGIN_T_IN
_AX_ADJUST = dict(
    left=_MARGIN_L_IN / FIG_W_IN,
    right=1.0 - _MARGIN_R_IN / FIG_W_IN,
    bottom=_BOTTOM_EXTRA_IN / FIG_H_IN,
    top=1.0 - _MARGIN_T_IN / FIG_H_IN,
)
MPL_RC = {
    "font.size": 9,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 7,
}
MARKER_S = 22
LEGEND_MARKER_PT = 5.5
LEGEND_NCOL = 5
LEGEND_FIG_Y = (_LEGEND_BAND_IN * 0.42) / FIG_H_IN
SAVE_DPI = 400


def _log_tick_plain(value: float, _pos: float) -> str:
    if value <= 0:
        return ""
    ticks = (0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100)
    for t in ticks:
        if abs(value - t) / t < 0.06:
            if t >= 1 and abs(t - round(t)) < 0.02:
                return f"{int(t)}"
            return f"{t:g}"
    return ""
PAPER_SOLO_MS = 0.506
LC_SAT_MS = 5_000.0
Y_FLOOR = 0.02
SEQ_RE = re.compile(r"^D3LC_rread_4k_(?P<arm>.+)_p\d+$")

FRONTIER_NONE_KEEP = {
    ("discovery2", "D3PV_none_run2"),
    ("discovery2", "D3PV_none_run3"),
}

REH_COLORS = {
    "none": "#575757",
    "bfq": "#2f6b8a",
    "mq_deadline": "#6a8f3a",
    "io_cost": "#7b68a6",
    "io_latency": "#4a9e9e",
    "io_max": "#8c6d4a",
}

FR_COLORS = {
    "none": "#575757",
    "BFQ": "#2f6b8a",
    "iox": "#c44e34",
    "arbonly": "#e07b39",
}

LEGEND_REH = {
    "io_latency": "io-latency",
    "io_cost": "io-cost",
    "bfq": "bfq",
    "mq_deadline": "mq-deadline",
    "io_max": "io-max",
}


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def truthy(s: str) -> bool:
    return str(s).strip().lower() in ("true", "1", "yes")


def paper_outlier(arm: str, x: float, y: float, lc_ms: float) -> bool:
    if arm == "none":
        return False
    if lc_ms >= LC_SAT_MS:
        return True
    if y > 50 or y < 0.08:
        return True
    if x < 12 and y > 8:
        return True
    return False


def frontier_series(row: dict) -> str:
    fam = row.get("pareto_family") or row.get("arm") or ""
    if fam == "none" or row.get("phase") == "calib_none_contention":
        return "none"
    if fam == "BFQ" or row.get("arm") == "bfqf":
        return "BFQ"
    if row.get("arm") == "arbonly" or fam == "arbonly":
        return "arbonly"
    return "iox"


def keep_frontier_row(row: dict) -> bool:
    if frontier_series(row) != "none":
        return True
    return (row["batch"], row["run_id"]) in FRONTIER_NONE_KEEP


def align_points(
    raw: list[tuple[str, float, float]],
    *,
    x_none: float,
    y_none: float,
) -> list[tuple[str, float, float]]:
    if x_none <= 0 or y_none <= 0:
        raise ValueError("bad none anchor")
    sx = 100.0 / x_none
    sy = 1.0 / y_none
    return [(arm, x * sx, y * sy) for arm, x, y in raw]


def paper_raw(rows: list[dict]) -> tuple[list[tuple[str, float, float]], float, float, float, int]:
    none_rows = [r for r in rows if SEQ_RE.match(r["sequence_id"]) and
                 SEQ_RE.match(r["sequence_id"]).group("arm") == "none"]
    if len(none_rows) != 1:
        raise RuntimeError(f"expected 1 paper none row, got {len(none_rows)}")
    nr = none_rows[0]
    b_none = float(nr["agg_bw_mbps"])
    lc_none_ms = float(nr["lc_p99_us"]) / 1000.0
    x_none = float(nr["agg_bw_mbps"]) / b_none * 100.0
    y_none = lc_none_ms / PAPER_SOLO_MS
    raw: list[tuple[str, float, float]] = []
    n_skip = 0
    for r in rows:
        m = SEQ_RE.match(r["sequence_id"])
        if not m:
            continue
        arm = m.group("arm")
        lc_ms = float(r["lc_p99_us"]) / 1000.0
        be = float(r["agg_bw_mbps"])
        if lc_ms <= 0 or be <= 0:
            n_skip += 1
            continue
        x = be / b_none * 100.0
        y = lc_ms / PAPER_SOLO_MS
        if paper_outlier(arm, x, y, lc_ms):
            n_skip += 1
            continue
        raw.append((arm, x, y))
    return raw, b_none, x_none, y_none, n_skip


def frontier_raw(
    rows: list[dict],
    anchor: dict,
) -> tuple[list[tuple[str, float, float]], float, float, float]:
    solo = float(anchor["solo_p99_ms_median"])
    b_none = float(anchor["b_none_mb_s"])
    raw: list[tuple[str, float, float]] = []
    none_xy: list[tuple[float, float]] = []
    for r in rows:
        if truthy(r.get("is_anomaly", "")):
            continue
        if r.get("phase") == "calib_lc_solo":
            continue
        if not keep_frontier_row(r):
            continue
        lc_ms = float(r["lc_p99_ms"])
        be = float(r["be_agg_mb_s"])
        x = be / b_none * 100.0
        y = lc_ms / solo
        if x <= 0 or y <= 0:
            continue
        raw.append((frontier_series(r), x, y))
        if frontier_series(r) == "none":
            none_xy.append((x, y))
    if not none_xy:
        raise RuntimeError("no 2026 none points for alignment anchor")
    x_none = statistics.median([t[0] for t in none_xy])
    y_none = statistics.median([t[1] for t in none_xy])
    return raw, b_none, x_none, y_none


def plot_merged(
    paper: list[tuple[str, float, float]],
    paper_align: dict,
    frontier: list[tuple[str, float, float]],
    frontier_align: dict,
    paper_skipped: int,
    out_dir: Path,
    *,
    stem: str = "fig_d3_kvmhost_pareto_merged",
) -> None:
    plt.rcParams.update(MPL_RC)
    fig, ax = plt.subplots(figsize=(FIG_W_IN, FIG_H_IN))

    reh_seen: set[str] = set()
    for arm, x, y in paper:
        reh_seen.add(arm)
        ax.scatter(
            x, y, s=MARKER_S, c=REH_COLORS.get(arm, "#888888"),
            marker="o", edgecolors="0.15", linewidths=0.65, alpha=0.9, zorder=2,
        )

    fr_seen: set[str] = set()
    for arm, x, y in frontier:
        fr_seen.add(arm)
        ax.scatter(
            x, y, s=MARKER_S, c=FR_COLORS.get(arm, "#333333"),
            marker="o", edgecolors="0.15", linewidths=0.65, alpha=0.92, zorder=3,
        )

    y_solo_paper = 1.0 / paper_align["y_none"]
    y_solo_2026 = 1.0 / frontier_align["y_none"]
    ax.axhline(y_solo_paper, color="#c62828", ls="--", lw=1.0, alpha=0.85, zorder=1)
    ax.axhline(y_solo_2026, color="#c62828", ls="-", lw=1.2, zorder=1)

    all_x = [p[1] for p in paper + frontier]
    all_y = [p[2] for p in paper + frontier]
    y_lo = max(Y_FLOOR, min(all_y) * 0.85)
    y_hi = max(all_y) * 1.15
    x_lo = max(0, min(all_x) - 4)
    x_hi = min(108, max(all_x) + 3)

    ax.set_yscale("log")
    ax.autoscale(False)
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("BE retention (%)")
    ax.set_ylabel("LC p99 / solo")
    ax.yaxis.set_major_locator(LogLocator(base=10))
    ax.yaxis.set_major_formatter(FuncFormatter(_log_tick_plain))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda *_: ""))
    ax.grid(True, which="major", alpha=0.22, lw=0.55)
    ax.tick_params(pad=2.0, length=3)

    handles: list[Line2D] = []
    for arm in ("io_latency", "io_cost", "bfq", "mq_deadline", "io_max"):
        if arm not in reh_seen:
            continue
        handles.append(
            Line2D(
                [0], [0], marker="o", color="w",
                markerfacecolor=REH_COLORS[arm], markeredgecolor="0.2",
                markersize=LEGEND_MARKER_PT, linewidth=0, label=LEGEND_REH.get(arm, arm),
            )
        )
    for arm, label in (
        ("iox", "io_ext qrl"),
        ("arbonly", "arbonly"),
        ("BFQ", "BFQ"),
    ):
        if arm not in fr_seen:
            continue
        handles.append(
            Line2D(
                [0], [0], marker="o", color="w",
                markerfacecolor=FR_COLORS.get(arm, "#575757"),
                markeredgecolor="0.2", markersize=LEGEND_MARKER_PT, linewidth=0, label=label,
            )
        )
    if "none" in reh_seen or "none" in fr_seen:
        handles.append(
            Line2D(
                [0], [0], marker="o", color="w",
                markerfacecolor="#575757", markeredgecolor="0.2",
                markersize=LEGEND_MARKER_PT, linewidth=0, label="none",
            )
        )
    handles.append(
        Line2D([0], [0], color="#c62828", ls="-", lw=1.5, label="solo 1×")
    )

    fig.subplots_adjust(**_AX_ADJUST)
    ax.set_box_aspect(1)

    fig.legend(
        handles=handles,
        loc="center",
        bbox_to_anchor=(0.5, LEGEND_FIG_Y),
        ncol=LEGEND_NCOL,
        frameon=False,
        columnspacing=0.5,
        handletextpad=0.2,
        labelspacing=0.15,
        handlelength=0.75,
        borderaxespad=0,
    )

    meta = {
        "method": "per-batch multiply; shape preserved",
        "paper": paper_align,
        "frontier_2026": frontier_align,
        "n_paper": len(paper),
        "n_2026": len(frontier),
        "ylim": [y_lo, y_hi],
        "figure_inches": [FIG_W_IN, FIG_H_IN],
        "plot_side_inches": PLOT_SIDE_IN,
        "page_quarter_width_in": FIG_W_IN,
        "mpl_rc": MPL_RC,
        "axes_adjust": _AX_ADJUST,
        "solo_hline_y_prime": {
            "paper_dashed": y_solo_paper,
            "2026_solid": y_solo_2026,
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(
            out_dir / f"{stem}.{ext}",
            dpi=SAVE_DPI if ext == "png" else None,
            bbox_inches=None,
            pad_inches=0,
        )
    (out_dir / f"{stem}.meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    plt.close(fig)
    print(f"Wrote {out_dir}/{stem}.png (paper {len(paper)} + 2026 {len(frontier)})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    ap.add_argument("--paper-csv", type=Path, default=PAPER_CSV)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "experiments/d3_kvmhost/figures")
    args = ap.parse_args()
    if not args.ledger.is_file():
        raise SystemExit("run summarize_d3_kvmhost_ledger.py first")
    if not args.paper_csv.is_file():
        raise SystemExit(f"missing {args.paper_csv}")
    anchor = json.loads(ANCHOR.read_text())
    raw_p, _, px, py, n_skip = paper_raw(load_csv(args.paper_csv))
    raw_f, _, fx, fy = frontier_raw(load_csv(args.ledger), anchor)
    paper_align = {"x_none": px, "y_none": py, "sx": 100.0 / px, "sy": 1.0 / py}
    frontier_align = {"x_none": fx, "y_none": fy, "sx": 100.0 / fx, "sy": 1.0 / fy}
    paper = align_points(raw_p, x_none=px, y_none=py)
    frontier = align_points(raw_f, x_none=fx, y_none=fy)
    plot_merged(
        paper, paper_align, frontier, frontier_align, n_skip, args.out_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())