#!/usr/bin/env python3
"""Reference-style mean bars from phase-revalidated bm homo pairs.

Mean bars are grouped by campaign and fast/slow p99 phase. Documented stall-
scale latency pairs are excluded as whole pairs; none is shown only when the
same campaign/repetition has an independently phase-valid none observation.
"""

import importlib.util
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("homo_revalidated", HERE / "plot_homo_v2_bm_revalidated.py")
DATA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DATA)
OUT = DATA.OUT
MM = 1 / 25.4
NONE_COLOR, NATIVE_COLOR, IOX_COLOR = "#999999", "#f28e2b", "#2878b5"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 7,
    "axes.titlesize": 7.5, "axes.labelsize": 6.5,
    "xtick.labelsize": 6, "ytick.labelsize": 5.8,
    "legend.fontsize": 7, "axes.linewidth": 0.55,
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.major.size": 2, "ytick.major.size": 2,
    "xtick.major.pad": 1.5, "ytick.major.pad": 1.2,
    "pdf.fonttype": 42, "savefig.bbox": None,
})

PANELS = [
    ("elev_mq", "elev_read_p99_us", "mq-deadline", "4 KiB randmix\nQD64"),
    ("elev_kyber", "elev_read_p99_us", "Kyber", "4 KiB randmix\nQD64"),
    ("ctrl_iomax", "lc_read_p99_us", "io.max", "LC/BE\nQD32/16"),
    ("ctrl_weight", "lc_read_p99_us", "io.weight", "LC/BE\nQD32/16"),
    ("ctrl_cost", "lc_read_p99_us", "io.cost", "LC/BE\nQD32/16"),
    ("ctrl_latency", "lc_read_p99_us", "io.latency", "LC/BE\nQD32/16"),
    ("front_linnos", "lc_read_p99_us", "LinnOS-like", "LC/BE\nQD32/16"),
    ("front_d2fq", "lc_read_p99_us", "D2FQ", "LC/BE\nQD32/16"),
]


def campaign_short(campaign):
    if "pilot" in campaign:
        return "P"
    if "tune_iomax" in campaign:
        return "T1"
    return "R"


def phase_filter(pair_id, native, iox):
    """Return phase class, or an explicit exclusion reason."""
    if pair_id.startswith("elev_"):
        if max(native, iox) >= 60000:
            return None, "reported repeated ~68.7 ms elevator stall plateau"
        return "F", None
    # The report identifies 31.8--44 ms controller/frontier p99 plateaus as
    # window phase-switch stall artifacts. Exclude the complete paired sample.
    if native >= 30000 or iox >= 30000:
        return None, "reported controller/frontier stall-scale p99 >=30 ms"
    # A large one-sided difference is a phase-switch mismatch, not an outlier
    # that can be repaired by averaging the two arms.
    if max(native, iox) >= 20000 and min(native, iox) < 20000:
        return None, "one-sided phase-switch stall"
    if pair_id == "front_linnos" and native < 20000 and iox >= 20000:
        return None, "documented cross-phase mismatch"
    return "F", None


