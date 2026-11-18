#!/usr/bin/env python3
"""Eight square panels: io.max / io.weight / kyber / io.latency / io.cost measured; rest TBD."""

from __future__ import annotations

import csv
import gzip
import json
import re
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patheffects as pe
from matplotlib.patches import Patch

from iocost_common import IOCOST_SLO_MS, io_cost_run, lc_p99_ms as iocost_lc_ms
from iocost_curated import IOCOST_CURATED_REPS
from iocost_curated import PHASE_LIST as IOCOST_PHASES
from kyber_curated import KYBER_CURATED_REPS
from kyber_common import kyber_run, lc_read_p99_ms
from iolat_curated import IOLAT_CURATED_REPS
from mq_common import bar_us, curated_values
from mq_curated import bar_value as mq_bar_value

REPO = Path(__file__).resolve().parents[3]
RESULTS = REPO / "experiments/reproduction/RESULTS"
CAMPAIGN = REPO / "experiments/reproduction/results/campaign"
RERUN1 = REPO / "experiments/reproduction/results/rerun1"
D2FQ_FIX = REPO / "experiments/reproduction/results/d2fqfix"
D2FQ_OUTLIER_REP = 2
D2FQ_TARGET = 0.8
OUT = Path(__file__).resolve().parents[1] / "figures"
IOLAT_SLO_MS = 12.0

# Square panel cell (in); height matches d1 paper strip for future side-by-side row.
PANEL_CELL_IN = 2.05
STRIP_NCOL = 4
STRIP_NROW = 2
STRIP_FIG_W_SCALE = 1.12
STRIP_FIG_H_SCALE = 1.30
STRIP_LEGEND_Y = 0.972
STRIP_TOP = 0.835
STRIP_BOTTOM = 0.14
STRIP_LEFT = 0.155

FS_TITLE = 12.0
FS_LABEL = 10.5
FS_TICK = 9.5
FS_LEGEND = 10.0
STRIP_FS_LEGEND = 11.0
FS_TBD = 20.0
FS_INBAR = 9.0


def apply_row_composite_typography() -> None:
    """Larger type when reproduction strip is embedded in the wide d1_row figure."""
    global FS_INBAR, FS_TICK, FS_LABEL, FS_TITLE
    FS_INBAR = 10.5
    FS_TICK = 11.0
    FS_LABEL = 11.5
    FS_TITLE = 12.0


def _bar_value_label(
    ax,
    x: float,
    text: str,
    *,
    y_top: float,
    yspan: float,
    inside_center: float | None = None,
) -> None:
    """Value on or above a bar; white stroke keeps digits readable on hatch/color."""
    if inside_center is not None and (y_top - inside_center) >= yspan * 0.14:
        y, va = inside_center, "center"
    else:
        y, va = y_top + yspan * 0.025, "bottom"
    t = ax.text(
        x,
        y,
        text,
        ha="center",
        va=va,
        fontsize=FS_INBAR,
        color="0.06",
        fontweight="bold",
        clip_on=False,
        zorder=6,
    )
    t.set_path_effects([pe.withStroke(linewidth=2.8, foreground="white", alpha=0.92)])

ARM_COLOR = {"none": "#7f7f7f", "native": "#d95f02", "iox": "#1f78b4"}
ARM_LABEL = {"none": "none", "native": "native", "iox": "io_ext"}
LC_HATCH = ""
BE_HATCH = "///"

PANELS = [
    ("a", "io.max", "measured_io_max"),
    ("b", "io.weight", "measured_io_weight"),
    ("c", "mq-deadline", "measured_mq_deadline"),
    ("d", "kyber", "measured_kyber"),
    ("e", "io.latency", "measured_io_latency"),
    ("f", "io.cost", "measured_io_cost"),
    ("g", "D2FQ", "measured_d2fq"),
]
# 2×4 grid (standalone 7-panel strip): row0 a–d ; row1 e–g (slot 7 unused).
PANEL_GRID_SLOT: dict[str, int] = {
    "a": 0,
    "b": 1,
    "c": 2,
    "d": 3,
    "e": 4,
    "f": 5,
    "g": 6,
}

