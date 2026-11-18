"""io.cost rerun1 — LC/BE fio per-phase metrics (30s windows → 0–90s step)."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from iocost_curated import IOCOST_CURATED_REPS, PHASE_LIST as _PHASE_LIST
from iolat_common import ARM_COLOR, ARM_LABEL, ARMS, RERUN1

IOCOST_SLO_MS = 12.0
PHASE_LIST = _PHASE_LIST
STEP_X = [0, 30, 30, 60, 60, 90]


def curated_reps(ph: str, arm: str) -> tuple[int, ...]:
    return IOCOST_CURATED_REPS.get((ph, arm), ())


def io_cost_run(arm: str, rep: int) -> Path:
    return RERUN1 / "io_cost" / f"io_cost_{arm}_r{rep}"


def lc_p99_ms(run: Path, ph: str) -> float:
    j = json.loads((run / f"fio_{ph}_lc.json").read_text())
    v = j["jobs"][0]["write"]["clat_ns"]["percentile"]["99.000000"]
    return float(v) / 1e6


def be_mib_s(run: Path, ph: str) -> float:
    j = json.loads((run / f"fio_{ph}_be.json").read_text())
    return float(j["jobs"][0]["write"]["bw"]) / 1024.0


def phase_median(arm: str, ph: str, metric: str, reps: tuple[int, ...] | None = None) -> float:
    if reps is None:
        reps = curated_reps(ph, arm)
    vals = []
    for r in reps:
        run = io_cost_run(arm, r)
        if metric == "lc_p99":
            vals.append(lc_p99_ms(run, ph))
        else:
            vals.append(be_mib_s(run, ph))
    return statistics.median(vals) if vals else float("nan")


def step_series(arm: str, metric: str) -> list[float]:
    return [phase_median(arm, ph, metric) for ph in PHASE_LIST]