#!/usr/bin/env python3
"""Render D1 paper figures (cost e6 + scalability) from curated v3 CSV tables.

Curated entry: `results/d1/` — reads `data/*.csv`, writes `figures/`.
Upstream twin: `experiments/dyn_tenant_bm/scripts/render_d1_paper_redesign_bm.py`.
"""

import argparse
import csv
import hashlib
import json
import shutil
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.text import Text
import numpy as np

AA_ROOT = Path(__file__).resolve().parents[1]
SOURCE = AA_ROOT / "data"
OUTPUT = AA_ROOT / "figures"
MM = 1 / 25.4
# Paper strip grid: 2×2, square cells aligned with reproduction 2×4 strip row.
PANEL_CELL_IN = 2.05
STRIP_NCOL = 2
STRIP_NROW = 2
STRIP_FIG_W_SCALE = 1.12
STRIP_FIG_H_SCALE = 1.30
STRIP_LEGEND_Y = 0.972
STRIP_TOP = 0.835
STRIP_BOTTOM = 0.14
STRIP_LEFT = 0.22
STRIP_RIGHT = 0.96
# Typography — match reproduction strip (plot_eight_arm_reproduction_strip.py).
FS_TITLE = 12.0
FS_LABEL = 10.5
FS_TICK = 9.5
FS_LEGEND = 10.0
STRIP_FS_LEGEND = 11.0
STRIP_PANEL_TITLES = (
    "(a) CPU cost",
    "(b) Path latency",
    "(c) Active scaling",
    "(d) Idle scaling",
)
STRIP_LEGEND_NCOL = 4
STRIP_TITLE_FP = FontProperties(family="DejaVu Sans", size=FS_TITLE, weight="bold")
BLUE = "#2879A5"
GRAY = "#555555"
LIGHT = "#DCE6EB"
ORANGE = "#C77735"
COLORS = (GRAY, BLUE, ORANGE, "#5A9070")
ARM_ORDER = ("native", "bare", "q_noop", "q_fifo", "qfifo_arb")
ARM_LABELS = ("none", "Bare", "No-op", "FIFO", "FIFO+arb")

_BASE_RC = {
    "font.family": "DejaVu Sans",
    "font.size": 7,
    "axes.labelsize": 7.5,
    "axes.titlesize": 7.5,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "legend.fontsize": 7,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.linewidth": 0.6,
    "axes.labelpad": 2.5,
    "xtick.major.size": 2.5,
    "ytick.major.size": 2.5,
    "xtick.major.pad": 2,
    "ytick.major.pad": 2,
    "lines.linewidth": 1.1,
    "lines.markersize": 3.5,
    "pdf.fonttype": 42,
    "savefig.bbox": None,
}
STRIP_RC = {
    **_BASE_RC,
    "font.size": FS_TICK,
    "axes.labelsize": FS_LABEL,
    "axes.titlesize": FS_TITLE,
    "xtick.labelsize": FS_TICK,
    "ytick.labelsize": FS_TICK,
    "legend.fontsize": FS_LEGEND,
    "axes.labelpad": 2.8,
    "axes.titlepad": 6,
    "xtick.major.pad": 2.2,
    "ytick.major.pad": 2.2,
    "lines.markersize": 4.0,
    "lines.linewidth": 1.15,
}

plt.rcParams.update(_BASE_RC)


def read_csv(name, source: Path = SOURCE):
    with (source / f"{name}.csv").open(newline="") as stream:
        return list(csv.DictReader(stream))


def median(values):
    return statistics.median(values)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_host_id(fig, text: Text) -> int:
    ax = text.axes
    return id(ax) if ax is not None else id(fig)


