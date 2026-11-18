#!/usr/bin/env python3
"""rq13 paper figures for the bm D2/D3/D4 campaign (GRAND FINAL
single-batch five-arm roots; the pre-GF n3 batches stay reachable via the
--d3-qonly/--d4-qonly aliases and explicit root paths).

Three single-panel figures in the DAC/IEEEtran quarter-page caliber
(8.5/4 in wide, the rq13 genre of paper/dac_ioext/figs/rq13_d2_jain_bw.pdf
and rq13_d4_timeseries.pdf; the D4 rendering follows
paper/dac_ioext/scripts/render_paper_d4_timeseries.py):

  rq13_d2_bm  grouped bars: w-indexed JainW per cg tier (cg2/4/8/16) x arm,
              perfect-fairness dashed line (labeled "1.0000" at its right
              end), green/red +X%% gain callouts only where |gain| >= 5%%
              (the sub-5%% parity of the iox weighted-share arms is stated
              once in a small gray bottom note; no bar-top value labels --
              the paper rq13_d2_jain_bw genre), overlaid BW/max polyline
              (agg median / global max, same axes).
  rq13_d3_bm  LC x BE Pareto scatter, v2 axes: y = LC read clat p99 (ms,
              LOG scale, 0.05-100, down = better), x = BE aggregate read
              BW (MB/s, 0-3200, right = better) -- the Pareto good corner
              is the BOTTOM RIGHT.  Per-arm median point + phase-clean
              min-max error bars (the clean-run count rides in the legend,
              e.g. "io.cost (n=3)"), horizontal SLO line at y = 2x solo
              p99, solo anchor at (0, solo).
  rq13_d4_bm  two stacked panels sharing x = time since burst arrival
              (pre-burst BE-only window, burst window, recovery):
              TOP = LC p99 time series, one median line per arm (per-250
              ms-window p99 of the 1 ms clat logs inside the per-run
              tar.gz, log_avg_msec=1, col2 = ns, median over the
              phase-clean runs) plus a min-max envelope band across the
              clean runs; solo reference line, burst-arrival line,
              pre-arrival BE-only shade.
              BOTTOM = BE aggregate BW (MB/s, linear 0-3200), one thin
              line per arm from the tar's d4_be write_bw_log members
              (log_avg_msec=1000 -> 1 s buckets; multi-BE jobs summed
              with analyze_d2d4_bm.py be_bw_series math), each arm's
              burst-window dip% labeled in its color.

DATA SOURCES (all pre-existing local analysis CSVs; nothing under results/
is modified -- tars are read-only, extraction goes to a self-managed
scratch dir with a sha256 fingerprint cache):

  D2  <d2-root>/analysis/d2_agg.csv            (arms none/io_cost/iox_qonly;
      the arb arms come from --arb-d2-root, which in the GRAND FINAL
      single-batch campaign is the SAME root)
  D3  <d3-root>/analysis/d3_pareto.csv         (arms none/io_cost/iox_qonly;
      arb arms via --arb-d3-root)
  D4  <d4-root>/analysis/d4_runs.csv + the per-run tar.gz clat AND bw logs
      (arb arms via --arb-d4-root; --d4-shape 4x1024 reads --d4-deep-root)
  calib <calib-root>/analysis/calib.csv         (D3_bm_lcsolo_none anchor:
      d3_solo_lc_p99_ms + suggested_slo_ms)

The GRAND FINAL (GF) campaign collects every arm of a family -- none,
io_cost, iox_qonly (= q_ratelimit), iox_arbonly (fb), iox_qarb (fb) -- in
ONE batch under one kernel, so the three figures default to the GF roots
(results/isol_d2d4_bm_GF_*); pass the SAME GF root to --arb-d{2,3,4}-root
to add its arb arms (only the arb arms are ever taken from an arb root;
when the arb root IS the primary root the "ignored arm" note is
informational).  The pre-GF n3 batches stay reachable through the
--d3-qonly/--d4-qonly aliases (ddl_root/wedf_root/fifo) and through
explicit --d2-root/--d3-root/--d4-root paths.

CONSISTENCY ASSERTIONS (script-internal; failures are printed to stderr and
flip the exit code, the figures still render):
  D2  every plotted bar/BW point equals the d2_agg.csv (or, when only
      d2_runs.csv carries device-phase columns, the phase-clean recompute
      of d2_runs.csv) value; the unfiltered recompute is cross-checked
      against d2_agg.csv.
  D3  every plotted point/error bar equals the d3_pareto.csv column values;
      SLO == 2 x solo (calib.csv) unless --slo-ms.  When the iox_qonly arm
      is q_ratelimit (GF): its LC p99 median AND max must sit below the SLO
      line and no plotted arm may dominate its point (it must be the
      deepest point of the bottom-right good corner).
  D4  per run, the 250 ms window-p99 peak recomputed with the EXACT
      analyzer window math (analyze_d2d4_bm.py lc_window_p99: windows
      anchored at the first sample, index ceil(n*0.99)-1) equals
      d4_runs.csv lc_p99_250ms_peak_ms; per arm, the median of those peaks
      equals d4_agg.csv lc_p99_250ms_peak_ms_median.  BE side: the per-run
      aggregate series recomputed from the tar bw logs (analyzer
      be_bw_series) reproduces d4_runs.csv be_pre_burst_mean_mibs /
      be_burst_min_mibs / be_dip_pct, and the arm median of the recomputed
      dips (over the gate-clean, device_phase_be-clean pool) equals
      d4_agg.csv be_dip_pct_median -- the value the bottom-panel dip%
      labels carry.

Usage:
  python3 scripts/plot_rq13_d2d3d4_bm.py [--fig all|d2,d3,d4]
      [--out-dir DIR] [--d2-root DIR] [--d3-root DIR] [--d3-qonly VAR]
      [--extra-io-cost DIR] [--d4-root DIR] [--d4-qonly VAR]
      [--d4-deep-root DIR] [--d4-shape {4x256,4x1024}]
      [--d2-qonly-label L] [--d3-qonly-label L] [--d4-qonly-label L]
      [--arb-d2-root DIR] [--arb-d3-root DIR] [--arb-d4-root DIR]
      [--calib-root DIR] [--slo-ms MS] [--solo-ms MS] [--d4-scratch DIR]

  --out-dir        default results/isol_d2d4_bm_n3/figs
  --d2-root        D2 results root (default the GF single-batch root
                   results/isol_d2d4_bm_GF_d2_all, five arms)
  --d3-root        D3 results root (default the GF root
                   results/isol_d2d4_bm_GF_d3_all); wins over --d3-qonly
  --d3-qonly       pre-GF n3 alias: ddl_root -> results/isol_d2d4_bm_n3_d3_ddl_root
                   (label "iox q_ddl (root)"); fifo -> ..._d3_fifo ("iox
                   q_fifo").  The none/io_cost points always come from the
                   SAME batch.
  --extra-io-cost  optional second D3 root; its io_cost row is added as a
                   light secondary point.
  --d4-root        D4 results root (default the GF root
                   results/isol_d2d4_bm_GF_d4_all); wins over --d4-qonly
  --d4-qonly       pre-GF n3 alias: wedf_root -> results/isol_d2d4_bm_n3_d4_wedf_root
                   ("iox q_wedf (root)"); ddl_root -> ..._d4_ddl_root
                   ("iox q_ddl (root)")
  --d4-deep-root   D4 results root for --d4-shape 4x1024 (default the GF
                   deep batch results/isol_d2d4_bm_GF_d4_deep, three arms);
                   the pre-GF alias --d4-qonly ddl_root keeps the n3 deep
                   batch results/isol_d2d4_bm_n3_d4_ddl_root_deep
  --d4-shape       BE shape of the D4 figure (default 4x256; 4x1024 reads
                   --d4-deep-root; the deep batch has no arb arms, so
                   --arb-d4-root is not merged under 4x1024)
  --d{2,3,4}-qonly-label   legend label of the iox_qonly arm (defaults: the
                   GF q_ratelimit labels -- "iox q_ratelimit (cap\u221dW)"
                   in D2, "iox q_ratelimit" in D3/D4; the pre-GF aliases
                   keep their old labels)
  --arb-d*-root    results root carrying the arb arms (iox_arbonly/
                   iox_qarb); only the arb arms are taken from it -- in
                   the GF campaign pass the SAME root as the family's
                   primary root.  Omitted arms are skipped with a stderr
                   note when the flag is not given.
  --calib-root     calib results root with analysis/calib.csv (default the
                   GF calib batch results/isol_d2d4_bm_GF_calib; the solo
                   anchor and SLO line default to its D3_bm_lcsolo_none
                   d3_solo_lc_p99_ms / suggested_slo_ms)
  --slo-ms/--solo-ms   override the SLO line / solo anchor; with BOTH
                   given the calib CSV is not read at all
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import shutil
import statistics
import sys
import tarfile
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe   # noqa: E402
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402
from matplotlib.lines import Line2D      # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RES = REPO / "results"

# ---- default data roots -----------------------------------------------------
# GRAND FINAL (GF) single-batch five-arm roots: every arm of a family is
# collected in ONE batch under one kernel, so the figure defaults point
# there (pass the SAME root to --arb-d{2,3,4}-root for the arb arms).
GF_D2_ROOT_DEF = RES / "isol_d2d4_bm_GF_d2_all"
GF_D3_ROOT_DEF = RES / "isol_d2d4_bm_GF_d3_all"
GF_D4_ROOT_DEF = RES / "isol_d2d4_bm_GF_d4_all"
GF_D4_DEEP_ROOT_DEF = RES / "isol_d2d4_bm_GF_d4_deep"
GF_CALIB_ROOT_DEF = RES / "isol_d2d4_bm_GF_calib"
# pre-GF n3 roots, kept for the --d3-qonly/--d4-qonly backward-compat aliases
D3_ROOTS = {"ddl_root": RES / "isol_d2d4_bm_n3_d3_ddl_root",
            "fifo": RES / "isol_d2d4_bm_n3_d3_fifo"}
D3_QONLY_LABEL = {"ddl_root": "iox q_ddl (root)", "fifo": "iox q_fifo"}
D4_ROOTS = {"ddl_root": RES / "isol_d2d4_bm_n3_d4_ddl_root",
            "wedf_root": RES / "isol_d2d4_bm_n3_d4_wedf_root"}
D4_QONLY_LABEL = {"ddl_root": "iox q_ddl (root)",
                  "wedf_root": "iox q_wedf (root)"}
D4_DEEP_ROOT = RES / "isol_d2d4_bm_n3_d4_ddl_root_deep"
D4_DEEP_LABEL = "iox q_ddl (root)"       # the n3 deep batch runs q_ddl only
# the GF iox_qonly arm is q_ratelimit: the D2 fairness caps scale with the
# tenant weight (cap prop. to W), the D3/D4 hetero shape caps each BE job
# at a fixed rate -- the per-family labels state which one the figure shows
GF_QONLY_LABEL = {"d2": "iox q_ratelimit (cap\u221dW)",
                  "d3": "iox q_ratelimit",
                  "d4": "iox q_ratelimit"}
OUT_DEF = RES / "isol_d2d4_bm_n3/figs"
SCRATCH_DEF = Path("/tmp/rq13_d4_extract")

# ---- arm order / labels / style --------------------------------------------
ARMS_BASE = ("none", "io_cost", "iox_qonly")
ARB_ARMS = ("iox_arbonly", "iox_qarb")
# the GF arb arms run --allow-fallback (fb): the arbiter survives but the
# weighted shares fall back to 1:1 -- the suffix states the mode in the
# legends (the D2 bottom note spells the semantics out)
ARM_LABEL = {"none": "none", "io_cost": "io.cost",
             "iox_arbonly": "iox arb_only (fallback)",
             "iox_qarb": "iox q_arb (fallback)"}
# bars / scatter: grey = native no control, green = cgroup knob, orange
# family = iox (paper rq13 semantics)
ARM_STYLE = {
    "none":        dict(color="#737373", hatch="",  marker="o"),
    "io_cost":     dict(color="#238b45", hatch="///", marker="o"),
    "iox_qonly":   dict(color="#d94801", hatch="",  marker="o"),
    "iox_arbonly": dict(color="#ec7014", hatch="//", marker="^"),
    "iox_qarb":    dict(color="#8c2d04", hatch="",  marker="s"),
}
# D4 timeseries lines: none grey solid, io_cost green dashed (the family
# color of io.cost in D2/D3), iox dark orange solid bold
# (render_paper_d4_timeseries), arb arms in the orange family
D4_LINE = {
    "none":        ("#737373", "-",  0.8),
    "io_cost":     ("#238b45", "--", 0.8),
    "iox_qonly":   ("#d94801", "-",  1.2),
    "iox_arbonly": ("#ec7014", "-.", 0.8),
    "iox_qarb":    ("#8c2d04", "-",  1.0),
}

FIG_W = 8.5 / 4                        # DAC/IEEEtran quarter page width (in)
# rq13_d2 gain-callout floor: only |gain| >= 5% gets a green/red callout.
# The iox weighted-share arms run with --allow-fallback liveness (weights
# shape shares only in strict mode), so their sub-5% JainW wiggles around
# none are parity, not signal -- they are stated in the figure's gray
# bottom note instead of as per-bar callout noise.
D2_GAIN_MIN_PCT = 5.0
INK = "#222222"
GRID = "#e6e6e6"
BURST = "#b8860b"
REF = "#8c8c8c"
GOOD = "#238b45"
BAD = "#c0392b"
# white halo behind every in-plot value/gain label: keeps the text legible
# where it grazes a bar edge, the BW polyline or the fairness line
WHITE_HALO = [pe.withStroke(linewidth=1.6, foreground="white")]

plt.rcParams.update({
    "font.family": ["Liberation Serif", "DejaVu Serif", "serif"],
    "font.size": 6.5,
    "axes.labelsize": 6.5,
    "xtick.labelsize": 5.5,
    "ytick.labelsize": 5.5,
    "legend.fontsize": 4.8,
    "axes.linewidth": 0.45,
    "xtick.major.width": 0.4,
    "ytick.major.width": 0.4,
    "xtick.major.size": 2.0,
    # NOTE: ytick.major.size stays at the 3.5pt default.  The reference
    # render_paper_d4_timeseries.py sets 2.0, but on matplotlib >= 3.10 that
    # pushes the rotated y-axis label past the figure edge (clipped, ~53 ink
    # px at the left frame); the reference PDF itself was rendered by an
    # older matplotlib without the quirk (verified by pixel diff 2026-11-11).
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.unicode_minus": False,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
})

D4_WIN_MS = 250          # rq13 metric: p99 per 250 ms window
D4_MIN_N = 4             # analyzer min-sample gate per window
# D4 burst geometry (analyze_d2d4_bm.py BURST_START_S / BURST_END_S): the
# LC burst arrives 60 s after BE start and runs 10 s.  The BE bw series is
# keyed by BE-log seconds, so "time since burst arrival" = BE t - 60.
BURST_AT_BE_S = 60.0
BURST_LEN_S = 10.0
# shared-x display window: >= 10 s of the pre-burst BE-only window, the
# 10 s burst, and as much of the BE recovery tail as the logs carry (the
# 80 s BE jobs end at x = +19-20).
D4_XLO = -13.0
D4_XHI = 20.0
D4_BE_YMAX = 3200.0      # bottom-panel y ceiling (MB/s, linear)
D4_BE_YTICKS = (0, 1000, 2000, 3000)

# device-phase column candidates (D2 tolerance: a parallel agent is adding
# them; when absent the full pool enters and a stderr warning is printed)
PHASE_COLS = ("device_phase", "device_phase_lc", "device_phase_be", "phase")

FAILS: list = []         # assertion failures (stderr + exit 1 at the end)


class PlotError(Exception):
    """A figure cannot be produced (missing file/column, bad data)."""


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"note: {msg}", file=sys.stderr)


def check(cond: bool, msg: str) -> bool:
    """Record an assertion failure (stderr + non-zero exit at the end)."""
    if not cond:
        FAILS.append(msg)
        print(f"ASSERTION FAIL: {msg}", file=sys.stderr)
    return cond


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def rows_of(path: Path, what: str):
    if not path.is_file():
        raise PlotError(f"{what}: missing file {path}")
    try:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        raise PlotError(f"{what}: cannot read {path}: {exc}")
    if not rows:
        raise PlotError(f"{what}: {path} has no data rows")
    return rows


def num(row: dict, key: str, ctx: str):
    s = (row.get(key) or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        raise PlotError(f"{ctx}: column {key!r} value {s!r} is not numeric")


def need_cols(rows: list, cols, ctx: str) -> None:
    missing = [c for c in cols if c not in rows[0]]
    if missing:
        raise PlotError(f"{ctx}: required column(s) {missing} missing "
                        f"(has {list(rows[0])})")


def split_phase_clean(rows: list, ctx: str):
    """Rows whose device-phase columns all say clean; (clean, dropped, cols).

    Only outright flip/na verdicts are dropped; mixed/unknown values stay
    in the pool (a cell median over a partially clean pool is still the
    analyzer's phase-aware median)."""
    cols = [c for c in PHASE_COLS if c in rows[0]]
    if not cols:
        return rows, [], []
    clean, dropped = [], []
    for r in rows:
        vals = [str(r.get(c) or "").strip() for c in cols]
        if any(v in ("flip", "na") for v in vals):
            dropped.append((r, vals))
        else:
            clean.append(r)
    return clean, dropped, cols


def batch_tag(root: Path) -> str:
    """Short batch name from a results-root dir name (for labels)."""
    name = root.name
    for pre in ("isol_d2d4_bm_n3_d3_", "isol_d2d4_bm_n3_d4_",
                "isol_d2d4_bm_n3_d2_", "isol_d2d4_bm_n3_",
                "isol_d2d4_bm_"):
        if name.startswith(pre):
            return name[len(pre):] or name
    return name


def load_calib(calib_root: Path, slo_ms=None, solo_ms=None):
    """(solo_lc_p99_ms, slo_ms) from analysis/calib.csv, with overrides.

    SLO = the calib row's suggested_slo_ms (= 2x the RAW solo p99, before
    the 4-decimal rounding of the anchor; 0.2365 vs 2x0.1183 = 0.2366).
    With BOTH --slo-ms and --solo-ms given the calib CSV is not read at all
    (the GF calib batch may not have landed yet)."""
    if slo_ms is not None and solo_ms is not None:
        return solo_ms, slo_ms
    calib_csv = calib_root / "analysis" / "calib.csv"
    if not calib_csv.is_file():
        raise PlotError(f"calib root {calib_root}: missing {calib_csv} "
                        "(the GRAND FINAL calib batch may not have landed "
                        "yet; pass the --calib-root of an older calib batch "
                        "or both --slo-ms and --solo-ms)")
    rows = rows_of(calib_csv, f"calib root {calib_root}")
    solo = None
    slo_from_calib = None
    for r in rows:
        if r.get("sequence") == "D3_bm_lcsolo_none":
            solo = num(r, "value", "calib.csv")
            slo_from_calib = num(r, "suggested_slo_ms", "calib.csv")
    if solo is None and solo_ms is None:
        raise PlotError(f"calib root {calib_root}: no D3_bm_lcsolo_none row "
                        "in analysis/calib.csv (pass --solo-ms/--slo-ms to "
                        "override)")
    if solo_ms is not None:
        solo = solo_ms
    if slo_ms is not None:
        slo = slo_ms
    else:
        slo = slo_from_calib if slo_from_calib is not None else round(2.0 * solo, 4)
        want = slo_from_calib if slo_from_calib is not None else 2.0 * solo
        check(abs(slo - want) < 1e-9,
              f"SLO {slo} != 2 x solo {solo} (calib)")
    return solo, slo


# ---------------------------------------------------------------------------
# D2: analysis/d2_agg.csv (device-phase tolerant)
# ---------------------------------------------------------------------------
def _d2_recompute(run_rows):
    """median jainw / median agg per (arm, ncg) over the given runs."""
    cells: dict = {}
    for r in run_rows:
        key = (r["arm"], int(r["ncg"]))
        cells.setdefault(key, []).append(r)
    out = {}
    for key, rs in cells.items():
        jw = [num(r, "jainw_windexed", "d2_runs.csv") for r in rs]
        bw = [num(r, "agg_bw_mb_s", "d2_runs.csv") for r in rs]
        out[key] = (round(statistics.median([v for v in jw if v is not None]), 4),
                    round(statistics.median([v for v in bw if v is not None]), 2))
    return out


def load_d2_cells(d2_root: Path):
    """{(arm, ncg): (jainw_median, agg_bw_median)} over the plotted arms.

    Pool policy (task spec):
      * d2_agg.csv carries a device-phase column -> rows marked flip/na are
        dropped (kept counts printed to stderr);
      * no device-phase column there, but d2_runs.csv has one -> the phase-
        clean medians are recomputed from d2_runs.csv (the unfiltered
        recompute is asserted against d2_agg.csv first);
      * nowhere a device-phase column -> the FULL pool enters, with the
        stderr warning the task requires.
    """
    ctx = f"D2 root {d2_root}"
    agg_path = d2_root / "analysis" / "d2_agg.csv"
    agg = rows_of(agg_path, ctx)
    need_cols(agg, ("arm", "ncg", "jainw_median", "agg_bw_mb_s_median"), ctx)

    clean, dropped, pcols = split_phase_clean(agg, ctx)
    if pcols:
        for r, vals in dropped:
            warn(f"D2: {ctx} d2_agg.csv row arm={r['arm']} ncg={r['ncg']} "
                 f"device-phase {vals} -> excluded")
    else:
        runs_path = d2_root / "analysis" / "d2_runs.csv"
        runs = None
        if runs_path.is_file():
            runs = rows_of(runs_path, ctx)
        rcols = [c for c in PHASE_COLS if runs and c in runs[0]]
        if rcols:
            # the unfiltered recompute must match d2_agg.csv; if it does,
            # the agg medians are NOT phase-aware and the plotted values
            # are recomputed over the phase-clean pool instead
            gate_clean = [r for r in runs if (r.get("gate_failed") or "0") == "0"]
            unfiltered = _d2_recompute(gate_clean)
            matches = True
            for r in clean:
                key = (r["arm"], int(r["ncg"]))
                jw = num(r, "jainw_median", ctx)
                bw = num(r, "agg_bw_mb_s_median", ctx)
                if key in unfiltered:
                    if (abs(unfiltered[key][0] - jw) > 1e-6
                            or abs(unfiltered[key][1] - bw) > 0.011):
                        matches = False
            pool = [r for r in gate_clean
                    if all(str(r.get(c) or "").strip() == "clean" for c in rcols)]
            recomputed = _d2_recompute(pool)
            if matches:
                warn(f"D2: d2_agg.csv has no device-phase column but "
                     f"d2_runs.csv does ({rcols}); medians recomputed over "
                     f"the phase-clean run pool")
                return recomputed
            warn("D2: d2_agg.csv medians already differ from the unfiltered "
                 "d2_runs.csv medians -> treating them as phase-aware and "
                 "using them as-is")
        else:
            warn("D2: no device_phase column in d2_agg.csv"
                 + (" or d2_runs.csv" if runs is not None else "")
                 + "; using the FULL run pool (task tolerance)")

    cells = {}
    for r in clean:
        cells[(r["arm"], int(r["ncg"]))] = (
            num(r, "jainw_median", ctx), num(r, "agg_bw_mb_s_median", ctx))
    return cells


def load_d2(root: Path, arb_root: Path | None):
    """{(arm, ncg): (jainw, agg_bw)} merged from the primary and arb roots."""
    cells = load_d2_cells(root)
    if arb_root is not None:
        arb = load_d2_cells(arb_root)
        ignored = set()
        for key, v in arb.items():
            if key[0] in ARB_ARMS:
                cells[key] = v
            else:
                ignored.add(key[0])
        if ignored:
            msg = (f"D2 arb root {arb_root}: arm(s) {sorted(ignored)} ignored "
                   "(only the arb arms are taken from the arb root)")
            if arb_root.resolve() == root.resolve():
                # the GRAND FINAL single-batch wiring: the same root is
                # passed as primary and arb root, so the "ignored" base
                # arms are already plotted from the primary -- informational
                info(msg + "; they are already plotted from the primary "
                     "root (single-batch wiring)")
            else:
                warn(msg)
    return cells


def render_d2(out_dir: Path, d2_root: Path, arb_root: Path | None,
              iox_label: str) -> None:
    cells = load_d2(d2_root, arb_root)
    cgs = sorted({k[1] for k in cells})
    if not cgs:
        raise PlotError(f"D2 root {d2_root}: no (arm, ncg) cells to plot")
    arms = [a for a in ARMS_BASE + ARB_ARMS if any(k[0] == a for k in cells)]
    labels = {a: (iox_label if a == "iox_qonly" else ARM_LABEL[a]) for a in arms}
    # short in-figure names for the parity note (legend names stay full)
    short = {"iox_qonly": "q_ratelimit", "iox_arbonly": "arb_only",
             "iox_qarb": "q_arb"}
    # D2-only bar faces: q_arb's dark #8c2d04 solid reads as a featureless
    # dark block at print size (any hatch drowned in the dark face), so the
    # D2 bars lift it to a light orange with a reverse hatch -- a brightness
    # step clearly apart from arb_only's forward //, same iox family.  The
    # D3 markers / D4 lines keep the shared ARM_STYLE.
    d2_style = {a: dict(ARM_STYLE[a]) for a in arms}
    if "iox_qarb" in d2_style:
        d2_style["iox_qarb"]["color"] = "#fdae6b"
        d2_style["iox_qarb"]["hatch"] = "\\\\"

    jain = {(a, c): cells[(a, c)][0] for a in arms for c in cgs if (a, c) in cells}
    bw = {(a, c): cells[(a, c)][1] for a in arms for c in cgs if (a, c) in cells}
    bw_vals = [v for v in bw.values() if v is not None]
    bw_max = max(bw_vals) if bw_vals else None
    bw_ref = int(round(bw_max)) if bw_max else 0
    for (a, c), v in jain.items():
        check(v is not None and abs(v - cells[(a, c)][0]) < 1e-12,
              f"D2 plotted JainW {v} != d2 CSV {cells[(a, c)][0]} ({a} cg{c})")

    n = len(arms)
    w = min(0.22, 0.9 / n)
    offs = (np.arange(n) - (n - 1) / 2.0) * w
    x = np.arange(len(cgs))

    fig, ax = plt.subplots(figsize=(FIG_W, 1.95))
    for ai, arm in enumerate(arms):
        vals = [jain.get((arm, c)) for c in cgs]
        style = d2_style[arm]
        ax.bar(x + offs[ai], [v if v is not None else np.nan for v in vals],
               w * 0.9, color=style["color"], edgecolor=INK, lw=0.3,
               hatch=style["hatch"], label=labels[arm], zorder=2)
    bar_rects = [(float(x[gi] + offs[ai] - w * 0.45), 0.0,
                  float(x[gi] + offs[ai] + w * 0.45), float(jain[(arm, cg)]))
                 for gi, cg in enumerate(cgs) for ai, arm in enumerate(arms)
                 if jain.get((arm, cg)) is not None]
    bw_groups = []
    for gi, cg in enumerate(cgs):
        xs, ys = [], []
        for ai, arm in enumerate(arms):
            v = bw.get((arm, cg))
            if v is None or not bw_max:
                continue
            xs.append(x[gi] + offs[ai])
            ys.append(v / bw_max)
            check(abs(v / bw_max - cells[(arm, cg)][1] / bw_max) < 1e-12,
                  f"D2 plotted BW/max {v / bw_max} != CSV {v}/{bw_max}")
        if xs:
            ax.plot(xs, ys, color=INK, lw=0.7, marker="o", ms=2.3,
                    mfc="white", mew=0.55, zorder=5,
                    label=(f"BW / {bw_ref} MB/s" if gi == 0 else None))
            bw_groups.append(list(zip(xs, ys)))

    # green/red gain callouts of the controlled arms vs none, drawn only
    # where the difference is narratively real: |gain| >= D2_GAIN_MIN_PCT
    # (the io.cost tier gains and, in the GRAND FINAL era, ALL of the
    # q_ratelimit gains -- its caps shape the weighted shares).  The
    # liveness parity note below now collects ONLY the arb arms: the fb
    # arb arms ride --allow-fallback liveness (shares fall back to 1:1), so
    # their sub-5% J_w wiggles around none are the stated parity, while a
    # sub-5% q_ratelimit wiggle would be a data anomaly -> stderr warning
    # (same channel as io_cost), never the figure note.  No bar-top value
    # labels:
    # the paper rq13_d2_jain_bw genre carries gains only (this deletion
    # also removes every label-vs-label / label-vs-BW-line overprint source
    # of the five-arm FAIL render).  Each callout keeps its white halo and
    # is fitted clear of bars / BW polyline / fairness line by measured
    # extents (_relayout_d2).
    ymax = 1.07                      # headroom for the "1.0000" label
    ctexts, parity_arms = [], []
    for gi, cg in enumerate(cgs):
        base = jain.get(("none", cg))
        for ai, arm in enumerate(arms):
            v = jain.get((arm, cg))
            if v is None or arm == "none" or not base:
                continue
            pct = (v / base - 1.0) * 100.0
            if abs(pct) < 0.05:
                continue
            if abs(pct) < D2_GAIN_MIN_PCT:
                if arm in ARB_ARMS:
                    if arm not in parity_arms:
                        parity_arms.append(arm)
                else:
                    warn(f"D2: {labels[arm]} cg{cg} gain {pct:+.1f}% below "
                         f"{D2_GAIN_MIN_PCT:g}% threshold; callout omitted")
                continue
            xc = float(x[gi] + offs[ai])
            color = GOOD if pct > 0 else BAD
            sign = "+" if pct > 0 else ""
            txt = f"{sign}{pct:.0f}%" if abs(pct) >= 9.95 else f"{sign}{pct:.1f}%"
            ct = ax.text(xc, v + 0.072, txt, ha="center", va="bottom",
                         fontsize=4.6, color=color, zorder=6, clip_on=False,
                         path_effects=WHITE_HALO)
            ctexts.append((ct, xc, v + 0.072))
            ymax = max(ymax, v + 0.072 + 0.075)

    ax.axhline(1.0, color=INK, lw=0.4, ls=(0, (1.5, 1.2)), zorder=1,
               label="perfect fairness")
    # the perfect-fairness dashed line keeps its numeric identity in-plot:
    # a small gray "1.0000" at its free right end (no bar reaches 1.0 past
    # the io_cost bar, no halo -> it stays real extractable PDF text)
    fair_txt = ax.text(len(cgs) - 0.47, 1.012, "1.0000", ha="right",
                       va="bottom", fontsize=4.2, color="#555555", zorder=6)
    ax.set_xlim(-0.55, len(cgs) - 0.45)
    ax.set_ylim(0, min(1.32, ymax + 0.045))
    ax.set_yticks([0, 0.5, 1.0])
    ax.set_ylabel(r"$J_w$ or BW / max")
    ax.set_xticks(x)
    ax.set_xticklabels([f"cg{c}" for c in cgs])
    # bottom margin widened to 0.20: the parity note needs the band under
    # the x tick labels (the axes interior is bar ink at every height)
    fig.subplots_adjust(left=0.22, right=0.98, bottom=0.20, top=0.72)
    _relayout_d2(fig, ax, ctexts, bar_rects, bw_groups,
                 soft_extra=[fair_txt])
    _box(ax)
    n_entries = len(arms) + 2          # arms + BW line + fairness line
    # 7 entries: a 4-column legend measures ~165 pt from the axes left,
    # past the 153 pt canvas (the clipped "iox q_arb" entry of the FAIL
    # five-arm render); 2 columns x 4 rows measures ~80 pt -- every entry
    # inside (measured; the 3-arm figure keeps its fitting 3 columns)
    _legend(ax, loc="lower left", ncol=(3 if n_entries <= 6 else 2),
            bbox_to_anchor=(0.0, 1.01))
    if parity_arms:
        # gray parity note at the figure bottom, under the x tick labels:
        # the plotted ARB arms ride --allow-fallback liveness, where the
        # arbiter survives but shares fall back to 1:1 (weights shape
        # shares only in strict mode), so J_w tracks none within noise.
        # The GF q_ratelimit arm never lands here -- its caps shape the
        # weighted shares (its gains are drawn as callouts instead).
        note = ("iox " + " / ".join(short[a] for a in parity_arms)
                + ": weighted shares require strict mode\n"
                  "(J_w \u2248 none in liveness mode)")
        fig.canvas.draw()              # measure the tick labels' extents
        ren = fig.canvas.get_renderer()
        tick_bot = min(t.get_window_extent(ren).y0
                       for t in ax.get_xticklabels())
        note_t = fig.text(0.5, 0.5, note, ha="center", va="top",
                          fontsize=3.6, color="#666666", linespacing=1.2)
        e = note_t.get_window_extent(ren)
        gap = 2.2 * fig.dpi / 72.0
        top_px = max(tick_bot - gap, e.height + gap)
        note_t.set_position((0.5, top_px / fig.bbox.height))
        e = note_t.get_window_extent(ren)
        if e.x0 < gap or e.x1 > fig.bbox.width - gap or e.y0 < gap:
            warn(f"D2 parity note overflows the canvas bottom band "
                 f"(bbox {e.x0:.1f},{e.y0:.1f}..{e.x1:.1f},{e.y1:.1f} px)")
    _save(fig, out_dir, "rq13_d2_bm")

    print(f"rq13_d2_bm <- {d2_root} (BW ref {bw_ref} MB/s)")
    for a in arms:
        row = ", ".join(
            f"cg{c}: J_w={jain[(a, c)]:.4f}" if jain[(a, c)] is not None
            else f"cg{c}: J_w=n/a"
            for c in cgs if (a, c) in jain)
        bws = ", ".join(
            f"cg{c}: BW={bw[(a, c)]:.0f}" if bw[(a, c)] is not None
            else f"cg{c}: BW=n/a"
            for c in cgs if (a, c) in bw)
        print(f"  {labels[a]:<24} {row} | {bws}")


# ---------------------------------------------------------------------------
# D3: analysis/d3_pareto.csv
# ---------------------------------------------------------------------------
def load_d3_points(root: Path, iox_label: str, arb_root: Path | None):
    """Ordered [(arm, label, row)] for the plotted arms of one D3 figure."""
    ctx = f"D3 root {root}"
    rows = rows_of(root / "analysis" / "d3_pareto.csv", ctx)
    need_cols(rows, ("arm", "lc_p99_ms_median", "lc_p99_ms_min",
                     "lc_p99_ms_max", "be_bw_mb_s_median", "be_bw_mb_s_min",
                     "be_bw_mb_s_max"), ctx)
    by_arm = {r["arm"]: r for r in rows}
    pts = []
    for arm in ARMS_BASE:
        if arm in by_arm:
            pts.append((arm, iox_label if arm == "iox_qonly" else ARM_LABEL[arm],
                        by_arm[arm], False))
        else:
            warn(f"D3 root {root}: arm {arm} absent from d3_pareto.csv")
    if arb_root is not None:
        arows = rows_of(arb_root / "analysis" / "d3_pareto.csv",
                        f"D3 arb root {arb_root}")
        aby = {r["arm"]: r for r in arows}
        for arm in ARB_ARMS:
            if arm in aby:
                pts.append((arm, ARM_LABEL[arm], aby[arm], False))
            else:
                warn(f"D3 arb root {arb_root}: arm {arm} absent")
        for arm in ("none", "io_cost", "iox_qonly"):
            if arm in aby:
                msg = (f"D3 arb root {arb_root}: arm {arm} ignored (only the "
                       "arb arms are taken from the arb root)")
                if arb_root.resolve() == root.resolve():
                    # GRAND FINAL single-batch wiring: same root for both
                    info(msg + "; it is already plotted from the primary "
                         "root (single-batch wiring)")
                else:
                    warn(msg)
    # arms whose clean-pool medians are empty (all runs device-phase-flagged,
    # e.g. the per-tenant q_ddl pathology on the unpatched kernel) are skipped
    # from the scatter and reported in the short bottom note instead (the
    # pathological cause is spelled out there once, not per arm).
    kept, notes = [], []
    for arm, label, row, light in pts:
        if ((row.get("lc_p99_ms_median") or "").strip() == ""
                or (row.get("be_bw_mb_s_median") or "").strip() == ""):
            notes.append(f"{label}: 0 clean runs")
            warn(f"D3: arm {arm} ({label}) skipped: empty clean medians")
        else:
            kept.append((arm, label, row, light))
    return kept, notes


def render_d3(out_dir: Path, d3_root: Path, iox_label: str, extra_root,
              arb_root: Path | None, solo_ms: float, slo_ms: float) -> None:
    pts, arm_notes = load_d3_points(d3_root, iox_label, arb_root)
    extra = None
    if extra_root is not None:
        erows = rows_of(extra_root / "analysis" / "d3_pareto.csv",
                        f"--extra-io-cost {extra_root}")
        by = {r["arm"]: r for r in erows}
        if "io_cost" in by:
            extra = (f"io.cost ({batch_tag(extra_root)} batch)", by["io_cost"])
        else:
            warn(f"--extra-io-cost {extra_root}: no io_cost arm in "
                 "d3_pareto.csv; extra point skipped")

    # GRAND FINAL confirmation, gated on the qonly arm BEING q_ratelimit
    # (the label says so; the pre-GF q_wedf/q_ddl/q_fifo variants are not
    # expected to cross): the q_ratelimit hetero point must sit BELOW the
    # SLO line (median AND max -- the probe was 0.1193 < 0.2365) and be the
    # deepest point of the bottom-right good corner: no plotted arm may
    # dominate it (LC <= and BE >= with one strict).
    q_pt = next((p for p in pts if p[0] == "iox_qonly"), None)
    if "ratelimit" in iox_label.lower():
        if q_pt is None:
            warn("D3: q_ratelimit SLO crossing not verified: the iox_qonly "
                 "arm has no clean point in d3_pareto.csv")
        else:
            q_row = q_pt[2]
            q_lc = float(q_row["lc_p99_ms_median"])
            q_lcmax = float(q_row["lc_p99_ms_max"])
            q_be = float(q_row["be_bw_mb_s_median"])
            check(q_lc < slo_ms,
                  f"D3 q_ratelimit: LC p99 median {q_lc} ms is NOT below "
                  f"the SLO line {slo_ms} ms")
            check(q_lcmax < slo_ms,
                  f"D3 q_ratelimit: LC p99 max {q_lcmax} ms is NOT below "
                  f"the SLO line {slo_ms} ms")
            comps = [(label, row) for _a, label, row, _l in pts
                     if _a != "iox_qonly"]
            if extra is not None:
                comps.append(extra)
            dominated = []
            for label, row in comps:
                try:
                    lc2 = float(row["lc_p99_ms_median"])
                    be2 = float(row["be_bw_mb_s_median"])
                except (KeyError, TypeError, ValueError):
                    continue          # non-numeric cell -> cannot judge
                if (lc2 <= q_lc and be2 >= q_be) \
                        and (lc2 < q_lc or be2 > q_be):
                    dominated.append(f"{label} (LC {lc2} ms, BE {be2} MB/s)")
            check(not dominated,
                  f"D3 q_ratelimit (LC {q_lc} ms, BE {q_be} MB/s) is "
                  f"dominated by {dominated} -- not the deepest "
                  "bottom-right good-corner point")
            if not dominated:
                print(f"  q_ratelimit hetero: LC p99 med {q_lc} ms (max "
                      f"{q_lcmax}) < SLO {slo_ms} ms; deepest "
                      "bottom-right point (no plotted arm dominates it)")

    # v2 AXES SWAP: y = LC read clat p99 (ms, log; DOWN = better), x = BE
    # aggregate read BW (MB/s; RIGHT = better).  The Pareto good corner is
    # the BOTTOM RIGHT (arb_only sits deepest in it: max BE median, min LC
    # median).  The SLO is the horizontal at y = 2x solo p99; the solo
    # anchor moves to (x=0, y=solo).
    fig, ax = plt.subplots(figsize=(FIG_W, 2.3))
    ax.axhline(slo_ms, color=INK, lw=0.8, ls=(0, (2.5, 2.0)), zorder=1)
    slo_txt = ax.text(40.0, slo_ms * 1.14, "SLO = 2\u00d7 solo p99",
                      fontsize=4.6, color=INK, style="italic", ha="left",
                      va="bottom", zorder=6)
    ax.plot([0.0], [solo_ms], marker="*", ms=5.5, color="#b30000",
            mec="0.25", mew=0.4, zorder=4, linestyle="none", clip_on=False)
    solo_txt = ax.text(120.0, solo_ms * 1.12, f"solo {solo_ms:.4g} ms",
                       fontsize=4.6, color="#555555", ha="left", va="bottom",
                       zorder=6)

    # the per-point clean-run count goes into the legend label (e.g.
    # "iox arb_only (n=3)"): floating n= tags next to the points landed on
    # the SLO text and far from their (overlapping) markers, so the
    # scatter stays label-free and the io_cost/iox marker pair is told
    # apart by the legend (the markers themselves may overlap).

    geoms: list = []       # the label-avoidance "off" table (data coords)

    def draw_point(label, row, style, light: bool):
        ctx = f"D3 {label}"
        ymed = num(row, "lc_p99_ms_median", ctx)
        ymin = num(row, "lc_p99_ms_min", ctx)
        ymaxv = num(row, "lc_p99_ms_max", ctx)
        xmed = num(row, "be_bw_mb_s_median", ctx)
        xmin = num(row, "be_bw_mb_s_min", ctx)
        xmax = num(row, "be_bw_mb_s_max", ctx)
        if None in (xmed, xmin, xmax, ymed, ymin, ymaxv):
            raise PlotError(f"{ctx}: d3_pareto.csv row has empty "
                            f"median/min/max cells: {row}")
        check(abs(ymed - float(row["lc_p99_ms_median"])) < 1e-9
              and abs(xmed - float(row["be_bw_mb_s_median"])) < 1e-9,
              f"D3 plotted point ({xmed}, {ymed}) != d3_pareto.csv "
              f"({row['be_bw_mb_s_median']}, {row['lc_p99_ms_median']})")
        color = style["color"]
        kw = dict(fmt=style["marker"], ms=4.6, ls="none", elinewidth=0.7,
                  capsize=2.2, capthick=0.7, mec="0.25", mew=0.55, zorder=3)
        if light:
            kw.update(mfc="white", alpha=0.75, elinewidth=0.5)
        else:
            kw.update(mfc=color)
        n_lc = (row.get("n_lc_clean") or "").strip()
        n_be = (row.get("n_be_clean") or "").strip()
        if n_lc and n_be and n_lc != n_be:
            label = f"{label} (n={n_lc}/{n_be})"
        elif n_lc:
            label = f"{label} (n={n_lc})"
        else:
            warn(f"{ctx}: no n_lc_clean column/value; "
                 "n not added to the legend label")
        geoms.append((xmed, ymed, xmin, xmax, ymin, ymaxv))
        return ax.errorbar(xmed, ymed,
                           xerr=[[xmed - xmin], [xmax - xmed]],
                           yerr=[[ymed - ymin], [ymaxv - ymed]],
                           color=color, label=label, **kw)

    handles = []
    summary = []
    for arm, label, row, _ in pts:
        handles.append(draw_point(label, row, ARM_STYLE[arm], light=False))
        summary.append((label, row))
    if extra is not None:
        handles.append(draw_point(extra[0], extra[1],
                                  ARM_STYLE["io_cost"], light=True))
        summary.append((extra[0], extra[1]))
    # legend label kept short: the value is annotated at the star itself
    # (a longer "solo (x.xxx ms)" overflows the right frame edge at 2.125in)
    handles.append(Line2D([0], [0], marker="*", ms=5.5, color="#b30000",
                          mec="0.25", mew=0.4, ls="none",
                          label="solo"))

    ax.set_yscale("log")
    ax.set_ylim(0.05, 100)
    ax.set_xlim(0, 3200)
    ax.set_xticks([0, 1000, 2000, 3000])
    ax.set_xlabel("BE aggregate read BW (MB/s)")
    ax.set_ylabel("LC read clat p99 (ms, log)")
    ax.xaxis.grid(True, color=GRID, lw=0.4, which="major", zorder=0)
    _box(ax)
    # the figure is only 8.5/4 in wide: with more than 4 entries a 3-column
    # legend overflows the right frame (measured), so wrap to 2 columns
    _legend(ax, handles=handles, loc="lower left",
            ncol=(2 if len(handles) > 4 else 3), bbox_to_anchor=(0.0, 1.01))
    fig.subplots_adjust(left=0.20, right=0.97, bottom=0.17, top=0.78)
    note_t = None
    if arm_notes:
        # the empty-arm note moves to the TOP LEFT inside the axes: with the
        # swapped axes the old bottom-left corner now hosts the solo anchor
        # star, while the top-left is the empty corner (points sit bottom
        # right, the SLO line at ~20% height).  The parenthetical names the
        # pathology generically (the GF case is the fb q_arb empty clean
        # pool; the pre-GF n3 case was the per-tenant q_ddl stall) -- the
        # per-batch cause lives in the campaign docs, not the figure
        note_t = ax.text(0.005, 0.955,
                         textwrap.fill(" ".join(arm_notes), 58)
                         + "\n(pathological empty clean pool; not plotted)",
                         transform=ax.transAxes, fontsize=3.6,
                         color="#666666", va="top", ha="left",
                         linespacing=1.1)

    # measured label avoidance (the layout engine, with the swapped-axes
    # obstacle table): every in-plot annotation is fitted clear of the
    # error bars/marker/caps, the SLO line, the solo star, the frame and
    # each other.  The SLO text keeps its task-fixed reading (left end,
    # above the line); the solo label hugs its star up-right.
    fig.canvas.draw()
    hard = [("frame", None, 0.15), ("hline", slo_ms, 1.3),
            ("circle", (0.0, solo_ms, 3.2), 0.4)]
    for xmed, ymed, xmin, xmax, ymin, ymaxv in geoms:
        hard += [("circle", (xmed, ymed, 3.0), 0.4),
                 ("seg", (xmed, ymin, xmed, ymaxv), 0.9),
                 ("seg", (xmin, ymed, xmax, ymed), 0.9),
                 ("circle", (xmed, ymin, 2.6), 0.3),
                 ("circle", (xmed, ymaxv, 2.6), 0.3),
                 ("circle", (xmin, ymed, 2.6), 0.3),
                 ("circle", (xmax, ymed, 2.6), 0.3)]
    soft = [t for t in (solo_txt, note_t) if t is not None]
    _fit_text(fig, ax, slo_txt,
              [(x, slo_ms * f)
               for f in (1.14, 1.26, 1.42, 1.62, 1.84, 2.05, 2.35, 2.70)
               for x in (40.0, 120.0, 280.0, 520.0)],
              hard, soft, what="D3 SLO label")
    soft = [t for t in (slo_txt, note_t) if t is not None]
    _fit_text(fig, ax, solo_txt,
              [(x, solo_ms * f) for f in (1.12, 1.32, 1.55, 1.80)
               for x in (120.0, 240.0, 420.0)],
              hard, soft, what="D3 solo label")
    _save(fig, out_dir, "rq13_d3_bm")

    print(f"rq13_d3_bm <- {d3_root} (SLO {slo_ms} ms, solo {solo_ms} ms)")
    for label, row in summary:
        print(f"  {label:<30} LC p99 med {row['lc_p99_ms_median']} ms "
              f"[{row['lc_p99_ms_min']}, {row['lc_p99_ms_max']}], "
              f"BE med {row['be_bw_mb_s_median']} MB/s "
              f"[{row['be_bw_mb_s_min']}, {row['be_bw_mb_s_max']}], "
              f"n_clean {row.get('n_lc_clean', '?')}/{row.get('n_be_clean', '?')}")


# ---------------------------------------------------------------------------
# D4: 1 ms clat logs inside the per-run tars -> 250 ms window series
# ---------------------------------------------------------------------------
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_tar(root: Path, seq: str) -> Path:
    """The tar of one D4 run (seq.tar.gz, or the single *.tar.gz in its dir)."""
    tar = root / "d4" / seq / f"{seq}.tar.gz"
    if not tar.is_file():
        tgz = sorted((root / "d4" / seq).glob("*.tar.gz")) \
            if (root / "d4" / seq).is_dir() else []
        tar = tgz[0] if len(tgz) == 1 else tar
    return tar


def run_logs_cached(tar: Path, scratch: Path):
    """(clat_log_or_None, [bw_logs]) of one run tar via the scratch cache.

    Entry key = <seq>_<tar sha256[:12]>; a marker holds the full fingerprint
    so a stale/foreign entry is never reused (same seq ids exist in several
    results roots -- the analyzer's extract-cache discipline).  A log KIND
    missing from the tar is not an error here (clat None / empty bw list):
    the caller decides which kinds it needs; a pre-existing clat-only cache
    entry (older renders) is transparently topped up with the bw logs."""
    if not tar.is_file():
        raise PlotError(f"missing tar {tar}")
    fp = _sha256_file(tar)
    entry = scratch / f"{tar.parent.name}_{fp[:12]}"
    clat = entry / "clat.1.log"
    marker = entry / ".tar_fingerprint"
    fp_ok = marker.is_file() and marker.read_text().strip() == fp
    bws = sorted(entry.glob("bw.*.log")) if fp_ok else []
    if fp_ok and clat.is_file() and bws:
        return clat, bws
    if entry.exists() and not fp_ok:
        shutil.rmtree(entry)
    entry.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar, "r:gz") as tf:
        members = tf.getmembers()
        if not (fp_ok and clat.is_file()):
            cands = [m for m in members
                     if m.name.endswith("_clat.1.log")
                     and "_lc" in (m.name.split("/")[-2] if "/" in m.name else "")]
            if len(cands) > 1:
                warn(f"{tar}: {len(cands)} clat-log members, using the first")
            if cands:
                clat.write_bytes(tf.extractfile(cands[0]).read())
        if not (fp_ok and bws):
            for m in members:
                base = m.name.split("/")[-1]
                parent = m.name.split("/")[-2] if "/" in m.name else ""
                mm = re.search(r"_bw\.(\d+)\.log$", base)
                if mm and "_be" in parent:
                    (entry / f"bw.{mm.group(1)}.log").write_bytes(
                        tf.extractfile(m).read())
    if not fp_ok:
        marker.write_text(fp + "\n")
    bws = sorted(entry.glob("bw.*.log"))
    return (clat if clat.is_file() else None), bws


