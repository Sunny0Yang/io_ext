#!/usr/bin/env python3
"""Render the audited D3 kvmhost paper figures.

Primary figure:
  (a) absolute BE bandwidth vs LC P99 amplification (log y), with a secondary
      x-axis showing retention against the Phase-1 calibration anchor;
  (b) tail-amplification bars that expose the measured 10x reduction from
      no isolation to the best replicated io_ext operating point.

Supplement:
  CPU utilization and BE-per-CPU efficiency. This is intentionally framed as
  the measurable cost of protection: the data do not support a CPU-efficiency
  advantage claim for io_ext on kvmhost.

Evidence discipline:
  - solid markers: replicated clean runs (BFQ n=3, qrl-k065 n=4, none n=2);
  - hollow markers: exploratory n=1 points (qrl k=.75/.85);
  - retention conversions use the Phase-1 B_none anchor (908.93 MB/s);
  - no discovery point is presented as a confirmation result.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
CALIB = ROOT / "results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"
RUNS_V1 = ROOT / "results/d3_kvmhost/discovery/analysis/discovery_runs.csv"
RUNS_V2 = ROOT / "results/d3_kvmhost/discovery2/analysis/discovery_runs.csv"

COLORS = {
    "bfq": "#2f6b8a",
    "qrl": "#d05b35",
    "none": "#575757",
    "explore": "#a8842b",
}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def number(row: dict[str, str], key: str) -> float:
    return float(row[key])


def clean_rows(rows: list[dict[str, str]], arms: set[str]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if row["arm"] in arms
        and row["status"] == "ok"
        and row.get("policy_engaged") == "True"
        and row.get("be_valid") == "True"
    ]


def make_point(
    label: str,
    family: str,
    rows: list[dict[str, str]],
    solo_ms: float,
    b_none_mb_s: float,
    replicated: bool,
) -> dict:
    if not rows:
        raise ValueError(f"no rows for point {label}")
    p99 = [number(row, "lc_p99_ms") for row in rows]
    bw = [number(row, "be_agg_mb_s") for row in rows]
    return {
        "label": label,
        "family": family,
        "n": len(rows),
        "replicated": replicated,
        "p99_med": statistics.median(p99),
        "p99_min": min(p99),
        "p99_max": max(p99),
        "amp_med": statistics.median(p99) / solo_ms,
        "amp_min": min(p99) / solo_ms,
        "amp_max": max(p99) / solo_ms,
        "bw_med": statistics.median(bw),
        "bw_min": min(bw),
        "bw_max": max(bw),
        "ret_med": statistics.median(bw) / b_none_mb_s * 100.0,
    }


def cpu_mean(run_dir: Path) -> float | None:
    values: list[float] = []
    for path in run_dir.rglob("*system_metric_summary.json"):
        try:
            doc = json.loads(path.read_text())
        except Exception:
            continue
        for sample in doc.get("samples") or []:
            cpu = sample.get("cpu") or {}
            if cpu.get("usage_valid") and cpu.get("usage_percent") is not None:
                values.append(float(cpu["usage_percent"]))
    return statistics.mean(values) if values else None


def configure_fonts() -> None:
    plt.rcParams.update(
        {
            "font.family": ["Liberation Serif", "DejaVu Serif", "serif"],
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "axes.linewidth": 0.8,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def render_main(
    points: list[dict], solo_ms: float, slo_ms: float, b_none_mb_s: float, out_dir: Path
) -> None:
    configure_fonts()
    fig, (ax, ax_bar) = plt.subplots(
        1, 2, figsize=(9.8, 4.0), gridspec_kw={"width_ratios": [1.35, 1.0]}
    )

    slo_amp = slo_ms / solo_ms
    x60 = 0.60 * b_none_mb_s

    # Panel A: absolute BE BW avoids denominator ambiguity. The upper axis maps
    # the same scale back to the D3 retention language.
    ax.axvspan(x60, 980, color="#e7f4e8", alpha=0.85, zorder=0)
    target_xfrac = max(0.0, min(1.0, (x60 - 230) / (980 - 230)))
    ax.axhspan(1.0, slo_amp, xmin=target_xfrac, xmax=1.0,
               color="#cde8d0", alpha=0.55, zorder=0)
    ax.axvline(x60, color="#57835d", lw=1.0, ls="--")
    ax.axhline(slo_amp, color="#57835d", lw=1.0, ls="--")
    ax.text(
        x60 + 10,
        2.22,
        "joint target region",
        color="#3f7047",
        fontsize=7.5,
        va="bottom",
        fontweight="bold",
    )

    by_label = {point["label"]: point for point in points}

    # Replicated frontier skeleton. Exploratory qrl points extend it with a
    # dotted line, preserving evidence status visually.
    solid_order = ["BFQ", "io_ext qrl k=.65", "None"]
    solid = [by_label[key] for key in solid_order]
    ax.plot(
        [point["bw_med"] for point in solid],
        [point["amp_med"] for point in solid],
        color="0.58",
        lw=1.2,
        zorder=1,
    )
    exploratory_order = [
        "io_ext qrl k=.65",
        "qrl k=.75 (n=1)",
        "qrl k=.85 (n=1)",
        "None",
    ]
    exploratory = [by_label[key] for key in exploratory_order]
    ax.plot(
        [point["bw_med"] for point in exploratory],
        [point["amp_med"] for point in exploratory],
        color="#b29a5a",
        lw=1.0,
        ls=":",
        zorder=1,
    )

    annotations = {
        "BFQ": (18, 10, "LC-first endpoint\n40% BE"),
        "io_ext qrl k=.65": (-8, 18, "io_ext middle\n66% BE"),
        "qrl k=.75 (n=1)": (-20, 11, "76% BE\nexploratory"),
        "qrl k=.85 (n=1)": (-16, 12, "83% BE\nexploratory"),
        "None": (-10, -32, "no isolation\n42x tail"),
    }
    marker_by_family = {"bfq": "s", "qrl": "o", "none": "D", "explore": "o"}
    for point in points:
        color = COLORS[point["family"]]
        marker = marker_by_family[point["family"]]
        face = color if point["replicated"] else "white"
        if point["replicated"]:
            ax.errorbar(
                point["bw_med"],
                point["amp_med"],
                xerr=[
                    [point["bw_med"] - point["bw_min"]],
                    [point["bw_max"] - point["bw_med"]],
                ],
                yerr=[
                    [point["amp_med"] - point["amp_min"]],
                    [point["amp_max"] - point["amp_med"]],
                ],
                fmt=marker,
                ms=7.5,
                mfc=face,
                mec="0.2",
                mew=0.7,
                color=color,
                ecolor=color,
                elinewidth=0.9,
                capsize=3,
                zorder=4,
            )
        else:
            ax.plot(
                point["bw_med"],
                point["amp_med"],
                marker=marker,
                ms=7,
                mfc=face,
                mec=color,
                mew=1.4,
                ls="None",
                zorder=4,
            )
        dx, dy, text = annotations[point["label"]]
        ax.annotate(
            text,
            (point["bw_med"], point["amp_med"]),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=7.4,
            color="0.18",
            ha="left" if dx >= 0 else "right",
        )

    ax.set_yscale("log")
    ax.set_xlim(230, 980)
    ax.set_ylim(1.0, 70)
    ax.set_xlabel("BE aggregate read bandwidth [MB/s]  (higher is better)")
    ax.set_ylabel("LC P99 amplification over solo  (log; lower is better)")
    ax.set_title("(a) io_ext fills the controllable middle of the frontier", loc="left")
    ax.grid(True, which="both", axis="y", alpha=0.22, lw=0.55)
    evidence_handles = [
        Line2D([0], [0], marker="o", color="0.45", markerfacecolor="0.45",
               markeredgecolor="0.25", lw=0, label="replicated clean runs"),
        Line2D([0], [0], marker="o", color=COLORS["explore"], markerfacecolor="white",
               markeredgewidth=1.3, lw=0, label="exploratory n=1"),
    ]
    ax.legend(handles=evidence_handles, loc="upper left", frameon=False,
              fontsize=7.0, handletextpad=0.4, borderaxespad=0.5)

    secondary = ax.secondary_xaxis(
        "top",
        functions=(
            lambda value: value / b_none_mb_s * 100.0,
            lambda value: value * b_none_mb_s / 100.0,
        ),
    )
    secondary.set_xlabel("BE retention against Phase-1 B_none [%]")
    secondary.set_xticks([30, 40, 60, 80, 100])

    # Panel B: the defensible measured claim is a 10x reduction from none to
    # qrl-k065, not a fabricated double-gate win.
    bar_order = [
        by_label["BFQ"],
        by_label["io_ext qrl k=.65"],
        by_label["qrl k=.75 (n=1)"],
        by_label["qrl k=.85 (n=1)"],
        by_label["None"],
    ]
    x_positions = np.arange(len(bar_order))
    heights = [point["amp_med"] for point in bar_order]
    colors = [COLORS[point["family"]] for point in bar_order]
    bars = ax_bar.bar(
        x_positions,
        heights,
        width=0.64,
        color=colors,
        alpha=0.92,
        edgecolor="0.25",
        linewidth=0.6,
    )
    for bar, point in zip(bars, bar_order):
        if not point["replicated"]:
            bar.set_facecolor("white")
            bar.set_edgecolor(COLORS[point["family"]])
            bar.set_linewidth(1.4)
        ax_bar.text(
            bar.get_x() + bar.get_width() / 2,
            point["amp_med"] * 1.11,
            f"{point['amp_med']:.1f}x",
            ha="center",
            va="bottom",
            fontsize=8,
            fontweight="bold" if point["label"] in {"io_ext qrl k=.65", "None"} else "normal",
        )
    ax_bar.axhline(slo_amp, color="#57835d", lw=1.0, ls="--", label="2x-solo SLO")
    ax_bar.set_yscale("log")
    ax_bar.set_ylim(1.0, 70)
    ax_bar.set_xticks(
        x_positions,
        ["BFQ\n40% BE", "qrl k=.65\n66% BE", "qrl k=.75*\n76% BE",
         "qrl k=.85*\n83% BE", "None\n101% BE"],
        fontsize=7.2,
    )
    ax_bar.set_ylabel("LC P99 / solo P99  (log)")
    ax_bar.set_title("(b) io_ext cuts no-isolation tail amplification by 10x", loc="left")
    ax_bar.grid(True, which="both", axis="y", alpha=0.22, lw=0.55)
    ax_bar.legend(loc="upper left", frameon=False, fontsize=7.2)

    fig.suptitle(
        "D3 on kvmhost: LC tail protection versus retained BE bandwidth",
        fontsize=12,
        y=1.03,
    )
    fig.text(
        0.5,
        -0.045,
        "LC solo anchor = 0.2468 ms; SLO = 0.4936 ms; Phase-1 B_none = 908.93 MB/s. "
        "BFQ: n=3 clean; qrl k=.65: n=4 clean (burst 100/250 ms); none: n=2 clean. "
        "Open qrl points are n=1 discovery evidence only. No tested point passes both frozen gates; "
        "io_ext's measured advantage is controllable coverage of the 66-83% BE operating region.",
        ha="center",
        va="top",
        fontsize=7.2,
        color="0.28",
    )
    fig.subplots_adjust(left=0.075, right=0.985, top=0.84, bottom=0.22, wspace=0.28)

    for ext in ("png", "pdf"):
        fig.savefig(
            out_dir / f"fig_d3_kvmhost_main.{ext}",
            dpi=320 if ext == "png" else None,
            bbox_inches="tight",
        )
    plt.close(fig)


def render_cpu(cpu: dict[str, dict], out_dir: Path) -> None:
    configure_fonts()
    labels = ["None", "BFQ", "io_ext qrl k=.65"]
    keys = ["none", "bfq", "qrl"]
    cpu_values = [cpu[key]["cpu"] for key in keys]
    cpu_yerr = [
        [cpu[key]["cpu"] - cpu[key]["cpu_min"] for key in keys],
        [cpu[key]["cpu_max"] - cpu[key]["cpu"] for key in keys],
    ]
    efficiencies = [cpu[key]["bw"] / cpu[key]["cpu"] for key in keys]
    efficiency_yerr = [
        [cpu[key]["bw"] / cpu[key]["cpu"] - cpu[key]["bw"] / cpu[key]["cpu_max"]
         for key in keys],
        [cpu[key]["bw"] / cpu[key]["cpu_min"] - cpu[key]["bw"] / cpu[key]["cpu"]
         for key in keys],
    ]
    colors = [COLORS["none"], COLORS["bfq"], COLORS["qrl"]]

    fig, (ax, ax_eff) = plt.subplots(1, 2, figsize=(7.6, 3.05))
    x_positions = np.arange(3)
    bars = ax.bar(
        x_positions,
        cpu_values,
        yerr=cpu_yerr,
        color=colors,
        edgecolor="0.25",
        linewidth=0.6,
        width=0.62,
        capsize=3,
        error_kw={"elinewidth": 0.8, "capthick": 0.8, "ecolor": "0.25"},
    )
    for bar, value, key in zip(bars, cpu_values, keys):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 1.2,
            f"{value:.1f}%\n{cpu[key]['bw']:.0f} MB/s",
            ha="center",
            fontsize=7.5,
        )
    ax.set_xticks(x_positions, labels)
    ax.set_ylim(0, 60)
    ax.set_ylabel("Mean guest CPU utilization [%]")
    ax.set_title("(a) CPU cost of the measured operating point", loc="left")
    ax.grid(True, axis="y", alpha=0.22, lw=0.5)

    bars_eff = ax_eff.bar(
        x_positions,
        efficiencies,
        yerr=efficiency_yerr,
        color=colors,
        edgecolor="0.25",
        linewidth=0.6,
        width=0.62,
        capsize=3,
        error_kw={"elinewidth": 0.8, "capthick": 0.8, "ecolor": "0.25"},
    )
    for bar, value in zip(bars_eff, efficiencies):
        ax_eff.text(
            bar.get_x() + bar.get_width() / 2,
            value + 0.6,
            f"{value:.1f}",
            ha="center",
            fontsize=8,
        )
    ax_eff.set_xticks(x_positions, labels)
    ax_eff.set_ylim(0, 28)
    ax_eff.set_ylabel("BE MB/s per CPU percentage point")
    ax_eff.set_title("(b) CPU efficiency: reported as cost, not an io_ext win", loc="left")
    ax_eff.grid(True, axis="y", alpha=0.22, lw=0.5)

    fig.suptitle("D3 kvmhost CPU accounting: protection is not free", fontsize=11, y=1.04)
    fig.text(
        0.5,
        -0.055,
        "Healthy-run means from system_metric_summary.json (84 valid 1-s CPU samples/run). "
        "None: n=2; BFQ: n=3; qrl k=.65: n=4. The io_ext qrl path pays about +14 CPU points "
        "over none; this figure is an honest overhead disclosure, not an advantage claim.",
        ha="center",
        va="top",
        fontsize=7.0,
        color="0.3",
    )
    fig.subplots_adjust(left=0.09, right=0.985, bottom=0.22, top=0.80, wspace=0.34)
    for ext in ("png", "pdf"):
        fig.savefig(
            out_dir / f"fig_d3_kvmhost_cpu_cost.{ext}",
            dpi=320 if ext == "png" else None,
            bbox_inches="tight",
        )
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "experiments/d3_kvmhost/figures",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    anchor = json.loads(CALIB.read_text())
    solo_ms = float(anchor["solo_p99_ms_median"])
    slo_ms = float(anchor["slo_batch_ms"])
    b_none_mb_s = float(anchor["b_none_mb_s"])
    v1 = load_csv(RUNS_V1)
    v2 = load_csv(RUNS_V2)

    points = [
        make_point("BFQ", "bfq", clean_rows(v2, {"bfqf"}), solo_ms, b_none_mb_s, True),
        make_point(
            "io_ext qrl k=.65",
            "qrl",
            clean_rows(v2, {"qrl_k065_b100", "qrl_k065_b250"}),
            solo_ms,
            b_none_mb_s,
            True,
        ),
        make_point(
            "qrl k=.75 (n=1)",
            "explore",
            clean_rows(v1, {"qrl_k075"}),
            solo_ms,
            b_none_mb_s,
            False,
        ),
        make_point(
            "qrl k=.85 (n=1)",
            "explore",
            clean_rows(v1, {"qrl_k085"}),
            solo_ms,
            b_none_mb_s,
            False,
        ),
        make_point("None", "none", clean_rows(v2, {"none"}), solo_ms, b_none_mb_s, True),
    ]
    render_main(points, solo_ms, slo_ms, b_none_mb_s, args.out_dir)

    cpu_groups = {
        "none": [
            ROOT / "results/d3_kvmhost/discovery2/D3PV_none_run2",
            ROOT / "results/d3_kvmhost/discovery2/D3PV_none_run3",
        ],
        "bfq": [
            ROOT / f"results/d3_kvmhost/discovery2/D3PV_bfqf_run{idx}" for idx in (1, 2, 3)
        ],
        "qrl": [
            ROOT / f"results/d3_kvmhost/discovery2/D3PV_qrl_k065_b{burst}_run{idx}"
            for burst in ("100", "250")
            for idx in (2, 3)
        ],
    }
    cpu_bw = {"none": 920.11, "bfq": 366.83, "qrl": 604.46}
    cpu: dict[str, dict] = {}
    for key, run_dirs in cpu_groups.items():
        values = [cpu_mean(run_dir) for run_dir in run_dirs]
        values = [value for value in values if value is not None]
        cpu[key] = {
            "cpu": statistics.mean(values),
            "cpu_min": min(values),
            "cpu_max": max(values),
            "bw": cpu_bw[key],
            "n": len(values),
        }
    render_cpu(cpu, args.out_dir)

    meta = {
        "generated_from": {
            "anchor": str(CALIB.relative_to(ROOT)),
            "runs_v1": str(RUNS_V1.relative_to(ROOT)),
            "runs_v2": str(RUNS_V2.relative_to(ROOT)),
        },
        "solo_ms": solo_ms,
        "slo_ms": slo_ms,
        "b_none_mb_s": b_none_mb_s,
        "points": points,
        "cpu": cpu,
        "evidence_policy": (
            "solid=replicated clean; hollow=exploratory n=1; CPU is overhead disclosure"
        ),
    }
    (args.out_dir / "fig_d3_kvmhost.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"rendered {args.out_dir}/fig_d3_kvmhost_main.{{png,pdf}}")
    print(f"rendered {args.out_dir}/fig_d3_kvmhost_cpu_cost.{{png,pdf}}")


if __name__ == "__main__":
    main()
