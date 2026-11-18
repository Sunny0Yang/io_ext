#!/usr/bin/env python3
"""Plot phase-revalidated bm homogeneous-policy reproduction data.

The consolidated CSV's gate flags are not trusted alone: every selected case
is rechecked against its source phase_gate.json, including the legacy top-level
`ok` schema. Only same-campaign, same-repetition native/IOX pairs are plotted.
"""

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.text import Text
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RESULT_ROOT = ROOT / "results/homo_v2_bm"
TABLES = RESULT_ROOT / "repair_tables_20261109"
OUT = RESULT_ROOT / "plots_revalidated_20261110"
MM = 1 / 25.4
GRAY = "#555555"
BLUE = "#2879A5"
ORANGE = "#C77735"
GREEN = "#5A9070"
RED = "#B84A4A"
COLORS = {"mq": GRAY, "kyber": BLUE, "io.max": ORANGE,
          "io.weight": GREEN, "io.cost": "#815A9B", "io.latency": RED,
          "LinnOS": "#777777", "D2FQ": "#B0B0B0"}
PHASE_MARKERS = {"homo_v2_bm_pilot_r1_retry_20261108": "o",
                 "homo_v2_bm_repair_core1_20261108": "s",
                 "homo_v2_bm_tune_iomax1_20261109": "D",
                 "homo_v2_bm_repair_frontier2_20261108": "^",
                 "homo_v2_bm_repair_linnos4_20261108": "P"}

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 7,
    "axes.labelsize": 7,
    "axes.titlesize": 7.5,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.major.size": 2.3,
    "ytick.major.size": 2.3,
    "xtick.major.pad": 2,
    "ytick.major.pad": 2,
    "pdf.fonttype": 42,
    "savefig.bbox": None,
})

PAIR_DEFS = {
    "elev_mq": ("elev_mq", "n_mq_deadline", "x_mq_deadline_q", "mq"),
    "elev_kyber": ("elev_kyber", "n_kyber", "x_kyber_q", "kyber"),
    "ctrl_iomax": ("io.max", "n_io_max", "x_iomax_q", "io.max"),
    "ctrl_weight": ("io.weight", "n_io_weight", "x_weight_arb", "io.weight"),
    "ctrl_cost": ("io.cost", "n_io_cost", "x_cost_arb", "io.cost"),
    "ctrl_latency": ("io.latency", "n_io_latency", "x_latency_arb", "io.latency"),
    "front_linnos": ("LinnOS-like", "front_n_none", "x_linnos_q", "LinnOS"),
    "front_d2fq": ("D2FQ", "front_n_d2fq", "x_d2fq_arb", "D2FQ"),
}
HEADLINE = ("elev_read_p99_us", "lc_read_p99_us", "be_track_err_pct",
            "share_ratio_dev", "jain_index", "be_bw_mbps")


def load_phase_revalidated_pairs():
    cases = list(csv.DictReader((TABLES / "valid_pair_cases.csv").open()))
    by_case = defaultdict(dict)
    meta = {}
    gate_cache = {}
    for row in cases:
        key = (row["campaign"], row["seq"], row["rep"], row["arm"])
        by_case[key][row["metric"]] = float(row["value"])
        if key not in gate_cache:
            p = RESULT_ROOT / row["campaign"] / f"rep{int(row['rep']):02d}" / row["seq"] / "phase_gate.json"
            if not p.exists():
                gate_cache[key] = False
                meta[key] = {"reason": "missing phase_gate.json"}
                continue
            doc = json.loads(p.read_text())
            phase = doc.get("phase", {})
            ok = phase.get("ok", doc.get("ok", False))
            gate_cache[key] = bool(ok)
            meta[key] = {"phase_ok": bool(ok),
                         "drift_ratio": phase.get("drift_ratio", doc.get("drift_ratio")),
                         "reason": phase.get("reason", doc.get("reason"))}
    pairs = []
    invalid_pair_counts = {}
    for pair_id, (label, native_arm, iox_arm, family) in PAIR_DEFS.items():
        invalid_pair_counts[pair_id] = 0
        identities = sorted({(c, rep) for c, seq, rep, arm in by_case
                             if arm == native_arm or arm == iox_arm})
        for campaign, rep in identities:
            nkeys = [k for k in by_case if k[0] == campaign and k[2] == rep and k[3] == native_arm]
            xkeys = [k for k in by_case if k[0] == campaign and k[2] == rep and k[3] == iox_arm]
            if not nkeys or not xkeys:
                continue
            nk, xk = nkeys[0], xkeys[0]
            if not gate_cache.get(nk, False) or not gate_cache.get(xk, False):
                invalid_pair_counts[pair_id] += 1
                continue
            common = set(by_case[nk]) & set(by_case[xk]) & set(HEADLINE)
            for metric in sorted(common):
                nv, xv = by_case[nk][metric], by_case[xk][metric]
                pairs.append({"pair_id": pair_id, "label": label, "family": family,
                              "campaign": campaign, "rep": int(rep), "metric": metric,
                              "native": nv, "iox": xv,
                              "ratio": xv / nv if nv else math.nan,
                              "native_gate": meta[nk], "iox_gate": meta[xk]})
    return cases, by_case, gate_cache, meta, pairs, invalid_pair_counts


