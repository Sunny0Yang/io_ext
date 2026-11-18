#!/usr/bin/env python3
"""homo-v2 (kvmhost) 8-panel grouped-bar figure from homo_v2_summary.csv.

Layout (gpu_ext style; one panel per method, three bar kinds per group):
  (a) mq-deadline   (b) kyber        (c) io.max       (d) io.weight
  (e) io.cost       (f) io.latency   (g) D2FQ         (h) LinnOS

Within each panel the x groups are the RUNBOOK per-arm main metrics (unit in
the group label, workload in the panel subtitle); each group carries up to
THREE bars: gray = Baseline (none), orange = Native knob, blue = io_ext
(homo-algorithm arm).  Missing columns simply drop the bar (fewer bars per
panel).  Error bars = the summary's bootstrap 95% CI; the bar-top label is
the raw delta vs the Baseline(none) bar in %.

Two y-axis modes:
  - default: each x group is drawn BASELINE-RELATIVE (baseline bar = 1.0, CI
    bars scaled by the same baseline) so panels can hold per-arm metric sets
    with different units (IOPS + p99 + MiB/s); group labels keep the unit.
  - --abs: raw medians of the panel's PRIMARY (first) metric only, in the
    metric's own unit.

Reproduction caliber (agreement with scripts/build_homo_v2_results.py):
every plotted value = per-arm MEDIAN across gate_ok reps (n=10 by campaign
contract; runs failing the sidecar arm-state gate are ledgered, not plotted);
error bars = bootstrap 95% CI of the median (percentile, B=10000); rep count
< 5 degrades to min/median/max and the CI is absent.  The frontier panels
(g)(h) have NO native column by design (no HOMO_front_n_* sequence exists) --
their Baseline(none) bar cross-references the seq_controller arm HOMO_ctrl_n_
none (identical workload / device / kernel, never re-run).

Usage:
  python3 scripts/plot_homo_v2.py \
      --summary results/homo_v2_kvmhost/homo_v2_summary.csv \
      --out-prefix results/homo_v2_kvmhost/homo_v2_panel_fig
  # writes <out-prefix>.pdf and <out-prefix>.png (300 dpi); exits 2 with
  # usage when the summary CSV is missing.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SUMMARY = BASE_DIR / "results" / "homo_v2_kvmhost" / "homo_v2_summary.csv"

COLORS = {"baseline": "#9a9a9a", "native": "#f28e2b", "iox": "#4c78a8"}
GROUP_LABELS = {"baseline": "Baseline\n(none)", "native": "Native", "iox": "io_ext"}
BAR_ORDER = ("baseline", "native", "iox")

# Panel primary metric = the FIRST list entry (drawn first; --abs renders it
# alone).  2026-11-07 iteration#1: elev head metric is read p99 (IOPS
# secondary); weight/share panels keep share_ratio as the ratio view, with
# lc_share/be_share/share_ratio_dev available in the summary for the appendix.
ELEV_GROUPS = [("read p99 [us]", "elev_read_p99_us"), ("IOPS", "elev_iops")]
SHARE_GROUPS = [("LC:BE share", "share_ratio"), ("Jain", "jain_index"),
                ("LC p99 [us]", "lc_read_p99_us")]

# (letter, method label, subtitle, baseline arm, native arm, iox arm, groups)
PANELS = [
    ("a", "mq-deadline", "seq_elevator · 4k randrw 70/30 QD64",
     "n_none", "n_mq_deadline", "x_mq_deadline_q", ELEV_GROUPS),
    ("b", "kyber", "seq_elevator · 4k randrw 70/30 QD64",
     "n_none", "n_kyber", "x_kyber_q", ELEV_GROUPS),
    ("c", "io.max", "seq_controller coloc · LC 4k rr QD32 + BE 64k seqwrite",
     "n_none", "n_io_max", "x_iomax_q",
     [("BE track err [%] (64 MiB/s cap)", "be_track_err_pct"),
      ("LC p99 [us]", "lc_read_p99_us")]),
    ("d", "io.weight", "seq_controller coloc · LC:BE 100:200 (native carrier bfq)",
     "n_none", "n_io_weight", "x_weight_arb", SHARE_GROUPS),
    ("e", "io.cost", "seq_controller coloc · qos rlat 20 ms + 100:200",
     "n_none", "n_io_cost", "x_cost_arb", SHARE_GROUPS),
    ("f", "io.latency", "seq_controller coloc · target 20 ms",
     "n_none", "n_io_latency", "x_latency_arb",
     [("LC p99 [us]", "lc_read_p99_us"),
      ("LC p99.9 [us]", "lc_read_p99_9_us"),
      ("BE throughput [MiB/s]", "be_bw_mbps")]),
    # frontier panels: NO native arm exists (baseline cross-references
    # HOMO_ctrl_n_none, never re-run -> the native bar slot is left empty)
    ("g", "D2FQ", "seq_frontier coloc · arb_d2fq (baseline = ctrl n_none)",
     "n_none", None, "x_d2fq_arb", SHARE_GROUPS),
    ("h", "LinnOS", "seq_frontier coloc · q_linnos (baseline = ctrl n_none)",
     "n_none", None, "x_linnos_q",
     [("LC p99 [us]", "lc_read_p99_us"),
      ("BE throughput [MiB/s]", "be_bw_mbps")]),
]


def load_summary(path: Path) -> dict:
    data: dict = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                med = float(row["median"])
            except (KeyError, TypeError, ValueError):
                continue
            entry = {"n": int(row["n"]), "median": med,
                     "min": float(row["min"]), "max": float(row["max"])}
            for k in ("ci95_lo", "ci95_hi"):
                try:
                    entry[k] = float(row[k])
                except (KeyError, TypeError, ValueError):
                    entry[k] = None
            data.setdefault(row["metric"], {}).setdefault(row["arm"], entry)
    return data


def _fmt_delta(delta_pct: float) -> str:
    return (f"+{delta_pct:.1f}%" if delta_pct >= 0
            else f"-{abs(delta_pct):.1f}%")


def render(data: dict, out_prefix: Path, abs_mode: bool = False) -> dict:
    """Draw the 4x2 panel figure; returns a {panel letter -> n_bars} report."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    fig, axes = plt.subplots(2, 4, figsize=(15.5, 8.4), squeeze=False)
    report = {}
    for ax, (letter, method, subtitle, base_arm, native_arm, iox_arm, groups) in \
            zip(axes.flat, PANELS):
        slots = []
        for idx, kind in enumerate(BAR_ORDER):
            arm = {"baseline": base_arm, "native": native_arm, "iox": iox_arm}[kind]
            if arm:
                slots.append((idx, kind, arm))
        drawable = []
        for label, metric in groups:
            if metric in data and any(arm in data[metric] for _, _, arm in slots):
                drawable.append((label, metric))
        if abs_mode and drawable:
            drawable = drawable[:1]  # raw mode: primary metric only
        if not drawable:
            ax.text(0.5, 0.5, "no gated data", transform=ax.transAxes,
                    ha="center", va="top", fontsize=9, color="0.45")
            ax.set_title(f"({letter}) {method}", fontsize=9.5)
            report[letter] = 0
            continue
        bar_w = 0.8 / len(slots)
        n_bars = 0
        for gi, (glabel, metric) in enumerate(drawable):
            ref = None
            if base_arm and base_arm in data.get(metric, {}):
                ref = data[metric][base_arm]["median"]
            for idx, kind, arm in slots:
                entry = data.get(metric, {}).get(arm)
                if entry is None:
                    continue
                median = entry["median"]
                lo, hi = entry["ci95_lo"], entry["ci95_hi"]
                if abs_mode:
                    val = median
                    yerr = None
                    if lo is not None and hi is not None:
                        yerr = [[max(median - lo, 0.0)],
                                [max(hi - median, 0.0)]]
                elif kind == "baseline":
                    val = 1.0
                    yerr = None
                elif ref:
                    val = median / ref
                    yerr = None
                    if lo is not None and hi is not None:
                        yerr = [[(median - lo) / ref], [(hi - median) / ref]]
                else:
                    val = median  # metric has no baseline reference
                    yerr = None
                x = gi + (idx - (len(slots) - 1) / 2.0) * bar_w
                ax.bar(x, val, width=bar_w * 0.92, color=COLORS[kind],
                       yerr=yerr, capsize=2.2, lw=0.4,
                       error_kw={"lw": 0.9, "ecolor": "0.25"}, zorder=2)
                if kind == "baseline" or ref in (None, 0):
                    label_txt = "ref" if kind == "baseline" else f"{median:.3g}"
                else:
                    label_txt = _fmt_delta((median - ref) / ref * 100.0)
                top = val + ((yerr[1][0] if hasattr(yerr[1], "__getitem__")
                              else yerr[1]) if yerr else 0)
                ax.text(x, top * 1.02, label_txt, ha="center", va="bottom",
                        fontsize=6.2, rotation=90, color="0.25")
                n_bars += 1
        ax.set_xticks(range(len(drawable)))
        ax.set_xticklabels([lab for lab, _ in drawable], fontsize=7.2)
        ax.set_ylabel(f"{drawable[0][0]} (panel primary metric)"
                      if abs_mode else "value vs Baseline(none)  [x]",
                      fontsize=8)
        ax.set_title(f"({letter}) {method}", fontsize=9.5, pad=12)
        ax.text(0.02, 0.98, subtitle, transform=ax.transAxes, va="top",
                fontsize=6.6, color="0.4")
        ax.axhline(1.0 if not abs_mode else 0.0, color="0.7", lw=0.7, zorder=1)
        ax.grid(True, axis="y", alpha=0.25, lw=0.5)
        ax.tick_params(labelsize=7.5)
        report[letter] = n_bars
    handles = [Patch(facecolor=COLORS[k], label=GROUP_LABELS[k].replace("\n", " "))
               for k in BAR_ORDER]
    fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, 0.985))
    fig.suptitle("homo-v2 homomorphic-algorithm reproduction (kvmhost 96G, "
                 "7.3.1-ioext3): per-method panels", fontsize=11.5, y=0.955)
    # overall caption (reproduction caliber) is documented in the module
    # docstring: median of gate_ok reps (n=10 contract), bootstrap 95% CI,
    # frontier native columns empty by design (baseline = HOMO_ctrl_n_none).
    fig.text(0.5, 0.012,
             "Median of gate_ok reps (n=10 contract) with bootstrap 95% CI; "
             "bars are baseline-relative (Baseline(none) = 1) unless --abs; "
             "bar-top labels = raw median vs baseline median in %.  "
             "Frontier panels (g)(h) have no native column: baseline "
             "cross-references HOMO_ctrl_n_none (never re-run).",
             ha="center", va="bottom", fontsize=6.8, color="0.3")
    fig.subplots_adjust(left=0.05, right=0.985, bottom=0.075, top=0.86,
                        wspace=0.24, hspace=0.42)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{out_prefix}.pdf")
    fig.savefig(f"{out_prefix}.png", dpi=300)
    plt.close(fig)
    return report


