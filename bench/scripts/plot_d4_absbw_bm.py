#!/usr/bin/env python3
"""D4 absolute-bandwidth figure (GF batch) -- per-run + per-arm aggregate.

Reads the offline analyzer outputs (scripts/analyze_d2d4_bm.py):

    <analysis-dir>/d4_runs.csv   per-run rows (be_pre_burst_mean_mibs,
                                 be_burst_min_mibs, be_dip_pct,
                                 lc_p99_250ms_peak_ms, device_phase_*,
                                 gate_failed)
    <analysis-dir>/d4_agg.csv     per-arm medians/min/max + <key>_n valid
                                  sample counts

and renders ONE figure with three panels (units/window conventions are the
analyzer's, see its MD note):

  Panel A  per-run ABSOLUTE BE bandwidth scatter: pre-burst mean (t < 60 s)
           x burst minimum 1 s bucket (60 s <= t < 70 s), MiB/s log-log
           (1 MiB = 2^20 B/s; fio write_bw_log col2 KiB/s summed over the
           per-job logs, /1024).  Axes ADAPT to the plotted pairs (no
           fixed decade range).  Gate/phase-CLEAN runs = filled markers
           (these are the runs entering the aggregates); gate-failed runs =
           open grey; phase-flip (device-phase) runs = open orange.  The
           y = x diagonal marks "no dip"; be-clean runs get their dip%
           annotated -- dip% is SIGNED, so negative dips (burst bucket
           ABOVE the pre-burst mean, e.g. -33.19) and exact 0.0 render too.
  Panel B  per-arm aggregate medians (pre-burst / burst-min, MiB/s log y)
           with min-max whiskers, annotated with the per-metric valid n
           (n = valid samples entering the median; phase-clean pool).  An
           arm with n < 3 reps is explicitly tagged "(insufficient)"
           (n=1) / "(exploratory)" (n=2) -- a thin pool is never drawn
           silently.  x positions map through the FILTERED arms list, so
           an arm without be-clean rows cannot shift the others off their
           xticks.
  Panel C  LC 250 ms window peak per run (ms, log y): the p99 of the 1 ms
           clat-log samples per 250 ms window, max over windows; clean runs
           filled, excluded runs open.

Usage:
  python3 scripts/plot_d4_absbw_bm.py --analysis-dir DIR [--out-dir DIR]
                                     [--batch LABEL]
Outputs: fig_d4_absbw_bm.{png,pdf} in --out-dir (default: --analysis-dir).

The script is strictly read-only on the analysis dir and never touches the
results roots, the extract cache or any RUNBOOK.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# arm palette (plot_d2d4_bm_figs.py caliber: none grey, io_cost orange,
# iox arms blue/green family)
COL = {"none": "#9a9a9a", "io_cost": "#f28e2b", "iox_qonly": "#4c78a8",
       "iox_arbonly": "#59a14f", "iox_qarb": "#1f4e79"}
EXCL_GATE = "#b0b0b0"     # open grey: gate-failed (never aggregated)
EXCL_FLIP = "#e8a33d"     # open orange: device-phase flip (per-window)
FS_TITLE, FS_SUB, FS_TICK, FS_LABEL, FS_ANN, FS_NOTE = 10, 6.4, 7.5, 8.5, 6.8, 6.4
ARM_ORDER = ["none", "io_cost", "iox_qonly", "iox_arbonly", "iox_qarb"]


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _f(x: object, positive: bool = True) -> float | None:
    """Parse a CSV cell to float.  positive=True (log-axis inputs: MiB/s
    bandwidths, ms latencies) drops non-positive values; positive=False
    admits ANY finite value (dip% is signed -- GF batch has -33.19..+9.55
    and exact 0.0 readings -- so the dip annotations must go through the
    free parser)."""
    if x in (None, "", "None"):
        return None
    try:
        v = float(x)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return v if not positive or v > 0 else None


def _be_state(r: dict) -> str:
    """be-window membership: 'clean' (enters the be aggregates),
    'flip' (device-phase flip), 'gate' (gate-failed)."""
    if r.get("gate_failed") == "1":
        return "gate"
    return "flip" if r.get("device_phase_be") == "flip" else "clean"


def _lc_state(r: dict) -> str:
    if r.get("gate_failed") == "1":
        return "gate"
    return "flip" if r.get("device_phase_lc") == "flip" else "clean"


def _mk_marker(ax, x, y, state, color, label, marker="o"):
    if y is None or x is None:
        return
    if state == "clean":
        ax.scatter(x, y, s=28, color=color, marker=marker, zorder=3, label=label)
    else:
        c = EXCL_FLIP if state == "flip" else EXCL_GATE
        ax.scatter(x, y, s=30, facecolors="none", edgecolors=c, marker=marker,
                   zorder=3, label=label)


def panel_a(ax, runs: list[dict], batch: str) -> None:
    xs_all: list[float] = []
    ys_all: list[float] = []
    for r in runs:
        pre, bmin = _f(r.get("be_pre_burst_mean_mibs")), _f(r.get("be_burst_min_mibs"))
        if pre is None or bmin is None:
            continue
        state = _be_state(r)
        col = COL.get(r["arm"], "#333333")
        lbl = r["arm"] if state == "clean" else None
        _mk_marker(ax, pre, bmin, state, col, lbl)
        xs_all.append(pre)
        ys_all.append(bmin)
    if not xs_all:
        ax.text(0.5, 0.5, "no absolute-BW values", ha="center", va="center",
                fontsize=FS_LABEL, transform=ax.transAxes)
        return
    # ADAPTIVE log axes: limits from the plotted pairs only (the old fixed
    # [1, 10000] range shrank the GF scatter to a corner); a modest x1.8
    # multiplicative pad keeps the diagonal readable
    lo = min(min(xs_all), min(ys_all))
    hi = max(max(xs_all), max(ys_all))
    ax.plot([lo, hi], [lo, hi], ls="--", lw=0.8, color="#777777", zorder=1,
            label="y = x (no dip)")
    ax.set_xlim(lo / 1.8, hi * 1.8)
    ax.set_ylim(lo / 1.8, hi * 1.8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("BE before burst (MiB/s)")
    ax.set_ylabel("BE minimum 1 s (MiB/s)")
    ax.set_title("A  BE before vs during burst", fontsize=FS_TITLE)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, which="major", ls=":", lw=0.5, color="#cccccc")
    _dedup_legend(ax, loc="lower right")


def panel_b(ax, agg: list[dict]) -> None:
    arms = [a["arm"] for a in agg
            if _f(a.get("be_pre_burst_mean_mibs_median")) is not None]
    if not arms:
        ax.text(0.5, 0.5, "no be-clean aggregate rows", ha="center", va="center",
                fontsize=FS_LABEL, transform=ax.transAxes)
        return
    xs = range(len(arms))

    def _n_label(a: dict, key: str) -> str:
        raw = a.get(key + "_n", "?")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return f"n={raw}"
        if n >= 3:
            return f"n={n}"
        return f"n={n}*"

    for key, off, marker, col in (("be_pre_burst_mean_mibs", -0.13, "o",
                                   COL["iox_qonly"]),
                                  ("be_burst_min_mibs", 0.13, "s",
                                   COL["iox_qarb"])):
        x_plt, med, lo, hi, lab = [], [], [], [], []
        for a in agg:
            if a["arm"] not in arms:
                continue
            m = _f(a.get(key + "_median"))
            if m is None:
                continue
            # SUBSET-SAFE index: map through the filtered arms list, never
            # enumerate(agg) -- an arm dropped from `arms` (no be-clean
            # rows, e.g. a missing pre-burst median) must not shift the
            # remaining arms off their xticks
            x_plt.append(arms.index(a["arm"]) + off)
            med.append(m)
            lo.append(m - (_f(a.get(key + "_min")) or m))
            hi.append((_f(a.get(key + "_max")) or m) - m)
            lab.append(_n_label(a, key))
        ax.errorbar(x_plt, med, yerr=[lo, hi], fmt=marker, ms=5, lw=1.2,
                    capsize=3, color=col,
                    label="pre-burst med (t<60s)" if off < 0
                    else "burst-min med (60-70s)")
        for x, y, t in zip(x_plt, med, lab):
            ax.annotate(t, (x, y), textcoords="offset points",
                        xytext=(-3 if off < 0 else 3, 9 if off < 0 else -14),
                        ha="right" if off < 0 else "left", fontsize=FS_ANN - 0.6,
                        color="#555555")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(arms, fontsize=FS_TICK)
    ax.set_yscale("log")
    y_max = max((_f(a.get("be_pre_burst_mean_mibs_max")) or
                 _f(a.get("be_pre_burst_mean_mibs_median")) or 0.0)
                for a in agg)
    if y_max > 0:
        ax.set_ylim(top=y_max * 2.0)
    ax.set_ylabel("aggregate BE bandwidth (MiB/s)")
    ax.set_title("B  BE medians and run counts", fontsize=FS_TITLE)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", which="major", ls=":", lw=0.5, color="#cccccc")
    ax.legend(fontsize=FS_SUB + 0.6, loc="best")


def panel_c(ax, runs: list[dict]) -> None:
    shown = [r for r in runs if _f(r.get("lc_p99_250ms_peak_ms")) is not None]
    if not shown:
        ax.text(0.5, 0.5, "no 250 ms peak values", ha="center", va="center",
                fontsize=FS_LABEL, transform=ax.transAxes)
        return
    for i, r in enumerate(shown):
        y = _f(r["lc_p99_250ms_peak_ms"])
        state = _lc_state(r)
        col = COL.get(r["arm"], "#333333")
        _mk_marker(ax, i, y, state, col,
                   r["arm"] if state == "clean" else None, marker="D")
        if state == "clean":
            ax.annotate(f"{y:g}", (i, y), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=FS_ANN - 0.6,
                        color="#555555")
    labels = [r["sequence"].replace("D4_bm_", "").replace("_run", " r")
              for r in shown]
    ax.set_xticks(range(len(shown)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=FS_TICK - 0.8)
    ax.set_yscale("log")
    ax.set_ylabel("LC 250 ms window peak p99 (ms)")
    ax.set_title("C  LC 250 ms peak by run", fontsize=FS_TITLE)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="y", which="major", ls=":", lw=0.5, color="#cccccc")


def _dedup_legend(ax, **kw) -> None:
    handles, labels = ax.get_legend_handles_labels()
    seen, h, l = set(), [], []
    for hh, ll in zip(handles, labels):
        if ll not in seen:
            seen.add(ll)
            h.append(hh)
            l.append(ll)
    ax.legend(h, l, fontsize=FS_SUB + 0.6, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis-dir", type=Path, required=True,
                    help="dir holding d4_runs.csv + d4_agg.csv (analyzer output)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="default: --analysis-dir")
    ap.add_argument("--batch", default=None,
                    help="batch label for titles/footers (default: derived "
                         "from the analysis dir path)")
    args = ap.parse_args()

    runs = _rows(args.analysis_dir / "d4_runs.csv")
    agg = _rows(args.analysis_dir / "d4_agg.csv")
    agg.sort(key=lambda a: ARM_ORDER.index(a["arm"]) if a["arm"] in ARM_ORDER
             else len(ARM_ORDER))
    runs.sort(key=lambda r: (ARM_ORDER.index(r["arm"]) if r["arm"] in ARM_ORDER
                             else len(ARM_ORDER)),)
    if not runs:
        print(f"no d4_runs.csv rows under {args.analysis_dir}", file=sys.stderr)
        return 1
    batch = args.batch
    if batch is None:
        parent = args.analysis_dir.resolve().parent.name
        batch = parent.replace("isol_d2d4_bm_", "") if parent.startswith(
            "isol_d2d4_bm_") else (args.analysis_dir.parent.name or "d4")

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.2))
    fig.subplots_adjust(left=0.075, right=0.98, top=0.83, bottom=0.26,
                        wspace=0.34)
    fig.suptitle(f"{batch}: D4 bandwidth and LC burst (exploratory)",
                 fontsize=14, y=0.95)
    panel_a(axes[0], runs, batch)
    panel_b(axes[1], agg)
    panel_c(axes[2], runs)
    n_gate = sum(1 for r in runs if r.get("gate_failed") == "1")
    n_be_flip = sum(1 for r in runs if r.get("gate_failed") != "1"
                    and r.get("device_phase_be") == "flip")
    n_lc_flip = sum(1 for r in runs if r.get("gate_failed") != "1"
                    and r.get("device_phase_lc") == "flip")
    fig.text(0.075, 0.105,
             f"{len(runs)} runs | Filled: phase-clean; open grey: gate failure ({n_gate}); "
             f"open orange: phase flip (BE {n_be_flip}, LC {n_lc_flip}). "
             "* n<3: insufficient evidence for a winner.",
             fontsize=9, color="#444444", ha="left")
    fig.text(0.075, 0.065,
             "BE from fio 1 s logs: sum per-job KiB/s /1024. Pre: t<60 s; "
             "burst: 60-70 s. Negative dip means BE rose during burst.",
             fontsize=9, color="#444444", ha="left")

    out_dir = args.out_dir if args.out_dir is not None else args.analysis_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        p = out_dir / f"fig_d4_absbw_bm.{ext}"
        fig.savefig(p, dpi=300)
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
