"""Kyber rerun1 — LC read p99 / BE write per phase."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from kyber_curated import KYBER_CURATED_REPS, PHASE_LIST
from iolat_common import ARM_COLOR, ARM_LABEL, ARMS, RERUN1

KYBER_TARGET_MS = 0.08  # 80 µs design (reference only)
STEP_X = [0, 30, 30, 60, 60, 90]


def kyber_run(arm: str, rep: int) -> Path:
    return RERUN1 / "kyber" / f"kyber_{arm}_r{rep}"


def lc_read_p99_ms(run: Path, ph: str) -> float:
    j = json.loads((run / f"fio_{ph}_lc.json").read_text())
    v = j["jobs"][0]["read"]["clat_ns"]["percentile"]["99.000000"]
    return float(v) / 1e6


def be_write_mib_s(run: Path, ph: str) -> float:
    j = json.loads((run / f"fio_{ph}_be.json").read_text())
    return float(j["jobs"][0]["write"]["bw"]) / 1024.0


def curated_reps(ph: str, arm: str) -> tuple[int, ...]:
    return KYBER_CURATED_REPS.get((ph, arm), ())


def phase_median(arm: str, ph: str, metric: str) -> float:
    reps = curated_reps(ph, arm)
    vals = []
    for r in reps:
        run = kyber_run(arm, r)
        if metric == "lc_p99":
            vals.append(lc_read_p99_ms(run, ph))
        else:
            vals.append(be_write_mib_s(run, ph))
    return statistics.median(vals) if vals else float("nan")