def _bw_log_rel_series(path: Path):
    """One bw log -> (t_s relative to that job's start, MB/s).

    analyze_d2d4_bm.py _bw_log_rel_series verbatim (fio write_bw_log,
    log_avg_msec=1000 -> one averaged sample per second, KiB/s -> MiB/s)."""
    samples = []
    for line in path.read_text().splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            samples.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    if not samples:
        return [], []
    t0 = samples[0][0]
    epoch_like = t0 > 1e12                  # unix epoch ms
    if epoch_like:
        ts = [(t - t0) / 1000.0 for t, _ in samples]
    else:
        ts = [t / 1000.0 for t, _ in samples]
    return ts, [v / 1024.0 for _, v in samples]


def be_bw_series(logs: list):
    """Aggregate BE bw series (t_s from BE start, MB/s) over extracted logs.

    analyze_d2d4_bm.py be_bw_series verbatim: single log -> its own series;
    N > 1 logs (v2 --d4-be 4x256/4x1024) -> each job's log reduced to
    seconds-since-its-own-start, bucketed to integer seconds and SUMMED;
    only bins covered by every active job are kept."""
    if not logs:
        return [], []
    if len(logs) == 1:
        return _bw_log_rel_series(logs[0])
    sums: dict = {}
    cover: dict = {}
    n_active = 0
    for log in logs:
        ts, bw = _bw_log_rel_series(log)
        if not ts:
            continue
        n_active += 1
        for t, v in zip(ts, bw):
            b = int(round(t))
            sums[b] = sums.get(b, 0.0) + v
            cover[b] = cover.get(b, 0) + 1
    if not sums or n_active == 0:
        return [], []
    full = [b for b in sorted(sums) if cover[b] == n_active]
    if len(full) < 5:      # degenerate overlap -> treat as unusable
        return [], []
    return [float(b) for b in full], [sums[b] for b in full]


