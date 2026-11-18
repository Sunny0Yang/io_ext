#!/usr/bin/env python3
"""Render framework-only D1 observations; preserve the historical input data."""

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.text import Text

BASE = Path(__file__).resolve().parents[1]
A_CONFIGS = [
    ("A1_native_none", "Native"),
    ("A2_scxio_bare", "Bare"),
    ("A3_scxio_qnone", "Direct"),
    ("A4_scxio_qfifo", "FIFO"),
]
DRIFT = "A7_native_none_rep"
WIDTHS = (2, 4, 8, 16)
COLORS = {"native": "#555555", "qfifo": "#2375A8"}


def dump_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percentile_us(row, kind, percentile="99.000000"):
    return float(row[f"json_{kind}_ns_{percentile}"]) / 1000.0


def load_data(data_dir):
    csv_path = data_dir / "d1_analyzed_percentiles.csv"
    with csv_path.open(newline="", encoding="utf-8") as stream:
        source = list(csv.DictReader(stream))
    by_name = {row["config"]: row for row in source}
    assert len(by_name) == len(source), "duplicate config rows"
    layers, used, files = [], [], [csv_path]
    baseline = float(by_name[A_CONFIGS[0][0]]["iops"])
    for config, label in A_CONFIGS + [(DRIFT, "Native (post)")]:
        row = by_name[config]
        json_path = data_dir / config / "ta_fio.json"
        files.append(json_path)
        document = json.loads(json_path.read_text())
        assert document["fio version"] == "fio-3.41"
        job = document["jobs"][0]
        assert job["error"] == 0
        options = job["job options"]
        for key, expected in (("direct", "1"), ("ioengine", "io_uring"),
                              ("bs", "4k"), ("rw", "randread"), ("iodepth", "1")):
            assert options[key] == expected, (config, key)
        read = job["read"]
        assert np.isclose(read["iops"], float(row["iops"]), atol=1e-6)
        for kind in ("slat", "clat"):
            for pct in ("50.000000", "99.000000", "99.900000"):
                assert read[f"{kind}_ns"]["percentile"][pct] == float(row[f"json_{kind}_ns_{pct}"])
        means = {kind: read[f"{kind}_ns"]["mean"] / 1000.0
                 for kind in ("slat", "clat", "lat")}
        for kind in means:
            assert np.isclose(means[kind], float(row[f"{kind}_mean"]) / 1000.0,
                              rtol=1e-6), (config, kind)
        assert abs(means["slat"] + means["clat"] - means["lat"]) < 0.001
        layers.append({
            "config": config, "label": label, "iops": read["iops"],
            "iops_retained_percent": 100 * read["iops"] / baseline,
            "slat_mean_us": means["slat"], "clat_mean_us": means["clat"],
            "lat_mean_us": means["lat"],
            "slat_p99_us": percentile_us(row, "slat"),
            "clat_p99_us": percentile_us(row, "clat"),
            "clat_p999_us": percentile_us(row, "clat", "99.900000"),
            "role": "drift check" if config == DRIFT else "measurement",
        })
        used.append(row)

    horizontal, jobs = [], []
    for width in WIDTHS:
        for stack in ("native", "qfifo"):
            prefix = f"B_{width}cg_{stack}"
            group = [row for row in source if row["config"].startswith(prefix + "/")]
            assert len(group) == width, (prefix, len(group))
            assert {row["config"].split("/")[1] for row in group} == {
                f"cg{i}" for i in range(1, width + 1)}
            aggregate = sum(float(row["iops"]) for row in group)
            record = {"active_cgroups": width, "stack": stack, "jobs": len(group),
                      "aggregate_iops": aggregate}
            for kind in ("slat", "clat"):
                values = [percentile_us(row, kind) for row in group]
                record.update({f"{kind}_job_p99_min_us": min(values),
                               f"{kind}_job_p99_mean_us": float(np.mean(values)),
                               f"{kind}_job_p99_median_us": float(np.median(values)),
                               f"{kind}_job_p99_max_us": max(values)})
            horizontal.append(record)
            for row in group:
                jobs.append({"config": row["config"], "active_cgroups": width,
                             "stack": stack, "iops": float(row["iops"]),
                             "slat_p99_us": percentile_us(row, "slat"),
                             "clat_p99_us": percentile_us(row, "clat")})
                used.append(row)
    for record in horizontal:
        native = next(x["aggregate_iops"] for x in horizontal
                      if x["active_cgroups"] == record["active_cgroups"] and x["stack"] == "native")
        record["iops_retained_percent"] = 100 * record["aggregate_iops"] / native
    for i, layer in enumerate(layers):
        if i == 0 or layer["role"] == "drift check":
            layer["dlat_mean_us"] = 0.0 if i == 0 else None
            layer["dslat_mean_us"] = 0.0 if i == 0 else None
            layer["dclat_mean_us"] = 0.0 if i == 0 else None
        else:
            prev = layers[i - 1]
            layer["dlat_mean_us"] = layer["lat_mean_us"] - prev["lat_mean_us"]
            layer["dslat_mean_us"] = layer["slat_mean_us"] - prev["slat_mean_us"]
            layer["dclat_mean_us"] = layer["clat_mean_us"] - prev["clat_mean_us"]
    assert len(jobs) == 60
    assert len(used) == 65
    return layers, horizontal, jobs, used, source, files


