#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the M1_2 device-bandwidth companion heatmap from native-matrix cases."""

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Rectangle
from matplotlib.text import Text

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "experiments/m1m2_bm/runs/m1_campaign/m1matrix_bm_20261105"
DEFAULT_FIGS = ROOT / "experiments/m1m2_bm/figs"
SCHEDULERS = ["none", "kyber", "mq-deadline", "bfq"]
SCENARIOS = ["YCSB-A", "YCSB-C", "db_bench-rww", "Shore-QPS50"]
WORKLOAD_LABELS = {
    "YCSB-A": "YCSB-A",
    "YCSB-C": "YCSB-C",
    "db_bench-rww": "db_bench",
    "Shore-QPS50": "Shore",
}
SAMPLE_INTERVAL_S = 1.0
SECTOR_BYTES = 512
MIB_BYTES = 1024 ** 2


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def app_result(case_dir):
    run = read_json(case_dir / "run_results.json")
    block = next(iter(run.values()))
    results = block["steps"][0]["results"]
    app = next(value for value in results.values()
               if isinstance(value, dict) and "scheduler" in value)
    return app


def measurement_window(case_dir, scenario, app):
    app_start = float(app["start_time"])
    app_end = float(app["end_time"])
    if scenario.startswith("YCSB-"):
        result_path = next((case_dir / "step_0").glob("*/ycsb_rocksdb_*.json"))
        result = read_json(result_path)
        snapshots = result.get("io_stat_snapshots") or {}
        before_run = snapshots.get("before_run") or {}
        after_run = snapshots.get("after_run") or {}
        start, end = float(before_run["timestamp"]), float(after_run["timestamp"])
        runtime_s = float(result["ycsb_latency_summary"]["overall_RunTime(ms)"]) / 1000.0
        if abs((end - start) - runtime_s) > 3.0:
            raise ValueError(f"YCSB run timestamps disagree with runtime: {case_dir.name}")
    elif scenario == "db_bench-rww":
        result_path = next((case_dir / "step_0").glob("*/dbbench_*.json"))
        phase = read_json(result_path)["phase_results"]["readwhilewriting"]["io_delta"]
        start, end = float(phase["before_ts"]), float(phase["after_ts"])
    else:
        # The fixed-rate Shore run warms up 1000 requests at 50 QPS.
        start, end = app_start + 20.0, app_end
    if not (start < end and app_start <= start and end <= app_end + 1.0):
        raise ValueError(f"invalid {scenario} measurement window in {case_dir.name}")
    return start, end


def device_rates(case_dir, scenario):
    app = app_result(case_dir)
    start, end = measurement_window(case_dir, scenario, app)
    metric_path = case_dir / "monitor/system_metric/ib_system_metric_summary.json"
    samples = read_json(metric_path)["samples"]
    timestamps = [float(sample["timestamp"]) for sample in samples]
    if len(timestamps) < 2 or timestamps[0] > start + 1.5 or timestamps[-1] < end - 1.5:
        raise ValueError(f"system metric does not cover ROI: {case_dir.name}")

    intervals = []
    sectors = {"read": 0.0, "write": 0.0}
    for previous, current in zip(samples, samples[1:]):
        t0, t1 = float(previous["timestamp"]), float(current["timestamp"])
        overlap = min(t1, end) - max(t0, start)
        if overlap <= 0:
            continue
        interval = t1 - t0
        if interval <= 0 or interval > 2.0:
            raise ValueError(f"invalid sampling interval in {case_dir.name}: {interval}")
        device = current["disk"]["devices"].get("nvme0n1")
        if device is None:
            raise ValueError(f"nvme0n1 missing in system metric: {case_dir.name}")
        intervals.append((t0, t1))
        fraction = overlap / interval
        for direction in sectors:
            delta = int(device[f"{direction}_sectors_delta"])
            if delta < 0:
                raise ValueError(f"negative sector delta in {case_dir.name}")
            sectors[direction] += delta * fraction

    duration = end - start
    covered_s = sum(min(t1, end) - max(t0, start) for t0, t1 in intervals)
    if covered_s < duration - 1.0:
        raise ValueError(f"incomplete system metric ROI: {case_dir.name} ({covered_s:.2f}/{duration:.2f}s)")
    return {
        direction: count * SECTOR_BYTES / MIB_BYTES / duration
        for direction, count in sectors.items()
    }, (start, end, len(intervals))