PHASES_KYBER = (("p1", 0, 30), ("p2", 30, 60), ("p3", 60, 90))
PHASES_IOLAT = ("p1", "p2", "p3")
PHASE_TICK_LABELS = ("T1", "T2", "T3")
# One caption under (d)(e)(f); no per-panel xlabel (kyber LC read, iolat/iocost LC write).
PHASE_STEP_FOOTER = "LC 4k QD32→64→32 · BE 128k write QD16"
PHASE_BAR_WIDTH = 0.24
MQ_XLABEL = "randrw 70/30"


def _style_phase_bar_panel(
    ax,
    *,
    ylabel: str,
    ymax: float,
    slo_ms: float | None = None,
    n_phases: int = 3,
) -> None:
    """Categorical T1–T3 grouped bars; no fake 0–90 s time axis."""
    if slo_ms is not None:
        ax.axhline(slo_ms, color="0.2", ls=":", lw=0.75, alpha=0.85, zorder=1)
    ax.set_xlim(-0.55, n_phases - 0.45)
    ax.set_ylim(0, ymax * 1.12 if ymax > 0 else 1.0)
    ax.set_xticks(np.arange(n_phases))
    ax.set_xticklabels(PHASE_TICK_LABELS[:n_phases], fontsize=FS_TICK)
    ax.set_xlabel("")
    ax.set_ylabel(ylabel, fontsize=FS_LABEL)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", alpha=0.28, linestyle=":")
    _square(ax)


def _draw_phase_grouped_bars(
    ax,
    phase_med: dict[str, list[float]],
    *,
    ylabel: str,
    slo_ms: float | None,
) -> float:
    """Grouped bars: x = phase, hue = arm."""
    arms = ("none", "native", "iox")
    n_ph = len(PHASE_TICK_LABELS)
    x = np.arange(n_ph, dtype=float)
    offsets = (-PHASE_BAR_WIDTH, 0.0, PHASE_BAR_WIDTH)
    ymax = 0.0
    for off, arm in zip(offsets, arms):
        pts = phase_med.get(arm, [])
        if not pts or any(v != v for v in pts):
            continue
        ymax = max(ymax, max(pts))
        ax.bar(
            x + off,
            pts,
            width=PHASE_BAR_WIDTH * 0.92,
            color=ARM_COLOR[arm],
            edgecolor="0.28",
            linewidth=0.45,
            zorder=2,
        )
    _style_phase_bar_panel(ax, ylabel=ylabel, ymax=ymax, slo_ms=slo_ms, n_phases=n_ph)
    return ymax


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else float("nan")


def load_io_max() -> dict[str, float]:
    p = RESULTS / "io_max/data.io_max.csv"
    by: dict[str, list[float]] = {a: [] for a in ARM_COLOR}
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("t_s") != "mean_whole_run":
                continue
            by[row["arm"]].append(float(row["bw_mib_s"]))
    return {a: _median(v) for a, v in by.items()}


def _fio_write_mib(path: Path) -> float:
    if not path.is_file():
        return float("nan")
    data = json.loads(path.read_text())
    for jo in data.get("jobs", []):
        bw = jo.get("write", {}).get("bw")
        if bw is not None:
            return float(bw) / 1024.0
    return float("nan")


def load_tenant_bw() -> dict[str, dict[str, float]]:
    acc: dict[str, list[tuple[float, float]]] = {a: [] for a in ARM_COLOR}
    for rep in range(1, 6):
        for arm in ARM_COLOR:
            d = CAMPAIGN / "io_weight" / f"rep{rep}" / arm
            lc = _fio_write_mib(d / "fio_w_lc.json")
            be = _fio_write_mib(d / "fio_w_be.json")
            if lc == lc and be == be and lc + be > 0:
                acc[arm].append((lc, be))
    out: dict[str, dict[str, float]] = {}
    for arm, pairs in acc.items():
        lc_m = _median([p[0] for p in pairs])
        be_m = _median([p[1] for p in pairs])
        out[arm] = {
            "lc_mib": lc_m,
            "be_mib": be_m,
            "total_mib": lc_m + be_m,
            "be_share": be_m / (lc_m + be_m),
            "lc_share": lc_m / (lc_m + be_m),
        }
    return out