def parse_clat_log(path: Path):
    """fio clat time log (log_avg_msec=1) -> ([t_ms], [latency_ns])."""
    ts, vs = [], []
    for line in path.read_text().splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            ts.append(float(parts[0]))
            vs.append(float(parts[1]))
        except ValueError:
            continue
    if not ts:
        raise PlotError(f"{path}: clat log has no parseable samples")
    return ts, vs


def win_p99_analyze(ts, vs, win_ms=D4_WIN_MS, min_n=D4_MIN_N):
    """analyze_d2d4_bm.py lc_window_p99 verbatim math: windows anchored at
    the first sample, p99 index ceil(n*0.99)-1; {win_start_s: p99_us}."""
    t0 = ts[0]
    out: dict = {}
    for start in range(0, int(ts[-1] - t0) + 1, win_ms):
        vals = sorted(v / 1000.0 for t, v in zip(ts, vs)   # ns -> us
                      if start <= t - t0 < start + win_ms)
        if len(vals) < min_n:
            continue
        idx = max(0, math.ceil(len(vals) * 0.99) - 1)
        out[start / 1000.0] = vals[idx]
    return out


def win_p99_render(ts, vs, win_s=D4_WIN_MS / 1000.0, min_n=D4_MIN_N):
    """render_paper_d4_timeseries.py win_p99 math: windows from t=0, linear-
    interpolation p99; {window_index: p99_us} (x = (k+0.5)*win)."""
    t = [x / 1000.0 for x in ts]      # ms -> s (log-relative start)
    v = [y / 1000.0 for y in vs]      # ns -> us
    n_win = int(math.floor(max(t) / win_s)) + 1
    out: dict = {}
    for k in range(n_win):
        a, b = k * win_s, (k + 1) * win_s
        sel = [y for x, y in zip(t, v) if a <= x < b]
        if len(sel) < min_n:
            continue
        out[k] = float(np.percentile(np.asarray(sel), 99))
    return out


