from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass(frozen=True)
class FioTimeSample:
    time_ms: float
    value: float
    direction: int | None = None
    block_size: int | None = None


def parse_fio_time_log(path: str | Path, value_scale: float = 1.0) -> List[FioTimeSample]:
    samples: List[FioTimeSample] = []
    with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) < 2:
                continue
            try:
                time_ms = float(row[0].strip())
                value = float(row[1].strip()) * value_scale
                direction = int(row[2].strip()) if len(row) > 2 and row[2].strip() else None
                block_size = int(row[3].strip()) if len(row) > 3 and row[3].strip() else None
            except ValueError:
                continue
            samples.append(FioTimeSample(time_ms, value, direction, block_size))
    return samples


def summarize_fio_time_log(path: str | Path) -> Dict[str, float | int | str]:
    path = Path(path)
    latency_log = any(token in path.name for token in ("_clat.", "_lat.", "_slat."))
    scale = 1.0 / 1000.0 if latency_log else 1.0
    samples = parse_fio_time_log(path, value_scale=scale)
    if not samples:
        return {"path": str(path), "sample_count": 0}
    values = sorted(sample.value for sample in samples)
    percentile_index = max(0, math.ceil(len(values) * 0.99) - 1)
    return {
        "path": str(path),
        "sample_count": len(samples),
        "start_time_ms": samples[0].time_ms,
        "end_time_ms": samples[-1].time_ms,
        "mean": sum(values) / len(values),
        "p99": values[percentile_index],
        "unit": "us" if latency_log else "KiB/s",
    }