def _square(ax) -> None:
    """Panel geometry comes from the strip grid; box_aspect would shift axes per y-range."""
    return


def _align_strip_ylabels(axes, used_slots: set[int]) -> None:
    """Column-wise y-axis title alignment (top/bottom); wide left margin avoids tick overlap."""
    first = next(axes[i] for i in sorted(used_slots))
    parent = first.get_figure(root=False)
    for top, bot in ((0, 4), (1, 5), (2, 6), (3, 7)):
        pair = [axes[i] for i in (top, bot) if i in used_slots]
        if len(pair) > 1:
            parent.align_ylabels(pair)
    for i in sorted(used_slots):
        ax = axes[i]
        ax.tick_params(axis="y", labelsize=FS_TICK, pad=2)
        for tick in ax.get_yticklabels():
            tick.set_horizontalalignment("right")


def draw_io_max(ax, vals: dict[str, float]) -> dict:
    from matplotlib.ticker import MaxNLocator, ScalarFormatter

    arms = ("none", "native", "iox")
    xs = [0.0, 1.0, 2.0]
    width = 0.48
    ymax = max(vals.values())
    for x, arm in zip(xs, arms):
        v = vals[arm]
        ax.bar(
            x,
            v,
            width=width,
            color=ARM_COLOR[arm],
            edgecolor="0.28",
            linewidth=0.55,
            zorder=2,
        )
        yspan = ymax * 1.22
        _bar_value_label(ax, x, f"{v:.1f}", y_top=v, yspan=yspan)
    ax.set_ylim(0, ymax * 1.22)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=False))
    fmt = ScalarFormatter(useMathText=False)
    fmt.set_scientific(False)
    ax.yaxis.set_major_formatter(fmt)
    ax.set_ylabel("BE write (MiB/s)", fontsize=FS_LABEL)
    ax.set_xlabel("32 MiB/s cap", fontsize=FS_LABEL, labelpad=4)
    ax.set_xticks(xs)
    ax.set_xticklabels([ARM_LABEL[a] for a in arms], fontsize=FS_TICK)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", alpha=0.35, linestyle=":")
    _square(ax)
    return {"kind": "io_max", "values_mib": vals, "yscale": "linear"}


def draw_io_weight(ax, data: dict[str, dict[str, float]]) -> dict:
    arms = ("none", "native", "iox")
    xs = [0.0, 1.0, 2.0]
    width = 0.48
    ymax = 0.0
    meta = {}
    for x, arm in zip(xs, arms):
        row = data[arm]
        lc, be = row["lc_mib"], row["be_mib"]
        total = lc + be
        meta[arm] = row
        ymax = max(ymax, total)
        lc_pct = int(round(row["lc_share"] * 100))
        be_pct = int(round(row["be_share"] * 100))
        ax.bar(
            x,
            lc,
            width=width,
            color=ARM_COLOR[arm],
            hatch=LC_HATCH,
            edgecolor="0.28",
            linewidth=0.55,
            zorder=1,
        )
        ax.bar(
            x,
            be,
            width=width,
            bottom=lc,
            color=ARM_COLOR[arm],
            hatch=BE_HATCH,
            edgecolor="0.28",
            linewidth=0.55,
            zorder=2,
        )
        yspan = ymax * 1.18
        if lc > 0:
            _bar_value_label(
                ax,
                x,
                f"{lc_pct}%",
                y_top=lc,
                yspan=yspan,
                inside_center=lc * 0.5,
            )
        if be > 0:
            _bar_value_label(
                ax,
                x,
                f"{be_pct}%",
                y_top=lc + be,
                yspan=yspan,
                inside_center=lc + be * 0.5,
            )
    ax.set_ylabel("stacked write (MiB/s)", fontsize=FS_LABEL)
    ax.set_xlabel("LC:BE = 100:400", fontsize=FS_LABEL, labelpad=4)
    ax.set_xticks(xs)
    ax.set_xticklabels([ARM_LABEL[a] for a in arms], fontsize=FS_TICK)
    ax.set_ylim(0, ymax * 1.18)
    ax.set_xlim(-0.55, 2.55)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", alpha=0.3)
    _square(ax)
    return {"kind": "io_weight_stack", "arms": meta}