def load_measurements(data_dir):
    with (data_dir / "native_matrix_cases.csv").open(newline="", encoding="utf-8") as stream:
        cases = list(csv.DictReader(stream))
    assert len(cases) == 48, f"expected 48 matrix cases, got {len(cases)}"
    assert all(row["gate_ok"].lower() == "true" for row in cases)
    values, audit = {}, []
    for scenario in SCENARIOS:
        for scheduler in SCHEDULERS:
            matches = [row for row in cases
                       if row["scenario"] == scenario and row["scheduler"] == scheduler]
            assert len(matches) == 3, f"expected 3 repeats: {scenario}/{scheduler}"
            reps = []
            for row in matches:
                rates, window = device_rates(data_dir / row["case"], scenario)
                reps.append(rates)
                audit.append({
                    "case": row["case"],
                    "scenario": scenario,
                    "scheduler": scheduler,
                    "repeat": int(row["repeat"]),
                    "window_start_epoch_s": window[0],
                    "window_end_epoch_s": window[1],
                    "sample_count": window[2],
                    "read_mib_s": rates["read"],
                    "write_mib_s": rates["write"],
                })
            values[(scenario, scheduler)] = {
                direction: float(np.mean([rep[direction] for rep in reps]))
                for direction in ("read", "write")
            }
    return values, audit


def make_rows(values):
    rows = []
    for scenario in ("YCSB-A", "db_bench-rww", "Shore-QPS50"):
        directions = ("read", "write")
        if scenario == "db_bench-rww":
            directions = ("write",)
        for direction in directions:
            means = {scheduler: values[(scenario, scheduler)][direction]
                     for scheduler in SCHEDULERS}
            best = max(means.values())
            rows.append({
                "scenario": scenario,
                "workload": WORKLOAD_LABELS[scenario],
                "metric": direction.capitalize(),
                "means": means,
                "retained": {scheduler: means[scheduler] / best
                             for scheduler in SCHEDULERS},
            })
    return rows


