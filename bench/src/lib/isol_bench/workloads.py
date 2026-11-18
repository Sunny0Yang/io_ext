"""Fio workload profiles for isol-bench — ported from upstream.

Source: ``isol-bench/1-overhead/run_latency.py``, ``run_bandwidth.py``,
``2-fairness/fairness.py``, ``3-prio-vs-util/prio.py``, ``4-bursts/burst.py``.

Three tenant profiles (fio option dictionaries, values match upstream exactly):

| Profile | bs  | rw        | iodepth | ioengine  | runtime | ramp  | cpus   | use |
|---------|-----|-----------|---------|-----------|---------|-------|--------|-----|
| LC-app  | 4k  | randread  | 1       | io_uring* | 60s     | 20s   | '2'    | D1 latency, D3 LC, D4 LC burst |
| Batch   | 4k  | randread  | 256     | io_uring* | 60s     | 20s   | '1-10' | D1 bandwidth scalability |
| BE-app  | 4k  | randread  | 256     | libaio    | 30s/60s | 20s   | '1-10' | D3/D4 background contention |

(*) D1 uses io_uring with fixedbufs=1 registerfiles=1; D2/D3/D4 use libaio
(upstream uses libaio for fairness/prio/bursts to match the paper's §VI setup).

Key fio options preserved:
- ``direct=1``, ``thread=0`` (fork mode — required because io.prio is not
  inherited across fork; upstream comment in fairness.py), ``group_reporting=1``
- ``time_based=1``, ``size=100%`` (raw device)
- ``percentile_list``: extra-high-tail for LC: 20:25:...:99.9:99.99:100
- ``exitall=1`` + ``exit_what=stonewall`` in D3 (so LC finishing ends the run)

Precondition profile (post write-experiments, non-Optane only):
fill pass (write bs=64k qd=64) + precondition (randwrite bs=4k qd=256
numjobs=4 runtime=15m).
"""

from __future__ import annotations

from typing import Any, Dict


__all__ = [
    "LC_APP",
    "BATCH_APP",
    "BE_APP",
    "PRECONDITION",
    "DEFAULT_RUNTIME_SEC",
    "DEFAULT_RAMP_SEC",
    "EXTRA_HIGH_TAIL_PERCENTILE_LIST",
    "profile_for",
]


DEFAULT_RAMP_SEC = 20
DEFAULT_RUNTIME_SEC = 60
EXTRA_HIGH_TAIL_PERCENTILE_LIST = (
    "20:25:30:35:40:45:50:55:60:65:70:75:80:85:90:95:99:99.9:99.99:100"
)


# ── LC-app (latency-sensitive) — D1 latency / D3 LC tenant / D4 LC burst ──
# Source: run_latency.py setup_jobs (io_uring + fixedbufs + registerfiles)
LC_APP: Dict[str, Any] = {
    "rw": "randread",
    "direct": 1,
    "thread": 0,
    "group_reporting": 1,
    "size": "100%",
    "ioengine": "io_uring",
    "fixedbufs": 1,
    "registerfiles": 1,
    "iodepth": 1,
    "bs": "4096",
    "numjobs": 1,
    "time_based": 1,
    "ramp_time": f"{DEFAULT_RAMP_SEC}s",
    "runtime": f"{DEFAULT_RUNTIME_SEC}s",
    "cpus_allowed": "2",
    "percentile_list": EXTRA_HIGH_TAIL_PERCENTILE_LIST,
}


# ── Batch-app (bandwidth scalability) — D1-Q2 ──
# Source: run_bandwidth.py (io_uring + QD=256, cpus 1-10)
BATCH_APP: Dict[str, Any] = {
    "rw": "randread",
    "direct": 1,
    "thread": 0,
    "group_reporting": 1,
    "size": "100%",
    "ioengine": "io_uring",
    "fixedbufs": 1,
    "registerfiles": 1,
    "iodepth": 256,
    "bs": "4096",
    "numjobs": 1,
    "time_based": 1,
    "ramp_time": f"{DEFAULT_RAMP_SEC}s",
    "runtime": f"{DEFAULT_RUNTIME_SEC}s",
    "cpus_allowed": "1-10",
}


# ── BE-app (best-effort background) — D3/D4 ──
# Source: prio.py setup_gjob (libaio, QD=256) — D3 runtime 30s, D4 runtime 80s
BE_APP: Dict[str, Any] = {
    "rw": "randread",
    "direct": 1,
    "thread": 0,
    "group_reporting": 1,
    "size": "100%",
    "ioengine": "libaio",
    "iodepth": 256,
    "bs": "4096",
    "numjobs": 1,
    "time_based": 1,
    "ramp_time": f"{DEFAULT_RAMP_SEC}s",
    "runtime": "30s",
    "cpus_allowed": "1-10",
}


# ── Precondition (non-Optane, post write-experiments) ──
# Source: 2-fairness/precondition.fio & 3-prio-vs-util/precondition.fio
PRECONDITION: Dict[str, Any] = {
    "global": {
        "direct": 1,
        "thread": 0,
        "size": "100%",
        "ioengine": "libaio",
        "time_based": 0,
    },
    "fill": {
        "rw": "write",
        "iodepth": 64,
        "bs": "65536",
        "numjobs": 1,
    },
    "precond": {
        "stonewall": True,
        "rw": "randwrite",
        "time_based": 1,
        "runtime": "15m",
        "bs": "4096",
        "iodepth": 256,
        "numjobs": 4,
    },
}


_PROFILE_MAP = {
    "lc": LC_APP,
    "lc_app": LC_APP,
    "lc-app": LC_APP,
    "batch": BATCH_APP,
    "batch_app": BATCH_APP,
    "batch-app": BATCH_APP,
    "be": BE_APP,
    "be_app": BE_APP,
    "be-app": BE_APP,
}


def profile_for(name: str) -> Dict[str, Any]:
    """Return a copy of the named fio profile (``lc``/``batch``/``be``)."""
    key = str(name).strip().lower()
    if key not in _PROFILE_MAP:
        raise ValueError(f"unknown isol-bench profile: {name!r} (lc|batch|be)")
    return dict(_PROFILE_MAP[key])