def draw_tbd(ax) -> None:
    ax.set_axis_off()
    _square(ax)
    ax.text(
        0.5,
        0.5,
        "TBD",
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=FS_TBD,
        color="0.5",
        fontweight="bold",
    )


def _read_text_maybe_gz(path: Path) -> str | None:
    if not path.is_file():
        return None
    if path.suffix == ".gz":
        with gzip.open(path, "rt", errors="replace") as f:
            return f.read()
    return path.read_text(errors="replace")


def _load_jsonl(path: Path) -> list[dict]:
    txt = _read_text_maybe_gz(path)
    if not txt:
        return []
    out: list[dict] = []
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _fio_read_p99_us(path: Path) -> float:
    if not path.is_file():
        return float("nan")
    data = json.loads(path.read_text())
    for jo in data.get("jobs", []):
        v = jo.get("read", {}).get("clat_ns", {}).get("percentile", {}).get("99.000000")
        if v is not None:
            return float(v) / 1000.0
    return float("nan")


def _fio_write_p99_us(path: Path) -> float:
    if not path.is_file():
        return float("nan")
    data = json.loads(path.read_text())
    for jo in data.get("jobs", []):
        v = jo.get("write", {}).get("clat_ns", {}).get("percentile", {}).get("99.000000")
        if v is not None:
            return float(v) / 1000.0
    return float("nan")


def _fio_write_bw_mib(path: Path) -> float:
    if not path.is_file():
        return float("nan")
    data = json.loads(path.read_text())
    for jo in data.get("jobs", []):
        bw = jo.get("write", {}).get("bw")
        if bw is not None:
            return float(bw) / 1024.0
    return float("nan")


def _kyber_run(arm: str, rep: int) -> Path:
    return RERUN1 / "kyber" / f"kyber_{arm}_r{rep}"


def _iolat_run(arm: str, rep: int) -> Path:
    return RERUN1 / "io_latency" / f"io_latency_{arm}_r{rep}"


def _armed_unix(run: Path) -> float | None:
    for name in ("state_snapshot.log.gz", "state_snapshot.log"):
        txt = _read_text_maybe_gz(run / name)
        if not txt:
            continue
        m = re.search(r"^== armed ([\d.]+)", txt, re.M)
        if m:
            return float(m.group(1))
    return None


def kyber_phase_p99_median() -> dict[str, list[float]]:
    """Per-phase LC read p99 (ms), median of curated reps."""
    out: dict[str, list[float]] = {a: [] for a in ARM_COLOR}
    for arm in ARM_COLOR:
        for ph, _, _ in PHASES_KYBER:
            reps = KYBER_CURATED_REPS.get((ph, arm), ())
            vals = [lc_read_p99_ms(kyber_run(arm, r), ph) for r in reps]
            vals = [v for v in vals if v == v]
            out[arm].append(_median(vals))
    return out


def kyber_iox_depth_median() -> tuple[np.ndarray, np.ndarray] | None:
    grid = np.arange(0, 91, 1.0)
    mats: list[np.ndarray] = []
    for rep in range(1, 6):
        run = _kyber_run("iox", rep)
        recs = _load_jsonl(run / "loader_q_kyber.stdout")
        recs = [r for r in recs if r.get("pol") == "q_kyber" and r.get("hctx")]
        if not recs:
            continue
        attach = next((r["t_ms"] for r in recs if r.get("event") == "attach"), recs[0]["t_ms"])
        j = json.loads((run / "fio_p1_lc.json").read_text())
        t1 = j["timestamp"] - 30
        aw = _armed_unix(run)
        if aw is None:
            continue
        base = (aw - 1.0) - t1
        ser: list[tuple[float, float]] = []
        for r in recs:
            dl0 = [x["dl"][0] for x in r["hctx"]]
            t_rel = base + (r["t_ms"] - attach) / 1000.0
            if -3 <= t_rel <= 95:
                ser.append((t_rel, float(np.median(dl0))))
        if not ser:
            continue
        tt = np.array([t for t, _ in ser])
        vv = np.array([v for _, v in ser])
        order = np.argsort(tt)
        mats.append(np.interp(grid, tt[order], vv[order], left=np.nan, right=np.nan))
    if not mats:
        return None
    return grid, np.nanmedian(np.vstack(mats), axis=0)


