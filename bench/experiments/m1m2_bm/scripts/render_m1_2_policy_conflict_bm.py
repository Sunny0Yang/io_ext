#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figure m1_2 (compact rebuild): native-scheduler x real-application KPI
adaptation-conflict heatmap (cache=on caliber, n=3). Native-only:
{none, kyber, mq-deadline, bfq}.

This is the paper pair version of the old wide m1_2 figure (old layout kept in
scripts/retired_m1_figs_20261105/): ONLY the left heatmap survives --
no suptitle, no footnote block, no wins/mean-rank summary strip, no
(b)-(d) throughput guardrails. Sized to stack with m1_timeseries_bm_1x4
(104.4 x 48.0 mm) into one full-text-width (174 mm) (a)/(b) figure pair:
canvas 68 x 48 mm.

Layout:
  Single heatmap: five P99 rows grouped by workload (YCSB-A: READ/UPDATE;
  YCSB-C: READ; db_bench: foreground READ; Shore: request P99).
  Workload and metric labels occupy separate aligned columns.
  Cell value = scheduler mean / row-best mean, with 1.00 best in that row.
  Bold face + black border identify the smallest observed mean, not a
  statistically established winner. Repeat ranges and SD remain in the
  input CSVs and printed diagnostic table; no uncertainty symbols are drawn.
  Color stops: 1.00 white, 1.10 pale orange, 1.15 orange, >=1.25 dark red.
  Only colors saturate; numeric annotations retain the actual ratios.
  Raw mean±SD numbers are intentionally NOT drawn (kept for the paper
  caption / ledger; see the retired wide figure for the annotated variant).

YCSB-C: the matrix campaign must include M1MATRIX_YCSB_C_* arms (config,
builder and this renderer are already extended). Until those runs exist the
row is skipped with a warning. --mock-ycsb-c injects PLACEHOLDER read-p99
numbers purely to preview the final layout; it appends "_mock" to the output
name and must never be used for the paper figure.

Data (cache=on campaign, do NOT mix with old direct-io data):
  bm build: the m1nativematrix campaign produced on baremetal bm by
  run_m1_cache_campaign_bm.py, under experiments/m1m2_bm/runs/m1_campaign/<RUN_ID>/:
    native_matrix_cases.csv    (per-case)
    native_matrix_summary.csv  (mean/SD/min/max, population SD, n=3)

Outputs (--fig-dir, default experiments/m1m2_bm/figs/):
  m1_2_policy_conflict.png (300 dpi) / m1_2_policy_conflict.pdf
  (mock preview: m1_2_policy_conflict_mock.png / .pdf)

Run:  python3 render_m1_2_policy_conflict_bm.py [--data-dir DIR] [--fig-dir DIR]
      [--mock-ycsb-c]
