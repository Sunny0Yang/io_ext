#!/usr/bin/env python3
"""D3 merged Pareto + paper sdb D4 LC burst; per-panel legend band + square plots."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnchoredOffsetbox, DrawingArea, HPacker, TextArea, VPacker
from matplotlib.patches import Circle
from matplotlib.ticker import FuncFormatter, LogLocator, NullLocator

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import plot_d3_kvmhost_pareto_merged as d3  # noqa: E402

D4_DATA = ROOT / "results/isol_bench/paper/paper_sdb_d4_lc"
D4_RERUN = ROOT / "results/isol_bench/paper/d4_kvmhost_rerun_kvmhost_d4_rerun_20261116"
D4_RERUN_ARMS = frozenset({"io_latency", "io_cost"})
D4_RERUN_REPS = 3
OUT_DEFAULT = ROOT / "experiments/d3_kvmhost/figures"
OUT_AA = ROOT / "results/d3/figures"
STEM = "fig_d3_d4_rq13_combo"
# Visible dash on plot + legend (non-iox); iox_q_* use "-".
D4_DASH = (0, (5, 2.5))

# Layout: legend row + two panels (width > height in data area).
FIG_W_IN = 5.35
FIG_H_IN = 2.65
PLOT_BOX_ASPECT = 0.70  # axes height/width; <1 ⇒ flat/wide plot area
# Inch-anchored side margins so D3 left / D4 right labels stay inside fixed canvas width.
MARGIN_L_IN = 0.46
MARGIN_R_IN = 0.52
GS_LEFT = MARGIN_L_IN / FIG_W_IN
GS_RIGHT = 1.0 - MARGIN_R_IN / FIG_W_IN
GS_BOTTOM, GS_TOP = 0.12, 0.99
GS_WSPACE = 0.22
GS_LEGEND_DUAL = 0.060
GS_LEGEND_SIDE = 0.054
GS_LEGEND = GS_LEGEND_DUAL + GS_LEGEND_SIDE
GS_LEGEND_NCOL = 5
GS_HSPACE = 0.02
TICK_PAD = 1.0
D4_YLABEL_PAD = 2.5

SAVE_DPI = 400
CROP_PAD_PT = 1.5
_CROP_SCRIPT = ROOT / "scripts/tmp_crop_fig_pdf.py"
BURST = "#b8860b"
SOLO_COLOR = "#c62828"
IO_EXT_COLOR = d3.FR_COLORS["iox"]

# D4 arms → D3 palette (lines); labels match D3 legend where shared.
# D4 curves: non-iox schedulers dashed; iox_q_* solid (legend reads linestyle from here).
D4_ARMS: list[tuple[str, str, str, str | tuple, float]] = [
    ("none", "none", d3.REH_COLORS["none"], D4_DASH, 0.9),
    ("mq_deadline", d3.LEGEND_REH["mq_deadline"], d3.REH_COLORS["mq_deadline"], D4_DASH, 0.9),
    ("kyber", "kyber", d3.REH_COLORS["io_latency"], D4_DASH, 0.9),
    ("bfq", d3.LEGEND_REH["bfq"], d3.REH_COLORS["bfq"], D4_DASH, 0.9),
    ("io_max", d3.LEGEND_REH["io_max"], d3.REH_COLORS["io_max"], D4_DASH, 0.9),
    ("io_latency", d3.LEGEND_REH["io_latency"], d3.REH_COLORS["io_latency"], D4_DASH, 0.9),
    ("io_cost", d3.LEGEND_REH["io_cost"], d3.REH_COLORS["io_cost"], D4_DASH, 0.9),
    ("iox_q_wedf", "iox q_wedf", IO_EXT_COLOR, "-", 1.05),
]


def _d4_is_solid(ls: str | tuple) -> bool:
    return ls == "-"


def _load_d4_us(key: str) -> tuple[np.ndarray, np.ndarray]:
    path = D4_DATA / f"{key}_clat.1.log"
    a = np.loadtxt(path, delimiter=",", usecols=(0, 1))
    return a[:, 0] / 1000.0, a[:, 1] / 1000.0


def _solo_p99_us() -> float:
    j = json.loads((D4_DATA / "solo_lc_none_fio.json").read_text())
    return j["jobs"][0]["read"]["clat_ns"]["percentile"]["99.000000"] / 1000.0


def _win_p99(t: np.ndarray, v: np.ndarray, win: float = 0.25, min_n: int = 4):
    edges = np.arange(0.0, t.max() + win, win)
    mids, vals = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (t >= a) & (t < b)
        if sel.sum() < min_n:
            continue
        mids.append(0.5 * (a + b))
        vals.append(np.percentile(v[sel], 99))
    return np.asarray(mids), np.asarray(vals)


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


def _d4_rerun_median_p99(arm: str) -> tuple[np.ndarray, np.ndarray]:
    by_m: dict[float, list[float]] = defaultdict(list)
    for rep in range(1, D4_RERUN_REPS + 1):
        tar = D4_RERUN / f"paper_sdb_D4LC_{arm}_r{rep}.tar.gz"
        if not tar.is_file():
            continue
        t, v = _clat_from_tar(tar)
        m, y = _win_p99(t, v)
        for mi, yi in zip(m, y):
            by_m[float(mi)].append(float(yi))
    if not by_m:
        raise FileNotFoundError(f"missing rerun tars for D4LC_{arm} under {D4_RERUN}")
    xs = sorted(by_m)
    med = [float(np.median(by_m[x])) for x in xs]
    return np.asarray(xs), np.asarray(med)


def _d4_timeseries_xy(key: str) -> tuple[np.ndarray, np.ndarray]:
    if key in D4_RERUN_ARMS:
        return _d4_rerun_median_p99(key)
    t, v = _load_d4_us(key)
    return _win_p99(t, v)


def _d3_frontier_color(arm: str) -> str:
    if arm in ("iox", "arbonly"):
        return IO_EXT_COLOR
    return d3.FR_COLORS.get(arm, "#333333")


def _d4_y_formatter(v: float, _p: float) -> str:
    """Compact right-axis ticks (avoid wide numeric labels past canvas edge)."""
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def draw_d3_pareto(
    ax,
    paper: list[tuple[str, float, float]],
    frontier: list[tuple[str, float, float]],
    paper_align: dict,
    frontier_align: dict,
) -> tuple[set[str], set[str], float, float, float, float]:
    reh_seen: set[str] = set()
    for arm, x, y in paper:
        reh_seen.add(arm)
        ax.scatter(
            x,
            y,
            s=d3.MARKER_S,
            c=d3.REH_COLORS.get(arm, "#888888"),
            marker="o",
            edgecolors="0.15",
            linewidths=0.65,
            alpha=0.9,
            zorder=2,
        )
    fr_seen: set[str] = set()
    for arm, x, y in frontier:
        fr_seen.add(arm)
        ax.scatter(
            x,
            y,
            s=d3.MARKER_S,
            c=_d3_frontier_color(arm),
            marker="o",
            edgecolors="0.15",
            linewidths=0.65,
            alpha=0.92,
            zorder=3,
        )
    y_solo_paper = 1.0 / paper_align["y_none"]
    y_solo_2026 = 1.0 / frontier_align["y_none"]
    ax.axhline(y_solo_paper, color=SOLO_COLOR, ls=D4_DASH, lw=1.0, alpha=0.85, zorder=1)
    ax.axhline(y_solo_2026, color=SOLO_COLOR, ls="-", lw=1.2, zorder=1)

    all_x = [p[1] for p in paper + frontier]
    all_y = [p[2] for p in paper + frontier]
    y_lo = max(d3.Y_FLOOR, min(all_y) * 0.85)
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
    ax.yaxis.set_major_formatter(FuncFormatter(d3._log_tick_plain))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda *_: ""))
    ax.grid(True, which="major", alpha=0.22, lw=0.55)
    ax.tick_params(pad=TICK_PAD, length=2.5)
    return reh_seen, fr_seen, x_lo, x_hi, y_lo, y_hi


def draw_d4_timeseries(ax) -> None:
    ax.axvspan(-1.3, 0.0, color="0.93", zorder=0)
    ax.axvline(0.0, color=BURST, lw=1.0, zorder=2)
    ref = _solo_p99_us()
    ax.plot([-1.3, 10.0], [ref, ref], color=SOLO_COLOR, lw=1.0, ls=D4_DASH, alpha=0.85, zorder=2)
    for key, _label, color, ls, lw in D4_ARMS:
        m, y = _d4_timeseries_xy(key)
        ax.plot(m, y, color=color, ls=ls, lw=lw, zorder=3)
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


def _d4_linestyle(key: str) -> str:
    for k, _, _, ls, _ in D4_ARMS:
        if k == key:
            return ls
    return "-"


# (kind, color, linestyle, name, line width) — kind in dual | circle | line
LegEntry = tuple[str, str, str, str, float]


def build_legend_entries(_reh_seen: set[str], fr_seen: set[str]) -> list[LegEntry]:
    """Two shared legend rows: ○/— glyph + name (D3 point / D4 trace)."""
    out: list[LegEntry] = [("line", SOLO_COLOR, D4_DASH, "solo 1×", 1.1)]
    for name, color, d4_key in (
        ("none", d3.REH_COLORS["none"], "none"),
        ("mq-deadline", d3.REH_COLORS["mq_deadline"], "mq_deadline"),
        ("bfq", d3.REH_COLORS["bfq"], "bfq"),
        ("io-max", d3.REH_COLORS["io_max"], "io_max"),
    ):
        ls = _d4_linestyle(d4_key)
        out.append(("dual", color, ls, name, 1.05 if _d4_is_solid(ls) else 0.95))
    for arm in ("io_latency", "io_cost"):
        ls = _d4_linestyle(arm)
        out.append(("dual", d3.REH_COLORS[arm], ls, d3.LEGEND_REH[arm], 0.95))
    ls_k = _d4_linestyle("kyber")
    out.append(("line", d3.REH_COLORS["io_latency"], ls_k, "kyber", 0.9))
    # D4 plots two iox traces; one io_ext dual entry (○ / solid) matches D3 frontier.
    if fr_seen & {"iox", "arbonly"} or any(k.startswith("iox_q_") for k, *_ in D4_ARMS):
        out.append(("dual", IO_EXT_COLOR, "-", "io_ext", 1.05))
    return out


def _legend_dot(color: str) -> DrawingArea:
    da = DrawingArea(8, 8, 0, 0)
    da.add_artist(Circle((4, 4), 3.2, facecolor=color, edgecolor="0.2", linewidth=0.4))
    return da


def _legend_stroke(color: str, ls: str | tuple, lw: float) -> DrawingArea:
    da = DrawingArea(16, 8, 0, 0)
    da.add_artist(
        Line2D([0, 16], [4, 4], color=color, linestyle=ls, lw=lw, solid_capstyle="butt")
    )
    return da


def _legend_item(
    kind: str, color: str, ls: str | tuple, name: str, lw: float, fs: float
) -> HPacker:
    slash = TextArea(" / ", textprops={"size": fs})
    if kind == "dual":
        glyph = HPacker(
            children=[_legend_dot(color), slash, _legend_stroke(color, ls, lw)],
            align="center",
            pad=0,
            sep=0,
        )
    elif kind == "circle":
        glyph = _legend_dot(color)
    else:
        glyph = _legend_stroke(color, ls, lw)
    return HPacker(
        children=[glyph, TextArea(name, textprops={"size": fs})],
        align="center",
        pad=0,
        sep=2,
    )


def _legend_row(entries: list[LegEntry], fs: float, sep: float = 7.0) -> HPacker:
    items = [
        _legend_item(kind, color, ls, name, lw, fs)
        for kind, color, ls, name, lw in entries
    ]
    return HPacker(children=items, align="center", pad=0, sep=sep)


def draw_offsetbox_legend(
    ax_leg,
    entries: list[LegEntry],
    *,
    ncol: int | None = None,
    loc: str = "lower center",
    fs: float | None = None,
    borderpad: float = 0.0,
) -> None:
    if not entries:
        return
    fs = fs if fs is not None else d3.MPL_RC["legend.fontsize"]
    if ncol is None or len(entries) <= ncol:
        box: HPacker | VPacker = _legend_row(entries, fs)
    else:
        rows = [
            _legend_row(entries[i : i + ncol], fs)
            for i in range(0, len(entries), ncol)
        ]
        box = VPacker(children=rows, align="center", pad=0, sep=2)
    ax_leg.add_artist(
        AnchoredOffsetbox(loc=loc, child=box, frameon=False, borderpad=borderpad, pad=0.0)
    )


def _top_align_box_aspect_axes(fig: plt.Figure, *axes: plt.Axes) -> None:
    """set_box_aspect() vertically centers axes; top-align to the GridSpec cell."""
    fig.canvas.draw()
    for ax in axes:
        pos = ax.get_position()
        cell = ax.get_subplotspec().get_position(fig)
        ax.set_position([pos.x0, cell.y1 - pos.height, pos.width, pos.height])


def plot_combo(
    paper: list[tuple[str, float, float]],
    paper_align: dict,
    frontier: list[tuple[str, float, float]],
    frontier_align: dict,
    out_dir: Path,
) -> None:
    from matplotlib.gridspec import GridSpec

    plt.rcParams.update(d3.MPL_RC)
    fig = plt.figure(figsize=(FIG_W_IN, FIG_H_IN))
    gs = GridSpec(
        2,
        1,
        figure=fig,
        height_ratios=[GS_LEGEND, 1.0],
        left=GS_LEFT,
        right=GS_RIGHT,
        bottom=GS_BOTTOM,
        top=GS_TOP,
        hspace=GS_HSPACE,
    )
    gs_plot = gs[1].subgridspec(1, 2, wspace=GS_WSPACE)
    ax_leg = fig.add_subplot(gs[0, 0])
    ax_leg.set_axis_off()
    ax3 = fig.add_subplot(gs_plot[0, 0])
    ax4 = fig.add_subplot(gs_plot[0, 1])

    reh_seen, fr_seen, *_ = draw_d3_pareto(ax3, paper, frontier, paper_align, frontier_align)
    draw_d4_timeseries(ax4)
    for ax in (ax3, ax4):
        ax.set_box_aspect(PLOT_BOX_ASPECT)
    _top_align_box_aspect_axes(fig, ax3, ax4)

    draw_offsetbox_legend(
        ax_leg,
        build_legend_entries(reh_seen, fr_seen),
        ncol=GS_LEGEND_NCOL,
        loc="lower center",
    )

    gen_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    meta = {
        "figure": STEM,
        "generated_at": gen_at,
        "d4_linestyles": {"non_iox": "D4_DASH", "iox": "solid"},
        "figure_inches": [FIG_W_IN, FIG_H_IN],
        "plot_box_aspect": PLOT_BOX_ASPECT,
        "gridspec": {
            "left": GS_LEFT,
            "right": GS_RIGHT,
            "bottom": GS_BOTTOM,
            "top": GS_TOP,
            "margin_l_in": MARGIN_L_IN,
            "margin_r_in": MARGIN_R_IN,
            "wspace": GS_WSPACE,
            "legend_row_height": GS_LEGEND,
            "legend_ncol": GS_LEGEND_NCOL,
            "hspace": GS_HSPACE,
            "plot_alignment": "top of plot row (after set_box_aspect)",
            "legend_anchor": "lower center",
            "legend_layout": "shared 2-row ○/line glyph legend spanning both panels",
        },
        "d3": {"paper_align": paper_align, "frontier_align": frontier_align},
        "d4_data": str(D4_DATA),
        "d4_rerun": {
            "dir": str(D4_RERUN),
            "arms": sorted(D4_RERUN_ARMS),
            "reps": D4_RERUN_REPS,
            "aggregate": "median_per_250ms_window",
        },
        "d4_arms_plotted": [k for k, *_ in D4_ARMS],
        "d4_yaxis": "right",
        "pdf_cropped": None,
    }
    primary = out_dir
    primary.mkdir(parents=True, exist_ok=True)
    OUT_AA.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(
            primary / f"{STEM}.{ext}",
            dpi=SAVE_DPI if ext == "png" else None,
            bbox_inches=None,
            pad_inches=0,
        )
    pdf_path = primary / f"{STEM}.pdf"

    def _same_output(a: Path, b: Path) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return a.resolve() == b.resolve()

    mirrored = False
    for ext in ("png", "pdf"):
        dst = OUT_AA / f"{STEM}.{ext}"
        if not _same_output(primary / f"{STEM}.{ext}", dst):
            shutil.copy2(primary / f"{STEM}.{ext}", dst)
            mirrored = True
    plt.close(fig)
    return meta, pdf_path, gen_at, mirrored


def _crop_pdf_inplace(pdf: Path, *, pad_pt: float = CROP_PAD_PT) -> None:
    if not _CROP_SCRIPT.is_file():
        raise SystemExit(f"missing crop helper {_CROP_SCRIPT}")
    proc = subprocess.run(
        [
            sys.executable,
            str(_CROP_SCRIPT),
            "--in-place",
            f"--pad-pt={pad_pt}",
            str(pdf),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--no-crop-pdf", action="store_true", help="skip tmp_crop_fig_pdf --in-place")
    ap.add_argument("--crop-pad-pt", type=float, default=CROP_PAD_PT)
    args = ap.parse_args()
    if not D4_DATA.is_dir():
        raise SystemExit(f"missing D4 data {D4_DATA}")
    if not D4_RERUN.is_dir():
        raise SystemExit(f"missing D4 rerun {D4_RERUN}")
    anchor = json.loads(d3.ANCHOR.read_text())
    raw_p, _, px, py, _ = d3.paper_raw(d3.load_csv(d3.PAPER_CSV))
    raw_f, _, fx, fy = d3.frontier_raw(d3.load_csv(d3.LEDGER), anchor)
    paper_align = {"x_none": px, "y_none": py}
    frontier_align = {"x_none": fx, "y_none": fy}
    paper = d3.align_points(raw_p, x_none=px, y_none=py)
    frontier = d3.align_points(raw_f, x_none=fx, y_none=fy)
    meta, pdf_path, gen_at, mirrored = plot_combo(
        paper, paper_align, frontier, frontier_align, args.out_dir,
    )
    if not args.no_crop_pdf:
        _crop_pdf_inplace(pdf_path, pad_pt=args.crop_pad_pt)
        meta["pdf_cropped"] = {"pad_pt": args.crop_pad_pt, "tool": str(_CROP_SCRIPT.name)}
    meta_path = args.out_dir / f"{STEM}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    aa_meta = OUT_AA / f"{STEM}.meta.json"
    try:
        if not os.path.samefile(meta_path, aa_meta):
            shutil.copy2(meta_path, aa_meta)
    except OSError:
        if meta_path.resolve() != aa_meta.resolve():
            shutil.copy2(meta_path, aa_meta)
    canonical = pdf_path.resolve()
    print(f"Wrote {args.out_dir}/{STEM}.png ({gen_at})")
    print(f"Canonical PDF (symlink target): {canonical}")
    if mirrored:
        print(f"Mirrored assets under {OUT_AA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())