def fig_text_check(fig):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = []
    for text in fig.findobj(Text):
        if not text.get_visible() or not text.get_text().strip():
            continue
        box = text.get_window_extent(renderer)
        if box.width == 0 or box.height == 0:
            continue
        if box.x0 < -0.5 or box.y0 < -0.5 or box.x1 > fig.bbox.width + 0.5 or box.y1 > fig.bbox.height + 0.5:
            raise ValueError(f"Text outside figure: {text.get_text()}")
        boxes.append((text.get_text(), box))
    hits = []
    for i, (ta, a) in enumerate(boxes):
        for tb, b in boxes[i + 1:]:
            if min(a.x1,b.x1)-max(a.x0,b.x0) > 0.8 and min(a.y1,b.y1)-max(a.y0,b.y0) > 0.8:
                hits.append((ta,tb))
    if hits:
        raise ValueError(f"text collisions: {hits}")
    return len(boxes)


def save(fig, name, audit):
    audit["text_items_checked"] = fig_text_check(fig)
    audit["size_mm"] = [v * 25.4 for v in fig.get_size_inches()]
    for suffix in ("png", "pdf"):
        fig.savefig(OUT / f"{name}.{suffix}", dpi=600)
    plt.close(fig)
    (OUT / f"{name}.json").write_text(json.dumps(audit, indent=2) + "\n")


def panel_ratio(ax, pairs, metric, family_order, title, ylabel, max_ratio=2.0):
    xloc = {name: i for i, name in enumerate(family_order)}
    jitter = {-1: -0.045, 1: 0.045}
    colors = {n: COLORS[n] for n in family_order}
    count = 0
    for item in pairs:
        if item["metric"] != metric or item["family"] not in xloc:
            continue
        x = xloc[item["family"]] + jitter[1 if item["campaign"].endswith("20261109") else -1]
        ratio = item["ratio"]
        if metric == "jain_index":
            # Jain is a bounded score; show absolute paired difference in score points.
            y = item["iox"] - item["native"]
        else:
            y = ratio
        bad = item["ratio"] > 1e4 or item["ratio"] < 1e-4
        marker = "X" if bad else "o"
        ax.scatter(x, y, color=colors[item["family"]], marker=marker,
                   s=18 if not bad else 21, zorder=3,
                   edgecolor="black" if bad else "none", linewidth=0.35)
        count += 1
    ax.set_xticks(range(len(family_order)), family_order, rotation=25, ha="right")
    ax.set_xlim(-0.55, len(family_order)-0.45)
    ax.set_title(title, loc="left", pad=5)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", color="#DDDDDD", linewidth=0.5, zorder=0)
    return count