"""

import argparse
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Rectangle

# --------------------------------------------------------------------------
# Paths (bare-metal build: parameterized; defaults point at the m1m2_bm campaign data)
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
M1M2_ROOT = os.path.normpath(os.path.join(HERE, ".."))
DEFAULT_FIG_DIR = os.path.join(M1M2_ROOT, "figs")

_ap = argparse.ArgumentParser(description=(
    "Fig. m1_2 compact heatmap (bare-metal build): native-scheduler x real-app KPI "
    "conflict matrix from the m1nativematrix cache=on campaign on baremetal "
    "86. Inputs = native_matrix_cases.csv + native_matrix_summary.csv "
    "produced by build_native_matrix_summary_bm.py."))
_ap.add_argument("--data-dir", default=None,
                 help="run dir holding native_matrix_cases.csv + "
                      "native_matrix_summary.csv (default: newest run dir "
                      "under experiments/m1m2_bm/runs/m1_campaign/*/)")
_ap.add_argument("--cases-csv", default=None, help="override cases CSV path")
_ap.add_argument("--summary-csv", default=None, help="override summary CSV path")
_ap.add_argument("--fig-dir", default=DEFAULT_FIG_DIR,
                 help=f"output dir for PNG/PDF (default: {DEFAULT_FIG_DIR})")
_ap.add_argument("--mock-ycsb-c", action="store_true",
                 help="PLACEHOLDER preview only: inject synthetic YCSB-C "
                      "read-p99 arms to preview the layout; appends _mock "
                      "to the output name. Never use for the paper figure.")
_args = _ap.parse_args()

_data_dir = _args.data_dir
if _data_dir is None and (_args.cases_csv is None or _args.summary_csv is None):
    import glob as _glob
    _cands = sorted(_glob.glob(os.path.join(
        M1M2_ROOT, "runs", "m1_campaign", "native_matrix_cases.csv")) +
        _glob.glob(os.path.join(M1M2_ROOT, "runs", "m1_campaign", "*",
                                "native_matrix_cases.csv")))
    if not _cands:
        raise SystemExit("no native_matrix_cases.csv found under "
                         "experiments/m1m2_bm/runs/m1_campaign/ -- pass --data-dir")
    _data_dir = os.path.dirname(_cands[-1])
    print(f"m1_2 bm: using data dir: {_data_dir}")
DATA_DIR = _data_dir
CSV_CASES = _args.cases_csv or os.path.join(DATA_DIR, "native_matrix_cases.csv")
CSV_SUM = _args.summary_csv or os.path.join(DATA_DIR, "native_matrix_summary.csv")
OUT_STEM = "m1_2_policy_conflict" + ("_mock" if _args.mock_ycsb_c else "")
OUT_PNG = os.path.join(_args.fig_dir, OUT_STEM + ".png")
OUT_PDF = os.path.join(_args.fig_dir, OUT_STEM + ".pdf")
os.makedirs(_args.fig_dir, exist_ok=True)

# --------------------------------------------------------------------------
# Negative content assert (native-only figure): no third-scheduler tokens.
# Tokens are assembled at runtime so this source file itself stays clean.
# --------------------------------------------------------------------------
BAD_TOKENS = ("i" + "ox", "sc" + "x_i" + "o")
with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
    _src = fh.read()
for tok in BAD_TOKENS:
    assert tok not in _src.lower(), f"figure source must not mention {tok!r}"

# --------------------------------------------------------------------------
# Load data + consistency check (cases -> summary)
# --------------------------------------------------------------------------
SCHEDS = ["none", "kyber", "mq-deadline", "bfq"]

cases = pd.read_csv(CSV_CASES)
summ = pd.read_csv(CSV_SUM)

assert (cases["gate_ok"] == True).all(), "non-gate_ok case found"
assert set(cases["scheduler"].unique()) == set(SCHEDS), \
    f"unexpected scheduler arm in cases: {set(cases['scheduler'].unique())}"
for tok in BAD_TOKENS:  # data-level negative assert
    assert tok not in cases["case"].str.lower().str.cat(sep=" "), "bad token in case ids"
    assert tok not in summ.to_string().lower(), "bad token in summary table"

# Re-derive every mean/SD/min/max used below from the per-case rows and
# check them against native_matrix_summary.csv (guards against silent drift).
_sd_count = 0
for (scen, sched), grp in cases.groupby(["scenario", "scheduler"]):
    srow = summ[(summ["scenario"] == scen) & (summ["scheduler"] == sched)]
    assert len(srow) == 1 and int(srow["n_gate_ok"].iloc[0]) == 3
    for col in grp.columns:
        if col.startswith("kpi_") and grp[col].notna().any():
            v = grp[col].dropna().values
            for stat, ref in (("mean", v.mean()), ("sd", v.std(ddof=0)),
                              ("min", v.min()), ("max", v.max())):
                scol = f"kpi_{col[4:]}_{stat}"
                if scol not in summ.columns:
                    continue  # summary CSV keeps a subset of KPI columns
                got = srow[scol].iloc[0]
                if pd.isna(got) or (isinstance(got, str) and not got.strip()):
                    # KPI present in cases but deliberately not summarized for
                    # this scenario (e.g. read p50 only aggregated for YCSB-C).
                    continue
                got = float(got)
                assert np.isclose(got, ref, rtol=1e-3, atol=1e-3), \
                    f"summary mismatch {scen}/{sched}/{col}/{stat}: {got} vs {ref}"
                _sd_count += 1
print(f"self-check: summary.csv consistent with cases.csv "
      f"({len(cases)} cases, {_sd_count} statistics re-derived)")

S = {(r["scenario"], r["scheduler"]): r for _, r in summ.iterrows()}

# --------------------------------------------------------------------------
# Placeholder YCSB-C arms (preview only; never for the paper figure)
# --------------------------------------------------------------------------
if _args.mock_ycsb_c:
    print("WARNING: --mock-ycsb-c: injecting PLACEHOLDER YCSB-C read-p99 "
          "arms; output is a layout preview, not data.")
    _mock_read_p99_us = {  # mean, sd  (us) -- plausible read-only magnitudes
        "none": (1850.0, 260.0), "kyber": (1705.0, 130.0),
        "mq-deadline": (2060.0, 300.0), "bfq": (2380.0, 480.0),
    }
    for sched, (m, sd) in _mock_read_p99_us.items():
        S[("YCSB-C", sched)] = {
            "kpi_read_p99_us_mean": m, "kpi_read_p99_us_sd": sd,
            "kpi_read_p99_us_min": m - sd, "kpi_read_p99_us_max": m + sd,
        }

# --------------------------------------------------------------------------
# Row definitions: each application reports only its own interpretable KPIs.
# "lower" = smaller is better (latency / miss rate); "higher" = larger better.
# Workload (grouper, header label) + per-row metric label; rows whose scenario
# has no measured arms are dropped with a warning (e.g. YCSB-C pre-campaign).
# --------------------------------------------------------------------------
def _fmt_ms2(m, s):   return f"{m/1e3:.2f}\u00b1{s/1e3:.2f} ms"
def _fmt_us2(m, s):   return f"{m:.2f}\u00b1{s:.2f} \u00b5s"
def _fmt_s2(m, s):    return f"{m/1e6:.2f}\u00b1{s/1e6:.2f} s"
def _fmt_ms1(m, s):   return f"{m/1e3:.1f}\u00b1{s/1e3:.1f} ms"
def _fmt_mibs(m, s):  return f"{m:.1f}\u00b1{s:.1f} MiB/s"
def _fmt_rate(m, s):  return f"{m:.3f}\u00b1{s:.3f}"

ROWS = [
    dict(scen="YCSB-A", group="YCSB-A", kpi="read_p99_us", better="lower",
         fmt=_fmt_ms2, label="READ P99"),
    dict(scen="YCSB-A", group="YCSB-A", kpi="update_p99_us", better="lower",
         fmt=_fmt_ms2, label="UPDATE P99"),
    dict(scen="YCSB-C", group="YCSB-C", kpi="read_p99_us", better="lower",
         fmt=_fmt_ms2, label="READ P99"),
    dict(scen="db_bench-rww", group="db_bench", kpi="fg_read_p99_us",
         better="lower", fmt=_fmt_us2, label="fg READ P99"),
    dict(scen="Shore-QPS50", group="Shore", kpi="p99_us", better="lower",
         fmt=_fmt_s2, label="P99"),
]

_grid_all = []
for row in ROWS:
    if all((row["scen"], s) in S for s in SCHEDS):
        _grid_all.append(row)
    else:
        print(f"WARNING: scenario {row['scen']!r} has no measured scheduler "
              f"arms in summary -- row {row['kpi']!r} skipped.")
ROWS = _grid_all

HATCH_RELERR = 0.20  # SD/mean threshold for the noise hatch

# Precompute per-cell: mean, sd, min, max, degradation factor, flags
grid = []
for row in ROWS:
    means, cells = {}, {}
    for j, sched in enumerate(SCHEDS):
        r = S[(row["scen"], sched)]
        col = f"kpi_{row['kpi']}"
        m, sd = float(r[f"{col}_mean"]), float(r[f"{col}_sd"])
        lo, hi = float(r[f"{col}_min"]), float(r[f"{col}_max"])
        means[sched] = m
        cells[sched] = dict(m=m, sd=sd, lo=lo, hi=hi)
    best_sched = (min if row["better"] == "lower" else max)(means, key=means.get)
    best = means[best_sched]
    for sched in SCHEDS:
        c = cells[sched]
        c["deg"] = c["m"] / best if row["better"] == "lower" else best / c["m"]
        c["relerr"] = c["sd"] / c["m"]
        c["is_best"] = (sched == best_sched)
        c["noisy_vs_best"] = (sched != best_sched) and not (
            c["lo"] > cells[best_sched]["hi"] or c["hi"] < cells[best_sched]["lo"])
        c["hatch"] = c["relerr"] >= HATCH_RELERR
    grid.append(dict(spec=row, best=best_sched, cells=cells))

# --------------------------------------------------------------------------
# Figure: single compact heatmap, 68 x 48 mm (pairs with the 1x4 timeseries)
# --------------------------------------------------------------------------
plt.rcParams.update({
    "font.family": "Liberation Serif",
    "font.size": 8.0,
    "axes.linewidth": 0.7,
    "xtick.major.width": 0.7,
    "ytick.major.width": 0.7,
    "hatch.linewidth": 0.9,
    "pdf.fonttype": 42,
})

SCHED_COLORS = {"none": "#8C8C8C", "kyber": "#E69F00",
                "mq-deadline": "#56B4E9", "bfq": "#009E73"}
SCHED_TOP_LABEL = {"mq-deadline": "mq-\ndeadline"}  # avoid rotated/overflow text

DEG_MAX = 1.25
COLOR_STOPS = [
    (1.00, "#FFFFFF"),
    (1.10, "#FAD4A6"),
    (1.15, "#DE741C"),
    (1.25, "#A50F15"),
]
cmap = LinearSegmentedColormap.from_list(
    "deg", [((value - 1.0) / (DEG_MAX - 1.0), color)
            for value, color in COLOR_STOPS], N=1024)
cmap.set_over(COLOR_STOPS[-1][1])

MM = 1.0 / 25.4
fig = plt.figure(figsize=(68 * MM, 48 * MM))
ax = fig.add_axes([0.49, 0.20, 0.495, 0.64])

deg_mat = np.array([[g["cells"][s]["deg"] for s in SCHEDS] for g in grid])
im = ax.imshow(deg_mat, cmap=cmap, vmin=1.0, vmax=DEG_MAX, aspect="auto")
ax.set_xticks(range(len(SCHEDS)))
ax.set_xticklabels([SCHED_TOP_LABEL.get(s, s) for s in SCHEDS],
                   fontsize=7.5, fontweight="bold")
ax.xaxis.tick_top()
ax.tick_params(axis="x", top=False, labeltop=True, length=0, pad=2)
ax.set_yticks([])
for i, g in enumerate(grid):
    fig.text(0.475, ax.transData.transform((0, i))[1] / fig.bbox.height,
             g["spec"]["label"], ha="right", va="center", fontsize=7.5)

# Workload group headers in the left gutter + white rules between groups.
groups = []
for i, g in enumerate(grid):
    grp = g["spec"]["group"]
    if groups and groups[-1][0] == grp:
        groups[-1][2] = i
    else:
        groups.append([grp, i, i])
for grp, i0, i1 in groups:
    if i0 > 0:
        ax.axhline(i0 - 0.5, color="white", lw=2.2)
    ctr = (i0 + i1) / 2.0
    fig.text(0.015, ax.transData.transform((0, ctr))[1] / fig.bbox.height,
             grp, ha="left", va="center", fontsize=8.0, fontweight="bold")

for i, g in enumerate(grid):
    for j, s in enumerate(SCHEDS):
        c = g["cells"][s]
        rgba = cmap(im.norm(c["deg"]))
        rgb = np.array(rgba[:3])
        linear = np.where(rgb <= 0.04045, rgb / 12.92,
                          ((rgb + 0.055) / 1.055) ** 2.4)
        luminance = np.dot(linear, [0.2126, 0.7152, 0.0722])
        tcol = "white" if luminance < 0.179 else "black"
        ax.text(j, i, f"{c['deg']:.2f}", ha="center", va="center",
                fontsize=8.0, fontweight="bold" if c["is_best"] else "normal",
                color=tcol, zorder=6)
        if c["is_best"]:
            ax.add_patch(Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False,
                                   ec="black", lw=1.2, zorder=4))

cax = fig.add_axes([0.49, 0.115, 0.495, 0.025])
cb = fig.colorbar(im, cax=cax, orientation="horizontal")
cb.set_ticks([1.0, 1.10, 1.25])
cb.set_ticklabels(["1.00", "1.10", "\u22651.25"])
cb.ax.tick_params(labelsize=7.0, length=1.5, pad=1)
cb.ax.get_xticklabels()[0].set_ha("left")
cb.ax.get_xticklabels()[-1].set_ha("right")
cb.outline.set_linewidth(0.6)

fig.canvas.draw()
renderer = fig.canvas.get_renderer()
from matplotlib.text import Text
texts = [text for text in fig.findobj(Text)
         if text.get_visible() and text.get_text().strip()]
for text in texts:
    box = text.get_window_extent(renderer)
    assert (box.x0 >= 0 and box.y0 >= 0 and
            box.x1 <= fig.bbox.width and box.y1 <= fig.bbox.height), \
        f"text outside canvas: {text.get_text()!r}"
for i, left in enumerate(texts):
    for right in texts[i + 1:]:
        assert not left.get_window_extent(renderer).overlaps(
            right.get_window_extent(renderer)), \
            f"overlapping text: {left.get_text()!r}, {right.get_text()!r}"

fig.savefig(OUT_PNG, dpi=300)
fig.savefig(OUT_PDF)
print("wrote", OUT_PNG)
print("wrote", OUT_PDF)

# --------------------------------------------------------------------------
# Numeric self-check table (must match native_matrix_summary.csv)
# --------------------------------------------------------------------------
print(f"\n--- self check: {len(grid) * len(SCHEDS)} cells "
      f"(degradation factor | mean\u00b1SD | "
      f"noise marks) ---------------------------------")
for g in grid:
    parts = []
    for s in SCHEDS:
        c = g["cells"][s]
        marks = ("\u2248" if c["noisy_vs_best"] else "") + \
                ("h" if c["hatch"] else "") + ("*" if c["is_best"] else "")
        parts.append(f"{s} {c['deg']:.2f}x [{g['spec']['fmt'](c['m'], c['sd'])}]{marks}")
    print(f"{g['spec']['group']:>9s} {g['spec']['kpi']:>20s} | " + " | ".join(parts))
