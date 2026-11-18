#!/usr/bin/env python3
"""kvmhost 8×2 design-ref figure: σ(B+C+D) red lines + unified scales, T1–T6."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_kvmhost_timeseries_combo import (  # noqa: E402
    BE,
    COLORS,
    OUT,
    RESULTS,
    collect_arm,
    p99_line_points,
)

COLS = 8
PANEL_IN = 1.02
ROW_TOP = [
    ("n1_native_none", "none", "static"),
    ("n2_native_mqddl", "mq-deadline", "static"),
    ("n4_native_kyber", "kyber", "static"),
    ("n3_native_bfq", "bfq", "static"),
    ("c2_bfq_weight", "io.bfq.weight", "static"),
    ("c1_io_max", "io.max", "static"),
    ("c3_io_latency", "io.latency", "static"),
    ("c4_io_cost", "io.cost", "static"),
]
ROW_IOEXT = [
    ("x1_qnone", "q_none", "heldout"),
    ("x2_qfifo_wdrr", "fifo+wdrr", "heldout"),
    ("x5_qkyber", "q_kyber", "static"),
    ("x3_qddl_wdrr", "ddl+wdrr", "heldout"),
    ("x4_spwedf_wdrr", "wedf+wdrr", "heldout"),
    ("f1_arbfb", "fifo+feedback", "heldout"),
    ("x6_qratelimit_wdrr", "q_ratelimit", "static"),
    ("x7_fifo_arbiocost", "arb_cost", "static"),
]

A_COLOR = COLORS["a"]
SUM_COLOR = "0.18"
HELDOUT = {
    "n1_native_none", "n2_native_mqddl", "n3_native_bfq", "n4_native_kyber",
    "x1_qnone", "x2_qfifo_wdrr", "x3_qddl_wdrr", "x4_spwedf_wdrr", "f1_arbfb",
}

END_MK = dict(marker="o", ms=3.2, mfc="white", mew=0.95, ls="", zorder=5)
PHASE_TICKS = list(range(1, 7))
IDEAL_COLOR = "#d62728"
# Frozen trajectory: which BE tenants run in each phase (§7.1).
TRAJECTORY_BE_ACTIVE: dict[int, tuple[str, ...]] = {
    1: (),
    2: ("b",),
    3: ("b", "c"),
    4: ("c", "d"),
    5: ("d",),
    6: (),
}
CALIB_JSON = RESULTS / "contract/calibration.json"


def resolve_seq(arm: str, kind: str) -> Path | None:
    if kind == "placeholder":
        return None
    if kind == "heldout":
        p = RESULTS / f"DYN_held_s201_{arm}"
        if p.is_dir():
            return p
    if kind == "static":
        p = RESULTS / f"DYN_arm_{arm}"
        return p if p.is_dir() else None
    p = RESULTS / f"DYN_arm_{arm}"
    if p.is_dir():
        return p
    if arm in HELDOUT:
        p = RESULTS / f"DYN_held_s201_{arm}"
        return p if p.is_dir() else None
    return None


def load_contract_sigma_bcd_mib() -> dict[int, float]:
    """Design Σ(B+C+D) per phase: frozen fio --rate on B/C + D solo ceiling when active."""
    cal = json.loads(CALIB_JSON.read_text())
    rates = cal.get("rates") or {}
    rb = int(rates["rate_b_mib_s"])
    rc = int(rates["rate_c_mib_s"])
    solo_d = float(cal["solo"]["d"]["steady_mib_s"])
    cap = {"b": rb, "c": rc, "d": solo_d}
    return {
        ph: sum(cap[t] for t in TRAJECTORY_BE_ACTIVE[ph])
        for ph in TRAJECTORY_BE_ACTIVE
    }


def draw_sigma_bcd_reference(ax, sigma_mib: dict[int, float], nphases: int) -> None:
    for ph in range(1, nphases + 1):
        mib = sigma_mib.get(ph, 0.0)
        if mib <= 0:
            continue
        ax.hlines(
            mib / 1024.0, ph - 1, ph,
            colors=IDEAL_COLOR, lw=1.4, zorder=5,
        )


def _to_phase_x(t: float, lphase: float) -> float:
    if lphase <= 0:
        return t
    return t / lphase


def sum_bcd_mib_series(data: dict) -> list[tuple[float, float]]:
    acc: dict[float, float] = {}
    for ten in BE:
        for t, v in data["bw"][ten]:
            if v != v:
                continue
            key = round(t, 1)
            acc[key] = acc.get(key, 0.0) + v
    return sorted(acc.items())


def sum_bcd_gib_points(data: dict) -> tuple[list[float], list[float]]:
    lphase = data["lphase"]
    xs, ys = [], []
    for t, v in sum_bcd_mib_series(data):
        xs.append(_to_phase_x(t, lphase))
        ys.append(v / 1024.0)
    return xs, ys


def p99_phase_points(data: dict) -> tuple[list[float], list[float]]:
    xs, ys = p99_line_points(data["a_p99"])
    xs = [_to_phase_x(x, data["lphase"]) for x in xs]
    return xs, ys


def bw_points(series: list[tuple[float, float]], lphase: float) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for t, v in series:
        if v != v:
            if xs:
                xs.append(float("nan"))
                ys.append(float("nan"))
            continue
        xs.append(_to_phase_x(t, lphase))
        ys.append(v / 1024.0)
    return xs, ys


def _endpoints(xs: list[float], ys: list[float]) -> tuple[tuple[float, float] | None, ...]:
    valid = [(x, y) for x, y in zip(xs, ys) if y == y]
    if not valid:
        return None, None
    return valid[0], valid[-1]


def plot_bw(ax, series: list[tuple[float, float]], lphase: float, color: str) -> None:
    bx, by = bw_points(series, lphase)
    ax.plot(bx, by, color=color, ls="-", lw=1.2, zorder=2)
    first, last = _endpoints(bx, by)
    for pt in (first, last):
        if pt is not None:
            ax.plot(pt[0], pt[1], mec=color, **END_MK)


def _phase_vlines(ax, nphases: int) -> None:
    for k in range(1, nphases):
        ax.axvline(k, color="0.88", lw=0.45, zorder=0)


def _hide_y_axis_values(ax) -> None:
    from matplotlib.ticker import NullFormatter

    ax.yaxis.set_major_formatter(NullFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.tick_params(axis="y", left=False, labelleft=False)


def draw_split(
    ax_p99,
    ax_bw,
    data: dict,
    *,
    nphases: int,
    p99_lo: float,
    p99_hi: float,
    bw_hi: float,
    show_xlabel: bool,
    show_ylabel: bool = False,
    sigma_bcd_mib: dict[int, float] | None = None,
) -> None:
    lphase = data["lphase"]
    xs, ys = p99_phase_points(data)
    ax_p99.plot(xs, ys, color=A_COLOR, ls="-", lw=1.35, zorder=4)
    ax_p99.set_yscale("log")
    ax_p99.set_ylim(p99_lo, p99_hi)
    ax_p99.set_xlim(0, nphases)
    _hide_y_axis_values(ax_p99)
    if show_ylabel:
        ax_p99.set_ylabel("p99", fontsize=8, labelpad=1)
    ax_p99.tick_params(axis="x", labelbottom=False, bottom=True, length=2)
    ax_p99.grid(True, axis="y", alpha=0.22, lw=0.4, zorder=0)
    _phase_vlines(ax_p99, nphases)

    sx, sy = sum_bcd_gib_points(data)
    ax_bw.plot(sx, sy, color=SUM_COLOR, ls="-", lw=1.55, zorder=1, alpha=0.92)
    for t in BE:
        plot_bw(ax_bw, data["bw"][t], lphase, COLORS[t])
    ax_bw.set_ylim(0, bw_hi)
    ax_bw.set_xlim(0, nphases)
    _hide_y_axis_values(ax_bw)
    if show_ylabel:
        ax_bw.set_ylabel("bw", fontsize=8, labelpad=1)
    ax_bw.tick_params(axis="x", labelsize=5.8, length=2)
    ax_bw.grid(True, axis="y", alpha=0.22, lw=0.4, zorder=0)
    _phase_vlines(ax_bw, nphases)
    if sigma_bcd_mib is not None:
        draw_sigma_bcd_reference(ax_bw, sigma_bcd_mib, nphases)

    if show_xlabel:
        ax_bw.set_xticks([i - 0.5 for i in PHASE_TICKS])
        ax_bw.set_xticklabels([f"T{i}" for i in PHASE_TICKS])
    else:
        ax_bw.tick_params(axis="x", labelbottom=False)


def render(
    all_data: dict,
    out_dir: Path,
    *,
    stem: str = "kvmhost_all_arms_compact_design_ref",
    sigma_bcd_mib: dict[int, float] | None = None,
) -> None:
    if sigma_bcd_mib is None:
        sigma_bcd_mib = load_contract_sigma_bcd_mib()
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    out_dir.mkdir(parents=True, exist_ok=True)
    sample = next(iter(all_data.values()))
    nph = int(sample["nphases"])

    all_p99 = [
        r["p99_clat_us"] for d in all_data.values() for r in d["a_p99"]
        if r["sample_count"] > 0 and r["p99_clat_us"] > 0
    ]
    p99_lo = 0.9 * 10 ** math.floor(math.log10(min(all_p99)))
    p99_hi = 1.08 * 10 ** math.ceil(math.log10(max(all_p99)))
    all_gib = []
    for d in all_data.values():
        for _, v in sum_bcd_mib_series(d):
            all_gib.append(v / 1024.0)
        for t in BE:
            for _, v in d["bw"][t]:
                if v == v and v > 0:
                    all_gib.append(v / 1024.0)
    sigma_gib = 0.0
    if sigma_bcd_mib:
        sigma_gib = max(sigma_bcd_mib.values()) / 1024.0
    bw_hi = 1.06 * max(all_gib + ([sigma_gib] if sigma_gib else []))

    legend_top = 1.0
    panel_top = 0.84
    fig = plt.figure(figsize=(COLS * PANEL_IN, 2 * PANEL_IN * 1.12))
    outer = fig.add_gridspec(2, COLS, wspace=0.06, hspace=0.38,
                             left=0.07, right=0.995, top=panel_top, bottom=0.07)

    p99_axes: list = []
    bw_axes: list = []

    for idx, (arm, title, kind) in enumerate(ROW_TOP + ROW_IOEXT):
        r, c = divmod(idx, COLS)
        data = all_data.get(arm)
        if data is None:
            ax = fig.add_subplot(outer[r, c])
            ax.text(0.5, 0.5, "TBD", ha="center", va="center", fontsize=9,
                    fontweight="bold", color="0.55", transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            for sp in ax.spines.values():
                sp.set_visible(False)
            ax.set_title(title, fontsize=8, fontweight="bold", pad=2)
            continue

        inner = outer[r, c].subgridspec(2, 1, height_ratios=[1, 1], hspace=0.06)
        ax_p99 = fig.add_subplot(inner[0])
        ax_bw = fig.add_subplot(inner[1], sharex=ax_p99)
        draw_split(
            ax_p99, ax_bw, data,
            nphases=nph, p99_lo=p99_lo, p99_hi=p99_hi, bw_hi=bw_hi,
            show_xlabel=(r == 1),
            show_ylabel=(r == 0 and c == 0),
            sigma_bcd_mib=sigma_bcd_mib,
        )
        ax_p99.set_title(title, fontsize=8, fontweight="bold", pad=3)
        p99_axes.append(ax_p99)
        bw_axes.append(ax_bw)

    if p99_axes:
        ref_p99, ref_bw = p99_axes[0], bw_axes[0]
        for ax in p99_axes[1:]:
            ax.sharey(ref_p99)
        for ax in bw_axes[1:]:
            ax.sharey(ref_bw)
        for ax in p99_axes + bw_axes:
            _hide_y_axis_values(ax)

    handles = [
        Line2D([0], [0], color=A_COLOR, ls="-", lw=2, label="LC A (p99)"),
        Line2D([0], [0], color=COLORS["b"], ls="-", lw=2, label="BE B (bw)"),
        Line2D([0], [0], color=COLORS["c"], ls="-", lw=2, label="BE C (bw)"),
        Line2D([0], [0], color=COLORS["d"], ls="-", lw=2, label="BE D (bw)"),
        Line2D([0], [0], color=SUM_COLOR, ls="-", lw=2.2, label="Σ BE (B+C+D)"),
        Line2D([0], [0], color="0.35", marker="o", mfc="white", mew=1.2,
               markersize=5, ls="", label="BE B/C/D start·end"),
    ]
    if sigma_bcd_mib is not None:
        handles.append(
            Line2D([0], [0], color=IDEAL_COLOR, ls="-", lw=2,
                   label="σ BE (B+C+D) (design)")
        )
    fig.legend(
        handles=handles,
        ncol=len(handles),
        loc="upper center",
        frameon=False,
        fontsize=6.2,
        handlelength=1.4,
        columnspacing=0.65,
        handletextpad=0.35,
        borderaxespad=0,
        bbox_to_anchor=(0.5, legend_top),
    )

    for ext in ("png", "pdf"):
        fig.savefig(
            out_dir / f"{stem}.{ext}",
            dpi=200,
            bbox_inches="tight",
            pad_inches=0.06,
        )
    plt.close(fig)


def main() -> None:
    all_data: dict = {}
    meta = []
    for arm, title, kind in ROW_TOP + ROW_IOEXT:
        sd = resolve_seq(arm, kind)
        if sd is None:
            meta.append({"arm": arm, "title": title, "status": "missing"})
            continue
        if not any((sd / "step_0").glob("ib_rdyn_*")):
            meta.append({"arm": arm, "title": title, "status": "incomplete", "sequence_id": sd.name})
            continue
        try:
            all_data[arm] = collect_arm(sd)
        except FileNotFoundError:
            meta.append({"arm": arm, "title": title, "status": "incomplete", "sequence_id": sd.name})
            continue
        meta.append({"arm": arm, "title": title, "sequence_id": sd.name, "source": str(sd)})

    if not all_data:
        raise SystemExit("no data")
    sigma = load_contract_sigma_bcd_mib()
    stem = "kvmhost_all_arms_compact_design_ref"
    render(all_data, OUT, stem=stem, sigma_bcd_mib=sigma)
    sigma_doc = {f"T{k}": f"{v} MiB/s" for k, v in sigma.items()}
    meta_path = OUT / f"{stem}.meta.json"
    meta_path.write_text(
        json.dumps({
            "figure": f"{stem}.png",
            "layout": "8x2 split p99|bw (Σ BCD line); unified global y scales",
            "x_axis": "T1..T6 (time / phase_length_sec)",
            "y_axis": "unified scales; left ylab p99 / bw (no tick numbers)",
            "sigma_bcd_mib_per_phase": sigma_doc,
            "sigma_note": (
                "B/C = frozen fio --rate (299/198 MiB/s); D = solo steady ceiling "
                "when active; per-phase sum over TRAJECTORY_BE_ACTIVE"
            ),
            "data_root": str(RESULTS),
            "panels": meta,
        }, indent=2) + "\n")
    print("Wrote", OUT / f"{stem}.png")


if __name__ == "__main__":
    main()