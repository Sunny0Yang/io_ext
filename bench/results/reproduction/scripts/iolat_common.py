"""Shared paths and fio helpers for results/reproduction (eight-arm strip deps)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RERUN1 = REPO / "experiments/reproduction/results/rerun1"
ARMS = ("none", "native", "iox")
ARM_COLOR = {"none": "#7f7f7f", "native": "#d95f02", "iox": "#1f78b4"}
ARM_LABEL = {"none": "none", "native": "native", "iox": "io_ext"}
IOLAT_SLO_MS = 12.0

_SCRIPTS = REPO / "experiments/reproduction/RESULTS/scripts"
if _SCRIPTS.is_dir() and str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
try:
    from common import load_json  # noqa: E402
except ImportError:
    load_json = None


def iolat_run(arm: str, rep: int) -> Path:
    return RERUN1 / "io_latency" / f"io_latency_{arm}_r{rep}"


def fio_lc_p99_ms(run: Path, phase: str) -> float:
    path = run / f"fio_{phase}_lc.json"
    if load_json is not None:
        j = load_json(str(path))
    else:
        j = json.loads(path.read_text())
    if not j:
        return float("nan")
    for jo in j.get("jobs", []):
        v = jo.get("write", {}).get("clat_ns", {}).get("percentile", {}).get("99.000000")
        if v is not None:
            return float(v) / 1e6
    return float("nan")