def draw_panel(ax, pairs, cases, gates, pair_id, metric, title, workload):
    rows = [p for p in pairs if p["pair_id"] == pair_id and p["metric"] == metric]
    groups, excluded = {}, []
    for p in rows:
        phase, reason = phase_filter(pair_id, p["native"], p["iox"])
        if reason:
            excluded.append({"rep": p["rep"], "campaign": p["campaign"], "reason": reason})
            continue
        groups.setdefault((p["campaign"], phase), []).append(p)

    ax.set_title(title, fontweight="bold", loc="center", pad=3)
    ax.set_xlabel(workload, labelpad=2, fontsize=6)
    if not groups:
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        ax.set_xticks([])
        ax.text(0.5, 0.5, "No clean pair", transform=ax.transAxes,
                ha="center", va="center", fontsize=6)
        return {"n_clean_pairs": 0, "groups": [], "excluded": excluded}

    group_keys = sorted(groups, key=lambda k: ("pilot" not in k[0], "tune_iomax" not in k[0], k[0]))
    all_values, records = [], []
    width = 0.23
    for i, key in enumerate(group_keys):
        campaign, phase = key
        ps = groups[key]
        native_vals = [p["native"] for p in ps]
        iox_vals = [p["iox"] for p in ps]
        none_vals = []
        none_arm = "front_n_none" if pair_id.startswith("front_") else "n_none"
        for p in ps:
            candidates = [k for k, vals in cases.items()
                          if k[0] == campaign and k[2] == str(p["rep"])
                          and k[3] == none_arm and metric in vals and gates.get(k, False)]
            if candidates:
                value = cases[candidates[0]][metric]
                if value < (60000 if pair_id.startswith("elev_") else 30000):
                    none_vals.append(value)
        bars = [(none_vals, NONE_COLOR, "///", "none"),
                (native_vals, NATIVE_COLOR, "///", "Native policy"),
                (iox_vals, IOX_COLOR, None, "IOX")]
        for j, (values, color, hatch, label) in enumerate(bars):
            if not values:
                continue
            mean = float(np.mean(values))
            all_values.append(mean)
            ax.bar(i + (j-1)*width, mean, width=width, color=color,
                   edgecolor="#333333", linewidth=0.35, hatch=hatch,
                   label=label, zorder=3)
        records.append({
            "campaign": campaign, "phase": phase,
            "n_pairs": len(ps), "reps": [p["rep"] for p in ps],
            "none_n": len(none_vals), "none_mean": float(np.mean(none_vals)) if none_vals else None,
            "native_policy_mean": float(np.mean(native_vals)),
            "iox_mean": float(np.mean(iox_vals)),
        })
    ymax = max(all_values) if all_values else 1
    ax.set_ylim(0, ymax * 1.22)
    ax.set_yticks([0, ymax*0.5, ymax], ["0", f"{ymax*0.5:.0f}", f"{ymax:.0f}"])
    ax.set_xticks(np.arange(len(group_keys)),
                  [f"{campaign_short(c)}-{phase}\nn={len(groups[(c, phase)])}" for c, phase in group_keys])
    ax.grid(axis="y", color="#DDDDDD", linewidth=0.45, zorder=0)
    ax.set_axisbelow(True)
    return {"n_clean_pairs": sum(len(v) for v in groups.values()), "groups": records, "excluded": excluded}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    _, cases, gates, _, pairs, invalid = DATA.load_phase_revalidated_pairs()
    fig, axes = plt.subplots(1, len(PANELS), figsize=(180*MM, 49*MM), squeeze=False)
    axes = axes.ravel()
    fig.subplots_adjust(left=0.065, right=0.997, bottom=0.25, top=0.78, wspace=0.72)
    panel_audit = {}
    for i, (ax, (pair_id, metric, title, workload)) in enumerate(zip(axes, PANELS)):
        panel_audit[title] = draw_panel(ax, pairs, cases, gates, pair_id, metric,
                                        f"({chr(97+i)}) {title}", workload)
        if i == 0:
            ax.set_ylabel("Read p99 (ms)", fontsize=6.5)
        elif i == 2:
            ax.set_ylabel("LC p99 (µs)", fontsize=6.5)
        else:
            ax.set_ylabel("")

    handles = [matplotlib.patches.Patch(facecolor=NONE_COLOR, edgecolor="#333333", hatch="///", label="none"),
               matplotlib.patches.Patch(facecolor=NATIVE_COLOR, edgecolor="#333333", hatch="///", label="Native policy"),
               matplotlib.patches.Patch(facecolor=IOX_COLOR, edgecolor="#333333", label="IOX")]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.995), ncol=3,
               frameon=False, handlelength=1.2, columnspacing=1.6, handletextpad=0.45)

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = [t for t in fig.findobj(matplotlib.text.Text) if t.get_visible() and t.get_text().strip()]
    boxes = []
    for t in texts:
        b = t.get_window_extent(renderer)
        if b.width and b.height:
            if b.x0 < -0.5 or b.y0 < -0.5 or b.x1 > fig.bbox.width+0.5 or b.y1 > fig.bbox.height+0.5:
                raise ValueError(f"text outside figure: {t.get_text()}")
            boxes.append((t.get_text(), b))
    for i, (ta, a) in enumerate(boxes):
        for tb, b in boxes[i+1:]:
            if min(a.x1,b.x1)-max(a.x0,b.x0) > 0.8 and min(a.y1,b.y1)-max(a.y0,b.y0) > 0.8:
                raise ValueError(f"text collision: {ta!r} / {tb!r}")

    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"homo86_reproduction_strip.{ext}", dpi=600)
    plt.close(fig)
    audit = {
        "figure_size_mm": [180, 49],
        "metric": "paired read p99; elevator in ms, controller/frontier in us",
        "pairing": "same campaign and repetition; raw phase_gate.json rechecked",
        "phase_valid_pairs_after_filter": {k: v["n_clean_pairs"] for k,v in panel_audit.items()},
        "filter": "exclude reported elevator 68.7ms stall plateau; exclude controller/frontier p99 >=30ms; means are arithmetic and grouped by campaign/phase",
        "panels": panel_audit,
        "phase_invalid_selected_cases_excluded": sum(not v for v in gates.values()),
        "rejected_pair_candidates": invalid,
        "none_rule": "only exact same campaign+rep+passing raw phase gate; unavailable baselines are omitted, not imputed",
        "n_is_clean_pairs_not_independent_campaigns": True,
    }
    (OUT / "homo86_reproduction_strip.json").write_text(json.dumps(audit, indent=2)+"\n")
    print(json.dumps({"png": str(OUT/"homo86_reproduction_strip.png"),
                      "pdf": str(OUT/"homo86_reproduction_strip.pdf"),
                      "panel_n": audit["phase_valid_pairs_after_filter"]}, indent=2))


if __name__ == "__main__":
    main()