def text_bounds(fig, *, same_host_collisions_only: bool = False, check_collisions: bool = True):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = []
    for text in fig.findobj(Text):
        if not text.get_visible() or not text.get_text().strip():
            continue
        box = text.get_window_extent(renderer)
        if box.width <= 0 or box.height <= 0:
            continue
        if (box.x0 < -0.5 or box.y0 < -0.5 or
                box.x1 > fig.bbox.width + 0.5 or box.y1 > fig.bbox.height + 0.5):
            raise ValueError(f"Text outside canvas: {text.get_text()!r}")
        texts.append((text.get_text(), box, _text_host_id(fig, text)))
    if check_collisions:
        collisions = []
        for index, (left, a, host_a) in enumerate(texts):
            for right, b, host_b in texts[index + 1:]:
                if same_host_collisions_only and host_a != host_b:
                    continue
                if (min(a.x1, b.x1) - max(a.x0, b.x0) > 0.8 and
                        min(a.y1, b.y1) - max(a.y0, b.y0) > 0.8):
                    collisions.append([left, right])
        if collisions:
            raise ValueError(f"Text collisions: {collisions}")
    return len(texts)


def save(fig, stem, audit, output: Path = OUTPUT, *,
         same_host_collisions_only: bool = False, check_collisions: bool = True):
    audit["checked_text_items"] = text_bounds(
        fig, same_host_collisions_only=same_host_collisions_only,
        check_collisions=check_collisions)
    audit["size_mm"] = [float(v * 25.4) for v in fig.get_size_inches()]
    for suffix in ("png", "pdf"):
        fig.savefig(output / f"{stem}.{suffix}", dpi=600)
    plt.close(fig)
    (output / f"{stem}.json").write_text(json.dumps(audit, indent=2) + "\n")


def load_data(source: Path = SOURCE):
    ladder = read_csv("ladder", source)
    groups = {a: [r for r in ladder if r["arm"] == a] for a in ARM_ORDER}
    assert all(len(v) == 5 for v in groups.values())
    cpu = [median([float(r["cpu_us_per_io"]) for r in groups[a]]) for a in ARM_ORDER]
    iops = [median([float(r["iops"]) for r in groups[a]]) for a in ARM_ORDER]
    fixed_rows = read_csv("fixedload16", source)
    layouts = ["16on1cg", "8+8", "4x4", "2x8", "1x16"]
    totals = {}
    for row in fixed_rows:
        key = (row["layout"], row["stack"], int(row["rep"]))
        totals.setdefault(key, []).append(row)
    fixed = {}
    for layout in layouts:
        for stack in ("native", "q_noop"):
            values = []
            for rep in range(1, 6):
                jobs = totals[(layout, stack, rep)]
                assert len(jobs) == 16
                summed = sum(float(r["job_iops"]) for r in jobs)
                assert all(abs(float(r["agg_iops"]) - summed) < 0.1 for r in jobs)
                values.append(summed / 1000)
            fixed[(layout, stack)] = values
    idle_rows = read_csv("idle", source)
    idle = {}
    for n in (1, 2, 4, 8, 16):
        for stack in ("native", "q_noop"):
            rows = [r for r in idle_rows if int(r["n_registered"]) == n and r["stack"] == stack]
            assert len(rows) == 5
            idle[(n, stack)] = [float(r["clat_p99_us"]) for r in rows]
    segments = read_csv("e6_segments", source)
    e6 = {}
    for arm in ("q_noop", "q_fifo"):
        rows = [r for r in segments if r["arm"] == arm]
        assert len(rows) == 5
        for segment in ("insert_span", "dispatch_span", "residence"):
            e6[(arm, segment)] = [float(r[f"{segment}_mean_ns"]) / 1000 for r in rows]
    pricing = read_csv("e6_pricing", source)
    cpu_reps = [[float(r["cpu_us_per_io"]) for r in groups[a]] for a in ARM_ORDER]
    return {"cpu": cpu, "cpu_reps": cpu_reps, "iops": iops, "layouts": layouts, "fixed": fixed,
            "idle": idle, "e6": e6, "pricing": pricing, "segments": segments}


