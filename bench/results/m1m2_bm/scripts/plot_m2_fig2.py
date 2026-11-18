#!/usr/bin/env python3
"""Plot six M2 motivation panels or the complete nine-arm grid.

Align samples using actual fio job_start, without adding startdelay twice.
Curve circles/squares mark first/last recorded samples; activity lanes mark
actual job_start and job_start + job_runtime. Missing log intervals are broken.

Run with --results ROOT --layout motivation --out BASENAME (PNG + PDF).
Use --layout all9 for the complete corrected figure.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from lib.benchmark.fio_time_log import parse_fio_time_log  # noqa: E402

TENANT_COLORS = {"A": "#c2559f", "B": "#2e8b2e", "C": "#b8960c"}
TENANT_ORDER = ("A", "B", "C")
TENANT_IDX = {"A": 1, "B": 2, "C": 3}
# (sequence_id, panel title, legend labels per tenant) in paper order; the
# added native-kyber arm fills panel (i).
PANELS: List[Tuple[str, str, Dict[str, str]]] = [
    ("M2F2_a_none", "(a) No knob or I/O scheduler", {"A": "A", "B": "B", "C": "C"}),
    ("M2F2_b_mqdl_prio", "(b) MQ-DL + io.prio.class",
     {"A": "A - promote-to-rt", "B": "B - idle", "C": "C - restrict-to-be"}),
    ("M2F2_c_bfq", "(c) BFQ + no knob", {"A": "A", "B": "B", "C": "C"}),
    ("M2F2_d_bfq_weight", "(d) BFQ + io.bfq.weight",
     {"A": "A - io.bfq.weight @ 1000", "B": "B - io.bfq.weight @ 1", "C": "C - io.bfq.weight @ 100"}),
    ("M2F2_e_io_max", "(e) io.max", None),  # parameters are recorded in the experiment notes
    ("M2F2_f_io_latency", "(f) io.latency",
     {"A": "A - io.latency @ 20us", "B": "B - io.latency @ 1000us", "C": "C - io.latency @ 100us"}),
    ("M2F2_g_io_cost", "(g) io.cost", {"A": "A", "B": "B", "C": "C"}),
    ("M2F2_h_io_cost_weight", "(h) io.cost + io.weight",
     {"A": "A - io.weight @ 1000", "B": "B - io.weight @ 1", "C": "C - io.weight @ 100"}),
    ("M2F2_i_native_kyber", "(i) Native kyber (added)", {"A": "A", "B": "B", "C": "C"}),
]


def detect_scale(values: List[float], rate_mib_s: float) -> float:
    """Return the divisor converting fio bw-log values to MiB/s.

    fio historically logs KiB/s; newer builds log bytes/s. The rate limit
    gives a robust reference: the true scale makes the plateau sit below the
    rate cap, the wrong one overshoots it by ~1000x.
    """
    if not values:
        return 1024.0
    reference = sorted(v for v in values if v > 0)
    if not reference:
        return 1024.0
    plateau = reference[int(len(reference) * 0.9) - 1]
    if plateau / 1024.0 > rate_mib_s * 1.5:
        return 1024.0 * 1024.0  # bytes/s
    return 1024.0


def _agg_bw_mib(scene_dir: Path) -> Optional[float]:
    """Aggregate read bw (MiB/s) from the tenant's fio json, if present.

    fio json 'bw' is KiB/s by definition -- a unit-true anchor for the bw-log
    scale, unlike the plateau heuristic below (which misfires when a tenant's
    catch-up burst exceeds 1.5x the rate cap: on bm, tenant B solo-bursts to
    ~3.2 GiB/s raw and the heuristic read its KiB/s log as bytes/s, drawing
    the tenant 1024x too small / flat zero).
    """
    for p in sorted(scene_dir.glob("*_jobs1_fio.json")):
        try:
            data = json.loads(p.read_text())
            return float(data["jobs"][0]["read"]["bw"]) / 1024.0
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            continue
    return None


def load_tenant_series(scene_dir: Path, rate_mib_s: float) -> Optional[Dict[str, object]]:
    """Load actual fio job timing and unit-anchored bandwidth samples."""
    stat_files = sorted(scene_dir.glob(f"fio_{scene_dir.name}_*.json"))
    start_time = None
    for stat_file in stat_files:
        try:
            data = json.loads(stat_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data.get("start_time"), (int, float)):
            start_time = float(data["start_time"])
            break
    if start_time is None:
        return None
    logs = sorted(
        p for p in scene_dir.glob("*bw*.log")
        if not any(token in p.name for token in ("iops", "clat", "slat", "lat."))
    )
    if not logs:
        return None
    samples: List[Tuple[float, float]] = []
    scale = None
    all_values: List[float] = []
    for log_path in logs:
        parsed = parse_fio_time_log(log_path)
        if not parsed:
            continue
        if scale is None:
            scale = detect_scale([s.value for s in parsed], rate_mib_s)
        for sample in parsed:
            if sample.direction is not None and sample.direction != 0:  # reads only
                continue
            samples.append((sample.time_ms / 1000.0, sample.value / scale))
            all_values.append(sample.value)
    if not samples:
        return None
    # aggregate-anchored scale correction (see _agg_bw_mib): re-decide the
    # divisor in LOG space (multiplicative error) -- absolute distance has a
    # degenerate zone for heavily starved tenants (agg ~tens of MiB/s puts
    # both candidate errors within ~3% of each other and can flip wrong).
    agg = _agg_bw_mib(scene_dir)
    if agg and all_values and agg > 0:
        log_mean = sum(all_values) / len(all_values)
        if log_mean > 0:
            cands = (1024.0, 1024.0 * 1024.0)
            best = min(cands,
                       key=lambda c: abs(math.log2(agg) - math.log2(log_mean / c)))
            if best != scale:
                scale = best
                samples = [(ts, v_raw / scale)
                           for (ts, _), v_raw in zip(samples, all_values)]
    timing = None
    for path in sorted(scene_dir.glob("*_jobs1_fio.json")):
        job = json.loads(path.read_text())["jobs"][0]
        if "job_start" in job and "job_runtime" in job:
            timing = (float(job["job_start"]) / 1000.0,
                      float(job["job_runtime"]) / 1000.0)
            break
    if timing is None:
        raise ValueError(f"missing actual fio job_start/job_runtime: {scene_dir}")
    samples.sort(key=lambda sample: sample[0])
    return {"start_time": timing[0], "runtime": timing[1],
            "scene_start_time": start_time, "samples": samples,
            "bw_log": logs[0].name, "aggregate_mib_s": agg, "scale": scale}


def load_sequence(result_dir: Path, rate_mib_s: float) -> Optional[Dict[str, Dict[str, object]]]:
    step_dir = result_dir / "step_0"
    if not step_dir.is_dir():
        return None
    tenants: Dict[str, Dict[str, object]] = {}
    for tenant, idx in TENANT_IDX.items():
        matches = sorted(step_dir.glob(f"ib_m2f2_*_t_{idx}"))
        if not matches:
            continue
        series = load_tenant_series(matches[0], rate_mib_s)
        if series is not None:
            tenants[tenant] = series
    return tenants or None


def relative_timeline(series: Dict[str, object], t0: float) -> Tuple[List[float], List[float]]:
    times = [float(series["start_time"]) - t0 + ts for ts, _ in series["samples"]]
    values = [v for _, v in series["samples"]]
    return times, values


def tenant_interval_labels(tenants: Dict[str, Dict[str, object]]) -> Dict[str, str]:
    """Relative job_start..job_end (s) shared across the eight-arm row."""
    t0 = min(float(s["start_time"]) for s in tenants.values())
    labels: Dict[str, str] = {}
    for tenant in TENANT_ORDER:
        s = tenants[tenant]
        start = int(round(float(s["start_time"]) - t0))
        end = int(round(start + float(s["runtime"])))
        labels[tenant] = f"{tenant} ({start}-{end})"
    return labels


MOTIVATION_PANELS = [
    ("M2F2_a_none", "none"),
    ("M2F2_b_mqdl_prio", "mq-deadline"),
    ("M2F2_i_native_kyber", "kyber"),
    ("M2F2_c_bfq", "bfq"),
    ("M2F2_d_bfq_weight", "io.bfq.weight"),
    ("M2F2_e_io_max", "io.max"),
    ("M2F2_f_io_latency", "io.latency"),
    ("M2F2_g_io_cost", "io.cost"),
]
ALL_TITLES = dict(MOTIVATION_PANELS, **{
    "M2F2_c_bfq": "BFQ", "M2F2_d_bfq_weight": "BFQ+W",
    "M2F2_i_native_kyber": "Kyber",
})
EIGHT_PANELS = [
    ("M2F2_a_none", "none"),
    ("M2F2_b_mqdl_prio", "mq-deadline"),
    ("M2F2_i_native_kyber", "kyber"),
    ("M2F2_c_bfq", "bfq"),
    ("M2F2_d_bfq_weight", "io.bfq.weight"),
    ("M2F2_e_io_max", "io.max"),
    ("M2F2_f_io_latency", "io.latency"),
    ("M2F2_g_io_cost", "io.cost"),
]
TENANT_STYLES = {"A": "-", "B": "--", "C": ":"}


def contiguous_curve(times, values, max_gap=1.5):
    """Break missing log intervals without inventing zero-bandwidth samples."""
    xs, ys = [], []
    for i, (time, value) in enumerate(zip(times, values)):
        if i and time - times[i - 1] > max_gap:
            xs.append(float("nan"))
            ys.append(float("nan"))
        xs.append(time)
        ys.append(value)
    return xs, ys


def draw_panels(seq_data, selected, out_base, dpi, single_row=False, circle_both=False):
    """Paper-sized panels; the single-row version uses curve endpoints only."""
    plt.rcParams.update({"font.family": "Liberation Serif", "pdf.fonttype": 42,
                         "axes.spines.top": False, "axes.spines.right": False})
    ncols = len(selected) if single_row else 3
    nrows = math.ceil(len(selected) / ncols)
    eight_row = single_row and ncols == 8
    if eight_row:
        height_mm = 40
        gs_left, gs_right, gs_top, gs_bottom, gs_wspace = 0.048, 0.987, 0.76, 0.225, 0.11
    elif single_row:
        height_mm = 42
        gs_left, gs_right, gs_top, gs_bottom, gs_wspace = 0.055, 0.99, 0.76, 0.245, 0.16
    else:
        height_mm = 90 if nrows == 2 else 134
        gs_left, gs_right, gs_top, gs_bottom, gs_wspace = 0.075, 0.985, 0.865, 0.105, 0.17
    fig = plt.figure(figsize=(174 / 25.4, height_mm / 25.4))
    outer = fig.add_gridspec(
        nrows, ncols, left=gs_left, right=gs_right, top=gs_top, bottom=gs_bottom,
        hspace=0.48, wspace=gs_wspace,
    )
    max_end = max(float(s["start_time"]) + float(s["runtime"]) -
                  min(float(v["start_time"]) for v in tenants.values())
                  for tenants in seq_data.values() for s in tenants.values())
    xlim = (-1.5, math.ceil(max_end) + 1)
    peak = max(v / 1024 for tenants in seq_data.values()
               for s in tenants.values() for _, v in s["samples"])
    ylim = (-0.10, math.ceil(peak * 1.04 * 2) / 2)
    if circle_both:
        ref_seq = selected[0][0]
        interval_labels = tenant_interval_labels(seq_data[ref_seq])
        handles = [
            plt.Line2D([], [], color=TENANT_COLORS[t], ls=TENANT_STYLES[t],
                       lw=1.7, label=interval_labels[t])
            for t in TENANT_ORDER
        ]
    else:
        handles = [plt.Line2D([], [], color=TENANT_COLORS[t], ls=TENANT_STYLES[t],
                             lw=1.7, label=t) for t in TENANT_ORDER]
        markers = (("o", "End"),) if single_row else (("o", "Start"), ("s", "End"))
        handles += [plt.Line2D([], [], color="0.25", marker=m, mfc="white", ls="",
                              markersize=5, label=label) for m, label in markers]
    from matplotlib.font_manager import FontProperties
    panel_title_fp = FontProperties(family="Liberation Serif", size=9, weight="bold")
    legend_gap = 0.014
    plot_axes: list = []
    audit = {}
    for i, (seq, title) in enumerate(selected):
        row, col = divmod(i, ncols)
        if single_row:
            ax = fig.add_subplot(outer[row, col])
            ax.set_box_aspect(1)
            lane = None
        else:
            sub = outer[row, col].subgridspec(2, 1, height_ratios=[1, 0.45], hspace=0.17)
            ax = fig.add_subplot(sub[0])
            lane = fig.add_subplot(sub[1], sharex=ax)
        tenants = seq_data[seq]
        t0 = min(float(s["start_time"]) for s in tenants.values())
        audit[seq] = {}
        for j, tenant in enumerate(TENANT_ORDER):
            s = tenants[tenant]
            ts, vs = relative_timeline(s, t0)
            vs = [v / 1024 for v in vs]
            xs, ys = contiguous_curve(ts, vs)
            color = TENANT_COLORS[tenant]
            ax.plot(xs, ys, color=color, ls=TENANT_STYLES[tenant], lw=1.65,
                    marker="." if any(b - a > 1.5 for a, b in zip(ts, ts[1:])) else None,
                    markersize=3)
            endpoints = (((0, "o"), (-1, "o")) if circle_both else
                         ((-1, "o"),) if single_row else ((0, "o"), (-1, "s")))
            for index, marker in endpoints:
                ax.plot(ts[index], vs[index], marker=marker, ms=4.5, mfc="white",
                        mec=color, mew=1.2, ls="", zorder=8 + j)
            start = float(s["start_time"]) - t0
            end = start + float(s["runtime"])
            if lane is not None:
                y = 2 - j
                lane.plot([start, end], [y, y], color=color, lw=1.6)
                lane.plot(start, y, marker="o", ms=3.8, mfc="white", mec=color, clip_on=False)
                lane.plot(end, y, marker="s", ms=3.8, mfc="white", mec=color)
            gaps = [[a, b] for a, b in zip(ts, ts[1:]) if b - a > 1.5]
            audit[seq][tenant] = {
                "start_s": start, "end_s": end, "first_sample_s": ts[0],
                "last_sample_s": ts[-1], "sample_count": len(ts), "gaps_s": gaps,
                "aggregate_mib_s": s["aggregate_mib_s"], "log_divisor": s["scale"],
            }
        ax.set(xlim=xlim, ylim=ylim)
        ax.set_yticks([0, 1, 2, 3])
        heading = title.replace(" + ", "\n+ ") if single_row else f"({chr(97 + i)}) {title}"
        ax.set_title(
            heading,
            fontproperties=panel_title_fp,
            loc="center" if single_row else "left",
            pad=(1 if eight_row else 2),
        )
        plot_axes.append(ax)
        ax.grid(axis="y", color="0.88", lw=0.45)
        ax.tick_params(axis="y", labelsize=9, length=2, pad=2, labelleft=(col == 0))
        if lane is None:
            ax.set_xticks([0, 35, 70])
            ax.tick_params(axis="x", labelsize=9, length=2, pad=2)
        else:
            ax.tick_params(axis="x", bottom=False, labelbottom=False)
            lane.set(ylim=(-0.6, 2.6), yticks=[2, 1, 0], yticklabels=["A", "B", "C"])
            lane.set_xticks([0, 20, 40, 60, 70])
            lane.tick_params(axis="y", labelsize=7.5, length=0, pad=3)
            lane.tick_params(axis="x", labelsize=9, length=2, pad=2)
            for spine in lane.spines.values():
                spine.set_visible(False)
        if col == 0:
            ax.set_ylabel("GiB/s", fontsize=10, labelpad=3)
    fig.supxlabel("Time (s)", fontsize=10, y=(0.036 if eight_row else 0.015))
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    title_tops = [
        ax.title.get_window_extent(renderer).y1 / fig.bbox.height for ax in plot_axes
    ]
    if eight_row:
        legend_y = min(max(title_tops) + 0.003, 0.955)
    else:
        legend_y = max(title_tops) + legend_gap
    legend = fig.legend(
        handles=handles,
        loc="lower center",
        ncol=len(handles),
        frameon=False,
        fontsize=(9 if eight_row else 10),
        handlelength=2.0,
        columnspacing=(1.0 if eight_row else 1.4),
        bbox_to_anchor=(0.5, legend_y),
    )
    legend.set_in_layout(False)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for text in fig.findobj(matplotlib.text.Text):
        if not text.get_visible() or not text.get_text():
            continue
        box = text.get_window_extent(renderer)
        page = fig.bbox
        if box.x0 < page.x0 or box.y0 < page.y0 or box.x1 > page.x1 or box.y1 > page.y1:
            raise ValueError(
                f"text exceeds figure boundary: {text.get_text()!r} "
                f"box=({box.x0:.1f},{box.y0:.1f},{box.x1:.1f},{box.y1:.1f}) "
                f"page=({page.x0:.1f},{page.y0:.1f},{page.x1:.1f},{page.y1:.1f})"
            )
    for ext in ("png", "pdf"):
        fig.savefig(out_base.with_suffix(f".{ext}"), dpi=dpi)
    plt.close(fig)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=REPO_ROOT / "results/m2_fig2_virtme")
    parser.add_argument("--calibration", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="output basename (png+pdf)")
    parser.add_argument("--copy-to", type=Path, default=None, help="also copy png+pdf here")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--layout", choices=("all9", "motivation", "motivation-row", "eight-row"), default="all9")
    args = parser.parse_args()

    rate_mib_s = 1536.0
    calib = None
    calib_path = args.calibration or (args.results / "calibration.json")
    if calib_path and calib_path.is_file():
        calib = json.loads(calib_path.read_text())
        rate_mib_s = float(calib["rate_mib_s"])
    print(f"rate cap: {rate_mib_s} MiB/s (calibration: {calib_path})")

    out_base = args.out or (args.results / f"figures/m2_fig2_{args.layout}_corrected")
    out_base.parent.mkdir(parents=True, exist_ok=True)

    seq_data: Dict[str, Dict[str, Dict[str, object]]] = {}
    missing: List[str] = []
    for seq, _, _ in PANELS:
        result_dir = args.results / seq
        tenants = load_sequence(result_dir, rate_mib_s) if result_dir.is_dir() else None
        if tenants is None:
            missing.append(seq)
        else:
            seq_data[seq] = tenants
    if missing:
        print(f"WARNING: missing or unparsable sequences: {', '.join(missing)}")
    if not seq_data:
        print("no data to plot", file=sys.stderr)
        return 1

    selected = (EIGHT_PANELS if args.layout == "eight-row" else
                MOTIVATION_PANELS if args.layout.startswith("motivation") else
                [(seq, ALL_TITLES[seq]) for seq, _, _ in PANELS])
    required = [seq for seq, _ in selected if seq not in seq_data or
                set(seq_data[seq]) != set(TENANT_ORDER)]
    if required:
        raise ValueError(f"incomplete selected sequences: {required}")
    audit = draw_panels(seq_data, selected, out_base, args.dpi,
                        single_row=args.layout in ("motivation-row", "eight-row"),
                        circle_both=args.layout == "eight-row")
    summary = {seq: {tenant: round(sum(v for _, v in s["samples"]) /
                                  len(s["samples"]), 1)
                     for tenant, s in tenants.items()}
               for seq, tenants in seq_data.items()}
    audit_path = out_base.parent / (out_base.name + "_timing.json")
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True))
    png_path = out_base.with_suffix(".png")
    pdf_path = out_base.with_suffix(".pdf")
    summary_path = out_base.parent / (out_base.name + "_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {png_path}")
    print(f"wrote {pdf_path}")
    print(f"wrote {summary_path}")

    if args.copy_to:
        args.copy_to.parent.mkdir(parents=True, exist_ok=True)
        for src in (png_path, pdf_path):
            shutil.copy2(src, args.copy_to.with_suffix(src.suffix))
        print(f"copied to {args.copy_to.with_suffix('.png')} / .pdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
