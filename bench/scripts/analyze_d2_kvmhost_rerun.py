#!/usr/bin/env python3
"""Analyze + plot the kvmhost D2 rerun (contention-only sequences).

Metric contract (per the 2026-11-14 rerun decision):
  PRIMARY   total bandwidth (agg MB/s, contention step)  — higher is better
  PRIMARY   JainW (w-indexed, bw_i/w_i)                  — higher is better
  AUXILIARY mean read p99                                — reported, kept small

Per (arm, cg): n=3 iters; median bars + per-iter dots; no cross-iter column
mixing (each iter's JainW is computed within that iter, then medians taken).

Outputs:
  results/d2_rerun_kvmhost/analysis/d2_rerun_by_arm.csv
  results/d2_rerun_kvmhost/analysis/fig_d2_rerum_by_cg.{png,pdf}
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results/d2_rerun_kvmhost"
OUT = RESULTS / "analysis"

ARMS = ["none", "bfq", "io_bfq_weight", "io_max", "io_cost", "iox_q_rl", "iox_arb_wdrr", "iox_arb_slo", "iox_arb_cost"]
LABEL = {
    "none": "none", "bfq": "bfq", "io_bfq_weight": "io.bfq.w", "io_max": "io.max", "io_cost": "io.cost",
    "iox_q_rl": "q_rl",
    "iox_arb_wdrr": "a_wdrr", "iox_arb_slo": "a_slo", "iox_arb_cost": "a_cost",
}
CGS = [2, 4, 8, 16]
SEQ_RX = re.compile(r"^D2_saturated_weighted_(?P<arm>[a-z_]+?)_cg(?P<cg>\d+)_iter(?P<it>\d+)$")

# contention reference bands measured by the runner's calibration stage
# (4k randread QD256 numjobs=N aggregates); used to exclude phase-collapsed
# iters from the "healthy" aggregates.
CONTENTION_REF_MB_S = {"2": 968.8, "4": 931.8, "8": 902.6, "16": 908.6}


def _contention_ref(cg: int) -> float:
    try:
        calib = json.loads((RESULTS.parent / "d2_rerun_kvmhost" / "calib.json").read_text())
        return float(calib["contention_ref_mb_s"][str(cg)])
    except Exception:
        return CONTENTION_REF_MB_S.get(str(cg), 0.0)


def parse_seq(seq_dir: Path, ncg: int):
    """Per-tenant BW (MB/s) + mean p99 (us) from the single contention step.

    Handles both layouts: per-tenant scenes (`..._cgK` dirs, one fio job
    each) and the collapsed subjob layout (one `..._ncgN_subjob` dir whose
    fio json carries ncg jobs with jobnames `..._cgK`)."""
    by_w = {}
    p99s = []
    for scene_dir in sorted(seq_dir.glob("step_*/*")):
        if not scene_dir.is_dir():
            continue
        for p in sorted(scene_dir.glob("*fio.json")):
            if "stderr" in p.name:
                continue
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            jobs = d.get("jobs") or []
            if len(jobs) > 1:
                for j in jobs:
                    mj = re.search(r"_cg(\d+)$", j.get("jobname") or "")
                    if not mj:
                        continue
                    w = int(mj.group(1))
                    if w not in by_w:
                        by_w[w] = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
                        clat = ((j.get("read") or {}).get("clat_ns") or {}).get("percentile") or {}
                        pv = clat.get("99.000000")
                        if pv:
                            p99s.append(float(pv) / 1000.0)
                break
            if not jobs:
                continue
            m = re.search(r"_cg(\d+)$", scene_dir.name) or re.search(r"_w(\d+)_ncg", scene_dir.name)
            if not m:
                continue
            w = int(m.group(1))
            if w in by_w:
                continue
            j = jobs[0]
            by_w[w] = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
            clat = ((j.get("read") or {}).get("clat_ns") or {}).get("percentile") or {}
            pv = clat.get("99.000000")
            if pv:
                p99s.append(float(pv) / 1000.0)
            break
    if len(by_w) != ncg:
        return None
    ws = [w for w, _ in sorted(by_w.items())]
    bws = [b for _, b in sorted(by_w.items())]
    ratios = [b / w for w, b in zip(ws, bws)]
    jw = sum(ratios) ** 2 / (len(ratios) * sum(x * x for x in ratios))
    return bws, jw, (statistics.mean(p99s) if p99s else None)


def main() -> None:
    rows = []
    per = defaultdict(list)  # (arm,cg) -> list of iter dicts
    for seq_dir in sorted(RESULTS.glob("D2_saturated_weighted_*")):
        if not seq_dir.is_dir():
            continue
        m = SEQ_RX.match(seq_dir.name)
        if not m or m.group("arm") not in ARMS:
            continue
        cg = int(m.group("cg"))
        parsed = parse_seq(seq_dir, cg)
        if not parsed:
            print(f"SKIP (unparseable) {seq_dir.name}", file=sys.stderr)
            continue
        bws, jw, p99 = parsed
        per[(m.group("arm"), cg)].append({
            "iter": int(m.group("it")), "bws": bws, "jw": jw, "p99_us": p99,
            "agg": sum(bws),
        })

    for (arm, cg), iters in sorted(per.items()):
        aggs = [x["agg"] for x in iters]
        jws = [x["jw"] for x in iters]
        p99s = [x["p99_us"] for x in iters if x["p99_us"] is not None]
        # healthy iters: aggregate >= 0.5 * contention ref for this cg (phase
        # collapse floor exclusion); the ref json is optional
        ref = _contention_ref(cg)
        healthy = [(x["agg"], x["jw"]) for x in iters if ref and x["agg"] >= 0.5 * ref]
        rows.append({
            "arm": arm, "label": LABEL[arm], "cg": cg, "n_iters": len(iters),
            "agg_med_mb_s": f"{statistics.median(aggs):.1f}",
            "agg_min_mb_s": f"{min(aggs):.1f}", "agg_max_mb_s": f"{max(aggs):.1f}",
            "jainw_med": f"{statistics.median(jws):.4f}",
            "jainw_min": f"{min(jws):.4f}", "jainw_max": f"{max(jws):.4f}",
            "p99_med_us": f"{statistics.median(p99s):.0f}" if p99s else "",
            "p99_max_us": f"{max(p99s):.0f}" if p99s else "",
            "per_iter_agg": ";".join(f"{a:.1f}" for a in aggs),
            "per_iter_jainw": ";".join(f"{j:.4f}" for j in jws),
            "n_healthy": str(len(healthy)),
            "agg_healthy_med": f"{statistics.median([a for a, _ in healthy]):.1f}" if healthy else "",
            "jain_healthy_med": f"{statistics.median([j for _, j in healthy]):.4f}" if healthy else "",
        })

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "d2_rerun_by_arm.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT/'d2_rerun_by_arm.csv'} ({len(rows)} rows)")

    # ---- figure -------------------------------------------------------------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib missing — CSV only", file=sys.stderr)
        return

    lookup = {(r["arm"], int(r["cg"])): r for r in rows}
    none_jw = {}
    for cg in CGS:
        r = lookup.get(("none", cg))
        if r and r.get("jain_healthy_med"):
            none_jw[cg] = float(r["jain_healthy_med"])
        elif r and r["jainw_med"]:
            none_jw[cg] = float(r["jainw_med"])

    fig, axes = plt.subplots(2, 4, figsize=(9.0, 4.6), sharex="col",
                             gridspec_kw={"height_ratios": [3.2, 1.0], "hspace": 0.08})
    x = np.arange(len(ARMS))
    for ai, cg in enumerate(CGS):
        ax, axp = axes[0][ai], axes[1][ai]
        aggs_m, aggs_lo, aggs_hi, jws_m, jws_lo, jws_hi, p99s_m, p99s_all = [], [], [], [], [], [], [], []
        for arm in ARMS:
            r = lookup.get((arm, cg))
            if r:
                aggs = [float(v) for v in r["per_iter_agg"].split(";")]
                jw = [float(v) for v in r["per_iter_jainw"].split(";")]
                # healthy-iter preference for bars; collapsed iters shown as x dots
                if r.get("agg_healthy_med"):
                    h_a = [float(v) for v in r["per_iter_agg"].split(";")]
                    h_j = [float(v) for v in r["per_iter_jainw"].split(";")]
                    pairs = [(a, j) for a, j in zip(h_a, h_j) if a >= 0.5 * _contention_ref(cg)]
                    h_aggs = [a for a, _ in pairs]; h_jws = [j for _, j in pairs]
                else:
                    h_aggs, h_jws = aggs, jw
                p99s = [float(r["p99_med_us"])] if r["p99_med_us"] else []
                aggs_m.append(statistics.median(h_aggs)); aggs_lo.append(min(h_aggs)); aggs_hi.append(max(h_aggs))
                jws_m.append(statistics.median(h_jws)); jws_lo.append(min(h_jws)); jws_hi.append(max(h_jws))
                p99s_m.append(statistics.median(p99s) if p99s else np.nan)
                p99s_all.append(aggs)  # placeholder
            else:
                aggs_m.append(np.nan); aggs_lo.append(np.nan); aggs_hi.append(np.nan)
                jws_m.append(np.nan); jws_lo.append(np.nan); jws_hi.append(np.nan)
                p99s_m.append(np.nan); p99s_all.append([])

        colors = ["#9ecae1" if a == "none" else ("#2166ac" if a.startswith("iox_") else "#f4a582") for a in ARMS]
        ax.bar(x, aggs_m, width=0.62, color=colors, edgecolor="white", zorder=2)
        ax.errorbar(x, aggs_m, yerr=[np.array(aggs_m) - np.array(aggs_lo),
                                     np.array(aggs_hi) - np.array(aggs_m)],
                    fmt="none", ecolor="#555", elinewidth=0.8, capsize=2, zorder=3)
        # per-iter dots on the bars
        for xi, arm in enumerate(ARMS):
            r = lookup.get((arm, cg))
            if r:
                for a in [float(v) for v in r["per_iter_agg"].split(";")]:
                    ax.plot(xi, a, "o", ms=2.2, color="#333", alpha=0.55, zorder=4)

        ax2 = ax.twinx()
        ax2.plot(x, jws_m, color="#1b7837", lw=1.4, marker="s", ms=3.5, zorder=6, clip_on=True)
        for xi in range(len(ARMS)):
            if jws_lo[xi] == jws_lo[xi]:
                ax2.plot([xi, xi], [jws_lo[xi], jws_hi[xi]], color="#1b7837", lw=0.7, alpha=0.5, zorder=5)
        if cg in none_jw:
            ax2.axhline(none_jw[cg], color="#d62728", lw=0.9, alpha=0.85, zorder=1)
        ax2.set_ylim(0, 1.04)
        ax2.spines["right"].set_visible(False)
        ax2.tick_params(axis="y", labelsize=6, colors="#1b7837", pad=1)
        if ai == 0:
            ax2.set_ylabel("JainW", fontsize=8, color="#1b7837")
            ax.set_ylabel("agg BW (MB/s)", fontsize=8)
        else:
            ax2.set_yticks([])
        ax.set_title(f"cg={cg}", fontsize=9)
        ax.tick_params(axis="y", labelsize=7)

        # auxiliary p99 strip (log scale, clipped — deliberately secondary)
        pv = [v if v == v and v > 0 else np.nan for v in p99s_m]
        axp.bar(x, np.ones(len(x)), width=0.62, color="#f0f0f0", edgecolor="none")
        for xi, arm in enumerate(ARMS):
            r = lookup.get((arm, cg))
            if not r or not r["p99_med_us"]:
                continue
            axp.plot(xi, p99s_m[xi], "o", ms=3, color="#555", clip_on=True)
        axp.set_yscale("log")
        vals = [v for v in pv if v == v]
        if vals:
            lo = min(vals) * 0.7
            hi = max(vals) * 1.4
            axp.set_ylim(lo, hi)
        axp.set_ylim(bottom=max(1e3, axp.get_ylim()[0]))
        axp.tick_params(axis="y", labelsize=6)
        axp.set_ylabel("p99 µs", fontsize=6, color="#555")
        if ai == 0:
            axp.set_yticks([1e3, 1e4, 1e5, 1e6])
            axp.set_yticklabels(["1k", "10k", "100k", "1M"], fontsize=6)
        else:
            axp.set_yticks([])
        axp.set_xticks(x)
        axp.set_xticklabels([LABEL[a] for a in ARMS], rotation=50, ha="right", fontsize=7)

    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    handles = [
        Patch(facecolor="#9ecae1", label="none (baseline)"),
        Patch(facecolor="#f4a582", label="native knob"),
        Patch(facecolor="#2166ac", label="iox arb"),
        Line2D([0], [0], color="#1b7837", marker="s", ms=3, lw=1.2, label="JainW (median±range)"),
        Line2D([0], [0], color="#d62728", lw=1, label="JainW of none"),
        Line2D([0], [0], color="#555", marker="o", ms=3, lw=0, label="p99 (aux, log)"),
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0),
               ncol=6, fontsize=7, frameon=False)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.88, bottom=0.17, wspace=0.25)
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"fig_d2_rerun_by_cg.{ext}", dpi=200)
    print(f"wrote {OUT}/fig_d2_rerun_by_cg.png")


if __name__ == "__main__":
    main()