def style(ax):
    ax.spines[["right", "top"]].set_visible(False)
    ax.tick_params(labelsize=8, length=2.5, pad=2)
    ax.grid(axis="y", color="0.9", linewidth=0.5)
    ax.set_axisbelow(True)


def draw(layers, horizontal, jobs, out_dir):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5,
                         "axes.labelsize": 8.5, "axes.titlesize": 9.5,
                         "axes.linewidth": 0.65, "pdf.fonttype": 42})
    fig = plt.figure(figsize=(174 / 25.4, 85 / 25.4))
    grid = fig.add_gridspec(1, 3, left=0.075, right=0.985, bottom=0.16, top=0.83,
                            wspace=0.55, width_ratios=[1.0, 1.02, 1.05])
    left = grid[0].subgridspec(2, 1, height_ratios=[1.8, 1], hspace=0.40)
    a = fig.add_subplot(left[0])
    ai = fig.add_subplot(left[1])
    b = fig.add_subplot(grid[1])
    right = grid[2].subgridspec(2, 1, hspace=0.35)
    cs = fig.add_subplot(right[0])
    cc = fig.add_subplot(right[1], sharex=cs)
    for ax in (a, ai, b, cs, cc):
        style(ax)
    plotted = layers[:4]
    rungs = layers[1:4]
    x = np.arange(3)
    deltas = np.array([r["dlat_mean_us"] for r in rungs])
    a.bar(x, deltas, width=0.55,
          color=["#9E9E9E", "#6E9EBC", "#D88225"])
    a.axhline(0, color="0.25", lw=0.8)
    a.set_title("(a) Layer increments", loc="left", pad=7)
    a.set_ylabel("Δ mean latency (µs)")
    a.set_ylim(-0.35, 2.1)
    a.set_yticks([0, 0.5, 1.0, 1.5, 2.0])
    a.set_xticks(x, [r["label"] for r in rungs], fontsize=7.5,
                 rotation=20, ha="right")
    for i, row in enumerate(rungs):
        value = row["dlat_mean_us"]
        a.text(i, value + (0.07 if value >= 0 else -0.13),
               f"{value:+.3f}", ha="center",
               va="bottom" if value >= 0 else "top", fontsize=7.5)
    a.text(0.03, 0.96,
           f"Native base {layers[0]['lat_mean_us']:.2f} µs"
           f" → stack {plotted[3]['lat_mean_us']:.2f} µs",
           transform=a.transAxes, fontsize=7, va="top")
    x = np.arange(4)
    retention = [r["iops_retained_percent"] for r in plotted]
    ai.bar(x, retention, color=[COLORS["native"], "#A2A2A2", "#6E9EBC", COLORS["qfifo"]],
           width=0.62)
    ai.set_ylim(0, 120)
    ai.set_yticks([0, 50, 100])
    ai.set_ylabel("IOPS (%)")
    ai.set_xticks(x, [r["label"] for r in plotted], fontsize=7.5, rotation=25, ha="right")
    for i, value in enumerate(retention):
        ai.text(i, value + 3, f"{value:.1f}", ha="center", va="bottom", fontsize=7)

    for stack in ("native", "qfifo"):
        records = [r for r in horizontal if r["stack"] == stack]
        b.plot(range(4), [r["aggregate_iops"] / 1000 for r in records],
               color=COLORS[stack], marker="o" if stack == "native" else "s",
               markersize=4, linewidth=1.6,
               linestyle="-" if stack == "native" else "--")
    fifo = [r for r in horizontal if r["stack"] == "qfifo"]
    for i, record in enumerate(fifo):
        b.annotate(f"{record['iops_retained_percent']:.1f}%", (i, record["aggregate_iops"] / 1000),
                   xytext=(0, -13), textcoords="offset points", ha="center", fontsize=7)
    b.set_title("(b) Aggregate IOPS", loc="left", pad=7)
    b.set_ylabel("Aggregate kIOPS")
    b.set_xlabel("Active cgroups (QD1 each)")
    b.set_xticks(range(4), WIDTHS)
    b.set_ylim(0, 710)
    b.set_xlim(-0.4, 3.4)

    for ax, metric in ((cs, "slat"), (cc, "clat")):
        for stack in ("native", "qfifo"):
            records = [r for r in horizontal if r["stack"] == stack]
            ax.plot(range(4), [r[f"{metric}_job_p99_mean_us"] for r in records],
                    color=COLORS[stack], marker="o" if stack == "native" else "s",
                    markersize=4, linewidth=1.6,
                    linestyle="-" if stack == "native" else "--")
        ax.set_ylabel(f"Mean {metric} P99 (µs)")
        ax.set_xlim(-0.4, 3.4)
        ax.set_ylim(bottom=0)
        ax.set_xticks(range(4), WIDTHS)
    cs.set_ylim(0, 30)
    cs.set_yticks([0, 10, 20, 30])
    cc.set_ylim(0, 170)
    cc.set_yticks([0, 50, 100, 150])
    cs.set_title("(c) Per-cgroup tails", loc="left", pad=7)
    cs.tick_params(labelbottom=False)
    cc.set_xlabel("Active cgroups (QD1 each)")
    handles = [Line2D([], [], color=COLORS[s], marker="o" if s == "native" else "s",
                      ls="-" if s == "native" else "--", lw=1.5, markersize=4,
                      label=label) for s, label in (("native", "Native"), ("qfifo", "FIFO shell"))]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.66, 0.985),
               ncol=2, frameon=False, handlelength=1.4, columnspacing=1.5)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = [t for t in fig.findobj(Text) if t.get_visible() and t.get_text().strip()]
    for text in texts:
        box = text.get_window_extent(renderer)
        if box.x0 < -0.5 or box.y0 < -0.5 or box.x1 > fig.bbox.width + 0.5 or box.y1 > fig.bbox.height + 0.5:
            raise ValueError(f"text outside canvas: {text.get_text()}")
    for i, text in enumerate(texts):
        for other in texts[i + 1:]:
            if text.get_window_extent(renderer).overlaps(other.get_window_extent(renderer)):
                raise ValueError(f"overlapping text: {text.get_text()!r} {text.get_window_extent(renderer).bounds}, {other.get_text()!r} {other.get_window_extent(renderer).bounds}")
    fig.savefig(out_dir / "d1_framework_overhead.png", dpi=300)
    fig.savefig(out_dir / "d1_framework_overhead.pdf")
    plt.close(fig)


