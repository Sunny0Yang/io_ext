#!/usr/bin/env python3
"""Render two compact 1x2 D1 paper figures from the v3 measurement tables."""

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
from matplotlib.text import Text
import numpy as np

BASE = Path(__file__).resolve().parents[1]
SOURCE = BASE / "results/D1_ladder_v3_figures"
OUTPUT = BASE / "results/D1_paper_redesign"
MM = 1 / 25.4
BLUE = "#2879A5"
GRAY = "#555555"
LIGHT = "#DCE6EB"
ORANGE = "#C77735"
COLORS = (GRAY, BLUE, ORANGE, "#5A9070")
ARM_ORDER = ("native", "bare", "q_noop", "q_fifo", "qfifo_arb")
ARM_LABELS = ("none", "Bare", "No-op", "FIFO", "FIFO+arb")

plt.rcParams.update({
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
})


def read_csv(name):
    with (SOURCE / f"{name}.csv").open(newline="") as stream:
        return list(csv.DictReader(stream))


def median(values):
    return statistics.median(values)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_bounds(fig):
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
        texts.append((text.get_text(), box))
    collisions = []
    for index, (left, a) in enumerate(texts):
        for right, b in texts[index + 1:]:
            if (min(a.x1, b.x1) - max(a.x0, b.x0) > 0.8 and
                    min(a.y1, b.y1) - max(a.y0, b.y0) > 0.8):
                collisions.append([left, right])
    if collisions:
        raise ValueError(f"Text collisions: {collisions}")
    return len(texts)


def save(fig, stem, audit):
    audit["checked_text_items"] = text_bounds(fig)
    audit["size_mm"] = [float(v * 25.4) for v in fig.get_size_inches()]
    for suffix in ("png", "pdf"):
        fig.savefig(OUTPUT / f"{stem}.{suffix}", dpi=600)
    plt.close(fig)
    (OUTPUT / f"{stem}.json").write_text(json.dumps(audit, indent=2) + "\n")


def load_data():
    ladder = read_csv("ladder")
    groups = {a: [r for r in ladder if r["arm"] == a] for a in ARM_ORDER}
    assert all(len(v) == 5 for v in groups.values())
    cpu = [median([float(r["cpu_us_per_io"]) for r in groups[a]]) for a in ARM_ORDER]
    iops = [median([float(r["iops"]) for r in groups[a]]) for a in ARM_ORDER]
    fixed_rows = read_csv("fixedload16")
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
    idle_rows = read_csv("idle")
    idle = {}
    for n in (1, 2, 4, 8, 16):
        for stack in ("native", "q_noop"):
            rows = [r for r in idle_rows if int(r["n_registered"]) == n and r["stack"] == stack]
            assert len(rows) == 5
            idle[(n, stack)] = [float(r["clat_p99_us"]) for r in rows]
    segments = read_csv("e6_segments")
    e6 = {}
    for arm in ("q_noop", "q_fifo"):
        rows = [r for r in segments if r["arm"] == arm]
        assert len(rows) == 5
        for segment in ("insert_span", "dispatch_span", "residence"):
            e6[(arm, segment)] = [float(r[f"{segment}_mean_ns"]) / 1000 for r in rows]
    pricing = read_csv("e6_pricing")
    cpu_reps = [[float(r["cpu_us_per_io"]) for r in groups[a]] for a in ARM_ORDER]
    return {"cpu": cpu, "cpu_reps": cpu_reps, "iops": iops, "layouts": layouts, "fixed": fixed,
            "idle": idle, "e6": e6, "pricing": pricing, "segments": segments}


def cost_figure(data, style):
    fig, (left, right) = plt.subplots(1, 2, figsize=(88 * MM, 44 * MM))
    fig.subplots_adjust(left=0.17, right=0.975, bottom=0.32, top=0.86, wspace=0.95)
    ys = np.arange(5)
    cpu = np.array(data["cpu"])
    delta = cpu - cpu[0]
    if style == "bars":
        left.barh(ys, np.full(5, cpu[0]), color=LIGHT, height=0.57)
        left.barh(ys, np.maximum(delta, 0), left=cpu[0], color=BLUE, height=0.57)
        left.axvline(cpu[0], color=GRAY, lw=0.65)
    else:
        left.hlines(ys, cpu[0], cpu, color=BLUE, lw=1.1)
        left.scatter(cpu, ys, color=BLUE, s=17, zorder=3)
        left.axvline(cpu[0], color=GRAY, lw=0.65, ls="--")
    left.errorbar(cpu, ys, xerr=[cpu - np.array([min(v) for v in data["cpu_reps"]]),
                               np.array([max(v) for v in data["cpu_reps"]]) - cpu],
                  fmt="none", ecolor=GRAY, lw=0.65, capsize=1.5, zorder=4)
    left.set_yticks(ys, ARM_LABELS)
    left.invert_yaxis()
    left.set_xlim(0 if style == "bars" else 2.6, 4.45)
    left.set_xticks([0, 2, 4] if style == "bars" else [2.8, 3.4, 4.0])
    for y, total, change in zip(ys, cpu, delta):
        if y:
            left.text(total + 0.09, y, f"+{change:.2f}", va="center", fontsize=7)
    left.set_xlabel("CPU time (µs/I/O)")
    left.legend(handles=[matplotlib.patches.Patch(facecolor=LIGHT, label="none"),
                         matplotlib.patches.Patch(facecolor=BLUE, label="Excess")],
                loc="upper center", bbox_to_anchor=(0.4, -0.22), frameon=False,
                ncol=2, columnspacing=0.7, handlelength=1, handletextpad=0.35)
    left.set_title("(a) Runtime cost", loc="left", pad=7)

    positions = np.array([0.0, 1.0, 2.35])
    keys = ("insert_span", "dispatch_span", "residence")
    for arm, color, offset, marker in (("q_noop", GRAY, -0.17, "o"),
                                       ("q_fifo", BLUE, 0.17, "s")):
        values = [median(data["e6"][(arm, k)]) for k in keys]
        low = [min(data["e6"][(arm, k)]) for k in keys]
        high = [max(data["e6"][(arm, k)]) for k in keys]
        if style == "bars":
            right.barh(positions + offset, values, height=0.28, color=color,
                       label="No-op" if arm == "q_noop" else "FIFO")
            right.errorbar(values, positions + offset,
                           xerr=[np.array(values) - low, np.array(high) - values],
                           fmt="none", ecolor=color, capsize=1.5, lw=0.65)
        else:
            right.errorbar(values, positions + offset,
                           xerr=[np.array(values) - low, np.array(high) - values],
                           fmt=marker, color=color, capsize=1.5, ms=3.5,
                           label="No-op" if arm == "q_noop" else "FIFO")
    right.set_yticks(positions, ["Insert", "Dispatch", "Entry→\ndispatch"])
    right.axhline(1.65, color="#CCCCCC", lw=0.6)
    right.invert_yaxis()
    right.set_ylim(3.0, -0.6)
    if style == "dots":
        right.set_xscale("log")
        right.set_xlim(0.03, 2)
        right.set_xticks([0.05, 0.2, 1.0], ["0.05", "0.2", "1"])
        right.minorticks_off()
    else:
        right.set_xlim(0, 1.6)
        right.set_xticks([0, 0.5, 1, 1.5])
    right.set_xlabel("Elapsed time (µs)")
    right.set_title("(b) I/O path timing", loc="left", pad=7)
    right.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22),
                 frameon=False, ncol=2, columnspacing=0.7, handlelength=1,
                 handletextpad=0.35)
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


