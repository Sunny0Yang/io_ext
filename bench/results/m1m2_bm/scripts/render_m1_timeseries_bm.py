#!/usr/bin/env python3
"""m1 补充时序图（bm 版）：四负载逐秒定量时序，替代 cache=on 下区分度变弱的
dense flush strip。

默认 2×2 负载面板；--layout 1x4 为 3/5 正文宽的一行四列版。
保留完整 measurement 窗口
（与 fingerprints 图同一口径 phase/repeat=1）：
  - 主轴（log）: 逐秒 read / write MiB/s（diskstats sectors delta, 512B 扇区）
  - 右轴（linear）: inflight（灰实线，1Hz ios_in_progress）
  - 顶部 lane: flush/s（红 step，各面板独立线性刻度；sampler @m1_sec_flush
    逐秒 map；老版回退全场景 scalar 均摊）
论文名称仅表示应用/标准 workload，线程数和 phase 是配置而非名称。
Run:
  python3 experiments/m1m2_bm/scripts/render_m1_timeseries_bm.py
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
import numpy as np

LEGEND_GAP = 0.006
PANEL_TITLE_FP = FontProperties(family="Liberation Serif", size=9, weight="bold")
import pandas as pd

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[3]
DEFAULT_ROOT = REPO / "experiments/m1m2_bm/runs/m1_campaign/m1cacheon_bm_20261105"
M1_TOOLS = REPO / "results/motivation_m1/tools"
sys.path.insert(0, str(M1_TOOLS))
import m1_metrics as mm  # noqa: E402

C_READ, C_WRITE, C_FLUSH, C_INFL = "#0072B2", "#D55E00", "#d73027", "0.35"
DEV = "nvme0n1"

plt.rcParams.update({
    "font.family": "Liberation Serif",
    "font.size": 8.5,
    "axes.titlesize": 8.5,
    "axes.labelsize": 7.5,
    "axes.spines.top": False,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "pdf.fonttype": 42,
})
MM = 1.0 / 25.4
TEXT_W = 174 * MM
FIG_W = TEXT_W * 0.62
LANDSCAPE_W = TEXT_W * 0.60

ROWS = [
    ("YCSB-A", "M1CACHE_YCSB_a_r1", "YCSB_a", "run"),
    ("YCSB-C", "M1CACHE_YCSB_c_r1", "YCSB_c", "run"),
    ("db_bench", "M1CACHE_DBB_rww_r1", "DBB_rww", "readwhilewriting"),
    ("Shore", "M1CACHE_SHORE_qps50_r1", "SHORE_qps50", "roi"),
]
def win_row(csv: Path, kind: str, phase: str, repeat: int = 1):
    df = pd.read_csv(csv)
    r = df[(df["kind"] == kind) & (df["phase"] == phase) & (df["repeat"] == repeat)
           & (df["cache_on"] == 1) & (df["gate_ok"] == True)].iloc[0]  # noqa: E712
    return float(r["window_start"]), float(r["window_end"])


def series(root: Path, kind: str, phase: str):
    """per-second arrays: t(rel s), rd_mibs, wr_mibs, inflight, flush_s"""
    case = root / {"YCSB_a": "M1CACHE_YCSB_a_r1", "YCSB_c": "M1CACHE_YCSB_c_r1",
                   "DBB_rww": "M1CACHE_DBB_rww_r1", "SHORE_qps50": "M1CACHE_SHORE_qps50_r1"}[kind]
    ws, we = win_row(root / "m1_cache_cases.csv", kind, phase)
    j = json_load(case / "step_0/ib_system_metric/ib_system_metric_summary.json")
    S = [s for s in j.get("samples") or [] if ws <= s["timestamp"] < we]
    if not S:
        raise ValueError(f"empty system-metric window for {kind}/{phase}")
    t = np.array([s["timestamp"] for s in S]) - ws
    rd, wr, infl = [], [], []
    for s in S:
        d = s["disk"]["devices"].get(DEV) or {}
        rd.append(d.get("read_sectors_delta", 0) * 512 / 1048576)
        wr.append(d.get("write_sectors_delta", 0) * 512 / 1048576)
        infl.append(d.get("ios_in_progress", 0))
    rd, wr, infl = np.array(rd), np.array(wr), np.array(infl, dtype=float)

    # flush/s: prefer per-second map (.bt newer); fall back to whole-run scalar spread
    sampler = mm.parse_sampler_log(str(_log(case)))
    sec_f = sampler["sec"].get("m1_sec_flush") or {}
    fl = mm.per_second_counts(sec_f, sampler["boot_ns0"], sampler["epoch0"], ws, we)
    if len(fl) < 2:
        whole = int(sampler["scalars"].get("m1_flush_issue_count") or 0)
        scwall = _scene_wall(case, kind)
        mean = whole / scwall if scwall else 0.0
        fl = [mean] * max(int(we - ws), 1)
        t_f = np.arange(len(fl), dtype=float) + math.ceil(ws) - ws
    else:
        # Both sources use window_start as t=0; counts cover full epoch seconds.
        t_f = np.arange(len(fl), dtype=float) + math.ceil(ws) - ws
    fl = np.array(fl, dtype=float)
    return t, rd, wr, infl, t_f, fl


def json_load(p: Path):
    import json
    with open(p) as f:
        return json.load(f)


def _log(case: Path) -> Path:
    for sub in ("step_0/m1_pattern_sampler", "monitor/m1_pattern_sampler"):
        hits = sorted(case.glob(f"{sub}/m1_pattern_sampler.bpftrace.log"))
        if hits:
            return hits[-1]
        hits_gz = sorted(case.glob(f"{sub}/m1_pattern_sampler.bpftrace.log.gz"))
        if hits_gz:
            return hits_gz[-1]
    raise FileNotFoundError(case)


def _scene_wall(case: Path, kind: str) -> float | None:
    import json
    pref = {"YCSB_a": "ycsb_rocksdb", "YCSB_c": "ycsb_rocksdb",
            "DBB_rww": "dbbench", "SHORE_qps50": "tailbench_shore"}[kind]
    hits = sorted((case / "step_0").glob(f"*_{pref}_*.json")) + sorted((case / "scene").glob(f"*/*_{pref}_*.json"))
    if not hits:
        return None
    j = json.loads(hits[-1].read_text())
    cd = j.get("collected_data") or j
    st, et = cd.get("start_time"), cd.get("end_time")
    if st and et:
        return float(et) - float(st)
    return cd.get("duration_wall")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--fig-dir", type=Path, default=REPO / "experiments/m1m2_bm/figs")
    ap.add_argument("--name", default="m1_timeseries_bm")
    ap.add_argument("--layout", choices=("compact", "landscape", "1x4"), default="compact")
    args = ap.parse_args()

    landscape = args.layout == "landscape"
    single_row = args.layout == "1x4"
    if args.layout != "compact" and args.name == "m1_timeseries_bm":
        args.name = f"m1_timeseries_bm_{args.layout}"
    fig_w = LANDSCAPE_W if landscape or single_row else FIG_W
    fig_h = (44 if single_row else 88 if landscape else 92) * MM
    margins = ((0.12, 0.895, 0.70, 0.24) if single_row else
               (0.14, 0.86, 0.80, 0.13) if landscape else (0.14, 0.86, 0.82, 0.17))
    hspace, wspace = (0, 0.22) if single_row else (0.9, 0.52) if landscape else (0.42, 0.52)
    flush_main_ratio = (0.14, 1.0) if single_row else (0.16, 1.0)
    fig = plt.figure(figsize=(fig_w, fig_h))
    outer = fig.add_gridspec(1 if single_row else 2, 4 if single_row else 2,
                             left=margins[0], right=margins[1],
                             top=margins[2], bottom=margins[3],
                             hspace=hspace, wspace=wspace)
    legend_handles = [
        plt.Line2D([], [], color=C_READ, lw=1.2, label="read" if landscape or single_row else "read (MiB/s)"),
        plt.Line2D([], [], color=C_WRITE, lw=1.2, label="write" if landscape or single_row else "write (MiB/s)"),
        plt.Line2D([], [], color=C_INFL, lw=1.1, label="in-flight" if single_row else "in-flight (right)"),
        plt.Line2D([], [], color=C_FLUSH, lw=1.1, label="flush/s (top)" if single_row else "flush/s (top lane)"),
    ]
    panel_axes: list[tuple] = []
    for i, (label, _cid, kind, phase) in enumerate(ROWS):
        row, col = (0, i) if single_row else divmod(i, 2)
        sub = outer[row, col].subgridspec(2, 1, height_ratios=list(flush_main_ratio), hspace=0.06 if single_row else 0.08)
        ax = fig.add_subplot(sub[1])
        rug = fig.add_subplot(sub[0], sharex=ax)
        t, rd, wr, infl, t_f, fl = series(args.root, kind, phase)
        ws, we = win_row(args.root / "m1_cache_cases.csv", kind, phase)
        ax.plot(t, rd, color=C_READ, lw=0.8, label="read")
        ax.plot(t, wr, color=C_WRITE, lw=0.8, label="write")
        ax.set_yscale("log")
        ax.set_ylim(0.005, 5000)
        ax.set_yticks([0.01, 1, 100])
        ax.grid(axis="y", color="#eeeeee", lw=0.35)
        ax.set_xlim(0, we - ws)
        rug.set_xlim(0, we - ws)
        if single_row:
            # Sparse whole-window ticks keep four narrow panels readable at 104 mm.
            duration = we - ws
            end_tick = math.floor(duration / 10) * 10
            ax.set_xticks([0, end_tick])
            ax.get_xticklabels()[0].set_ha("left")
            ax.get_xticklabels()[-1].set_ha("right")
        else:
            ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=3))
        ax.tick_params(labelsize=8 if single_row else 9 if landscape else 7.2, pad=2)
        if col == 0:
            ax.set_ylabel("MiB/s", fontsize=10 if landscape or single_row else 8, labelpad=2)
        else:
            ax.tick_params(labelleft=False)
        if not single_row:
            ax.set_xlabel("time (s)", fontsize=10 if landscape else 8.5, labelpad=2)
        title_loc = "center" if single_row else "left"
        title_fp = PANEL_TITLE_FP if single_row else FontProperties(
            family="Liberation Serif", size=11 if landscape else 9, weight="bold",
        )
        rug.set_title(label, fontproperties=title_fp, loc=title_loc, pad=2)
        # Each lane has its own explicitly labelled linear scale, not a shared rate.
        if len(fl):
            edges = np.r_[t_f, min(t_f[-1] + 1, we - ws)]
            rug.stairs(fl, edges, fill=True, color=C_FLUSH, alpha=0.75, lw=0)
        peak = float(fl.max()) if len(fl) else 0.0
        rug.set_ylim(0, max(peak * 1.15, 1.0))
        rug.set_yticks([peak] if peak else [0])
        rug.set_yticklabels([f"{peak:.0f}" if peak >= 10 else f"{peak:.1f}"])
        rug.tick_params(axis="y", labelsize=8 if landscape else 6.5,
                        colors=C_FLUSH, length=0, pad=2)
        if single_row:
            rug.set_yticks([])
            rug.text(0.98, 0.5, f"{peak:.0f}", transform=rug.transAxes, ha="right",
                     va="center", fontsize=7.5, color="#8b0000",
                     bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=0.2))
        rug.yaxis.tick_right()
        rug.tick_params(axis="x", labelbottom=False, length=0)
        for sp in rug.spines.values():
            sp.set_color("#e8c4bc")
        rug.set_facecolor("#fdf6f5")
        ax2 = ax.twinx()
        ax2.plot(t, infl, color=C_INFL, lw=0.7, alpha=0.85, label="in-flight")
        ax2.set_ylim(0, 800)
        ax2.set_yticks([0, 400, 800])
        ax2.tick_params(labelsize=8 if single_row else 9 if landscape else 6.5, length=0)
        if single_row:
            ax2.tick_params(labelright=(col == 3))
            if col == 3:
                ax2.set_ylabel("in-flight", fontsize=10, labelpad=2)
        elif col == 1:
            ax2.set_ylabel("in-flight", fontsize=10 if landscape else 7, labelpad=2)
        ax2.spines["top"].set_visible(False)
        panel_axes.append((ax, rug, ax2))

    fig.canvas.draw()
    for ax, rug, ax2 in panel_axes:
        pos = ax.get_position()
        rug_pos = rug.get_position()
        rug.set_position([pos.x0, rug_pos.y0, pos.width, rug_pos.height])
        ax2.set_position(pos)
        ax2.set_xlim(ax.get_xlim())

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    title_tops = [
        rug.title.get_window_extent(renderer).y1 / fig.bbox.height
        for _, rug, _ in panel_axes
    ]
    anchor_y = max(title_tops) + LEGEND_GAP
    legend = fig.legend(
        handles=legend_handles,
        loc="lower center",
        ncol=4 if single_row else 2,
        frameon=False,
        fontsize=9 if single_row else (9 if landscape else 7.5),
        handlelength=1.05 if single_row else 2.0,
        handletextpad=0.28 if single_row else 0.4,
        columnspacing=0.55 if single_row else 1.0,
        bbox_to_anchor=(0.5, anchor_y),
    )
    legend.set_in_layout(False)

    if single_row:
        fig.supxlabel("time (s)", y=0.04, fontsize=9.5)
    args.fig_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        # Fixed canvas preserves the intended two-thirds text width.
        fig.savefig(args.fig_dir / f"{args.name}.{ext}", dpi=300)
    plt.close(fig)
    print(f"wrote {args.fig_dir / (args.name + '.png')} (+pdf)")


if __name__ == "__main__":
    main()