def load_d4_series(root: Path, arms: tuple, scratch: Path):
    """{arm: (xs_s, p99_us medians, p99_us mins, p99_us maxs, n_runs,
    per_run_peaks_ms)} from the phase-clean runs of one D4 root.

    The mins/maxs are the per-window min/max across the clean runs -- the
    top-panel envelope band (the q_wedf window-p99 oscillation and the
    run-to-run spread both live in it)."""
    ctx = f"D4 root {root}"
    runs = rows_of(root / "analysis" / "d4_runs.csv", ctx)
    need_cols(runs, ("sequence", "arm", "gate_failed", "lc_p99_250ms_peak_ms"),
              ctx)
    has_phase = "device_phase_lc" in runs[0]
    if not has_phase:
        warn(f"{ctx}: d4_runs.csv has no device_phase_lc column; using every "
             "gate-clean run for the LC time series")
    agg = {}
    agg_path = root / "analysis" / "d4_agg.csv"
    if agg_path.is_file():
        for r in rows_of(agg_path, ctx):
            agg[r["arm"]] = r

    out: dict = {}
    for arm in arms:
        pool = [r for r in runs if r["arm"] == arm
                and (r.get("gate_failed") or "0") == "0"
                and (not has_phase
                     or (r.get("device_phase_lc") or "").strip() == "clean")]
        if not pool:
            warn(f"{ctx}: no phase-clean run for arm {arm}; line omitted")
            continue
        per_run: dict = {}      # window index -> [p99_us]
        peaks_ms = []
        skipped = 0             # pool runs unusable (missing/mid-resync tar)
        for r in pool:
            seq = r["sequence"]
            tar = _run_tar(root, seq)
            try:
                clat, _bws = run_logs_cached(tar, scratch)
                if clat is None:
                    raise PlotError(f"{tar}: no LC clat log member "
                                    "(step_*/*_lc*/*_clat.1.log) inside "
                                    "the tar")
                ts, vs = parse_clat_log(clat)
            except PlotError as exc:
                warn(f"{ctx}: run {seq} skipped ({exc}); its windows are "
                     "left out of the arm median")
                skipped += 1
                continue
            # frozen-analyzer convention: assert against d4_runs.csv
            ana = win_p99_analyze(ts, vs)
            peak_us = max(ana.values())
            csv_peak = (r.get("lc_p99_250ms_peak_ms") or "").strip()
            if csv_peak:
                check(abs(peak_us / 1000.0 - float(csv_peak)) <= 1.05e-4,
                      f"D4 {seq}: recomputed 250 ms peak "
                      f"{peak_us / 1000.0:.4f} ms != d4_runs.csv {csv_peak}")
            else:
                warn(f"{ctx}: run {seq} has no lc_p99_250ms_peak_ms value; "
                     "peak not asserted")
            peaks_ms.append(peak_us / 1000.0)
            for k, v in win_p99_render(ts, vs).items():
                per_run.setdefault(k, []).append(v)
        if not per_run:
            warn(f"{ctx}: arm {arm} has no usable clat log; line omitted")
            continue
        # the arm-level median assertion only holds when the WHOLE phase-
        # clean pool was usable (d4_agg.csv was computed before any run dir
        # went missing)
        if skipped == 0 and arm in agg \
                and agg[arm].get("lc_p99_250ms_peak_ms_median", "").strip():
            want = float(agg[arm]["lc_p99_250ms_peak_ms_median"])
            got = round(statistics.median(peaks_ms), 4)
            check(abs(got - want) <= 1.05e-4,
                  f"D4 arm {arm}: median of recomputed 250 ms peaks {got} "
                  f"!= d4_agg.csv {want} (n={len(peaks_ms)} runs)")
        keys = sorted(per_run)
        xs = [(k + 0.5) * D4_WIN_MS / 1000.0 for k in keys]
        ys = [statistics.median(per_run[k]) for k in keys]
        lo = [min(per_run[k]) for k in keys]
        hi = [max(per_run[k]) for k in keys]
        out[arm] = (xs, ys, lo, hi, len(peaks_ms), peaks_ms)
    return out