def write_tables(layers, horizontal, out_dir):
    lines = ["# D1 framework-only observations", "", "One run per configuration; cgroup points are jobs, not repeated trials.", "",
             "## Layer configurations (single QD1 job)", "",
             "| Configuration | IOPS | Retained (%) | Mean slat (µs) | Mean clat (µs) | Mean lat (µs) | slat P99 (µs) | clat P99 (µs) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in layers:
        lines.append(f"| {r['label']} | {r['iops']:.0f} | {r['iops_retained_percent']:.2f} | {r['slat_mean_us']:.3f} | {r['clat_mean_us']:.3f} | {r['lat_mean_us']:.3f} | {r['slat_p99_us']:.3f} | {r['clat_p99_us']:.3f} |")
    drift = layers[-1]["iops_retained_percent"] - 100
    lines += ["", f"Native post/pre IOPS drift: {drift:+.4f}%. This is a drift check, not a replication estimate.", "",
              "## Layer increments (differences of consecutive single-run means)", "",
              "| Rung | Δ mean lat (µs) | Δ mean slat (µs) | Δ mean clat (µs) | Cumulative mean lat (µs) | IOPS retained (%) |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in layers[:4]:
        dlat = r["dlat_mean_us"]
        dlat_s = "—" if r["label"] == "Native" else f"{dlat:+.3f}"
        lines.append(f"| {r['label']} | {dlat_s} | {r['dslat_mean_us']:+.3f} | {r['dclat_mean_us']:+.3f} | {r['lat_mean_us']:.3f} | {r['iops_retained_percent']:.2f} |")
    lines += ["", "Only means are additive; percentile differences are not an overhead distribution and P99(slat)+P99(clat)≠P99(lat). At QD1 solo the queue wait is ~0, so the FIFO-shell increment is dominated by the submission path (slat) while clat stays flat.", "",
              "Target ladder still pending: the probe did not run `q_noop` (BPF enter-and-return queue) or `arb_noop` (empty arbiter) arms; `q_none` is a DIRECT bypass, not `q_noop`. Until those arms exist this composition shows the measured rungs only (Native → +Bare mount → +DIRECT shell → +FIFO queue).", "",
              "Device-state provenance: d1_probe_111427 was captured before the 2026-11-07 device slowdown (D1_VERIFY_ioext3.md §4: same-day native floor ~16.6–21k IOPS, kernel-independent). No cross-day absolute comparisons; supplementary arms must run a same-day native baseline and re-baseline solo fio first.", "",
              "## Horizontal concurrency scaling", "",
              "| Active cgroups | Stack | Aggregate IOPS | Retained (%) | Mean per-cgroup slat P99 (µs) | Mean per-cgroup clat P99 (µs) | Per-job slat P99 min–max (µs) | Per-job clat P99 min–max (µs) |",
              "|---:|---|---:|---:|---:|---:|---:|---:|"]
    for r in horizontal:
        label = "Native" if r["stack"] == "native" else "FIFO shell"
        lines.append(f"| {r['active_cgroups']} | {label} | {r['aggregate_iops']:.0f} | {r['iops_retained_percent']:.2f} | {r['slat_job_p99_mean_us']:.3f} | {r['clat_job_p99_mean_us']:.3f} | {r['slat_job_p99_min_us']:.3f}–{r['slat_job_p99_max_us']:.3f} | {r['clat_job_p99_min_us']:.3f}–{r['clat_job_p99_max_us']:.3f} |")
    lines += ["", "Aggregate IOPS = sum of the N reported job IOPS; jobs have independently started measurement windows. Native and shell runs are paired by cgroup count, not simultaneous trials. Mean per-cgroup P99 averages the N per-job P99 values; per-job P99 ranges are not confidence intervals or a pooled P99.", "",
              "## Coverage and required measurements", "",
              "| Axis | Current evidence | Required completion |", "|---|---|---|",
              "| Bare / DIRECT | A1–A3 | Paired repeats, verified hook paths |",
              "| Minimal queue shell | A4; FIFO push/pop only | Paired repeats and deployed source/binary hashes |",
              "| Empty arbiter layer | Missing | Same FIFO shell + arb_noop, verify hooks and no hold/budget |",
              "| Active width | 2/4/8/16 jobs, QD1 each | Multiple paired runs and fixed aggregate offered-load sweep |",
              "| Registered idle width | Missing | One fixed active job, vary idle registrations |",
              "| Hierarchy depth | Missing on this rig/protocol | Depth 1/2/4/8/16, fixed leaf workload, identical native topology |", "",
              "No vertical-depth empirical plot is generated because this dataset has no depth axis. Native controller toggles are not a depth experiment. Feedback arbiters, controller effects, and their telemetry are excluded.", "",
              "Historical threshold-fraction columns are invalid (ns/µs mismatch) and are never consumed. JSON latency percentiles and validated means are used instead.", ""]
    (out_dir / "tables.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE / "results/D1_overhead_bm_data")
    parser.add_argument("--out-dir", type=Path, default=BASE / "results/D1_framework_figures")
    args = parser.parse_args()
    layers, horizontal, jobs, used, source, files = load_data(args.data_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    draw(layers, horizontal, jobs, args.out_dir)
    dump_csv(args.out_dir / "layers.csv", layers)
    dump_csv(args.out_dir / "horizontal.csv", horizontal)
    dump_csv(args.out_dir / "source_jobs.csv", [{k: r[k] for k in ("config", "iops", "json_slat_ns_99.000000", "json_clat_ns_99.000000")} for r in used])
    write_tables(layers, horizontal, args.out_dir)
    manifest = {
        "source_job_count": len(source), "selected_job_count": len(used),
        "plotted_layer_configs": [r[0] for r in A_CONFIGS], "drift_check": DRIFT,
        "horizontal_counts": list(WIDTHS), "horizontal_stacks": ["native", "qfifo"],
        "source_csv_threshold_fractions": "invalid historical units; not used",
        "excluded_configs": [r["config"] for r in source if r not in used],
        "input_files": [{"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "visual_review": "Not recorded by renderer; consult design document/session result",
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"PASS: {len(source)} source jobs; {len(used)} selected; {len(jobs)} horizontal jobs")
    print(f"Wrote figure, tables and provenance to {args.out_dir}")


if __name__ == "__main__":
    main()
