"""Control Profile IDs and cgroup knob definitions for isol-bench.

Maps the paper's 5 evaluated knob mechanisms (§II / Table in
``isol-bench-origin-result.md``) to ioext-bench scheduler + cgroup
configuration. Each Control Profile ID is referenced by name in
``src/vm/configs/task/isol_bench/*.yml`` under ``io_ext_policy_profiles``.

Ported from:
- ``util/util_sysfs/cgroups.py`` — IOMax, IOCostModel, IOCostQOS, IOWeight,
  IOBFQWeight, IOLatency, IOPriorityClass dataclasses (exact sysfs formats)
- ``util/util_sysfs/nvme.py`` — IOScheduler enum
- ``2-fairness/fairness.py``, ``3-prio-vs-util/prio.py``, ``4-bursts/burst.py``
  — per-knob configurator functions (the exact knob values per profile)

Knob → sysfs file mapping (cgroup v2, ``/sys/fs/cgroup/<cg>/``):
- io.max        → ``{mm} rbps=.. wbps=.. riops=.. wiops=..``        (None → "max")
- io.weight     → ``{mm} {weight}``                                  (range 1..10000)
- io.bfq.weight → ``{mm} {weight}``                                  (range 1..1000)
- io.latency    → ``{mm} target={usec}``
- io.prio.class → ``{label}``  (no-change|promote-to-rt|restrict-to-be|idle|none-to-rt)
- io.cost.model → ``{mm} ctrl=user model=linear rbps=.. rseqiops=.. rrandiops=.. wbps=.. wseqiops=.. wrandiops=..``
- io.cost.qos   → ``{mm} enable=1 ctrl=user rpct=.. rlat=.. wpct=.. wlat=.. min=.. max=..``

io.cost.model and io.cost.qos are written at the cgroup **root**
(``/sys/fs/cgroup/io.cost.{model,qos}``), not per-child-cgroup.

Known SSD models (from ``get_iocostmodel_from_nvme_model``):
- Samsung SSD 980 PRO:  rbps=2706339840 riops=786432 rriops=786432 wbps=1063126016 wiops=135560 rwiops=130734
- Intel Optane P5800X:  rbps=2413821952 riops=589312 rriops=589312 wbps=2413821952 wiops=589312 rwiops=589312
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


__all__ = [
    "IOMax",
    "IOCostModel",
    "IOCostQOS",
    "IOWeight",
    "IOBFQWeight",
    "IOLatency",
    "IOPriorityClass",
    "IOScheduler",
    "IOCOST_MODELS",
    "ControlProfile",
    "CONTROL_PROFILES",
    "profile_by_id",
]


# ── Knob dataclasses (sysfs format matches upstream cgroups.py exactly) ──

@dataclass
class IOMax:
    major_minor: str
    rbps: Optional[int] = None
    wbps: Optional[int] = None
    riops: Optional[int] = None
    wiops: Optional[int] = None

    def to_str(self) -> str:
        def h(s):
            return "max" if s is None else str(s)
        return (
            f"{self.major_minor} rbps={h(self.rbps)} wbps={h(self.wbps)} "
            f"riops={h(self.riops)} wiops={h(self.wiops)}"
        )


@dataclass
class IOCostModel:
    major_minor: str
    ctrl: str = "user"
    model: str = "linear"
    rbps: int = 0
    rseqiops: int = 0
    rrandiops: int = 0
    wbps: int = 0
    wseqiops: int = 0
    wrandiops: int = 0

    def to_str(self) -> str:
        return (
            f"{self.major_minor} ctrl={self.ctrl} model={self.model} "
            f"rbps={self.rbps} rseqiops={self.rseqiops} rrandiops={self.rrandiops} "
            f"wbps={self.wbps} wseqiops={self.wseqiops} wrandiops={self.wrandiops}"
        )


@dataclass
class IOCostQOS:
    major_minor: str
    enable: bool = True
    ctrl: str = "user"
    rpct: float = 95.0
    rlat: int = 1_000_000
    wpct: float = 95.0
    wlat: int = 1_000_000
    min: float = 50.0
    max: float = 150.0

    def to_str(self) -> str:
        return (
            f"{self.major_minor} enable={'1' if self.enable else '0'} ctrl={self.ctrl} "
            f"rpct={self.rpct} rlat={self.rlat} wpct={self.wpct} wlat={self.wlat} "
            f"min={self.min} max={self.max}"
        )


@dataclass
class IOWeight:
    major_minor: str
    weight: int = 100

    def to_str(self) -> str:
        return f"{self.major_minor} {self.weight}"


@dataclass
class IOBFQWeight:
    major_minor: str
    weight: int = 100

    def to_str(self) -> str:
        return f"{self.major_minor} {self.weight}"


@dataclass
class IOLatency:
    major_minor: str
    target: int = 0

    def to_str(self) -> str:
        return f"{self.major_minor} target={self.target}"


class IOPriorityClass(Enum):
    NO_CHANGE = "no-change"
    PROMOTE_TO_RT = "promote-to-rt"
    RESTRICT_TO_BE = "restrict-to-be"
    IDLE = "idle"
    NONE_TO_RT = "none-to-rt"


class IOScheduler(Enum):
    NONE = "none"
    MQ_DEADLINE = "mq-deadline"
    BFQ = "bfq"
    KYBER = "kyber"
    SCX_IO = "scx_io"


# ── Known NVMe io.cost models (from get_iocostmodel_from_nvme_model) ──

IOCOST_MODELS: Dict[str, Dict[str, int]] = {
    "Samsung SSD 980 PRO": {
        "rbps": 2706339840, "riops": 786432, "rriops": 786432,
        "wbps": 1063126016, "wiops": 135560, "rwiops": 130734,
    },
    "INTEL SSDPE21D280GA": {  # Optane P5800X
        "rbps": 2413821952, "riops": 589312, "rriops": 589312,
        "wbps": 2413821952, "wiops": 589312, "rwiops": 589312,
    },
}


# ── Control Profile registry (design.md §4) ──

@dataclass
class ControlProfile:
    """One isol-bench control profile.

    Fields mirror the structure consumed by ioext-bench task YAML's
    ``io_ext_policy_profiles`` entry — so a YAML profile can be generated
    programmatically if needed.
    """

    profile_id: str
    name: str
    scheduler: str  # block-device scheduler
    # cgroup knob config, any of:
    io_weight: Optional[int] = None          # io.weight (iocost)
    io_bfq_weight: Optional[int] = None      # io.bfq.weight
    io_max: Optional[Dict[str, Any]] = None  # {rbps,wbps,riops,wiops}
    io_latency_target: Optional[int] = None  # io.latency target (usec)
    io_prio_class: Optional[str] = None      # io.prio.class label
    io_cost_model: Optional[Dict[str, int]] = None  # IOCOST_MODELS entry
    io_cost_qos: Optional[Dict[str, Any]] = None    # {rpct,rlat,wpct,wlat,min,max}
    io_cost_weight: Optional[int] = None     # io.weight applied to LC cgroup
    # io-ext loader chain (for scx_io profiles)
    scx_loaders: Optional[Dict[str, Any]] = None
    # BFQ scheduler params (set_ioscheduler_parameter)
    bfq_low_latency: Optional[int] = None
    bfq_slice_idle: Optional[int] = None
    # mq-deadline scheduler params
    mq_read_expire: Optional[int] = None
    mq_fifo_batch: Optional[int] = None
    mq_front_merges: Optional[int] = None
    # priority (desiderata relevance)
    priority: str = "P0"
    description: str = ""


CONTROL_PROFILES: Dict[str, ControlProfile] = {
    # ── A-class: scheduler-dependent ──
    "IB_CTRL_none": ControlProfile(
        profile_id="IB_CTRL_none",
        name="IB native none",
        scheduler="none",
        priority="P0",
        description="no knob, baseline (D1 reference)",
    ),
    "IB_CTRL_mq_deadline": ControlProfile(
        profile_id="IB_CTRL_mq_deadline",
        name="IB mq-deadline (no prio)",
        scheduler="mq-deadline",
        priority="P0",
        description="mq-deadline scheduler only, no io.prio.class",
    ),
    "IB_CTRL_mq_prio": ControlProfile(
        profile_id="IB_CTRL_mq_prio",
        name="IB mq-deadline + io.prio.class",
        scheduler="mq-deadline",
        io_prio_class="restrict-to-be",
        priority="P1",
        description="mq-deadline + io.prio.class (D3 sweep: idle/restrict-to-be/promote-to-rt)",
    ),
    "IB_CTRL_bfq": ControlProfile(
        profile_id="IB_CTRL_bfq",
        name="IB BFQ + io.bfq.weight",
        scheduler="bfq",
        bfq_low_latency=0,
        bfq_slice_idle=0,
        io_bfq_weight=100,
        priority="P2",
        description="BFQ scheduler + io.bfq.weight (range 1..1000)",
    ),
    # ── B-class: blk-mq native (no scheduler dependency) ──
    "IB_CTRL_iomax": ControlProfile(
        profile_id="IB_CTRL_iomax",
        name="IB io.max",
        scheduler="none",
        io_max={"rbps": 524288000, "wbps": 524288000, "riops": 1_000_000, "wiops": 1_000_000},
        # 500 MiB/s active (D1); 5000 MiB/s inactive. ioext-bench YAML overrides per-experiment.
        priority="P1",
        description="io.max hard bandwidth/IOPS limit (D1 active=500MiB/s, D3 sweep 80..2320)",
    ),
    "IB_CTRL_iolatency": ControlProfile(
        profile_id="IB_CTRL_iolatency",
        name="IB io.latency",
        scheduler="none",
        io_latency_target=10,
        # D1 active=10us; inactive=1_000_000us; D3 sweep 75..1175 step 25
        priority="P1",
        description="io.latency soft target (P90-based QD halving every 500ms)",
    ),
    "IB_CTRL_iocost": ControlProfile(
        profile_id="IB_CTRL_iocost",
        name="IB io.cost + io.weight",
        scheduler="none",
        io_cost_qos={
            "enable": True, "rpct": 95.0, "rlat": 1_000_000,
            "wpct": 95.0, "wlat": 1_000_000, "min": 50.0, "max": 150.0,
        },
        io_cost_weight=100,
        priority="P0",
        description="io.cost (BPF cost model) + io.weight — paper's strongest baseline",
    ),
    # ── io-ext scx_io profiles (D2/D3/D5 equivalents) ──
    "IB_SCX_homo": ControlProfile(
        profile_id="IB_SCX_homo",
        name="IB scx_io q_sp_edf (homo)",
        scheduler="scx_io",
        scx_loaders={
            "root_policies": [{
                "name": "q_sp_edf_root",
                "path": "/opt/io-ext/userspace/loader_q_sp_edf",
                "scope": "root_policy",
                "args": ["-c", "{policy_cgroup_path}"],
            }],
        },
        priority="P0",
        description="scx_io + loader_q_sp_edf (homogeneous tenants, D2 equivalent)",
    ),
    "IB_SCX_hetero": ControlProfile(
        profile_id="IB_SCX_hetero",
        name="IB scx_io arb_mix + per-tenant q (hetero)",
        scheduler="scx_io",
        scx_loaders={
            "arbiters": [{
                "name": "arb_mix",
                "path": "/opt/io-ext/userspace/loader_arb_mix",
                "scope": "arbiter",
            }],
        },
        priority="P0",
        description="scx_io + arb_mix + per-tenant queue policies (D3 hetero equivalent)",
    ),
    "IB_SCX_hetero_iocost": ControlProfile(
        profile_id="IB_SCX_hetero_iocost",
        name="IB scx_io hetero + io.cost",
        scheduler="scx_io",
        scx_loaders={
            "arbiters": [{
                "name": "arb_mix",
                "path": "/opt/io-ext/userspace/loader_arb_mix",
                "scope": "arbiter",
            }],
        },
        io_cost_qos={
            "enable": True, "rpct": 95.0, "rlat": 1_000_000,
            "wpct": 95.0, "wlat": 1_000_000, "min": 50.0, "max": 150.0,
        },
        io_cost_weight=100,
        priority="P2",
        description="cross product: hetero scx_io + io.cost (D5 equivalent)",
    ),
}


def profile_by_id(profile_id: str) -> ControlProfile:
    """Look up a ControlProfile by ID; raises ValueError if unknown."""
    if profile_id not in CONTROL_PROFILES:
        raise ValueError(
            f"unknown isol-bench control profile: {profile_id!r}; "
            f"available: {sorted(CONTROL_PROFILES)}"
        )
    return CONTROL_PROFILES[profile_id]