def load_d4_be_series(root: Path, arms: tuple, scratch: Path):
    """{arm: (xs_s, bw_mibs_medians, n_runs, dip_med)} from the gate-clean,
    device_phase_be-clean pool of one D4 root.

    xs = seconds since burst arrival (BE-log t - BURST_AT_BE_S).  The
    per-run aggregate is the analyzer be_bw_series over the tar's d4_be
    write_bw_log members; the per-run dip% (pre-burst mean vs burst-window
    min) is asserted against d4_runs.csv and its pool median against
    d4_agg.csv be_dip_pct_median (the analyzer's be-side phase pool), so
    the bottom-panel dip% labels are the analyzer's own number."""
    ctx = f"D4 root {root}"
    runs = rows_of(root / "analysis" / "d4_runs.csv", ctx)
    need_cols(runs, ("sequence", "arm", "gate_failed", "be_dip_pct"), ctx)
    has_phase = "device_phase_be" in runs[0]
    if not has_phase:
        warn(f"{ctx}: d4_runs.csv has no device_phase_be column; using every "
             "gate-clean run for the BE time series")
    agg = {}
    agg_path = root / "analysis" / "d4_agg.csv"
    if agg_path.is_file():
        for r in rows_of(agg_path, ctx):
            agg[r["arm"]] = r

    out: dict = {}
    for arm in arms:
        pool = [r for r in runs if r["arm"] == arm
                and (r.get("gate_failed") or "0") == "0"
                and (not has_phase
                     or (r.get("device_phase_be") or "").strip() == "clean")]
        if not pool:
            warn(f"{ctx}: no phase-clean BE run for arm {arm}; "
                 "BE line omitted")
            continue
        per_bin: dict = {}      # BE-log second -> [mibs]
        dips: list = []         # per-run rounded dip% (analyzer rounding)
        skipped = 0
        for r in pool:
            seq = r["sequence"]
            tar = _run_tar(root, seq)
            try:
                _clat, bws = run_logs_cached(tar, scratch)
                ts, bw = be_bw_series(bws)
                if not ts:
                    raise PlotError(f"{tar}: no usable d4_be write_bw_log "
                                    "members inside the tar")
            except PlotError as exc:
                warn(f"{ctx}: run {seq} skipped ({exc}); its bins are left "
                     "out of the arm BE median")
                skipped += 1
                continue
            pre = [b for t, b in zip(ts, bw) if t < BURST_AT_BE_S]
            dur = [b for t, b in zip(ts, bw)
                   if BURST_AT_BE_S <= t < BURST_AT_BE_S + BURST_LEN_S]
            if not pre or not dur:
                warn(f"{ctx}: run {seq} has no pre-burst or burst bins in "
                     "its BE series; run skipped")
                skipped += 1
                continue
            pre_mean = sum(pre) / len(pre)
            burst_min = min(dur)
            # frozen-analyzer convention (d4_row): assert against d4_runs.csv
            cells = ((pre_mean, "be_pre_burst_mean_mibs"),
                     (burst_min, "be_burst_min_mibs"))
            for got, key in cells:
                want = (r.get(key) or "").strip()
                if want:
                    check(abs(round(got, 2) - float(want)) <= 0.011,
                          f"D4 {seq}: recomputed {key} {round(got, 2)} "
                          f"!= d4_runs.csv {want}")
                else:
                    warn(f"{ctx}: run {seq} has no {key} value; "
                         "not asserted")
            dip = round((pre_mean - burst_min) / pre_mean * 100.0, 2)
            want = (r.get("be_dip_pct") or "").strip()
            if want:
                check(abs(dip - float(want)) <= 0.011,
                      f"D4 {seq}: recomputed be_dip_pct {dip} "
                      f"!= d4_runs.csv {want}")
            else:
                warn(f"{ctx}: run {seq} has no be_dip_pct value; "
                     "dip not asserted")
            dips.append(dip)
            for t, b in zip(ts, bw):
                per_bin.setdefault(int(round(t)), []).append(b)
        if not per_bin:
            warn(f"{ctx}: arm {arm} has no usable BE series; line omitted")
            continue
        # the arm-median assertion mirrors the analyzer pipeline exactly:
        # median over the per-run 2-decimal dips, rounded to 4 decimals
        if skipped == 0 and arm in agg \
                and (agg[arm].get("be_dip_pct_median") or "").strip():
            want = float(agg[arm]["be_dip_pct_median"])
            got = round(statistics.median(dips), 4)
            check(abs(got - want) <= 1.05e-4,
                  f"D4 arm {arm}: median of recomputed BE dips {got} "
                  f"!= d4_agg.csv {want} (n={len(dips)} runs)")
        keys = sorted(per_bin)
        xs = [float(k) - BURST_AT_BE_S for k in keys]
        ys = [statistics.median(per_bin[k]) for k in keys]
        out[arm] = (xs, ys, len(dips),
                    round(statistics.median(dips), 4) if dips else None)
    return out