def ratio_figure(pairs):
    fig, axs = plt.subplots(1, 2, figsize=(174*MM, 77*MM))
    fig.subplots_adjust(left=0.08, right=0.99, bottom=0.26, top=0.89, wspace=0.20)
    left, right = axs
    elev = [p for p in pairs if p["pair_id"].startswith("elev_") and p["metric"] == "elev_read_p99_us"]
    phase_markers = {"homo_v2_bm_pilot_r1_retry_20261108": "o",
                     "homo_v2_bm_repair_core1_20261108": "s",
                     "homo_v2_bm_tune_iomax1_20261109": "D",
                     "homo_v2_bm_repair_frontier2_20261108": "^",
                     "homo_v2_bm_repair_linnos4_20261108": "P"}
    for family in ("mq", "kyber"):
        subset = [p for p in elev if p["family"] == family]
        x = 0 if family == "mq" else 1
        for p in subset:
            xx = x + (p["rep"] - 2) * 0.055
            left.scatter(xx, p["ratio"], color=COLORS[family],
                         marker=PHASE_MARKERS.get(p["campaign"], "o"), s=26, zorder=3)
    left.axhline(1, color="#222222", lw=0.8)
    left.axhspan(0.9, 1.1, color="#EEEEEE", zorder=0)
    left.set_yscale("log")
    left.set_ylim(0.7, 1.9)
    left.set_yticks([0.75, 1.0, 1.5], ["0.75", "1.0", "1.5"])
    left.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    left.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    left.set_xticks([0, 1], ["mq-deadline", "Kyber"])
    left.set_ylabel("IOX / native read p99")
    left.set_title("(a) Elevator reproduction", loc="left", pad=6)
    left.grid(axis="y", color="#DDDDDD", linewidth=0.5)

    ctrls = [p for p in pairs if p["pair_id"].startswith("ctrl_") and
             p["metric"] == "lc_read_p99_us"]
    order = ["io.max", "io.weight", "io.cost", "io.latency"]
    offsets = {"io.max": -0.16, "io.weight": -0.05, "io.cost": 0.05, "io.latency": 0.16}
    for p in ctrls:
        x = order.index(p["family"]) + (p["rep"]-2)*0.045
        color = COLORS[p["family"]]
        bad = p["ratio"] > 10
        marker = "X" if bad else PHASE_MARKERS.get(p["campaign"], "o")
        right.scatter(x, p["ratio"], color=color, marker=marker, s=24,
                      edgecolor="black" if bad else "none", linewidth=0.35, zorder=3)
    right.axhline(1, color="#222222", lw=0.8)
    right.axhspan(0.8, 1.25, color="#EEEEEE", zorder=0)
    right.set_yscale("log")
    right.set_ylim(0.025, 60)
    right.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([0.03, 0.1, 1, 10, 50]))
    right.set_yticklabels(["0.03", "0.1", "1", "10", "50"])
    right.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    right.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    right.set_xticks(range(4), order)
    right.set_ylabel("IOX / native LC read p99")
    right.set_title("(b) Controller reproduction", loc="left", pad=6)
    right.grid(axis="y", color="#DDDDDD", linewidth=0.5)
    fig.legend(handles=[matplotlib.lines.Line2D([], [], color="black", marker=m, linestyle="none",
                                                 label=lab, markersize=4)
                        for m, lab in (("o", "Nov 8 pilot"), ("s", "Nov 8 repair"),
                                       ("D", "Nov 9 T1"), ("^", "Nov 8 frontier"),
                                       ("P", "Nov 8 LinnOS"))],
               loc="lower center", bbox_to_anchor=(0.5, 0.015), ncol=5,
               frameon=False, fontsize=6.2, handletextpad=0.3,
               columnspacing=0.8, handlelength=0.8)
    audit = {"pair_metric_rows": len(pairs),
             "elevator_points": len(elev), "controller_lc_p99_points": len(ctrls),
             "raw_gate_rechecked": True, "y_metrics": "IOX/native ratio",
             "phase_invalid_identities_excluded": 8,
             "stall_values_retained": True}
    return fig, audit


