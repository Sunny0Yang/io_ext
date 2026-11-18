#!/usr/bin/env python3
"""D2 (cg4 BW) + D3 Pareto + D4 LC burst in one flat page-width row (1×3)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ROOT / "scripts"
_D2_SCRIPTS = ROOT / "results/d2/scripts"
for p in (_SCRIPTS, _D2_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import plot_d3_kvmhost_pareto_merged as d3  # noqa: E402
import plot_d3_d4_rq13_combo as combo  # noqa: E402

STEM = "fig_d2_d3_d4_rq13_row"
OUT_DEFAULT = ROOT / "experiments/d3_kvmhost/figures"
OUT_AA = ROOT / "results/d3/figures"

# Slightly wider than text column to separate Y labels between panels.
PAGE_W_MM = 214.0
MM = 1.0 / 25.4
FIG_W_IN = PAGE_W_MM * MM
FIG_H_IN = 2.48
LEGEND_FS = 7.5
LABEL_FS = 8.5
TICK_FS = 7.8

PLOT_BOX_ASPECT = combo.PLOT_BOX_ASPECT
# Tight figure L margin — D2 carries JainW/BW labels inside column 0.
MARGIN_L_IN = 0.50
MARGIN_R_IN = 0.56
GS_LEFT = MARGIN_L_IN / FIG_W_IN
GS_RIGHT = 1.0 - MARGIN_R_IN / FIG_W_IN
GS_BOTTOM = 0.16
GS_TOP = 0.985
LEGEND_H = 0.20
GS_HSPACE = 0.03
GS_WSPACE = 0.36
GS_LEGEND_NCOL = combo.GS_LEGEND_NCOL
SAVE_DPI = combo.SAVE_DPI


def _load_d2_module():
    path = _D2_SCRIPTS / "plot_kvmhost_d2_cg_scale.py"
    spec = importlib.util.spec_from_file_location("kvmhost_d2_cg_scale", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row_rc() -> dict:
    base = dict(d3.MPL_RC)
    base.update({
        "font.size": TICK_FS,
        "axes.labelsize": LABEL_FS,
        "xtick.labelsize": TICK_FS,
        "ytick.labelsize": TICK_FS,
        "legend.fontsize": LEGEND_FS,
    })
    return base


def _draw_d2_legends(ax, h_bw, h_meta, *, font_pt: float) -> None:
    ax.set_axis_off()
    leg_kw = dict(
        fontsize=font_pt,
        frameon=False,
        columnspacing=0.4,
        handlelength=0.7,
        handletextpad=0.3,
        labelspacing=0.12,
        borderaxespad=0.0,
    )
    leg_bw = ax.legend(
        handles=h_bw, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=4, **leg_kw,
    )
    ax.add_artist(leg_bw)
    ax.legend(
        handles=h_meta, loc="upper center", bbox_to_anchor=(0.5, 0.54), ncol=3, **leg_kw,
    )


def _yaxis_right(ax, *, labelpad: float = 5) -> None:
    """Y ticks/label on right; keep left spine so the panel stays boxed."""
    ylab = ax.get_ylabel()
    for side in ("left", "bottom", "right"):
        ax.spines[side].set_visible(True)
        ax.spines[side].set_linewidth(0.55)
        ax.spines[side].set_color("0.15")
    ax.spines["top"].set_visible(False)
    ax.yaxis.set_ticks_position("right")
    ax.yaxis.set_label_position("right")
    ax.set_ylabel(ylab, labelpad=labelpad)
    kw = dict(
        left=False,
        right=True,
        labelleft=False,
        labelright=True,
        pad=combo.TICK_PAD,
        length=2.5,
    )
    ax.tick_params(axis="y", which="major", **kw)
    ax.tick_params(axis="y", which="minor", **kw)


def _align_legend_band_tops(fig: plt.Figure, ax_l2, ax_l34) -> None:
    """Legend row subplots can drift — equalize height and top edge."""
    fig.canvas.draw()
    p2, p34 = ax_l2.get_position(), ax_l34.get_position()
    y_top = max(p2.y1, p34.y1)
    h = max(p2.height, p34.height)
    y0 = y_top - h
    ax_l2.set_position([p2.x0, y0, p2.width, h])
    ax_l34.set_position([p34.x0, y0, p34.width, h])


def _align_d2_plot_to_ref(fig: plt.Figure, ax_d2, ref_ax) -> None:
    """Match D2 main axes to D3 plot band (draw D2 only after this)."""
    fig.canvas.draw()
    ref = ref_ax.get_position()
    cell = ax_d2.get_subplotspec().get_position(fig)
    ax_d2.set_position([cell.x0, ref.y0, cell.width, ref.height])


def plot_row(
    d2_rows: list[dict],
    d2_arms: list[str],
    paper,
    paper_align: dict,
    frontier,
    frontier_align: dict,
    out_dir: Path,
) -> dict:
    d2 = _load_d2_module()
    d2._apply_paper_style()
    plt.rcParams.update(_row_rc())

    fig = plt.figure(figsize=(FIG_W_IN, FIG_H_IN))
    gs = GridSpec(
        2,
        3,
        figure=fig,
        height_ratios=[LEGEND_H, 1.0],
        width_ratios=[1.05, 1.0, 1.0],
        left=GS_LEFT,
        right=GS_RIGHT,
        bottom=GS_BOTTOM,
        top=GS_TOP,
        hspace=GS_HSPACE,
        wspace=GS_WSPACE,
    )
    ax_leg_d2 = fig.add_subplot(gs[0, 0])
    ax_leg_d34 = fig.add_subplot(gs[0, 1:])
    ax_leg_d34.set_axis_off()

    ax_d2 = fig.add_subplot(gs[1, 0])
    ax_d3 = fig.add_subplot(gs[1, 1])
    ax_d4 = fig.add_subplot(gs[1, 2])

    reh_seen, fr_seen, *_ = combo.draw_d3_pareto(
        ax_d3, paper, frontier, paper_align, frontier_align,
    )
    combo.draw_d4_timeseries(ax_d4)
    for ax in (ax_d3, ax_d4):
        ax.set_box_aspect(PLOT_BOX_ASPECT)
    combo._top_align_box_aspect_axes(fig, ax_d3, ax_d4)
    ax_d4.set_ylabel(ax_d4.get_ylabel(), labelpad=5)

    _align_d2_plot_to_ref(fig, ax_d2, ax_d3)
    h_bw, h_meta = d2.draw_bw_cg_panel(
        ax_d2,
        d2_rows,
        d2_arms,
        font_pt=LEGEND_FS,
        ylabel_xc=-0.17,
        x_tick_rotation=42,
    )
    d2.sync_twin_axes_position(ax_d2)
    ax_d2.tick_params(axis="x", labelsize=TICK_FS)
    ax_d2.set_xlabel("")

    _align_legend_band_tops(fig, ax_leg_d2, ax_leg_d34)
    _draw_d2_legends(ax_leg_d2, h_bw, h_meta, font_pt=LEGEND_FS)
    for artist in ax_leg_d2.get_children():
        if isinstance(artist, matplotlib.legend.Legend):
            d2._legend_text_colors(artist)

    combo.draw_offsetbox_legend(
        ax_leg_d34,
        combo.build_legend_entries(reh_seen, fr_seen),
        ncol=GS_LEGEND_NCOL,
        loc="upper center",
        fs=LEGEND_FS,
        borderpad=0.0,
    )

    # D2 layout / savefig each run draw_event — keep D3 LC-p99 axis on the right
    def _pin_d3_y_right(_event=None) -> None:
        _yaxis_right(ax_d3, labelpad=5)

    _d3_y_cid = fig.canvas.mpl_connect("draw_event", _pin_d3_y_right)
    _pin_d3_y_right()

    gen_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    meta = {
        "figure": STEM,
        "generated_at": gen_at,
        "page_width_mm": PAGE_W_MM,
        "figure_inches": [FIG_W_IN, FIG_H_IN],
        "legend_fontsize_pt": LEGEND_FS,
        "plot_box_aspect": PLOT_BOX_ASPECT,
        "layout": "1x3",
        "panels": ["d2_cg4_bw", "d3_pareto", "d4_lc_burst"],
        "d3_yaxis": "right",
        "d2_arms": d2_arms,
        "d3": {"paper_align": paper_align, "frontier_align": frontier_align},
        "gridspec": {
            "left": GS_LEFT,
            "right": GS_RIGHT,
            "bottom": GS_BOTTOM,
            "top": GS_TOP,
            "wspace": GS_WSPACE,
            "legend_row_height_ratio": LEGEND_H,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    OUT_AA.mkdir(parents=True, exist_ok=True)
    try:
        for ext in ("png", "pdf"):
            fig.savefig(
                out_dir / f"{STEM}.{ext}",
                dpi=SAVE_DPI if ext == "png" else None,
                bbox_inches=None,
                pad_inches=0,
            )
    finally:
        fig.canvas.mpl_disconnect(_d3_y_cid)
    plt.close(fig)

    def _same(a: Path, b: Path) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return a.resolve() == b.resolve()

    for ext in ("png", "pdf"):
        dst = OUT_AA / f"{STEM}.{ext}"
        if not _same(out_dir / f"{STEM}.{ext}", dst):
            shutil.copy2(out_dir / f"{STEM}.{ext}", dst)
    meta_path = out_dir / f"{STEM}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    aa_meta = OUT_AA / f"{STEM}.meta.json"
    if not _same(meta_path, aa_meta):
        shutil.copy2(meta_path, aa_meta)
    return meta


def _load_d2_rows(d2_mod) -> list[dict]:
    path = d2_mod.DATA / "kvmhost_d2_by_cg.csv"
    if path.is_file():
        return d2_mod.read_rows_csv(path)
    _, rows = d2_mod.build_matrix()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()
    if not combo.D4_DATA.is_dir():
        raise SystemExit(f"missing D4 data {combo.D4_DATA}")
    if not combo.D4_RERUN.is_dir():
        raise SystemExit(f"missing D4 rerun {combo.D4_RERUN}")

    d2_mod = _load_d2_module()
    d2_rows = _load_d2_rows(d2_mod)
    d2_arms = d2_mod.curated_arms(d2_rows)

    anchor = json.loads(d3.ANCHOR.read_text())
    raw_p, _, px, py, _ = d3.paper_raw(d3.load_csv(d3.PAPER_CSV))
    raw_f, _, fx, fy = d3.frontier_raw(d3.load_csv(d3.LEDGER), anchor)
    paper_align = {"x_none": px, "y_none": py}
    frontier_align = {"x_none": fx, "y_none": fy}
    paper = d3.align_points(raw_p, x_none=px, y_none=py)
    frontier = d3.align_points(raw_f, x_none=fx, y_none=fy)

    meta = plot_row(
        d2_rows, d2_arms, paper, paper_align, frontier, frontier_align, args.out_dir,
    )
    print(f"Wrote {args.out_dir}/{STEM}.png ({meta['generated_at']})")
    print(f"Canvas: {meta['page_width_mm']} mm × {meta['figure_inches'][1]:.2f} in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())