def draw(rows, output_stem):
    plt.rcParams.update({
        "font.family": "Liberation Serif",
        "font.size": 8.0,
        "axes.linewidth": 0.7,
        "xtick.major.width": 0.7,
        "pdf.fonttype": 42,
    })
    stops = [
        (0.45, "#A50F15"),
        (0.72, "#DE741C"),
        (0.90, "#FAD4A6"),
        (1.00, "#FFFFFF"),
    ]
    cmap = LinearSegmentedColormap.from_list(
        "retained_bandwidth",
        [((value - 0.45) / (1.0 - 0.45), color) for value, color in stops],
        N=1024,
    )
    cmap.set_under(stops[0][1])
    cmap.set_over(stops[-1][1])
    vmin, vmax = 0.45, 1.0

    # Row height 44 mm (matches m1_timeseries_bm_1x4); heat band aligns to M1 main axes.
    fig = plt.figure(figsize=(68 / 25.4, 44 / 25.4))
    heat_left, heat_bottom = 0.22, 0.24
    heat_width, heat_height = 0.44, 0.38
    ax = fig.add_axes([heat_left, heat_bottom, heat_width, heat_height])
    matrix = np.array([[row["retained"][scheduler] for scheduler in SCHEDULERS]
                       for row in rows])
    image = ax.imshow(matrix, cmap=cmap, vmin=vmin, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(SCHEDULERS)))
    ax.set_xticklabels(["none", "kyber", "mq-\ndeadline", "bfq"],
                       fontsize=6.5, fontweight="bold")
    ax.xaxis.tick_bottom()
    ax.tick_params(axis="x", bottom=False, labelbottom=True, length=0, pad=2)
    ax.set_yticks([])

    groups = []
    for index, row in enumerate(rows):
        scenario = row["scenario"]
        if groups and groups[-1][0] == scenario:
            groups[-1][2] = index
        else:
            groups.append([scenario, index, index])
    for scenario, first, last in groups:
        if first:
            ax.axhline(first - 0.5, color="white", lw=2.0)
        center = (first + last) / 2
        y_fraction = ax.transData.transform((0, center))[1] / fig.bbox.height
        fig.text(heat_left - 0.012, y_fraction, WORKLOAD_LABELS[scenario],
                 ha="right", va="center", fontsize=8.0, fontweight="bold")

    for index, row in enumerate(rows):
        for column, scheduler in enumerate(SCHEDULERS):
            value = row["retained"][scheduler]
            rgba = cmap(image.norm(value))
            rgb = np.array(rgba[:3])
            linear = np.where(rgb <= 0.04045, rgb / 12.92,
                              ((rgb + 0.055) / 1.055) ** 2.4)
            luminance = np.dot(linear, [0.2126, 0.7152, 0.0722])
            color = "white" if luminance < 0.179 else "black"
            best = np.isclose(value, 1.0)
            ax.text(column, index, f"{value:.2f}", ha="center", va="center",
                    fontsize=8.0, fontweight="bold" if best else "normal",
                    color=color, zorder=6)
            if best:
                ax.add_patch(Rectangle((column - 0.5, index - 0.5), 1, 1,
                                       fill=False, ec="black", lw=1.1, zorder=4))

    metric_ax = fig.add_axes([0.676, heat_bottom, 0.078, heat_height], sharey=ax)
    metric_ax.set_axis_off()
    metric_ax.set_xlim(0, 1)
    for index, row in enumerate(rows):
        metric_ax.text(0.0, index, row["metric"], ha="left", va="center", fontsize=7.0)

    cax = fig.add_axes([0.800, heat_bottom, 0.014, heat_height])
    colorbar = fig.colorbar(image, cax=cax, orientation="vertical")
    colorbar.set_ticks([0.45, 0.72, 0.90, 1.0])
    colorbar.set_ticklabels(["0.45", "0.72", "0.90", "1.00"])
    colorbar.ax.tick_params(
        axis="y", labelsize=6.5, length=1.5, pad=1, labelleft=False, labelright=True,
    )
    colorbar.ax.yaxis.set_ticks_position("right")
    colorbar.ax.get_yticklabels()[0].set_va("bottom")
    colorbar.ax.get_yticklabels()[-1].set_va("top")
    colorbar.outline.set_linewidth(0.6)

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = [text for text in fig.findobj(Text)
             if text.get_visible() and text.get_text().strip()]
    for text in texts:
        box = text.get_window_extent(renderer)
        assert (box.x0 >= 0 and box.y0 >= 0 and
                box.x1 <= fig.bbox.width and box.y1 <= fig.bbox.height), \
            f"text outside canvas: {text.get_text()!r}"
    for index, left in enumerate(texts):
        for right in texts[index + 1:]:
            assert not left.get_window_extent(renderer).overlaps(
                right.get_window_extent(renderer)), \
                f"overlapping text: {left.get_text()!r}, {right.get_text()!r}"

    fig.savefig(output_stem.with_suffix(".png"), dpi=300)
    fig.savefig(output_stem.with_suffix(".pdf"))
    return matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--fig-dir", type=Path, default=DEFAULT_FIGS)
    args = parser.parse_args()
    args.fig_dir.mkdir(parents=True, exist_ok=True)

    values, audit = load_measurements(args.data_dir)
    rows = make_rows(values)
    output_stem = args.fig_dir / "m1_2_policy_conflict_bandwidth"
    matrix = draw(rows, output_stem)
    (args.fig_dir / "m1_2_policy_conflict_bandwidth_timing.json").write_text(
        json.dumps({
            "device": "nvme0n1",
            "source": "/proc/diskstats read/write sector deltas",
            "sector_bytes": SECTOR_BYTES,
            "unit": "MiB/s, 1 MiB = 2^20 bytes",
            "normalization": "per scenario and direction: scheduler mean / highest scheduler mean; 1.00 is highest observed mean",
            "window_policy": {
                "YCSB-A/YCSB-C": "application runtime from overall_RunTime(ms), ending at app end_time",
                "db_bench-rww": "readwhilewriting phase io_delta before_ts through after_ts",
                "Shore-QPS50": "app start_time + 20 s warmup through app end_time",
            },
            "rows": rows,
            "cases": audit,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output_stem}.png and .pdf")
    print("matrix retained-bandwidth ratios (rows: YCSB-A R/W, YCSB-C R/W, db_bench R/W, Shore R/W):")
    for row, values_row in zip(rows, matrix):
        print(f"{row['workload']:>8} {row['metric']:<5} " +
              " ".join(f"{value:.3f}" for value in values_row))
    print(f"audited {len(audit)} case-level windows across {len(rows) * 4} cells")


if __name__ == "__main__":
    main()
