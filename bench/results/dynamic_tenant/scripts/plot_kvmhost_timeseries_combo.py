#!/usr/bin/env python3
"""Shared loaders for kvmhost dynamic-tenant timeline arms (compact figure)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_here = Path(__file__).resolve()
REPO = _here
for _ in range(6):
    if (REPO / "scripts" / "fio_hist_p99_windows.py").is_file():
        break
    REPO = REPO.parent
else:
    raise RuntimeError(f"ioext-bench repo root not found above {_here}")
sys.path.insert(0, str(REPO / "scripts"))
from fio_hist_p99_windows import tenant_windows  # noqa: E402

RESULTS = REPO / "results/dyn_tenant_kvmhost"
OUT = REPO / "dynamic_tenant/kvmhost/figures"

COLORS = {"a": "#a83b86", "b": "#218741", "c": "#bc8b0a", "d": "#1f6fb8"}
BE = ("b", "c", "d")


def load_stat(scene_dir: Path) -> dict:
    paths = sorted(scene_dir.glob("timeline_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        raise FileNotFoundError(scene_dir)
    return json.loads(paths[0].read_text())


def parse_bw_kib_series(bw_log: Path, t0_epoch_s: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    prev_ms: int | None = None
    for line in bw_log.read_text().splitlines():
        if not line.strip():
            continue
        parts = [int(x.strip()) for x in line.split(",")]
        t_ms, kib_s = parts[0], parts[1]
        t_rel = t_ms / 1000.0 - t0_epoch_s
        mib_s = kib_s / 1024.0
        out.append((t_rel, mib_s))
        if prev_ms is not None and t_ms - prev_ms > 1500:
            out.append((t_rel, float("nan")))
        prev_ms = t_ms
    return out


def collect_arm(seq_dir: Path) -> dict:
    scene_dirs = sorted((seq_dir / "step_0").glob("ib_rdyn_*"))
    if not scene_dirs:
        raise FileNotFoundError(seq_dir)
    scene_dir = scene_dirs[0]
    stat = load_stat(scene_dir)
    t0 = float(stat["start_time"])
    t0_mono = float(stat["t0_monotonic"])
    lphase = float(stat.get("phase_length_sec", 90.0))
    nph = int(stat["num_phases"])
    token = stat["timeline_id"]
    boundaries = [float(b["planned_monotonic"]) - t0_mono for b in stat["boundaries"]]

    a_rows, _ = tenant_windows(
        scene_dir / f"rdyn_{token}_ta_clat_hist.1.log",
        scene_dir / f"rdyn_{token}_ta_fio.json",
        0,
        t0,
    )
    bw = {}
    for t in BE:
        bw[t] = parse_bw_kib_series(scene_dir / f"rdyn_{token}_t{t}_bw_bw.1.log", t0)
    return {
        "a_p99": a_rows,
        "bw": bw,
        "boundaries": boundaries,
        "lphase": lphase,
        "nphases": nph,
        "seq": seq_dir.name,
    }


def p99_line_points(rows: list[dict]) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for r in rows:
        if r["sample_count"] == 0:
            continue
        x = 0.5 * (r["window_start_s"] + r["window_end_s"])
        xs.append(x)
        ys.append(r["p99_clat_us"])
    return xs, ys