def kyber_native_depth_median() -> tuple[np.ndarray, np.ndarray] | None:
    grid = np.arange(0, 91, 1.0)
    mats: list[np.ndarray] = []
    rx = re.compile(r"^\s*([\d.]+)\s+(\d+)\s+(\d+)\s*$")
    for rep in range(1, 6):
        run = _kyber_run("native", rep)
        path = run / "native_kyber_depth.log.gz"
        txt = _read_text_maybe_gz(path)
        j = json.loads((run / "fio_p1_lc.json").read_text())
        t1 = j["timestamp"] - 30
        if not txt:
            continue
        ser: list[tuple[float, float]] = []
        for line in txt.splitlines():
            m = rx.match(line)
            if not m:
                continue
            t_rel = float(m.group(1)) - t1
            if -3 <= t_rel <= 95:
                ser.append((t_rel, float(m.group(2))))
        if not ser:
            continue
        tt = np.array([t for t, _ in ser])
        vv = np.array([v for _, v in ser])
        order = np.argsort(tt)
        mats.append(np.interp(grid, tt[order], vv[order], left=np.nan, right=np.nan))
    if not mats:
        return None
    return grid, np.nanmedian(np.vstack(mats), axis=0)


def draw_kyber(ax) -> dict:
    """LC read p99 — per-phase grouped bars."""
    phase_med = kyber_phase_p99_median()
    _draw_phase_grouped_bars(
        ax,
        phase_med,
        ylabel="LC read p99 (ms)",
        slo_ms=None,
    )
    return {
        "kind": "kyber_lc_read_p99_phase_bars",
        "source": str(RERUN1 / "kyber"),
        "p99": "per-phase fio JSON; bar = curated-rep median",
        "depth": "omitted in strip (see fig_kyber depth panel)",
        "phase_p99_ms_median": phase_med,
    }


def _d2fq_whole_share(run: Path) -> float:
    lc = json.loads((run / "fio_d2fq_lc.json").read_text())
    be = json.loads((run / "fio_d2fq_be.json").read_text())

    def nbytes(j: dict) -> float:
        for jo in j.get("jobs", []):
            return float(jo.get("write", {}).get("io_bytes") or 0)
        return float("nan")

    lb, bb = nbytes(lc), nbytes(be)
    return bb / (lb + bb) if lb + bb > 0 else float("nan")


def load_d2fq_bars() -> dict[str, float]:
    """R3 d2fqfix only: none median, target 0.8, io_ext mean excl outlier rep."""
    none_vals = [
        _d2fq_whole_share(D2FQ_FIX / f"d2fq_none_r{r}") for r in range(1, 6)
    ]
    none_vals = [v for v in none_vals if v == v]
    iox_vals = [
        (r, _d2fq_whole_share(D2FQ_FIX / f"d2fq_iox_r{r}")) for r in range(1, 6)
    ]
    iox_trim = [v for r, v in iox_vals if v == v and r != D2FQ_OUTLIER_REP]
    return {
        "none": _median(none_vals),
        "target": D2FQ_TARGET,
        "iox": statistics.mean(iox_trim) if iox_trim else float("nan"),
    }