def render_d4(out_dir: Path, d4_root: Path, iox_label: str, shape: str,
              arb_root: Path | None, solo_ms: float, scratch: Path) -> None:
    lc = load_d4_series(d4_root, ARMS_BASE, scratch)
    be = load_d4_be_series(d4_root, ARMS_BASE, scratch)
    if arb_root is not None:
        lc.update(load_d4_series(arb_root, ARB_ARMS, scratch))
        be.update(load_d4_be_series(arb_root, ARB_ARMS, scratch))
    order = [a for a in ARMS_BASE + ARB_ARMS if a in lc or a in be]
    if not order:
        raise PlotError(f"D4 root {d4_root}: no arm has a usable LC or BE "
                        "series")
    labels = {a: (iox_label if a == "iox_qonly" else ARM_LABEL[a])
              for a in order}
    for a in order:
        if a not in lc:
            labels[a] += " (BE only)"
            warn(f"D4 arm {a}: no phase-clean LC run; the arm is carried "
                 "by the BE panel only")
    top_arms = [a for a in order if a in lc]
    be_arms = [a for a in order if a in be]

    solo_us = solo_ms * 1000.0
    lows = [v for a in top_arms for v in lc[a][2]]
    highs = [v for a in top_arms for v in lc[a][3]]
    series_min = min(lows) if lows else solo_us
    series_max = max(highs) if highs else solo_us
    # below the solo reference by default, but never above the lowest band
    # (the deep-shape io_cost medians dip to ~34 us, under the solo anchor)
    ylo = min(solo_us * 0.72, series_min * 0.8)
    yhi = max(series_max * 2.6, solo_us * 2.6)

    # two stacked panels sharing x = time since burst arrival; the panel
    # heights keep the 1.5 : 1.0 split (top 1.15 in, bottom 0.76 in at the
    # 2.70 in figure), hspace = 0 so the burst-arrival rule reads as one
    # line through both panels
    fig, (ax1, ax2) = plt.subplots(
        2, 1, sharex=True, figsize=(FIG_W, 2.70),
        gridspec_kw={"height_ratios": [1.5, 1.0], "hspace": 0.0})
    for ax in (ax1, ax2):
        ax.axvspan(D4_XLO, 0.0, color="0.93", zorder=0)
        ax.axvline(0.0, color=BURST, lw=1.1, zorder=2)
        # burst-window end (the LC burst is 10 s): a light dotted rule
        # delimiting the burst window from the BE recovery zone
        ax.axvline(BURST_LEN_S, color=REF, lw=0.5, ls=(0, (1, 1)), zorder=1)
    ax1.plot([D4_XLO, D4_XHI], [solo_us, solo_us], color=REF, lw=0.7,
             ls=(0, (3, 1.8)), zorder=2)
    for arm in top_arms:
        xs, ys, lo, hi, _n, _peaks = lc[arm]
        color, ls, lw = D4_LINE[arm]
        # min-max envelope band across the clean runs (alpha 0.15 wash):
        # the q_wedf window-p99 oscillation and the run-to-run spread both
        # live in its width
        ax1.fill_between(xs, lo, hi, color=color, alpha=0.15, lw=0,
                         zorder=2)
        ax1.plot(xs, ys, color=color, ls=ls, lw=lw, zorder=3)
    for arm in be_arms:
        xs, ys, _n, _dip = be[arm]
        color, ls, _lw = D4_LINE[arm]
        ax2.plot(xs, ys, color=color, ls=ls, lw=0.8, alpha=0.8, zorder=3)

    for ax in (ax1, ax2):
        _box(ax)
    ax1.set_yscale("log")
    ax1.set_ylim(ylo, yhi)
    ticks = [10 ** e for e in range(1, 7) if ylo <= 10 ** e <= yhi]
    ax1.set_yticks(ticks)
    ax1.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(
            lambda v, _p: f"{v / 1000:g} ms" if v >= 1000 else fr"{v:g} $\mu$s"))
    ax1.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax1.set_ylabel("LC read p99")
    ax1.tick_params(axis="x", which="both", bottom=False,
                    labelbottom=False)
    ax2.set_ylim(0, D4_BE_YMAX)
    ax2.set_yticks(D4_BE_YTICKS)
    ax2.set_ylabel("BE aggregate BW (MB/s)")
    for ax in (ax1, ax2):
        ax.set_xlim(D4_XLO, D4_XHI)
    ax2.set_xticks([-10, -5, 0, 5, 10, 15, 20])
    ax2.set_xlabel("Time since burst arrival (s)")

    ymid = math.sqrt(ylo * yhi)
    be_only = ax1.text(D4_XLO / 2.0, ymid, "BE-only\nsaturation",
                       fontsize=4.2, color="#666666", ha="center",
                       va="center", style="italic", rotation=90,
                       linespacing=1.2)
    # the solo label folds to two lines: the pre-burst window is 13 s wide
    # on the shared axis now, and the one-liner ran past the burst line
    solo_label = ax1.text(D4_XLO + 0.4, solo_us * 1.08,
                         "uncontended LC p99\n"
                         f"(solo, {solo_ms:.3f} ms)",
                         fontsize=4.6, color="#555555", ha="left",
                         va="bottom", style="italic", zorder=6,
                         linespacing=1.2,
                         bbox=dict(facecolor="white", edgecolor="none",
                                   alpha=0.75, pad=0.6))
    # burst-arrival marker, paper style: side arrow from the vertical line
    # (through both panels).  The label is fitted into the empty upper band
    # by measured clearance, as before.
    burst = ax1.annotate("burst arrives", xy=(0.0, ymid), xytext=(2.2, ymid),
                         fontsize=5.0, color=BURST, va="center", ha="left",
                         arrowprops=dict(arrowstyle="-", lw=0.5, color=BURST))
    note_text = ax1.text(0.985, 0.96,
                         "p99 per 250 ms window; median line\n"
                         "with min\u2013max band over clean runs\n"
                         "LC burst: 4K randread QD1 (10 s)",
                         transform=ax1.transAxes, ha="right", va="top",
                         fontsize=3.8, color="#555555", style="italic",
                         linespacing=1.3,
                         bbox=dict(facecolor="white", edgecolor="none",
                                   alpha=0.75, pad=0.6))
    shape_note = ("BE 4\u00d7QD256 agg 1024" if shape == "4x256"
                  else "BE 4\u00d7QD1024 agg 4096")
    # bottom-panel captions: the BE shape note sits in the empty band under
    # the BE curves at the right, the dip% definition at the left
    shape_t = ax2.text(D4_XHI - 0.4, D4_BE_YMAX * 0.035, shape_note,
                       ha="right", va="bottom", fontsize=3.8,
                       color="#555555", style="italic",
                       bbox=dict(facecolor="white", edgecolor="none",
                                 alpha=0.75, pad=0.5))
    dip_note = ax2.text(D4_XLO + 0.4, D4_BE_YMAX * 0.035,
                        "dip% = burst drop vs\npre-burst mean\n"
                        "(median, clean runs)",
                        ha="left", va="bottom", fontsize=3.4,
                        color="#666666", linespacing=1.15)
    # left=0.235 (not the paper's 0.17): the wide '100 us'/'1 ms' y-tick
    # labels already fill a 0.17 margin on this figure size, and with 0.17
    # the rotated y-axis title lands past the canvas edge (clipped
    # invisible -- measured, matplotlib 3.10); 0.235 keeps the ylabel and
    # the tick labels fully inside the canvas
    fig.subplots_adjust(left=0.235, right=0.975, bottom=0.135, top=0.84)

    # one shared legend on top of the figure: the arm lines (both panels
    # use the same arm color/ls) plus a small panel-semantics title
    handles = [Line2D([0], [0], color=D4_LINE[a][0], ls=D4_LINE[a][1],
                      lw=1.2, label=labels[a]) for a in order]
    leg = _legend(ax1, handles=handles, loc="lower left",
                  ncol=(2 if len(order) > 4 else 3),
                  bbox_to_anchor=(0.0, 1.01))
    leg.set_title("top: LC read p99 \u00b7 bottom: BE aggregate BW (MB/s)",
                  prop=dict(size=3.6, style="italic"))
    leg.get_title().set_color("#555555")

    fig.canvas.draw()          # finalize layout before measuring text boxes
    gap = 3.0 * fig.dpi / 72.0
    ren = fig.canvas.get_renderer()
    for _try in range(2):
        le = leg.get_window_extent(ren)
        if le.y1 <= fig.bbox.height - gap:
            break
        fig.subplots_adjust(
            top=ax1.get_position().y1
            - (le.y1 - (fig.bbox.height - gap)) / fig.bbox.height)
        fig.canvas.draw()
        ren = fig.canvas.get_renderer()
    le = leg.get_window_extent(ren)
    if le.y1 > fig.bbox.height - 1.0 or le.x1 > fig.bbox.width - 1.0 \
            or le.x0 < 1.0:
        warn(f"D4 legend overflows the canvas (bbox {le.x0:.1f},{le.y0:.1f}"
             f"..{le.x1:.1f},{le.y1:.1f} px)")

    # top-panel label fits (the layout engine, measured)
    hard1 = [("frame", None, 0.15)]
    for arm in top_arms:
        xs, ys, _lo, _hi, _n, _p = lc[arm]
        pts = list(zip(xs, ys))
        hard1 += [("seg", (a[0], a[1], b[0], b[1]), 0.4)
                  for a, b in zip(pts, pts[1:])]
    hard1.append(("seg", (D4_XLO, solo_us, D4_XHI, solo_us), 0.4))
    hard_static = [("vline", 0.0, 0.9), ("vline", BURST_LEN_S, 0.7),
                   ("hline", solo_us, 0.8), ("frame", None, 0.15)]
    _fit_text(fig, ax1, solo_label,
              [(D4_XLO + dx, solo_us * f) for f in (1.08, 1.30, 1.55)
               for dx in (0.4, 2.5, 5.0, 8.0)],
              hard_static, [be_only, note_text, burst],
              what="D4 solo label")
    _fit_text(fig, ax1, be_only,
              [(x, ylo * (yhi / ylo) ** f)
               for f in (0.50, 0.42, 0.58, 0.34, 0.66)
               for x in (D4_XLO / 2.0, -9.5, -3.5)],
              hard_static, [solo_label, note_text, burst],
              what="D4 BE-only label")
    # mid-gap fractions first (between the solo line and the top curve band,
    # clear of the top-right note block); upper-band spots kept as fallback.
    spots = [(2.2, ylo * (yhi / ylo) ** f)
             for f in (0.58, 0.52, 0.46, 0.42,
                       0.84, 0.80, 0.88, 0.76, 0.91, 0.72, 0.68, 0.62)]
    by = _fit_text(fig, ax1, burst, spots, hard1,
                   [solo_label, be_only, note_text], what="D4 'burst arrives'",
                   on_spot=lambda t, s: setattr(t, "xy", (0.0, s[1])))
    # the side arrow (vline -> text) must clear the arm lines as well
    arr_obs = [o for o, _t in _obst_disp(fig, ax1, hard1) if o[0] == "poly"]
    arr_pts = _obst_disp(
        fig, ax1, [("seg", (0.0, by[1], 2.2, by[1]), 0.0)])[0][0][1]
    arr_clear = min(_clearance_px((px - 0.5, py - 0.5, px + 0.5, py + 0.5), o)
                    for o in arr_obs for px, py in arr_pts)
    if arr_clear < 0.4 * fig.dpi / 72.0:
        warn(f"D4 burst arrow grazes an arm line (clearance "
             f"{arr_clear * 72.0 / fig.dpi:.2f} pt)")

    # bottom-panel dip% labels: arm-colored, fitted by the layout engine
    # clear of every BE line, both vertical rules, the two captions and
    # each other (deepest burst first).  Two passes per arm: below the
    # burst-segment minimum first, above it as a fallback when the below
    # pocket is occupied -- the io_cost/wedf/q_arb curves crowd the band
    # under arb_only's segment, and a deep below-spot there would leave
    # the label hugging a FOREIGN line.  The pass with the shallower
    # clean spot wins.
    def _burst_min_pt(arm):
        xs, ys, _n, _dip = be[arm]
        pts = [(t, v) for t, v in zip(xs, ys)
               if 0.0 <= t < BURST_LEN_S]
        return min(pts, key=lambda p: p[1])

    be_segs = []
    for arm in be_arms:
        xs, ys, _n, _dip = be[arm]
        pts = list(zip(xs, ys))
        be_segs += [("seg", (a[0], a[1], b[0], b[1]), 1.15)
                    for a, b in zip(pts, pts[1:])]
    hard_be = be_segs + [("vline", 0.0, 0.9), ("vline", BURST_LEN_S, 0.7),
                         ("frame", None, 0.15)]
    dip_dxs = (0.8, -1.2, 2.7, -3.1, 4.6, -5.0, 6.5)
    ren = fig.canvas.get_renderer()
    hard_be_disp = _obst_disp(fig, ax2, hard_be)
    soft_px = 0.2 * fig.dpi / 72.0

    def _dip_slack(t):
        """Final clearance (px) of a placed dip label to lines/rules/softs."""
        e = t.get_window_extent(ren)
        r = (e.x0, e.y0, e.x1, e.y1)
        s = min(_clearance_px(r, o) - thr for o, thr in hard_be_disp)
        for other in dip_texts + [dip_note, shape_t]:
            e2 = other.get_window_extent(ren)
            m = 0.25 * other.get_fontsize() * fig.dpi / 72.0
            sb = (e2.x0 + m, e2.y0 + m, e2.x1 - m, e2.y1 - m)
            dx = max(r[0] - sb[2], sb[0] - r[2])
            dy = max(r[1] - sb[3], sb[1] - r[3])
            s = min(s, max(dx, dy) - soft_px)
        return s

    dip_texts, dip_spots = [], {}
    for arm in sorted(be_arms, key=lambda a: -_burst_min_pt(a)[1]):
        xs, ys, _n, dip = be[arm]
        if dip is None:
            warn(f"D4 arm {arm}: no recomputed burst dip; label omitted")
            continue
        bx, by_v = _burst_min_pt(arm)
        color = D4_LINE[arm][0]
        soft_now = list(dip_texts) + [dip_note, shape_t]
        kw = dict(fontsize=3.8, color=color, ha="left", zorder=6)
        below = ax2.text(bx + 0.8, by_v - 150.0, f"{dip:.1f}%", va="top",
                         **kw)
        spot = _fit_text(
            fig, ax2, below,
            [(bx + dx, by_v - dy)
             for dy in (150.0, 300.0, 450.0, 600.0, 750.0, 900.0, 1050.0)
             for dx in dip_dxs],
            hard_be, soft_now, what=f"D4 dip% below {labels[arm]!r}")
        chosen = below
        if abs(spot[1] - by_v) > 560.0:     # deep pocket: try above the line
            above = ax2.text(bx + 0.8, by_v + 150.0, f"{dip:.1f}%",
                             va="bottom", **kw)
            spot_a = _fit_text(
                fig, ax2, above,
                [(bx + dx, by_v + dy)
                 for dy in (150.0, 300.0, 450.0, 600.0, 750.0)
                 for dx in dip_dxs],
                hard_be, soft_now, what=f"D4 dip% above {labels[arm]!r}")
            sa, sb = _dip_slack(above), _dip_slack(below)
            if abs(spot_a[1] - by_v) < abs(spot[1] - by_v) \
                    and (sa >= 0.0 or sa >= sb):
                below.remove()
                chosen, spot = above, spot_a
            else:
                above.remove()
        dip_texts.append(chosen)
        dip_spots[arm] = spot

    _save(fig, out_dir, "rq13_d4_bm")

    print(f"rq13_d4_bm <- {d4_root} (BE shape {shape})")
    if be_arms:
        cover = [x for a in be_arms for x in be[a][0]]
        print(f"  BE curve coverage: pre-burst BE-only window "
              f"-{BURST_AT_BE_S:g}..0 s (panel shows from {D4_XLO:g}), "
              f"burst 0..{BURST_LEN_S:g} s, recovery to "
              f"+{max(cover):.0f} s; y 0..{D4_BE_YMAX:g} MB/s")
    for arm in order:
        parts = []
        if arm in lc:
            xs, ys, lo, hi, n, peaks = lc[arm]
            parts.append(
                f"n={n} clean run(s), window p99 medians "
                f"{min(ys) / 1000:.4f}-{max(ys) / 1000:.4f} ms "
                f"(band {min(lo) / 1000:.4f}-{max(hi) / 1000:.4f}), "
                f"per-run 250 ms peaks {['%.4f' % p for p in peaks]}")
        if arm in be:
            xs, ys, n, dip = be[arm]
            s = (f"BE agg BW median {min(ys):.0f}-{max(ys):.0f} MB/s over "
                 f"{n} clean run(s), burst dip {dip}%")
            if arm in dip_spots:
                s += (f", dip label at (x={dip_spots[arm][0]:+.1f} s, "
                      f"y={dip_spots[arm][1]:.0f} MB/s)")
            parts.append(s)
        print(f"  {labels[arm]:<24} " + "; ".join(parts))