def scaling_figure(data):
    fig, (left, right) = plt.subplots(1, 2, figsize=(88 * MM, 46 * MM))
    fig.subplots_adjust(left=0.145, right=0.975, bottom=0.32, top=0.89, wspace=0.65)
    for ax in (left, right):
        ax.set_box_aspect(1)
    xs = np.arange(5)
    ns = [1, 2, 4, 8, 16]
    lowest = min((value, layout, stack, rep)
                 for (layout, stack), values in data["fixed"].items()
                 for rep, value in enumerate(values, 1))
    audit = {"active_cgroups": ns, "registered_cgroups": ns, "fixedload_jobs": 16,
             "omitted_scatter_point": {"layout": lowest[1], "stack": lowest[2],
                                        "rep": lowest[3], "kIOPS": lowest[0]},
             "median_uses_all_repetitions": True,
             "plot_box_aspect": 1}
    for stack, color, marker, offset in (("native", GRAY, "o", -0.045),
                                         ("q_noop", BLUE, "s", 0.045)):
        reps = [data["fixed"][(layout, stack)] for layout in data["layouts"]]
        for x, layout, values in zip(xs, data["layouts"], reps):
            jitter = x + offset + np.linspace(-0.07, 0.07, 5)
            keep = [(layout, stack, rep) != (lowest[1], lowest[2], lowest[3])
                    for rep in range(1, 6)]
            left.scatter(jitter[keep], np.array(values)[keep],
                         color=color, alpha=0.38, s=6, linewidths=0)
        medians = [median(v) for v in reps]
        left.plot(xs, medians, color=color, marker=marker,
                  label="none" if stack == "native" else "No-op")
        tail = [median(data["idle"][(n, stack)]) for n in ns]
        right.plot(xs + offset, tail, color=color, marker=marker)
        audit[f"{stack}_fixed_kIOPS"] = medians
        audit[f"{stack}_idle_p99_us"] = tail
    for ax in (left, right):
        ax.set_xticks(xs, [str(n) for n in ns])
        ax.set_xlim(-0.3, 4.3)
    left.set_ylim(280, 335)
    left.set_yticks([280, 300, 320])
    left.set_ylabel("IOPS (k)")
    left.set_xlabel("Active cgroups")
    left.set_title("(a) Fixed load", loc="left", pad=7)
    right.set_ylim(45, 65)
    right.set_yticks([45, 55, 65])
    right.set_ylabel("clat p99 (µs)")
    right.set_xlabel("Registered cgroups")
    right.set_title("(b) Idle cgroups", loc="left", pad=7)
    handles, labels = left.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.54, 0.005),
               frameon=False, ncol=2, handlelength=1.4, columnspacing=1.5)
    return fig, audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = load_data()
    styles = ("bars", "dots") if args.variants else ("bars",)
    for style in styles:
        fig, audit = cost_figure(data, style)
        save(fig, f"d1_cost_e6_{style}", audit)
    for extension in ("png", "pdf", "json"):
        shutil.copyfile(OUTPUT / f"d1_cost_e6_bars.{extension}",
                        OUTPUT / f"d1_cost_e6.{extension}")
    fig, audit = scaling_figure(data)
    save(fig, "d1_scalability", audit)
    sources = {name: sha256(SOURCE / name) for name in
               ("ladder.csv", "fixedload16.csv", "idle.csv", "e6_segments.csv", "e6_pricing.csv", "manifest.json")}
    (OUTPUT / "sources.json").write_text(json.dumps({"source_dir": str(SOURCE),
        "sha256": sources, "generator_sha256": sha256(Path(__file__))}, indent=2) + "\n")
    print(json.dumps({"outputs": str(OUTPUT), "cost_styles": styles,
                      "fixed_layouts": data["layouts"]}))


if __name__ == "__main__":
    main()