def draw_d2fq(ax, vals: dict[str, float]) -> dict:
    width = 0.48
    order = ("none", "target", "iox")
    labels = ("none", "0.8", "io_ext")
    xs = [0.0, 1.0, 2.0]
    ymax = 0.92
    for x, key, lab in zip(xs, order, labels):
        v = vals[key]
        if key == "target":
            ax.bar(
                x,
                v,
                width=width,
                facecolor="white",
                edgecolor="0.28",
                hatch="///",
                linewidth=0.55,
                zorder=2,
            )
        else:
            ax.bar(
                x,
                v,
                width=width,
                color=ARM_COLOR[key],
                edgecolor="0.28",
                linewidth=0.55,
                zorder=2,
            )
        _bar_value_label(ax, x, f"{v:.3f}", y_top=v, yspan=ymax)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=FS_TICK)
    ax.set_ylim(0, ymax * 1.14)
    ax.set_ylabel("t400 BE share", fontsize=FS_LABEL)
    ax.set_xlabel("100:400", fontsize=FS_LABEL, labelpad=4)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", alpha=0.3, linestyle=":")
    _square(ax)
    return {
        "kind": "d2fq_three_bar",
        "source": str(D2FQ_FIX),
        "aggregate": vals,
        "iox_rule": f"mean 4 reps excl r{D2FQ_OUTLIER_REP}",
        "none_rule": "median n=5",
    }


def _iolat_phase_p99_median(arm: str) -> list[float]:
    med: list[float] = []
    for ph in PHASES_IOLAT:
        reps = IOLAT_CURATED_REPS.get((ph, arm), ())
        vals = [
            _fio_write_p99_us(_iolat_run(arm, r) / f"fio_{ph}_lc.json") / 1000.0 for r in reps
        ]
        vals = [v for v in vals if v == v]
        med.append(_median(vals))
    return med


def _iocost_phase_lc_median(arm: str) -> list[float]:
    med: list[float] = []
    for ph in IOCOST_PHASES:
        reps = IOCOST_CURATED_REPS.get((ph, arm), ())
        vals = [iocost_lc_ms(io_cost_run(arm, r), ph) for r in reps]
        vals = [v for v in vals if v == v]
        med.append(_median(vals))
    return med


def draw_io_cost(ax) -> dict:
    """LC write p99 — per-phase grouped bars (same encoding as (d)(e))."""
    medians: dict[str, dict[str, float]] = {}
    phase_med: dict[str, list[float]] = {}
    for arm in ("none", "native", "iox"):
        vals = _iocost_phase_lc_median(arm)
        phase_med[arm] = vals
        medians[arm] = dict(zip(IOCOST_PHASES, vals))
    _draw_phase_grouped_bars(
        ax,
        phase_med,
        ylabel="LC write p99 (ms)",
        slo_ms=IOCOST_SLO_MS,
    )
    return {
        "kind": "io_cost_lc_p99_phase_bars",
        "source": str(RERUN1 / "io_cost"),
        "aggregate": "median of curated reps per phase",
        "lc_p99_ms_median_by_phase": medians,
        "slo_ms": IOCOST_SLO_MS,
    }


def draw_mq_deadline(ax) -> dict:
    """v2 audit: read p99 µs only, curated bars."""
    arms = ("none", "native", "iox")
    x = np.arange(len(arms))
    width = 0.52
    read_bars = {a: bar_us("read", a) for a in arms}
    ymax = max(read_bars.values()) * 1.12
    for i, arm in enumerate(arms):
        vals = curated_values("read", arm)
        h = mq_bar_value(vals, arm)
        ax.bar(i, h, width=width, color=ARM_COLOR[arm], edgecolor="0.28", linewidth=0.45, zorder=2)
    ax.set_xticks(x)
    ax.set_xticklabels([ARM_LABEL[a] for a in arms], fontsize=FS_TICK - 1.0)
    ax.set_ylabel("read p99 (µs)", fontsize=FS_LABEL)
    ax.set_xlabel(MQ_XLABEL, fontsize=FS_LABEL, labelpad=4)
    ax.set_ylim(0, ymax)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", alpha=0.28, linestyle=":")
    _square(ax)
    return {
        "kind": "mq_deadline_v2_read_p99_bars",
        "source": "results/mq_deadline_v2",
        "aggregate": "none top2 med; native med5; iox max",
        "read_p99_us": read_bars,
        "audit": "phantom-read domain; not paper-grade",
    }