def self_test(out_dir: Path = Path("/tmp/homo_v2_plot_smoke")) -> None:
    """Synthesize a minimal summary from the build-script fixture values and
    verify a non-empty render (PDF + PNG) under /tmp."""
    import importlib.util
    import shutil
    spec = importlib.util.spec_from_file_location(
        "bh2", Path(__file__).resolve().parent / "build_homo_v2_results.py")
    bh2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bh2)
    shutil.rmtree(out_dir, ignore_errors=True)
    root = out_dir / "fixtures"
    bh2.make_fixtures(root, reps=3)
    result = bh2.build_rows(root, root)
    summary = bh2.summarize(result["rows"])
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)
    summary_path = out_dir / "homo_v2_summary.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bh2.SUMMARY_HEADER)
        w.writeheader()
        w.writerows(summary)
    data = load_summary(summary_path)
    report = render(data, out_dir / "homo_v2_panel_fig", abs_mode=False)
    png = out_dir / "homo_v2_panel_fig.png"
    pdf = out_dir / "homo_v2_panel_fig.pdf"
    total = sum(report.values())
    assert total > 0, report
    assert png.stat().st_size > 8000 and pdf.stat().st_size > 1000, \
        (png.stat().st_size, pdf.stat().st_size)
    print(f"plot self-test OK: {total} bars rendered {report}; png="
          f"{png.stat().st_size}B pdf={pdf.stat().st_size}B -> {out_dir}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY,
                    help="build_homo_v2_results.py homo_v2_summary.csv")
    ap.add_argument("--out-prefix", type=Path, default=None,
                    help="output basename (default <summary dir>/homo_v2_panel_fig)")
    ap.add_argument("--abs", dest="abs_mode", action="store_true",
                    help="raw primary-metric values instead of baseline-relative")
    ap.add_argument("--self-test", action="store_true",
                    help="fixture-render verification under /tmp")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.summary.is_file():
        print(__doc__)
        print(f"error: summary CSV not found: {args.summary} (build it with "
              "scripts/build_homo_v2_results.py first)", file=sys.stderr)
        return 2
    out_prefix = args.out_prefix or args.summary.parent / "homo_v2_panel_fig"
    data = load_summary(args.summary)
    if not data:
        print("error: summary has no numeric rows", file=sys.stderr)
        return 2
    report = render(data, out_prefix, abs_mode=args.abs_mode)
    empty = [k for k, v in report.items() if v == 0]
    print(f"rendered {out_prefix}.pdf / .png; bars per panel: "
          f"{ {k: v for k, v in report.items()} }")
    if empty:
        print(f"warning: panels without gated data: {empty}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