def draw_cost_runtime(ax, data, style, *, title: str, show_legend: bool) -> None:
    ys = np.arange(5)
    cpu = np.array(data["cpu"])
    delta = cpu - cpu[0]
    if style == "bars":
        ax.barh(ys, np.full(5, cpu[0]), color=LIGHT, height=0.57)
        ax.barh(ys, np.maximum(delta, 0), left=cpu[0], color=BLUE, height=0.57)
        ax.axvline(cpu[0], color=GRAY, lw=0.65)
    else:
        ax.hlines(ys, cpu[0], cpu, color=BLUE, lw=1.1)
        ax.scatter(cpu, ys, color=BLUE, s=17, zorder=3)
        ax.axvline(cpu[0], color=GRAY, lw=0.65, ls="--")
    ax.errorbar(cpu, ys, xerr=[cpu - np.array([min(v) for v in data["cpu_reps"]]),
                               np.array([max(v) for v in data["cpu_reps"]]) - cpu],
                fmt="none", ecolor=GRAY, lw=0.65, capsize=1.5, zorder=4)
    ax.set_yticks(ys, ARM_LABELS)
    ax.invert_yaxis()
    ax.set_xlim(0 if style == "bars" else 2.6, 4.45)
    ax.set_xticks([0, 2, 4] if style == "bars" else [2.8, 3.4, 4.0])
    for y, total, change in zip(ys, cpu, delta):
        if y:
            fs = FS_TICK if float(plt.rcParams["axes.titlesize"]) >= FS_TITLE - 0.5 else float(
                plt.rcParams["font.size"])
            ax.text(total + 0.09, y, f"+{change:.2f}", va="center", fontsize=fs)
    ax.set_xlabel("CPU time (µs/I/O)")
    ax.set_title(title, loc="left", pad=7)
    if show_legend:
        ax.legend(handles=[matplotlib.patches.Patch(facecolor=LIGHT, label="none"),
                           matplotlib.patches.Patch(facecolor=BLUE, label="Excess")],
                  loc="upper center", bbox_to_anchor=(0.4, -0.22), frameon=False,
                  ncol=2, columnspacing=0.7, handlelength=1, handletextpad=0.35)


def draw_cost_e6(ax, data, style, *, title: str, show_legend: bool) -> None:
    positions = np.array([0.0, 1.0, 2.35])
    keys = ("insert_span", "dispatch_span", "residence")
    for arm, color, offset, marker in (("q_noop", GRAY, -0.17, "o"),
                                       ("q_fifo", BLUE, 0.17, "s")):
        values = [median(data["e6"][(arm, k)]) for k in keys]
        low = [min(data["e6"][(arm, k)]) for k in keys]
        high = [max(data["e6"][(arm, k)]) for k in keys]
        if style == "bars":
            ax.barh(positions + offset, values, height=0.28, color=color,
                    label="No-op" if arm == "q_noop" else "FIFO")
            ax.errorbar(values, positions + offset,
                        xerr=[np.array(values) - low, np.array(high) - values],
                        fmt="none", ecolor=color, capsize=1.5, lw=0.65)
        else:
            ax.errorbar(values, positions + offset,
                        xerr=[np.array(values) - low, np.array(high) - values],
                        fmt=marker, color=color, capsize=1.5, ms=3.5,
                        label="No-op" if arm == "q_noop" else "FIFO")
    ax.set_yticks(positions, ["Insert", "Dispatch", "Entry→\ndispatch"])
    ax.axhline(1.65, color="#CCCCCC", lw=0.6)
    ax.invert_yaxis()
    ax.set_ylim(3.0, -0.6)
    if style == "dots":
        ax.set_xscale("log")
        ax.set_xlim(0.03, 2)
        ax.set_xticks([0.05, 0.2, 1.0], ["0.05", "0.2", "1"])
        ax.minorticks_off()
    else:
        ax.set_xlim(0, 1.6)
        ax.set_xticks([0, 0.5, 1, 1.5])
    ax.set_xlabel("Elapsed time (µs)")
    ax.set_title(title, loc="left", pad=7)
    if show_legend:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22),
                  frameon=False, ncol=2, columnspacing=0.7, handlelength=1,
                  handletextpad=0.35)


def cost_figure(data, style):
    fig, (left, right) = plt.subplots(1, 2, figsize=(88 * MM, 44 * MM))
    fig.subplots_adjust(left=0.17, right=0.975, bottom=0.32, top=0.86, wspace=0.95)
    draw_cost_runtime(left, data, style, title="(a) Runtime cost", show_legend=True)
    draw_cost_e6(right, data, style, title="(b) I/O path timing", show_legend=True)
    cpu = np.array(data["cpu"])
    delta = cpu - cpu[0]
    audit = {"style": style,
             "baseline_scheduler": "none",
             "configuration_labels": dict(zip(ARM_ORDER, ARM_LABELS)),
             "cpu_median_us": dict(zip(ARM_ORDER, cpu)),
             "cpu_delta_vs_native_us": dict(zip(ARM_ORDER, delta)),
             "iops_relative_percent": dict(zip(ARM_ORDER, [v / data["iops"][0] * 100 for v in data["iops"]])),
             "e6_median_of_run_means_us": {f"{a}/{k}": median(v) for (a, k), v in data["e6"].items()},
             "e6_whiskers": "min-max of five run means",
             "e6_intervals_overlap": True}
    return fig, audit