def draw_io_latency(ax) -> dict:
    """LC write p99 — per-phase grouped bars."""
    medians: dict[str, dict[str, float]] = {}
    phase_med: dict[str, list[float]] = {}
    for arm in ("none", "native", "iox"):
        vals = _iolat_phase_p99_median(arm)
        phase_med[arm] = vals
        medians[arm] = dict(zip(PHASES_IOLAT, vals))
    _draw_phase_grouped_bars(
        ax,
        phase_med,
        ylabel="LC write p99 (ms)",
        slo_ms=IOLAT_SLO_MS,
    )
    return {
        "kind": "io_latency_lc_p99_phase_bars",
        "source": str(RERUN1 / "io_latency"),
        "p99": "per-phase fio JSON; bar = curated-rep median",
        "lc_p99_ms_median_by_phase": medians,
        "slo_ms": IOLAT_SLO_MS,
    }


def main() -> int:
    plt.rcParams.update({"font.family": "DejaVu Sans", "pdf.fonttype": 42})
    io_max = load_io_max()
    io_weight = load_tenant_bw()
    d2fq_vals = load_d2fq_bars()

    fig_w = PANEL_CELL_IN * STRIP_NCOL * STRIP_FIG_W_SCALE
    fig_h = PANEL_CELL_IN * STRIP_NROW * STRIP_FIG_H_SCALE
    fig, axes_grid = plt.subplots(STRIP_NROW, STRIP_NCOL, figsize=(fig_w, fig_h))
    axes = axes_grid.ravel()
    meta = {
        "figure": "reproduction_eight_arm_strip.png",
        "layout": f"{STRIP_NROW}x{STRIP_NCOL}",
        "panel_cell_in": PANEL_CELL_IN,
        "panel_grid_slots": PANEL_GRID_SLOT,
        "panels": [],
    }

    used_slots: set[int] = set()
    for pid, title, kind in PANELS:
        slot = PANEL_GRID_SLOT[pid]
        ax = axes[slot]
        used_slots.add(slot)
        ax.set_title(f"({pid}) {title}", fontsize=FS_TITLE, fontweight="bold", pad=6)
        if kind == "tbd":
            draw_tbd(ax)
            meta["panels"].append({"panel": pid, "title": title, "status": "tbd"})
            continue
        if kind == "measured_io_max":
            rec = draw_io_max(ax, io_max)
        elif kind == "measured_io_weight":
            rec = draw_io_weight(ax, io_weight)
        elif kind == "measured_mq_deadline":
            rec = draw_mq_deadline(ax)
        elif kind == "measured_kyber":
            rec = draw_kyber(ax)
        elif kind == "measured_io_latency":
            rec = draw_io_latency(ax)
        elif kind == "measured_io_cost":
            rec = draw_io_cost(ax)
        elif kind == "measured_d2fq":
            rec = draw_d2fq(ax, d2fq_vals)
        else:
            continue
        rec.update({"panel": pid, "title": title, "status": "measured", "grid_slot": slot})
        meta["panels"].append(rec)

    for i, ax in enumerate(axes):
        if i not in used_slots:
            ax.set_visible(False)

    arm_handles = [
        Patch(facecolor=ARM_COLOR[k], edgecolor="0.28", label=ARM_LABEL[k])
        for k in ("none", "native", "iox")
    ]
    fig.legend(
        handles=arm_handles,
        ncol=3,
        loc="upper center",
        frameon=False,
        fontsize=STRIP_FS_LEGEND,
        bbox_to_anchor=(0.5, STRIP_LEGEND_Y),
        columnspacing=1.05,
        handlelength=1.25,
        handletextpad=0.38,
        labelspacing=0.22,
    )
    fig.subplots_adjust(
        left=STRIP_LEFT,
        right=0.995,
        bottom=STRIP_BOTTOM,
        top=STRIP_TOP,
        wspace=0.42,
        hspace=0.50,
    )
    _align_strip_ylabels(axes, used_slots)

    # Shared (d)(e)(f) workload caption — under (e) on the def row.
    axes[PANEL_GRID_SLOT["e"]].set_xlabel(PHASE_STEP_FOOTER, fontsize=FS_LABEL, labelpad=4)

    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "reproduction_eight_arm_strip.png"
    fig.savefig(png, dpi=300)
    fig.savefig(OUT / "reproduction_eight_arm_strip.pdf")
    plt.close(fig)
    (OUT / "reproduction_eight_arm_strip.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())