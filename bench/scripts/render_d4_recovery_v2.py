#!/usr/bin/env python3
"""Render the D4 burst-recovery figure (design-fix rerun, 7 representative arms).

Inputs: an extracted-results root containing paper_sdb_D4LC_*_v2 sequence dirs
(BE job now carries write_bw_log -> 1s bandwidth buckets; LC job keeps the 1ms
clat log). Timeline convention:
  BE job   : starts at t=0, runs 80 s (QD1024 4k randread, saturation).
  LC burst : startdelay=60s, runs 10 s (QD1 4k randread), i.e. wall 60-70 s.
Panel A: LC per-second P99 (clat, log-y) inside the burst window.
Panel B: BE per-second throughput showing the dip when the burst lands and the
         recovery after the burst ends (annotated per arm).

Output: results/rq1_paper_figs/fig_d4_burst_recovery_v2.{png,pdf}
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

BURST_START_S = 60.0
BURST_END_S = 70.0

ARM_LABELS = {
    "none": "none",
    "bfq": "bfq",
    "io_latency": "io.latency",
    "io_max": "io.max",
    "iox_q_wedf": "iox q_wedf",
    "iox_arb_only": "iox_arb_only (arb_wdrr+q_fifo)",
    "iox_q_ddl_arb_wdrr": "iox_q_ddl_arb_wdrr",
}
ARM_COLORS = {
    "none": "#888888",
    "bfq": "#1f77b4",
    "io_latency": "#2ca02c",
    "io_max": "#9467bd",
    "iox_q_wedf": "#ff7f0e",
    "iox_arb_only": "#d62728",
    "iox_q_ddl_arb_wdrr": "#17becf",
}
ARM_ORDER = ["none", "bfq", "io_latency", "io_max", "iox_q_wedf", "iox_arb_only",
             "iox_q_ddl_arb_wdrr"]


def find_log(seq_dir: Path, scene_sub: str, kind: str) -> Path | None:
    hits = [p for p in sorted(seq_dir.glob(f"step_*/*{scene_sub}*/*{kind}*.log"))
            if not p.name.endswith(("slat", "lat")) or kind == "clat"]
    # precise: match suffix pattern *_<kind>.1.log and lat vs clat
    hits = [p for p in sorted(seq_dir.glob(f"step_*/*{scene_sub}*/*_{kind}.1.log"))]
    return hits[0] if hits else None


def parse_time_log(path: Path) -> list[tuple[float, float]]:
    out = []
    for line in path.read_text().splitlines():
        parts = [x.strip() for x in line.split(",") if x.strip()]
        if len(parts) < 2:
            continue
        try:
            t, v = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        out.append((t, v))
    return out


def lc_window_p99(seq_dir: Path, win_ms: int = 250, min_n: int = 4) -> dict[float, float]:
    """LC clat 1ms log -> windowed p99 in us, keyed by window-start offset (s).

    Unit note (verified against each arm's fio-JSON clat_ns p99, same as
    paper/dac_ioext/scripts/render_paper_d4_timeseries.py): fio lat-log col2 is
    NANOSECONDS for these runs (log_avg_msec=1 -> one averaged sample per ms).
    We render p99 of the 1ms-averaged samples per 250 ms window — same metric
    convention as the accepted rq13_d4_timeseries figure.
    """
    log = find_log(seq_dir, "_lc", "clat")
    if log is None:
        return {}
    samples = parse_time_log(log)
    if not samples:
        return {}
    out: dict[float, float] = {}
    t0 = samples[0][0]
    for start in range(0, int(samples[-1][0] - t0) + 1, win_ms):
        vals = sorted(v / 1000.0 for t, v in samples  # ns -> us
                      if start <= t - t0 < start + win_ms)
        if len(vals) < min_n:
            continue
        idx = max(0, math.ceil(len(vals) * 0.99) - 1)
        out[start / 1000.0] = vals[idx]
    return out


def be_bw_series(seq_dir: Path) -> tuple[list[float], list[float]]:
    """BE *_bw.1.log -> (t_s from BE start, MB/s). Handles log_unix_epoch
    timestamps (epoch-ms) and relative-ms timestamps."""
    log = find_log(seq_dir, "_be", "bw")
    if log is None:
        return [], []
    samples = parse_time_log(log)
    if not samples:
        return [], []
    t0 = samples[0][0]
    epoch_like = t0 > 1e12  # unix epoch ms
    if epoch_like:
        ts = [(t - t0) / 1000.0 for t, _ in samples]
    else:
        ts = [t / 1000.0 for t, _ in samples]
    bw = [v / 1024.0 for _, v in samples]  # KiB/s -> MiB/s
    return ts, bw


def summarize(seq_dir: Path) -> dict:
    out: dict = {"seq_dir": str(seq_dir)}
    out["lc_p99_by_s"] = lc_window_p99(seq_dir)
    p99s = list(out["lc_p99_by_s"].values())
    out["lc_p99_overall_us"] = max(p99s) if p99s else None
    ts, bw = be_bw_series(seq_dir)
    pre = [b for t, b in zip(ts, bw) if BURST_START_S - 10 <= t < BURST_START_S]
    dur = [b for t, b in zip(ts, bw) if BURST_START_S <= t < BURST_END_S]
    post = [b for t, b in zip(ts, bw) if t >= BURST_END_S]
    out["be_t"] = ts
    out["be_bw"] = bw
    out["be_pre_median"] = sorted(pre)[len(pre) // 2] if pre else None
    out["be_burst_min"] = min(dur) if dur else None
    # recovery: first second at/after burst end where bw >= 90% of pre median
    rec = None
    if out["be_pre_median"] and post:
        thr = 0.9 * out["be_pre_median"]
        for t, b in zip(ts, bw):
            if t >= BURST_END_S and b >= thr:
                rec = t - BURST_END_S
                break
    out["be_recovery_s"] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted-root", type=Path, required=True)
    ap.add_argument("--out-prefix", type=Path,
                    default=Path("results/rq1_paper_figs/fig_d4_burst_recovery_v2"))
    ap.add_argument("--json-out", type=Path,
                    default=Path("results/rq1_paper_figs/fig_d4_burst_recovery_v2_summary.json"))
    args = ap.parse_args()

    data: dict[str, dict] = {}
    for arm in ARM_ORDER:
        cand = sorted(args.extracted_root.glob(f"paper_sdb_D4LC_{arm}_v2")) or \
            sorted(args.extracted_root.glob(f"D4LC_{arm}_v2"))
        if not cand:
            print(f"MISSING arm {arm}", file=sys.stderr)
            continue
        data[arm] = summarize(cand[-1])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8.5, 6.6), sharex=True)
    for arm in ARM_ORDER:
        if arm not in data:
            continue
        d = data[arm]
        color = ARM_COLORS[arm]
        label = ARM_LABELS[arm]
        if d["lc_p99_by_s"]:
            xs = [BURST_START_S + s + 0.125 for s in sorted(d["lc_p99_by_s"])]
            ys = [d["lc_p99_by_s"][s] for s in sorted(d["lc_p99_by_s"])]
            ax1.plot(xs, ys, marker="o", ms=3.5, lw=1.4, color=color, label=label)
        if d["be_t"]:
            ax2.plot(d["be_t"], d["be_bw"], lw=1.4, color=color, label=label)
    # uncontended LC reference (CALIB_solo_lc_none = 506 us, PROVENANCE.md)
    ax1.axhline(0.506, color="k", ls="--", lw=0.9)
    ax1.text(BURST_START_S + 0.3, 0.506 * 1.18,
             "uncontended LC p99 (solo, 506 $\\mu$s)", fontsize=7)

    for ax in (ax1, ax2):
        ax.axvspan(BURST_START_S, BURST_END_S, color="gold", alpha=0.18, zorder=0)
        ax.axvline(BURST_END_S, color="k", ls=":", lw=0.8)
    ax1.set_yscale("log")
    ax1.set_ylabel("LC p99 clat (us)")
    ax1.set_title("D4 burst: LC P99 recovery (top) and background throughput (bottom)\n"
                  "design-fix rerun, 7 representative arms (sdb, BE QD1024 80 s + LC QD1 burst 60-70 s)")
    ax1.legend(fontsize=7, ncol=2, loc="upper right")
    ax2.set_ylabel("BE throughput (MiB/s)")
    ax2.set_xlabel("time since BE start (s)")
    ax2.legend(fontsize=7, ncol=2, loc="lower right")
    ax2.set_xlim(45, 82)
    fig.tight_layout()
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(args.out_prefix) + ".png", dpi=160)
    fig.savefig(str(args.out_prefix) + ".pdf")

    import json
    slim = {}
    for arm, d in data.items():
        slim[arm] = {
            "lc_p99_by_250ms_window_us": d["lc_p99_by_s"],
            "lc_p99_overall_us": d["lc_p99_overall_us"],
            "be_pre_burst_median_mibs": d["be_pre_median"],
            "be_burst_min_mibs": d["be_burst_min"],
            "be_recovery_s_after_burst_end": d["be_recovery_s"],
        }
    args.json_out.write_text(json.dumps(slim, indent=2))
    print(f"figure -> {args.out_prefix}.png/.pdf")
    print(f"summary -> {args.json_out}")
    for arm in ARM_ORDER:
        if arm in data:
            d = data[arm]
            print(f"{ARM_LABELS[arm]:34s} LCp99_overall={d['lc_p99_overall_us']}us "
                  f"BEpre={d['be_pre_median']} BEburstMin={d['be_burst_min']} "
                  f"BErec={d['be_recovery_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