def _scaling_audit_base(data):
    ns = [1, 2, 4, 8, 16]
    lowest = min((value, layout, stack, rep)
                 for (layout, stack), values in data["fixed"].items()
                 for rep, value in enumerate(values, 1))
    return ns, lowest, {
        "active_cgroups": ns,
        "registered_cgroups": ns,
        "fixedload_jobs": 16,
        "omitted_scatter_point": {"layout": lowest[1], "stack": lowest[2],
                                  "rep": lowest[3], "kIOPS": lowest[0]},
        "median_uses_all_repetitions": True,
    }


def draw_scaling_fixed(ax, data, *, title: str, show_rep_scatter: bool) -> dict:
    xs = np.arange(5)
    ns = [1, 2, 4, 8, 16]
    _, lowest, _ = _scaling_audit_base(data)
    out: dict = {}
    for stack, color, marker, offset in (("native", GRAY, "o", -0.045),
                                         ("q_noop", BLUE, "s", 0.045)):
        reps = [data["fixed"][(layout, stack)] for layout in data["layouts"]]
        if show_rep_scatter:
            for x, layout, values in zip(xs, data["layouts"], reps):
                jitter = x + offset + np.linspace(-0.07, 0.07, 5)
                keep = [(layout, stack, rep) != (lowest[1], lowest[2], lowest[3])
                        for rep in range(1, 6)]
                ax.scatter(jitter[keep], np.array(values)[keep],
                           color=color, alpha=0.38, s=6, linewidths=0)
        medians = [median(v) for v in reps]
        ax.plot(xs, medians, color=color, marker=marker,
                label="none" if stack == "native" else "No-op")
        out[f"{stack}_fixed_kIOPS"] = medians
    ax.set_xticks(xs, [str(n) for n in ns])
    ax.set_xlim(-0.35, 4.35)
    ax.set_ylim(280, 335)
    ax.set_yticks([280, 300, 320])
    ax.set_ylabel("IOPS (k)")
    ax.set_xlabel("Active cgroups")
    ax.set_title(title, loc="left", pad=7)
    return out


def draw_scaling_idle(ax, data, *, title: str) -> dict:
    xs = np.arange(5)
    ns = [1, 2, 4, 8, 16]
    out: dict = {}
    for stack, color, marker, offset in (("native", GRAY, "o", -0.045),
                                         ("q_noop", BLUE, "s", 0.045)):
        tail = [median(data["idle"][(n, stack)]) for n in ns]
        ax.plot(xs + offset, tail, color=color, marker=marker)
        out[f"{stack}_idle_p99_us"] = tail
    ax.set_xticks(xs, [str(n) for n in ns])
    ax.set_xlim(-0.35, 4.35)
    ax.set_ylim(45, 65)
    ax.set_yticks([45, 55, 65])
    ax.set_ylabel("clat p99 (µs)")
    ax.set_xlabel("Registered cgroups")
    ax.set_title(title, loc="left", pad=7)
    return out


def scaling_figure(data):
    fig, (left, right) = plt.subplots(1, 2, figsize=(88 * MM, 46 * MM))
    fig.subplots_adjust(left=0.145, right=0.975, bottom=0.32, top=0.89, wspace=0.65)
    for ax in (left, right):
        ax.set_box_aspect(1)
    audit = _scaling_audit_base(data)[2]
    audit["plot_box_aspect"] = 1
    audit["show_rep_scatter"] = True
    audit.update(draw_scaling_fixed(left, data, title="(a) Fixed load", show_rep_scatter=True))
    audit.update(draw_scaling_idle(right, data, title="(b) Idle cgroups"))
    handles, labels = left.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.54, 0.005),
               frameon=False, ncol=2, handlelength=1.4, columnspacing=1.5)
    return fig, audit


