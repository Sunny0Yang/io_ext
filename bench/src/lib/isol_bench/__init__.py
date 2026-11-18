"""isol-bench integration library for ioext-bench.

Ports the IISWC 2025 isol-bench (atlarge-research/isol-bench) metrics,
workload profiles, and cgroup knob definitions so ioext-bench task YAMLs
under ``src/vm/configs/task/isol_bench/`` can faithfully reproduce the
D1-D4 performance-isolation experiments.

Submodules:
- ``desiderata``: D1-D4 constant identifiers
- ``metrics``: Jain's fairness index (weighted), proportional slowdown
- ``workloads``: LC-app / Batch-app / BE-app fio profile dictionaries
- ``knobs``: Control Profile IDs → scheduler/cgroup knob descriptions

Importable as ``lib.isol_bench.*`` when ``PYTHONPATH`` includes ``src``
(the ioext-bench convention, see README of explore_scene_combo).
"""

from .desiderata import D1_OVERHEAD, D2_FAIRNESS, D3_PRIO_UTIL, D4_BURST  # noqa: F401