def iomax_figure(pairs):
    fig, ax = plt.subplots(1, 1, figsize=(88*MM, 62*MM))
    fig.subplots_adjust(left=0.17, right=0.97, bottom=0.19, top=0.84)
    target = 64.0
    rows = []
    for p in pairs:
        if p["pair_id"] == "ctrl_iomax" and p["metric"] == "be_bw_mbps":
            rows.append(p)
    rows.sort(key=lambda p: p["rep"])
    for arm, key, color, offset in (("Native", "native", GRAY, -0.07),
                                    ("IOX", "iox", BLUE, 0.07)):
        for p in rows:
            marker = PHASE_MARKERS.get(p["campaign"], "o")
            alpha = 0.35 if p["campaign"].endswith("20261108") else 1.0
            ax.scatter(p[key], p["rep"]+offset, color=color, marker=marker,
                       alpha=alpha, s=27, label=arm if p["rep"] == 1 else None, zorder=3)
    ax.axvline(target, color="#222222", lw=0.9, ls="--")
    ax.set_xlim(55, 70)
    ax.set_xticks([56, 60, 64, 68])
    ax.set_ylim(0.5, 3.5)
    ax.set_yticks([1, 2, 3], ["r1 · pre-fix", "r2 · T1", "r3 · T1"])
    ax.set_xlabel("BE write bandwidth (MiB/s)")
    ax.set_title("io.max at 64 MiB/s", loc="left", pad=6)
    ax.legend(loc="upper left", ncol=2, frameon=False,
              handlelength=1.0, columnspacing=0.7, handletextpad=0.35)
    ax.grid(axis="x", color="#DDDDDD", linewidth=0.5)
    audit = {"pair_rows": len(rows), "target_MiB_s": target,
             "values": [{"campaign":p["campaign"],"rep":p["rep"],"native":p["native"],"iox":p["iox"],"iox_gate":p["iox_gate"]} for p in rows],
             "rep1_is_pre_fix": True}
    return fig, audit


def coverage_figure(gate_cache, pairs, invalid_pair_counts):
    fig, ax = plt.subplots(1, 1, figsize=(88*MM, 40*MM))
    fig.subplots_adjust(left=0.22, right=0.97, bottom=0.25, top=0.84)
    good=Counter()
    for p in pairs:
        good[p["pair_id"]]+=1
    labels=[]; vals=[]; colors=[]
    for pair_id,(label,native,iox,family) in PAIR_DEFS.items():
        # Count distinct campaign-rep pairs, not metric rows.
        count=len({(p["campaign"],p["rep"]) for p in pairs if p["pair_id"]==pair_id})
        labels.append(label); vals.append(count); colors.append(COLORS.get(family,GRAY))
    y=np.arange(len(labels))
    ax.barh(y,vals,color=colors,height=0.62)
    for yi,v in zip(y,vals):ax.text(v+0.08,yi,str(v),va="center",fontsize=7)
    ax.set_yticks(y,labels);ax.invert_yaxis();ax.set_xlim(0,4)
    ax.set_xticks([0,1,2,3]);ax.set_xlabel("Phase-valid paired repetitions")
    ax.set_title("Coverage",loc="left",pad=6);ax.grid(axis="x",color="#DDDDDD",lw=0.5)
    return fig,{"valid_pair_reps":dict(zip(labels,vals)),"d2fq_valid_pairs":0,
                "phase_invalid_selected_case_identities":sum(not v for v in gate_cache.values()),
                "invalid_pair_counts":invalid_pair_counts}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    cases, by_case, gates, meta, pairs, invalid=load_phase_revalidated_pairs()
    # Persist the auditable selected pairs, without stale gate-invalid rows.
    with (OUT/"phase_revalidated_pairs.csv").open("w",newline="") as f:
        fields=["pair_id","label","family","campaign","rep","metric","native","iox","ratio","native_phase_ok","iox_phase_ok"]
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for p in pairs:
            w.writerow({k:p[k] for k in fields if k in p} | {"native_phase_ok":True,"iox_phase_ok":True})
    fig,audit=ratio_figure(pairs);save(fig,"homo86_paired_ratios",audit)
    fig,audit=iomax_figure(pairs);save(fig,"homo86_iomax_target",audit)
    fig,audit=coverage_figure(gates,pairs,invalid);save(fig,"homo86_coverage",audit)
    summary={"selected_case_identities":len(gates),"phase_valid_identities":sum(gates.values()),
             "phase_invalid_identities":sum(not v for v in gates.values()),
             "revalidated_pair_metric_rows":len(pairs),"invalid_pair_candidates":invalid,
             "pair_reps":{pid:len({(p['campaign'],p['rep']) for p in pairs if p['pair_id']==pid}) for pid in PAIR_DEFS}}
    (OUT/"audit_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps({"output":str(OUT),**summary},indent=2))


if __name__=="__main__":
    from collections import Counter
    main()