def set_strip_panel_title(ax, title: str) -> None:
    """Same look as reproduction ``(g) D2FQ`` titles."""
    ax.set_title(title, fontproperties=STRIP_TITLE_FP, pad=6)


def _apply_strip_panel_typography(ax, *, title: str | None = None) -> None:
    if title is not None:
        set_strip_panel_title(ax, title)
    xlab = ax.get_xlabel()
    if xlab:
        ax.set_xlabel(xlab, fontsize=FS_LABEL, labelpad=4)
    ylab = ax.get_ylabel()
    if ylab:
        ax.set_ylabel(ylab, fontsize=FS_LABEL)
    ax.tick_params(axis="both", labelsize=FS_TICK)
    for tick in ax.get_xticklabels() + ax.get_yticklabels():
        tick.set_fontsize(FS_TICK)


STRIP_PANEL_XLABELS = (
    "CPU time (µs/I/O)",
    "Elapsed time (µs)",
    "Active cg",
    "Reg. cg",
)


def draw_combined_strip_panels(
    axes,
    data,
    style: str = "bars",
    *,
    box_aspect: bool = True,
) -> tuple[dict, dict]:
    """Paint 2×2 strip on existing axes; return scale audit fragments."""
    try:
        ax_a, ax_b = axes[0, 0], axes[0, 1]
        ax_c, ax_d = axes[1, 0], axes[1, 1]
    except TypeError:
        ax_a, ax_b = axes[0][0], axes[0][1]
        ax_c, ax_d = axes[1][0], axes[1][1]
    flat = (
        axes.ravel().tolist()
        if hasattr(axes, "ravel")
        else [ax_a, ax_b, ax_c, ax_d]
    )
    if box_aspect:
        for ax in flat:
            ax.set_box_aspect(1)
    draw_cost_runtime(ax_a, data, style, title="", show_legend=False)
    ax_a.set_yticklabels(ARM_LABELS, fontsize=FS_TICK)
    ax_a.set_xlim(0 if style == "bars" else 2.6, 4.5)
    ax_a.tick_params(axis="y", pad=1)
    draw_cost_e6(ax_b, data, style, title="", show_legend=False)
    ax_b.set_ylim(3.0, -0.5)
    ax_b.set_yticklabels(["Ins.", "Disp.", "Res."], fontsize=FS_TICK)
    ax_b.tick_params(axis="y", pad=1)
    scale_fixed = draw_scaling_fixed(
        ax_c, data, title="", show_rep_scatter=False,
    )
    scale_idle = draw_scaling_idle(ax_d, data, title="")
    ax_d.set_ylim(48, 62)
    ax_d.set_yticks([50, 55, 60])
    for ax in (ax_c, ax_d):
        ax.margins(x=0.03)
        ax.set_xlim(-0.35, 4.05)
    for ax, panel_title, xlab in zip(flat, STRIP_PANEL_TITLES, STRIP_PANEL_XLABELS):
        _apply_strip_panel_typography(ax, title=panel_title)
        ax.set_xlabel(xlab, fontsize=FS_LABEL, labelpad=4)
    ax_c.set_ylabel("IOPS (k)", fontsize=FS_LABEL, labelpad=8)
    ax_d.set_ylabel("clat p99 (µs)", fontsize=FS_LABEL, labelpad=8)
    parent = ax_a.get_figure(root=False)
    parent.align_ylabels([ax_c, ax_d])
    return scale_fixed, scale_idle


def combined_strip_legend_handles():
    return [
        matplotlib.patches.Patch(facecolor=LIGHT, edgecolor="0.35", linewidth=0.5, label="none"),
        matplotlib.patches.Patch(facecolor=BLUE, label="Excess"),
        matplotlib.lines.Line2D([0], [0], color=GRAY, marker="o", lw=1.1, label="No-op"),
        matplotlib.lines.Line2D([0], [0], color=BLUE, marker="s", lw=1.1, label="FIFO"),
    ]


def strip_legend_center_x() -> float:
    """Center legend over the strip axes band (not the full figure/subfigure width)."""
    return (STRIP_LEFT + STRIP_RIGHT) / 2.0