# ---------------------------------------------------------------------------
# shared style helpers (render_paper_d4_timeseries.py / render_rq1_figs.py)
# ---------------------------------------------------------------------------
def _clearance_px(rect, obst):
    """Signed display-space clearance (px) of rect to one obstacle.

    Positive = gap, negative = penetration. rect = (x0, y0, x1, y1);
    obstacles: ("rect", (x0, y0, x1, y1)) | ("poly", [(x, y), ...]) |
    ("circle", (cx, cy, r)) | ("inside", (x0, y0, x1, y1)) -- a frame that
    the rect must stay within.
    """
    rx0, ry0, rx1, ry1 = rect
    kind, a = obst
    if kind == "rect":
        ox0, oy0, ox1, oy1 = a
        dx = max(rx0 - ox1, ox0 - rx1)
        dy = max(ry0 - oy1, oy0 - ry1)
        return max(dx, dy)
    if kind == "poly":
        worst = 1e18
        for px, py in a:
            inside = rx0 <= px <= rx1 and ry0 <= py <= ry1
            if inside:
                worst = min(worst, -min(px - rx0, rx1 - px, py - ry0, ry1 - py))
            else:
                qx = min(max(px, rx0), rx1)
                qy = min(max(py, ry0), ry1)
                worst = min(worst, math.hypot(px - qx, py - qy))
        return worst
    if kind == "circle":
        cx, cy, r = a
        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            return -min(cx - rx0, rx1 - cx, cy - ry0, ry1 - cy) - r
        qx = min(max(cx, rx0), rx1)
        qy = min(max(cy, ry0), ry1)
        return math.hypot(cx - qx, cy - qy) - r
    if kind == "inside":
        fx0, fy0, fx1, fy1 = a
        return min(rx0 - fx0, ry0 - fy0, fx1 - rx1, fy1 - ry1)
    raise ValueError(f"unknown obstacle kind {kind!r}")


def _obst_disp(fig, ax, hard):
    """Data-space obstacles -> display-space primitives + thresholds (px).

    Entries: ("rect", (x0, y0, x1, y1), thr_pt) | ("seg", (xa, ya, xb, yb),
    thr_pt) | ("circle", (cx, cy, r_pt), thr_pt) | ("hline", y, thr_pt) |
    ("vline", x, thr_pt) | ("frame", None, thr_pt).  Segments and lines are
    sampled in data space, so log axes (which bend them) are covered.
    """
    tr = ax.transData.transform
    dpi = fig.dpi
    out = []

    def poly(p0, p1):
        return [(p0[0] + (p1[0] - p0[0]) * k / 24.0,
                 p0[1] + (p1[1] - p0[1]) * k / 24.0) for k in range(25)]

    for kind, a, thr in hard:
        tpx = thr * dpi / 72.0
        if kind == "rect":
            (x0, y0) = tr((a[0], a[1]))
            (x1, y1) = tr((a[2], a[3]))
            out.append((("rect", (min(x0, x1), min(y0, y1),
                                 max(x0, x1), max(y0, y1))), tpx))
        elif kind == "seg":
            out.append((("poly", poly(tr((a[0], a[1])), tr((a[2], a[3])))),
                       tpx))
        elif kind == "circle":
            (cx, cy) = tr((a[0], a[1]))
            out.append((("circle", (cx, cy, a[2] * dpi / 72.0)), tpx))
        elif kind == "hline":
            bb = ax.bbox
            out.append((("poly", poly((bb.x0, tr((0.0, a))[1]),
                                      (bb.x1, tr((0.0, a))[1]))), tpx))
        elif kind == "vline":
            bb = ax.bbox
            out.append((("poly", poly((tr((a, 0.0))[0], bb.y0),
                                      (tr((a, 0.0))[0], bb.y1))), tpx))
        elif kind == "frame":
            bb = ax.bbox
            out.append((("inside", (bb.x0, bb.y0, bb.x1, bb.y1)), tpx))
        else:
            raise ValueError(f"unknown hard obstacle {kind!r}")
    return out


def _fit_text(fig, ax, t, spots, hard, soft, soft_min_pt=0.2, what="",
              on_spot=None):
    """Place Text t on the first of `spots` (data xy) clear of everything.

    A spot is clean when every hard obstacle clears its own threshold (pt)
    and every other Text in `soft` (layout box shrunk by a quarter of the
    font size per side) keeps a soft_min_pt gap.  on_spot(t, spot) runs
    after each set_position (annotations must move their arrow head too:
    their window extent covers text AND arrow).  With no clean spot the
    max-slack spot is kept and a stderr note is printed -- the white halo
    stroke keeps sub-point grazing legible.  Returns the chosen (x, y).
    """
    ren = fig.canvas.get_renderer()
    hard_disp = _obst_disp(fig, ax, hard)
    soft_px = soft_min_pt * fig.dpi / 72.0
    soft_boxes = []
    for s in soft:
        if s is t:
            continue
        e = s.get_window_extent(ren)
        m = 0.25 * s.get_fontsize() * fig.dpi / 72.0
        soft_boxes.append((e.x0 + m, e.y0 + m, e.x1 - m, e.y1 - m))
    best, best_slack = None, -1e18
    for spot in spots:
        t.set_position(spot)
        if on_spot is not None:
            on_spot(t, spot)
        e = t.get_window_extent(ren)
        r = (e.x0, e.y0, e.x1, e.y1)
        slack = 1e18
        for ob, thr in hard_disp:
            slack = min(slack, _clearance_px(r, ob) - thr)
        for sb in soft_boxes:
            dx = max(r[0] - sb[2], sb[0] - r[2])
            dy = max(r[1] - sb[3], sb[1] - r[3])
            slack = min(slack, max(dx, dy) - soft_px)
        if slack >= 0.0:
            return spot
        if slack > best_slack:
            best, best_slack = spot, slack
    if best == spots[0]:
        t.set_position(best)        # the loop left t on the last spot tried
        if on_spot is not None:
            on_spot(t, best)
        return best                 # already at the best-effort spot
    t.set_position(best)
    if on_spot is not None:
        on_spot(t, best)
    warn(f"{what or 'label'}: no collision-free spot (best slack "
         f"{best_slack * 72.0 / fig.dpi:+.2f} pt); white halo covers the "
         "residual graze")
    return best


def _spot_grid(xc, y0, dys, dxs):
    """Candidate spots around (xc, y0), smallest displacement first."""
    spots = [(xc + dx, y0 + dy) for dy in dys for dx in dxs]
    spots.sort(key=lambda s: (abs(s[0] - xc) + abs(s[1] - y0),
                              0 if abs(s[0] - xc) < 1e-12 else 1,
                              abs(s[0] - xc)))
    return spots


def _relayout_d2(fig, ax, ctexts, bar_rects, bw_groups, soft_extra=()):
    """Measured collision avoidance for the D2 gain callouts.

    The bar-top value labels are gone by design (the paper rq13_d2_jain_bw
    genre draws gains only), so the engine now fits just the |gain| >= 5%
    callouts: each keeps clear of every bar, the BW polyline and markers,
    the perfect-fairness line, every other callout and the fixed texts of
    `soft_extra` (e.g. the "1.0000" fairness label), and stays inside the
    axes.  A callout that is already clear does not move.
    """
    fig.canvas.draw()      # finalize layout before measuring text boxes
    marker_pt = 1.15 + 0.55 / 2.0           # BW marker radius incl. edge
    marks = [("circle", (float(px), float(py), marker_pt), 0.4)
             for pts in bw_groups for px, py in pts]
    segs = []
    for pts in bw_groups:
        segs += [("seg", (a[0], a[1], b[0], b[1]), 0.4)
                 for a, b in zip(pts, pts[1:])]
    bar_ob = [("rect", b, 0.4) for b in bar_rects]
    frame_ob = ("frame", None, 0.15)
    all_texts = [t for t, *_ in ctexts] + list(soft_extra)

    def fit(t, spots, hard):
        cur = t.get_position()
        return _fit_text(fig, ax, t, [cur] + [s for s in spots if s != cur],
                         hard, all_texts, what=f"D2 label {t.get_text()!r}")

    for _sweep in range(3):
        moved = 0
        for t, xc, y0 in ctexts:
            hard = bar_ob + segs + marks + [("hline", 1.0, 0.4), frame_ob]
            spots = _spot_grid(xc, y0, [0.02 * k for k in range(13)],
                               [0.0, 0.06, -0.06, 0.10, -0.10, 0.12, -0.12,
                                0.18, -0.18])
            before = t.get_position()
            fit(t, spots, hard)
            if t.get_position() != before:
                moved += 1
        if not moved:
            break


