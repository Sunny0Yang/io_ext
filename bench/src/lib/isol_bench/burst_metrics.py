from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Dict, List, Sequence

from lib.benchmark.fio_time_log import FioTimeSample, parse_fio_time_log


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile from empty values")
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def compute_burst_response_ms(
    log_path: str | Path,
    *,
    mode: str,
    burst_start_ms: float = 0.0,
    target: float | None = None,
    reference_window_ms: float = 1000.0,
    stable_window_ms: float = 100.0,
    tolerance: float = 0.10,
) -> Dict[str, float | int | str | None]:
    if mode not in {"latency", "bandwidth"}:
        raise ValueError("mode must be 'latency' or 'bandwidth'")
    scale = 1.0 / 1000.0 if mode == "latency" else 1.0
    samples = parse_fio_time_log(log_path, value_scale=scale)
    samples = [sample for sample in samples if sample.time_ms >= burst_start_ms]
    if not samples:
        raise ValueError(f"no fio samples at or after burst_start_ms={burst_start_ms}")

    end_time = samples[-1].time_ms
    reference = [sample.value for sample in samples if sample.time_ms >= end_time - reference_window_ms]
    if not reference:
        reference = [sample.value for sample in samples]
    if target is None:
        target = _percentile(reference, 0.99) if mode == "latency" else statistics.median(reference)
    threshold = target * (1.0 + tolerance) if mode == "latency" else target * (1.0 - tolerance)

    response_ms: float | None = None
    for sample in samples:
        window_end = sample.time_ms + stable_window_ms
        window = [item.value for item in samples if sample.time_ms <= item.time_ms < window_end]
        if not window:
            continue
        stable_value = _percentile(window, 0.99) if mode == "latency" else statistics.mean(window)
        reached = stable_value <= threshold if mode == "latency" else stable_value >= threshold
        if reached:
            response_ms = sample.time_ms - burst_start_ms
            break

    return {
        "mode": mode,
        "sample_count": len(samples),
        "target": target,
        "threshold": threshold,
        "response_ms": response_ms,
        "value_unit": "us" if mode == "latency" else "KiB/s",
    }