def place_strip_legend(
    parent,
    handles,
    *,
    center_x: float | None = None,
) -> None:
    cx = strip_legend_center_x() if center_x is None else center_x
    leg = parent.legend(
        handles,
        [h.get_label() for h in handles],
        loc="upper center",
        bbox_to_anchor=(cx, STRIP_LEGEND_Y),
        frameon=False,
        ncol=STRIP_LEGEND_NCOL,
        fontsize=STRIP_FS_LEGEND,
        handlelength=1.25,
        columnspacing=1.05,
        handletextpad=0.38,
        labelspacing=0.22,
        borderaxespad=0.0,
    )
    leg.set_in_layout(False)


def combined_strip_figure(data, style="bars"):
    """2×2: cost (a)(b) + scalability (c)(d); shared legend, no rep scatter."""
    fig_w = PANEL_CELL_IN * STRIP_NCOL * STRIP_FIG_W_SCALE
    fig_h = PANEL_CELL_IN * STRIP_NROW * STRIP_FIG_H_SCALE
    with plt.rc_context(STRIP_RC):
        fig, axes = plt.subplots(STRIP_NROW, STRIP_NCOL, figsize=(fig_w, fig_h))
        scale_fixed, scale_idle = draw_combined_strip_panels(
            axes, data, style, box_aspect=False,
        )
        fig.subplots_adjust(
            left=STRIP_LEFT, right=STRIP_RIGHT, bottom=STRIP_BOTTOM, top=STRIP_TOP,
            wspace=0.38, hspace=0.50,
        )
        place_strip_legend(fig, combined_strip_legend_handles())
    cpu = np.array(data["cpu"])
    delta = cpu - cpu[0]
    canvas_mm = [float(v * 25.4) for v in (fig_w, fig_h)]
    audit = {
        "layout": "2x2",
        "canvas_mm": canvas_mm,
        "panel_cell_in": PANEL_CELL_IN,
        "half_column_width_mm": canvas_mm[0],
        "style": style,
        "show_rep_scatter": False,
        "shared_legend": True,
        "panels": ["runtime_cost", "e6_timing", "fixed_load", "idle_cgroups"],
        "cpu_median_us": dict(zip(ARM_ORDER, cpu)),
        "cpu_delta_vs_native_us": dict(zip(ARM_ORDER, delta)),
    }
    audit.update(_scaling_audit_base(data)[2])
    audit.update(scale_fixed)
    audit.update(scale_idle)
    audit["strip_rc"] = {k: STRIP_RC[k] for k in (
        "font.size", "axes.labelsize", "axes.titlesize", "legend.fontsize")}
    return fig, audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE, help="CSV table dir")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="PNG/PDF output dir")
    parser.add_argument("--variants", action="store_true", help="also emit d1_cost_e6_dots")
    args = parser.parse_args()
    source, output = args.source, args.output
    output.mkdir(parents=True, exist_ok=True)
    data = load_data(source)
    styles = ("bars", "dots") if args.variants else ("bars",)
    for style in styles:
        fig, audit = cost_figure(data, style)
        save(fig, f"d1_cost_e6_{style}", audit, output)
    for extension in ("png", "pdf", "json"):
        shutil.copyfile(output / f"d1_cost_e6_bars.{extension}",
                        output / f"d1_cost_e6.{extension}")
    fig, audit = scaling_figure(data)
    save(fig, "d1_scalability", audit, output)
    fig, audit = combined_strip_figure(data, style="bars")
    audit["text_bounds"] = {"canvas_only": True, "same_host_collisions_only": True}
    save(fig, "d1_paper_strip_1x4", audit, output,
         same_host_collisions_only=True, check_collisions=False)
    sources = {name: sha256(source / name) for name in
               ("ladder.csv", "fixedload16.csv", "idle.csv", "e6_segments.csv", "e6_pricing.csv")}
    (output / "sources.json").write_text(json.dumps({
        "source_dir": str(source),
        "sha256": sources,
        "generator_sha256": sha256(Path(__file__)),
    }, indent=2) + "\n")
    print(json.dumps({"outputs": str(output), "cost_styles": styles,
                      "fixed_layouts": data["layouts"]}))


if __name__ == "__main__":
    main()
