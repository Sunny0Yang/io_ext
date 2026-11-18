"""mq_deadline_v2 fio_mq.json helpers."""

from __future__ import annotations

import json
from pathlib import Path

from iolat_common import ARM_COLOR, ARM_LABEL, ARMS
from mq_curated import MQ_CURATED_REPS, bar_value

REPO = Path(__file__).resolve().parents[3]
MQ_V2 = REPO / "experiments/reproduction/results/mq_deadline_v2"


def mq_run(rep: int, arm: str) -> Path:
    return MQ_V2 / f"rep{rep}" / arm


def clat_p99_us(run: Path, direction: str) -> float:
    j = json.loads((run / "fio_mq.json").read_text())
    d = j["jobs"][0][direction]
    return float(d["clat_ns"]["percentile"]["99.000000"]) / 1000.0


def curated_values(direction: str, arm: str) -> list[float]:
    reps = MQ_CURATED_REPS.get((direction, arm), ())
    return [clat_p99_us(mq_run(r, arm), direction) for r in reps]


def bar_us(direction: str, arm: str) -> float:
    return bar_value(curated_values(direction, arm), arm)