def _box(ax):
    for s in ax.spines.values():
        s.set_visible(True)
        s.set_linewidth(0.45)
        s.set_color(INK)
    # NOTE: no tick_params(pad=...) here.  The reference _box uses pad=1,
    # but on matplotlib >= 3.10 the small pad pushes the rotated y-axis
    # label past the left frame of the narrow (8.5/4 in) D4 figure (clipped
    # ylabel, ~400 ink px at the left frame; pixel-diffed 2026-11-11).  The
    # default pad (3.5 pt) renders clean; the reference PDF predates the
    # layout change.
    ax.tick_params(colors=INK)
    ax.yaxis.grid(True, color=GRID, lw=0.4, which="major", zorder=0)
    ax.set_axisbelow(True)


def _legend(ax, **kw):
    return ax.legend(frameon=False, borderpad=0.1, handlelength=1.4,
                     handletextpad=0.35, labelspacing=0.18,
                     columnspacing=0.6, **kw)


def _save(fig, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        path = out_dir / f"{name}.{ext}"
        fig.savefig(path, dpi=400)
        print(f"wrote {path}  {fig.get_size_inches()[0]:.3f} x "
              f"{fig.get_size_inches()[1]:.3f} in")
    plt.close(fig)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fig", default="all",
                    help="all (default) or a comma list of d2,d3,d4")
    ap.add_argument("--out-dir", type=Path, default=OUT_DEF,
                    help=f"output dir (default {OUT_DEF}); writes "
                         "rq13_{{d2,d3,d4}}_bm.{{pdf,png}}")
    ap.add_argument("--d2-root", type=Path, default=GF_D2_ROOT_DEF,
                    help=f"D2 results root (default the GRAND FINAL "
                         f"single-batch root {GF_D2_ROOT_DEF})")
    ap.add_argument("--d3-root", type=Path, default=None,
                    help=f"D3 results root (default the GF root "
                         f"{GF_D3_ROOT_DEF}); wins over --d3-qonly")
    ap.add_argument("--d3-qonly", choices=tuple(D3_ROOTS), default=None,
                    help="pre-GF n3 D3 iox variant batch (ddl_root -> "
                         f"{D3_ROOTS['ddl_root']}, legend \"iox q_ddl "
                         '(root)"; fifo -> ..._d3_fifo, "iox q_fifo"); '
                         "ignored when --d3-root is given")
    ap.add_argument("--d2-qonly-label", default=None,
                    help=f"legend label of the D2 iox_qonly arm (default "
                         f"{GF_QONLY_LABEL['d2']!r})")
    ap.add_argument("--d3-qonly-label", default=None,
                    help=f"legend label of the D3 iox_qonly arm (default "
                         f"{GF_QONLY_LABEL['d3']!r}; the pre-GF --d3-qonly "
                         "aliases keep their old labels)")
    ap.add_argument("--extra-io-cost", type=Path, default=None,
                    help="optional second D3 results root; its io_cost row "
                         "is added as a light secondary Pareto point")
    ap.add_argument("--d4-root", type=Path, default=None,
                    help=f"D4 results root (default the GF root "
                         f"{GF_D4_ROOT_DEF}); wins over --d4-qonly")
    ap.add_argument("--d4-qonly", choices=tuple(D4_ROOTS), default=None,
                    help="pre-GF n3 D4 iox variant batch (wedf_root -> "
                         f"{D4_ROOTS['wedf_root']}, legend \"iox q_wedf "
                         '(root)"; ddl_root -> ..._d4_ddl_root, "iox q_ddl '
                         '(root)\"); ignored when --d4-root is given')
    ap.add_argument("--d4-deep-root", type=Path, default=None,
                    help="D4 results root for --d4-shape 4x1024 (default "
                         f"the GF deep batch {GF_D4_DEEP_ROOT_DEF}, three "
                         "arms; the pre-GF --d4-qonly ddl_root alias keeps "
                         f"{D4_DEEP_ROOT})")
    ap.add_argument("--d4-shape", choices=("4x256", "4x1024"),
                    default="4x256",
                    help="D4 BE shape (default 4x256; 4x1024 reads "
                         "--d4-deep-root; the deep batch has no arb arms, "
                         "so --arb-d4-root is not merged under 4x1024)")
    ap.add_argument("--d4-qonly-label", default=None,
                    help=f"legend label of the D4 iox_qonly arm, deep "
                         f"batch included (default "
                         f"{GF_QONLY_LABEL['d4']!r}; the pre-GF --d4-qonly "
                         "aliases keep their old labels)")
    ap.add_argument("--arb-d2-root", type=Path, default=None,
                    help="D2 results root carrying the arb arms "
                         "(iox_arbonly/iox_qarb); only the arb arms are "
                         "taken from it -- in the GF campaign pass the "
                         "SAME root as --d2-root; omitted when not given")
    ap.add_argument("--arb-d3-root", type=Path, default=None,
                    help="D3 results root carrying the arb arms (pass the "
                         "SAME root as --d3-root in the GF campaign)")
    ap.add_argument("--arb-d4-root", type=Path, default=None,
                    help="D4 results root carrying the arb arms (pass the "
                         "SAME root as --d4-root in the GF campaign; not "
                         "merged under --d4-shape 4x1024)")
    ap.add_argument("--calib-root", type=Path, default=GF_CALIB_ROOT_DEF,
                    help=f"calib results root with analysis/calib.csv "
                         f"(default the GF calib batch {GF_CALIB_ROOT_DEF}; "
                         "the solo anchor and SLO line default to its "
                         "D3_bm_lcsolo_none d3_solo_lc_p99_ms / "
                         "suggested_slo_ms)")
    ap.add_argument("--slo-ms", type=float, default=None,
                    help="override the D3 SLO line (default 2x the calib "
                         "solo-LC p99)")
    ap.add_argument("--solo-ms", type=float, default=None,
                    help="override the solo-LC p99 anchor (default the "
                         "calib D3_bm_lcsolo_none value)")
    ap.add_argument("--d4-scratch", type=Path, default=SCRATCH_DEF,
                    help=f"self-managed tar-extract scratch dir with sha256 "
                         f"fingerprint cache (default {SCRATCH_DEF})")
    args = ap.parse_args()

    want = [f.strip() for f in args.fig.split(",") if f.strip()]
    if want == ["all"]:
        want = ["d2", "d3", "d4"]
    bad = [f for f in want if f not in ("d2", "d3", "d4")]
    if bad:
        print(f"error: unknown --fig entries {bad} (d2, d3, d4, all)",
              file=sys.stderr)
        return 2

    # arb arms: omit with the required stderr note when no arb root is
    # given (in the GF campaign the SAME root as the family's primary is
    # passed, so this note only fires for genuinely arb-less renders)
    for fam, root in (("d2", args.arb_d2_root), ("d3", args.arb_d3_root),
                      ("d4", args.arb_d4_root)):
        if root is None and fam in want:
            print(f"note: arb arm(s) omitted from rq13_{fam}_bm "
                  f"(no --arb-{fam}-root given; in the GRAND FINAL "
                  "single-batch campaign pass the SAME root as the "
                  "family's primary root)", file=sys.stderr)

    # D2 label: the GF q_ratelimit label unless overridden
    d2_label = args.d2_qonly_label or GF_QONLY_LABEL["d2"]

    # D3 root/label: an explicit --d3-root wins over the pre-GF --d3-qonly
    # alias; with neither, the GF single-batch root is the default
    if args.d3_root is not None:
        if args.d3_qonly is not None:
            info(f"--d3-qonly {args.d3_qonly} ignored (--d3-root "
                 f"{args.d3_root} given)")
        d3_root = args.d3_root
        d3_label = args.d3_qonly_label or GF_QONLY_LABEL["d3"]
    elif args.d3_qonly is not None:
        d3_root = D3_ROOTS[args.d3_qonly]
        d3_label = args.d3_qonly_label or D3_QONLY_LABEL[args.d3_qonly]
    else:
        d3_root = GF_D3_ROOT_DEF
        d3_label = args.d3_qonly_label or GF_QONLY_LABEL["d3"]

    # D4 root/label: same pattern, with the deep batch (4x1024) as a
    # separate root; the deep batch has no arb arms, so an --arb-d4-root
    # is not merged under 4x1024
    arb_d4_root = args.arb_d4_root
    if args.d4_shape == "4x1024":
        if args.d4_deep_root is not None:
            d4_root = args.d4_deep_root
            d4_label = args.d4_qonly_label or GF_QONLY_LABEL["d4"]
        elif args.d4_qonly is not None:      # pre-GF alias deep default
            d4_root = D4_DEEP_ROOT
            d4_label = args.d4_qonly_label or D4_DEEP_LABEL
            if args.d4_qonly != "ddl_root":
                warn(f"--d4-shape 4x1024 uses {D4_DEEP_ROOT.name}, whose only "
                     f"iox variant is q_ddl; --d4-qonly {args.d4_qonly} "
                     "ignored")
        else:                               # GRAND FINAL deep default
            d4_root = GF_D4_DEEP_ROOT_DEF
            d4_label = args.d4_qonly_label or GF_QONLY_LABEL["d4"]
        if arb_d4_root is not None:
            info("--d4-shape 4x1024: --arb-d4-root not merged (the deep "
                 "batch carries none/io_cost/iox_qonly only)")
            arb_d4_root = None
    else:
        if args.d4_root is not None:
            if args.d4_qonly is not None:
                info(f"--d4-qonly {args.d4_qonly} ignored (--d4-root "
                     f"{args.d4_root} given)")
            d4_root = args.d4_root
            d4_label = args.d4_qonly_label or GF_QONLY_LABEL["d4"]
        elif args.d4_qonly is not None:
            d4_root = D4_ROOTS[args.d4_qonly]
            d4_label = args.d4_qonly_label or D4_QONLY_LABEL[args.d4_qonly]
        else:
            d4_root = GF_D4_ROOT_DEF
            d4_label = args.d4_qonly_label or GF_QONLY_LABEL["d4"]

    for fam, root in (("d2", args.d2_root), ("d3", d3_root), ("d4", d4_root)):
        if fam in want and not root.is_dir():
            print(f"error: results root {root} does not exist (the GRAND "
                  "FINAL batch may not have landed yet; pass an existing "
                  "root via --d{2,3,4}-root or use the pre-GF --d3-qonly/"
                  "--d4-qonly aliases)", file=sys.stderr)
            return 2

    errors: list = []
    if "d2" in want:
        try:
            render_d2(args.out_dir, args.d2_root, args.arb_d2_root, d2_label)
        except PlotError as exc:
            errors.append(f"rq13_d2_bm: {exc}")
            print(f"error: rq13_d2_bm: {exc}", file=sys.stderr)
    if "d3" in want or "d4" in want:
        try:
            solo, slo = load_calib(args.calib_root, args.slo_ms, args.solo_ms)
        except PlotError as exc:
            errors.append(f"calib: {exc}")
            print(f"error: {exc}", file=sys.stderr)
            solo = slo = None
    if "d3" in want and solo is not None:
        try:
            render_d3(args.out_dir, d3_root, d3_label, args.extra_io_cost,
                      args.arb_d3_root, solo, slo)
        except PlotError as exc:
            errors.append(f"rq13_d3_bm: {exc}")
            print(f"error: rq13_d3_bm: {exc}", file=sys.stderr)
    if "d4" in want and solo is not None:
        try:
            render_d4(args.out_dir, d4_root, d4_label, args.d4_shape,
                      arb_d4_root, solo, args.d4_scratch)
        except PlotError as exc:
            errors.append(f"rq13_d4_bm: {exc}")
            print(f"error: rq13_d4_bm: {exc}", file=sys.stderr)

    if FAILS:
        print(f"{len(FAILS)} consistency assertion(s) failed "
              "(see ASSERTION FAIL lines above)", file=sys.stderr)
    if errors or FAILS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
