"""Dynamic multi-tenant timeline scene (main-experiment skeleton).

Six-phase continuous trajectory on ONE monotonic clock (``t0``) with absolute
deadlines.  Unlike ``policy_transition.py`` (per-phase relative sleeps that
accumulate drift) this scene computes every phase boundary as an absolute
monotonic deadline ``t0 + k * L`` up-front and drives the main loop by the
*remaining* time to the next deadline, so boundary drift never accumulates.

Phase contract (M2 motivation; frozen in
``docs/plans/dynamic_tenant_reconfiguration_experiment.md``):

    T1={A}  T2={A,B}  T3={A,B,C}  T4={A,C,D}  T5={A,D}  T6={A}

i.e. tenant spans (inclusive phase numbers):

    A: T1..T6   B: T2..T3   C: T3..T4   D: T4..T5

Tenants that stay active across a phase boundary are NOT restarted and keep
their parameters; a boundary only joins/exits whole tenants.

Tenant workload precedents (do not change silently; recorded per contract):
  A: 4K randread QD1 single job   -- rig_iox d4 foreground precedent
     (src/vm/configs/task/isol_bench/rig_iox/d4_burst_rig_iox.yml: bs 4k,
      randread, iodepth 1, numjobs 1)
  B: 64K randread QD8 single job  -- M2 fig2 precedent
     (scripts/gen_m2_fig2_yamls.py: bs 64k, randread, iodepth 8, numjobs 1)
  C: 64K randread QD8 single job  -- same shape as B (second batch tenant)
  D: 4K randwrite QD64 single job -- explore_scene_combo 08 ycsb+randwrite
     co-location precedent
     (src/vm/configs/task/explore_scene_combo/08_ycsb_lat_fio_randwrite.yml:
      bs 4k, randwrite, iodepth 64, numjobs 1)
All tenants: direct=1, io_uring, one fio job each, per-tenant cgroup
``/sys/fs/cgroup/rdyn_<seq>_t<tenant>``, disjoint disk regions (see
``DISK_LAYOUT_DEFAULT``).

Per-tenant fio instrumentation follows the verified histogram collection
implementation ``scripts/m2_fig2_p99_guest.py`` (``fio_command``):
``--write_hist_log --log_hist_msec=1000 --log_hist_coarseness=0
--log_unix_epoch=1 --write_bw_log --log_avg_msec=1000 --clat_percentiles=1
--lat_percentiles=0 --output-format=json+ --randseed=<explicit>``.
D's completion-latency evidence comes from the write entries of the same
clat_hist log (fio tags each entry with a direction field).

Service preference A > C > B = D is recorded in the scene config comments and
the contract document; this skeleton does NOT implement any scheduling
preference (native scheduler only).

Static cgroup-v2 controller arms (contract C1-C4, supported 20261105):
``io_ext.cgroup_tenant_controls`` applies per-tenant io.max / io.weight /
io.latency / io.bfq.weight when the tenant spawns; ``io_ext.io_cost`` writes
the root io.cost model/qos once in ``setup()`` (before any tenant spawn) and
turns ``tenant_weights`` into per-tenant io.weight ``<MAJ:MIN> <weight>``
device lines at spawn.  Every write is followed by a strict read-back
verification: a missing io.* file or a mismatching line is a fatal scene
error -- a silently skipped control would turn a C-arm run into an
unthrottled run and corrupt the arm comparison matrix.

Phase-boundary action hooks: each boundary may declare a list of pre-declared
actions (``phase_actions``).  Supported:
  * ``none``          -- record-only marker event.
  * ``switch_q``      -- placeholder interface (records events; only invokes a
                     pre-resolved ``loader_command`` when
                     ``allow_switch_q_exec`` is true).  Kept for backwards
                     compatibility; prefer ``swap_q``.
  * ``switch_native`` -- native<->native elevator transition on the device
                     (``common.set_scheduler``); the switched-phase tenants'
                     fio keeps running.
  * ``swap_q``        -- io_ext q hot swap for specific tenants under ONE
                     RESIDENT arbiter: start the new per-tenant q loader
                     (kernel replaces the old policy), wait for the
                     policy_meta owner_token flip, then stop the old loader.
                     Tenant fio is never restarted.
  * ``attach_io_ext`` -- native->io_ext boundary: switch the device to
                     ``scx_io`` and start the loader chain (arbiter first,
                     then per-tenant q) from an inline ``io_ext`` profile
                     (same mechanism as the static-arms global loader scene).
  * ``detach_io_ext`` -- io_ext->native boundary: stop the loader chain in
                     reverse order, drain (assert pending==inflight==0 and
                     queue_depth==0), then restore the native elevator.

Feasibility boundaries (frozen from
ioext/userspace/io/tools/hotsplit/NOTES.md + kernel_fix/FIX_NOTES.md):
the arbiter is NEVER hot-replaced by this scene; the q_wedf<->q_sp_edf swap
pair is rejected (known-unfixed service-share collapse, NOTES 4.3).  The
scene validates the action sequence statically in ``__init__`` (a native/io_ext
mode state machine over boundaries) and again at runtime.

Every transition records an event pair (transition_begin/transition_end) with
type, start/end monotonic times, rc, drain result and an io_ext account
snapshot delta (policy_meta + tenant_state via bpftool).  Failures are
fail-visible into ``collected_data.errors``.  ``cleanup_stale`` /
``cleanup_bpffs_pins`` stay forbidden while tenants are active -- including
inside transition profiles.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from vm.scene.base_scene import Scene

from .common import (
    BASH_BIN,
    SUDO_BIN,
    _config_bool,
    cleanup_cgroup,
    expand_path,
    interrupt_process,
    is_process_scheduler_alias,
    popen_in_own_process_group,
    popen_in_own_session,
    resolve_scheduler_device,
    run_sudo_shell,
    set_scheduler,
    setup_cgroup_io_controls,
    setup_cgroup_v2,
    setup_iocost_at_root,
    signal_process,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
    write_json_stat,
)

logger = logging.getLogger("io_ext_timeline")


# ---------------------------------------------------------------------------
# Pure timeline math (unit-tested in tests/test_dynamic_tenant_timeline.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantSpan:
    """Absolute monotonic span of one tenant on the shared clock."""

    tenant: str
    join_boundary: int          # boundary index k at which the tenant joins
    exit_boundary: int          # boundary index k at which the tenant exits
    start_monotonic: float      # absolute planned start (t0 + join*L)
    end_monotonic: float        # absolute planned exit  (t0 + exit*L)

    @property
    def span_sec(self) -> float:
        return self.end_monotonic - self.start_monotonic


@dataclass
class TimelinePlan:
    """Pre-computed six-phase plan on one absolute monotonic clock."""

    t0_monotonic: float
    phase_length_sec: float
    num_phases: int
    boundaries: List[float] = field(default_factory=list)   # len == num_phases + 1
    spans: Dict[str, TenantSpan] = field(default_factory=dict)
    phase_active: Dict[int, Tuple[str, ...]] = field(default_factory=dict)  # 1-based phase -> tenants

    def boundary_deadline(self, boundary: int) -> float:
        """Absolute monotonic deadline of boundary *boundary* (0..num_phases)."""
        if boundary < 0 or boundary > self.num_phases:
            raise ValueError(f"boundary {boundary} outside 0..{self.num_phases}")
        return self.boundaries[boundary]

    def joins_at(self, boundary: int) -> List[str]:
        return sorted(
            tenant
            for tenant, span in self.spans.items()
            if span.join_boundary == boundary
        )

    def exits_at(self, boundary: int) -> List[str]:
        return sorted(
            tenant
            for tenant, span in self.spans.items()
            if span.exit_boundary == boundary
        )

    def expected_active_at_boundary(self, boundary: int) -> Tuple[str, ...]:
        """Tenants expected alive just after *boundary* (0..num_phases)."""
        return tuple(sorted(
            tenant
            for tenant, span in self.spans.items()
            if span.join_boundary <= boundary < span.exit_boundary
        ))


def compute_timeline(
    t0_monotonic: float,
    phase_length_sec: float,
    num_phases: int,
    tenant_windows: Mapping[str, Tuple[int, int]],
) -> TimelinePlan:
    """Compute all absolute boundaries and per-tenant spans.

    ``tenant_windows`` maps tenant -> (start_phase, end_phase) with inclusive
    1-based phase numbers, e.g. contract defaults A=(1,6) B=(2,3) C=(3,4)
    D=(4,5).  Boundary index k means ``t0 + k * phase_length``: a tenant with
    start_phase p joins at boundary p-1 and a tenant with end_phase p exits at
    boundary p (the start of phase p+1).
    """
    if phase_length_sec <= 0:
        raise ValueError(f"phase_length_sec must be positive, got {phase_length_sec!r}")
    if num_phases < 1:
        raise ValueError(f"num_phases must be >= 1, got {num_phases!r}")
    boundaries = [t0_monotonic + k * phase_length_sec for k in range(num_phases + 1)]
    spans: Dict[str, TenantSpan] = {}
    for tenant, window in tenant_windows.items():
        start_phase, end_phase = int(window[0]), int(window[1])
        if not 1 <= start_phase <= end_phase <= num_phases:
            raise ValueError(
                f"tenant {tenant!r} window ({start_phase}, {end_phase}) outside 1..{num_phases}"
            )
        spans[tenant] = TenantSpan(
            tenant=str(tenant),
            join_boundary=start_phase - 1,
            exit_boundary=end_phase,
            start_monotonic=boundaries[start_phase - 1],
            end_monotonic=boundaries[end_phase],
        )
    plan = TimelinePlan(
        t0_monotonic=t0_monotonic,
        phase_length_sec=float(phase_length_sec),
        num_phases=int(num_phases),
        boundaries=boundaries,
        spans=spans,
    )
    for phase in range(1, num_phases + 1):
        plan.phase_active[phase] = plan.expected_active_at_boundary(phase - 1)
    return plan


# Contract defaults (frozen; see module docstring and the contract document).
CONTRACT_TENANT_WINDOWS: Dict[str, Tuple[int, int]] = {
    "a": (1, 6), "b": (2, 3), "c": (3, 4), "d": (4, 5),
}
CONTRACT_PHASE_ACTIVE: Dict[int, Tuple[str, ...]] = {
    1: ("a",), 2: ("a", "b"), 3: ("a", "b", "c"),
    4: ("a", "c", "d"), 5: ("a", "d"), 6: ("a",),
}

# Non-overlapping disk regions: four contiguous quarters of the target device.
# Raw-device direct IO, no filesystem (run_dynamic_tenant_virtme.sh only fills
# the image once).  25% of any >=4GiB device is 64K-aligned, so tenants B/C
# keep direct-IO alignment.  Overridable per tenant in YAML.
DISK_LAYOUT_DEFAULT: Dict[str, Dict[str, str]] = {
    "a": {"offset": "0%", "size": "25%"},
    "b": {"offset": "25%", "size": "25%"},
    "c": {"offset": "50%", "size": "25%"},
    "d": {"offset": "75%", "size": "25%"},
}

# Workload precedents, see module docstring.  Do not change silently.
TENANT_WORKLOAD_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "a": {"bs": "4k", "rw": "randread", "iodepth": 1, "numjobs": 1},    # rig_iox d4 foreground
    "b": {"bs": "64k", "rw": "randread", "iodepth": 8, "numjobs": 1},   # M2 fig2
    "c": {"bs": "64k", "rw": "randread", "iodepth": 8, "numjobs": 1},   # M2 fig2 shape
    "d": {"bs": "4k", "rw": "randwrite", "iodepth": 64, "numjobs": 1},  # explore combo 08
}

# Tenant-spec keys consumed explicitly by _spawn_tenant (never passed through
# as generic --key=value fio options); every other scalar spec key is forwarded
# so the frozen contract's per-tenant "rate" (etc.) reaches fio.  "filename"
# is reserved (20261106, bm root-disk mode): fio treats MULTIPLE --filename
# occurrences as a multi-file STRIPE (verified fio-3.36: an absent first
# --filename gets CREATED at --size/nfiles and the I/O range is split across
# both files; the JSON "job options" echo keeps only the last string, which
# is NOT evidence of override) — so a spec filename must REPLACE the built-in
# --filename=<device_path>, never ride the passthrough.  Spec filenames are
# restricted to absolute FILE paths: a /dev node is rejected here (the
# per-tenant filename exists to target preallocated region files; omit it to
# keep the raw-device default of the platform).
_TENANT_SPEC_RESERVED_KEYS = frozenset(
    {"bs", "rw", "iodepth", "numjobs", "randseed", "disk_layout", "filename"})

# Per-tenant cgroup v2 io controller knobs accepted by
# io_ext.cgroup_tenant_controls (value formats are exactly
# setup_cgroup_io_controls': io_weight scalar/default+device mapping,
# io_max {rbps,wbps,riops,wiops}, io_latency {target: usec},
# io_bfq_weight int 1..1000 -> "default <w>" line).
_TENANT_IO_CONTROL_KEYS = ("io_weight", "io_max", "io_latency", "io_bfq_weight")

# rc values accepted when fio is SIGINT-ed exactly at a boundary deadline.
# 128 is fio's observed exit code for signal termination (verified in the
# DYN_smoke_ab vng smoke: fio prints "terminating on signal 2" and exits 128);
# 130/143 cover shells that translate SIGINT/SIGTERM.
BOUNDARY_SIGINT_RCS = {0, 128, 128 + 2, 128 + 15}
# fio 3.36 occasionally dies with the DEFAULT SIGINT disposition despite the
# installed handler (observed ~1/10 boundaries, rc=-2 i.e. killed-by-signal
# negative convention; final JSON not flushed but hist/bw logs are complete
# because they flush continuously).  Measured and archived in
# results/dynamic_tenant_virtme/contract/DYN_virtme_failed_attempt1_rc-2.tar.gz.
# fio returncode conventions: 0=ok, 130/128+2=SIGINT handled, -2=killed by
# SIGINT (Python negative convention).
BOUNDARY_SIGINT_RCS.add(-2)
# _exit_tenant: wait this long for a tenant fio to exit on its own before
# sending any signal — fio's --runtime carries a +2s margin on its window and
# the measured natural end of the always-on tenant on bm was boundary+9.6s.
# Signalling a fio whose last request is stuck in the ioext dispatch path is
# the wedge trigger (DIAG_f1_validation_sigint_hang.md); natural exit never
# sends signals.  Only a genuinely overdue tenant falls back to the
# interrupt + drain-stall/evacuate machinery.
NATURAL_EXIT_GRACE_SEC = 12.0

# NOTE on fio JSON after a boundary SIGINT: fio still writes its final JSON,
# but prefixes it with the line "fio: terminating on signal N" -- parse from
# the first '{'.  The per-window artifacts (clat_hist / bw logs) are complete
# because fio flushes them continuously.

# ---------------------------------------------------------------------------
# Phase-action transition model (unit-tested in tests/test_timeline_transitions.py)
# ---------------------------------------------------------------------------

TRANSITION_ACTION_TYPES = (
    "none", "switch_q", "switch_native", "swap_q", "attach_io_ext", "detach_io_ext",
)
IOEXT_SCX_SCHEDULER = "scx_io"

# q_wedf<->q_sp_edf hot replacement collapses the arbiter service share
# (hotsplit NOTES 4.3, unfixed) -- banned as a switch pair everywhere.
BANNED_Q_SWAP_PAIRS = {("q_wedf", "q_sp_edf"), ("q_sp_edf", "q_wedf")}

# bpftool + pinned maps (the loader creates policy_meta nameless and without
# BTF: it MUST be dumped by pin path, and bpftool emits raw little-endian
# byte lists -- field offsets from hotsplit NOTES 4.5 / guest_hotswitch.sh).
DEFAULT_BPFTOOL_PATH = (
    "/opt/ioext-io_ext/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
)
IOEXT_POLICY_META_PIN = "/sys/fs/bpf/ioext_policy_meta"
# Loader binaries (same prebuilt set the static-arms runner and the generator
# profiles use; never rebuilt by this scene).
DEFAULT_IOEXT_LOADER_DIR = "/opt/ioext-io_ext/userspace/io/build"
IOEXT_META_FIELDS: Dict[str, Tuple[int, int]] = {
    "queue_depth": (0, 8), "dispatches": (8, 8), "active_inserts": (16, 8),
    "owner_token": (24, 8), "arb_weight": (32, 4), "arb_prio": (36, 4),
    "flags": (40, 4),
}
IOEXT_TENANT_FIELDS: Dict[str, Tuple[int, int]] = {
    "deficit": (0, 8), "pending": (8, 8), "inflight": (16, 8),
    "completed_bytes": (24, 8), "completed_cost": (32, 8),
    "dispatches": (40, 8), "requeues": (48, 8), "requeued_bytes": (56, 8),
}


def _to_int(value: Any) -> int:
    """bpftool emits byte scalars as '0x..' strings; tolerate plain ints too."""
    return int(value, 0) if isinstance(value, str) else int(value)


def _parse_map_key_cgid(key: Any) -> Optional[int]:
    """bpftool map key -> cgroup id (dict with cgroup_id, or LE byte list)."""
    if isinstance(key, Mapping):
        try:
            return int(key.get("cgroup_id"))
        except (TypeError, ValueError):
            return None
    if isinstance(key, list) and len(key) >= 8:
        try:
            value = 0
            for i, b in enumerate(key[:8]):
                value |= _to_int(b) << (8 * i)
            return value
        except (TypeError, ValueError):
            return None
    return None


def _parse_map_value_field(value: Any, field: str,
                           fields: Mapping[str, Tuple[int, int]]) -> Optional[int]:
    """bpftool map value -> field value (BTF dict or raw byte list)."""
    if isinstance(value, Mapping):
        raw = value.get(field)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, list) and field in fields:
        off, size = fields[field]
        try:
            chunk = value[off:off + size]
            if len(chunk) != size:
                return None
            return int.from_bytes(bytes(_to_int(x) for x in chunk), "little")
        except (TypeError, ValueError):
            return None
    return None


def parse_ioext_map_dump(entries: Sequence[Any],
                         fields: Mapping[str, Tuple[int, int]]) -> Dict[int, Dict[str, int]]:
    """Parse a bpftool -j map dump into {cgid: {field: value}}.

    Tolerates BTF-decoded dict entries and raw byte-list entries (the io_ext
    loaders create their maps without BTF); entries whose key is not a
    cgroup id are skipped.
    """
    out: Dict[int, Dict[str, int]] = {}
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        cgid = _parse_map_key_cgid(entry.get("key"))
        if cgid is None:
            continue
        value = entry.get("value")
        row: Dict[str, int] = out.setdefault(cgid, {})
        for field_name in fields:
            parsed = _parse_map_value_field(value, field_name, fields)
            if parsed is not None:
                row[field_name] = parsed
    return out


def account_snapshot_delta(before: Mapping[str, Mapping[str, int]],
                           after: Mapping[str, Mapping[str, int]]) -> Dict[str, Any]:
    """Per-cgid numeric delta between two account snapshots.

    owner_token is reported as a change flag (it is re-written on every
    policy upsert; the upsert also zeroes meta counters, so meta counter
    deltas across a swap are NOT meaningful -- recorded per hotsplit NOTES
    4.2).
    """
    delta: Dict[str, Any] = {}
    for cgid in sorted(set(before) | set(after), key=str):
        b, a = before.get(cgid, {}), after.get(cgid, {})
        entry: Dict[str, Any] = {}
        for field_name in sorted(set(b) | set(a)):
            if field_name == "owner_token":
                if b.get("owner_token") != a.get("owner_token"):
                    entry["owner_token_changed"] = True
                    entry["owner_token"] = {"before": b.get("owner_token"),
                                            "after": a.get("owner_token")}
                continue
            bv, av = b.get(field_name), a.get(field_name)
            if bv is None or av is None:
                continue
            entry[field_name] = av - bv
        if entry:
            delta[str(cgid)] = entry
    return delta


def _argv_loader_name(argv: Sequence[str]) -> Optional[str]:
    """Loader argv -> bare q/policy name (basename minus the loader_ prefix)."""
    for arg in argv:
        base = os.path.basename(str(arg))
        if base.startswith("loader_") and "=" not in arg:
            return base[len("loader_"):]
    return None


def _profile_tenant_q(profile: Mapping[str, Any],
                      cgroup_paths: Mapping[str, str]) -> Dict[str, str]:
    """attach profile -> {tenant: q name}, by matching each cgroup policy's
    policy_cgroup_path against the scene cgroup paths."""
    out: Dict[str, str] = {}
    for entry in profile.get("cgroup_policies") or ():
        if not isinstance(entry, Mapping):
            continue
        cg = str(entry.get("policy_cgroup_path") or "")
        path = str(entry.get("path") or "")
        q = os.path.basename(path)
        if q.startswith("loader_"):
            q = q[len("loader_"):]
        for tenant, tenant_cg in cgroup_paths.items():
            if cg == tenant_cg:
                out[tenant] = q
    return out


def account_drained(snapshot: Mapping[str, Mapping[str, Mapping[str, int]]]) -> Tuple[bool, str]:
    """True when the account shows a fully drained io_ext state.

    Drain closure (hotsplit checklist): tenant_state pending==inflight==0 and
    policy_meta queue_depth==0 && active_inserts==0.  Maps may legitimately
    disappear once every loader exited (the loader unmaps its pins); that is
    reported as drained with an explicit note, not as an assertion failure.
    """
    meta = snapshot.get("policy_meta") or {}
    tenant = snapshot.get("tenant_state") or {}
    if not meta and not tenant:
        return True, "maps_absent_after_loader_exit"
    residual: List[str] = []
    for cgid, row in sorted(tenant.items(), key=str):
        for field_name in ("pending", "inflight"):
            if row.get(field_name, 0) != 0:
                residual.append(f"tenant_state[cgid={cgid}].{field_name}={row[field_name]}")
    for cgid, row in sorted(meta.items(), key=str):
        for field_name in ("queue_depth", "active_inserts"):
            if row.get(field_name, 0) != 0:
                residual.append(f"policy_meta[cgid={cgid}].{field_name}={row[field_name]}")
    if residual:
        return False, "; ".join(residual[:12])
    return True, "pending==inflight==0 && queue_depth==0 && active_inserts==0"


class IoExtTimelineScene(Scene):
    """Drive a multi-tenant fio trajectory on absolute monotonic deadlines."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))
        self.device_path = f"/dev/{self.target_device}"

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results/io_ext/timeline"
        self.scheduler = str(parameters.get("scheduler", "none"))
        # Native-only skeleton; the parameter exists so the future io_ext arms
        # can reuse the same scene with a policy scene layered on top.
        self.timeline_id = str(parameters.get("timeline_id") or scene_id)

        self.phase_length_sec = float(parameters.get("phase_length_sec", 60.0))
        if self.phase_length_sec <= 0:
            raise ValueError(f"phase_length_sec must be positive, got {self.phase_length_sec!r}")
        self.num_phases = int(parameters.get("num_phases", 6))
        self.fio_runtime_margin_sec = float(parameters.get("fio_runtime_margin_sec", 2.0))
        self.poll_interval_sec = float(parameters.get("poll_interval_sec", 0.1))

        # cleanup guardrails: forbid while tenants are active.  Keys must be
        # explicitly false or absent (gen_rq5_coloc / policy_transition lesson:
        # setdefault(False) alone proved insufficient).
        io_ext_cfg = parameters.get("io_ext") or {}
        if not isinstance(io_ext_cfg, Mapping):
            raise ValueError("parameters.io_ext must be a mapping when provided")
        for key in ("cleanup_stale", "cleanup_bpffs_pins", "cleanup_stale_when_no_specs"):
            if key in io_ext_cfg and _config_bool(io_ext_cfg[key], False):
                raise ValueError(
                    f"io_ext.{key} must be explicitly false for the timeline scene: "
                    "cleanup helpers are forbidden while tenants are active"
                )
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext_cfg)

        self.allow_switch_q_exec = bool(parameters.get("allow_switch_q_exec", False))
        # Transition exec gate (switch_native / swap_q / attach_io_ext /
        # detach_io_ext): transitions only execute when explicitly enabled.
        self.allow_transition_exec = bool(parameters.get("allow_transition_exec", False))
        # bpftool binary for io_ext account snapshots (env > param > default).
        self.bpftool_path = str(
            parameters.get("bpftool_path")
            or os.environ.get("BPFTOOL")
            or DEFAULT_BPFTOOL_PATH
        )
        # Loader binaries dir for swap_q (per-action "loader_dir" overrides).
        self.loader_dir = str(parameters.get("loader_dir") or DEFAULT_IOEXT_LOADER_DIR)
        # Initial io_ext mode: "native" (default) or "io_ext" (a resident
        # arbiter + per-tenant q policies were already attached by a global
        # loader scene before this scene started; declare them so swap_q can
        # validate against them).  io_ext_current_q optionally declares the
        # per-tenant q under the resident policies.
        self.io_ext_initial_state = str(parameters.get("io_ext_initial_state", "native"))
        if self.io_ext_initial_state not in ("native", "io_ext"):
            raise ValueError(
                f"parameters.io_ext_initial_state must be 'native' or 'io_ext', "
                f"got {self.io_ext_initial_state!r}"
            )
        raw_current_q = parameters.get("io_ext_current_q") or {}
        if not isinstance(raw_current_q, Mapping):
            raise ValueError("parameters.io_ext_current_q must be a mapping tenant -> q name")

        raw_actions = parameters.get("phase_actions") or {}
        if not isinstance(raw_actions, Mapping):
            raise ValueError("parameters.phase_actions must be a mapping of boundary -> action list")
        self.phase_actions: Dict[int, List[Dict[str, Any]]] = {}
        for raw_boundary, raw_list in raw_actions.items():
            boundary = int(raw_boundary)
            if boundary < 0 or boundary > self.num_phases:
                raise ValueError(f"phase_actions boundary {boundary} outside 0..{self.num_phases}")
            actions = raw_list if isinstance(raw_list, list) else [raw_list]
            normalized: List[Dict[str, Any]] = []
            for action in actions:
                if not isinstance(action, Mapping):
                    raise ValueError(f"phase_actions[{boundary}] entries must be mappings")
                action_type = str(action.get("type") or "none")
                if action_type not in TRANSITION_ACTION_TYPES:
                    raise ValueError(f"unsupported phase action type: {action_type!r}")
                normalized.append(dict(action))
            self.phase_actions[boundary] = normalized

        # Tenants: workload defaults above, overridable per tenant.  Tenant
        # windows default to the frozen contract; a shortened run may select a
        # subset of tenants/phases (e.g. A+B over T1..T2) via parameters.
        raw_tenants = parameters.get("tenants")
        if raw_tenants is None:
            selected = list(CONTRACT_TENANT_WINDOWS)
        elif isinstance(raw_tenants, Mapping):
            selected = [str(t) for t in raw_tenants]
        elif isinstance(raw_tenants, Sequence) and not isinstance(raw_tenants, str):
            selected = [str(t) for t in raw_tenants]
        else:
            raise ValueError("parameters.tenants must be a list or mapping of tenant names")
        unknown = [t for t in selected if t not in CONTRACT_TENANT_WINDOWS]
        if unknown:
            raise ValueError(f"unknown tenants: {unknown}; contract tenants are {sorted(CONTRACT_TENANT_WINDOWS)}")
        if not selected:
            raise ValueError("parameters.tenants must select at least one tenant")

        raw_windows = parameters.get("tenant_windows") or {}
        if not isinstance(raw_windows, Mapping):
            raise ValueError("parameters.tenant_windows must be a mapping tenant -> [start_phase, end_phase]")
        self.tenant_windows: Dict[str, Tuple[int, int]] = {}
        for tenant in selected:
            window = raw_windows.get(tenant) or CONTRACT_TENANT_WINDOWS[tenant]
            self.tenant_windows[tenant] = (int(window[0]), int(window[1]))

        self.tenants: Dict[str, Dict[str, Any]] = {}
        raw_tenant_cfg = raw_tenants if isinstance(raw_tenants, Mapping) else {}
        for tenant in selected:
            spec = dict(TENANT_WORKLOAD_DEFAULTS[tenant])
            spec.update(raw_tenant_cfg.get(tenant) or {})
            spec.setdefault("randseed", int(parameters.get("randseed_base", 20261103))
                            + sorted(CONTRACT_TENANT_WINDOWS).index(tenant) + 1)
            raw_filename = spec.get("filename")
            if raw_filename is not None:
                # Reserved key consumed by _spawn_tenant (single --filename).
                # Absolute FILE paths only: a /dev node here would be a raw
                # block device target, which the per-tenant filename exists
                # to avoid (bare-metal root-disk campaign; see _TENANT_SPEC_RESERVED_KEYS).
                filename = str(raw_filename)
                if not filename.startswith("/") or filename.startswith("/dev/"):
                    raise ValueError(
                        f"tenant {tenant}: spec filename must be an absolute FILE path "
                        f"(a /dev node is rejected; omit filename to use the device "
                        f"default): {filename!r}"
                    )
                spec["filename"] = filename
            layout = dict(DISK_LAYOUT_DEFAULT[tenant])
            raw_layout = spec.pop("disk_layout", None)
            if isinstance(raw_layout, Mapping):
                layout.update(raw_layout)
            spec["disk_layout"] = layout
            self.tenants[tenant] = spec

        # When the selected phases no longer cover the full contract (shortened
        # smoke), num_phases shrinks with the windows; the full-contract default
        # is asserted only for the complete tenant set.
        max_end = max(window[1] for window in self.tenant_windows.values())
        if self.num_phases < max_end:
            raise ValueError(
                f"num_phases={self.num_phases} cannot cover tenant window end {max_end}"
            )
        if set(self.tenant_windows) == set(CONTRACT_TENANT_WINDOWS):
            contract_windows = {t: CONTRACT_TENANT_WINDOWS[t] for t in sorted(self.tenant_windows)}
            if self.tenant_windows != contract_windows:
                raise ValueError(
                    f"full tenant set must use contract windows {contract_windows}, "
                    f"got {self.tenant_windows}"
                )

        self.cgroup_root = str(parameters.get("cgroup_root", "/sys/fs/cgroup")).rstrip("/")
        self.cgroup_paths: Dict[str, str] = {
            tenant: f"{self.cgroup_root}/rdyn_{self._seq_token()}_t{tenant}" for tenant in self.tenants
        }

        # Static cgroup v2 io controller arms (contract 7.10 C1-C4): per-tenant
        # controls applied when the tenant spawns (C1 io.max / C2 io.bfq.weight /
        # C3 io.latency) and root io.cost model+qos applied once in setup()
        # (C4; tenant_weights become per-tenant io.weight device lines at
        # spawn).  Value formats are exactly setup_cgroup_io_controls'.
        # Fail-visible at config time: unknown tenants, empty control dicts
        # and missing io_cost model/qos are rejected here; at spawn time a
        # missing io.* file or a read-back mismatch is fatal (a silently
        # skipped control would turn a C-arm run into an unthrottled run).
        raw_tenant_controls = io_ext_cfg.get("cgroup_tenant_controls") or {}
        if not isinstance(raw_tenant_controls, Mapping):
            raise ValueError(
                "io_ext.cgroup_tenant_controls must be a mapping tenant -> controls"
            )
        self.cgroup_tenant_controls: Dict[str, Dict[str, Any]] = {}
        for raw_tenant, raw_controls in raw_tenant_controls.items():
            if raw_tenant not in self.tenants:
                raise ValueError(
                    f"io_ext.cgroup_tenant_controls: unknown tenant {raw_tenant!r}; "
                    f"selected tenants are {sorted(self.tenants)}"
                )
            if not isinstance(raw_controls, Mapping):
                raise ValueError(
                    f"io_ext.cgroup_tenant_controls[{raw_tenant!r}] must be a mapping "
                    f"with keys from {list(_TENANT_IO_CONTROL_KEYS)}"
                )
            for key in raw_controls:
                if key not in _TENANT_IO_CONTROL_KEYS:
                    raise ValueError(
                        f"io_ext.cgroup_tenant_controls[{raw_tenant!r}]: unsupported key "
                        f"{key!r} (supported: {list(_TENANT_IO_CONTROL_KEYS)})"
                    )
            has_value = False
            for key, value in raw_controls.items():
                if value is None:
                    continue
                if isinstance(value, (str, Mapping)) and not value:
                    raise ValueError(
                        f"io_ext.cgroup_tenant_controls[{raw_tenant!r}].{key} "
                        f"must not be empty"
                    )
                has_value = True
            if not has_value:
                raise ValueError(
                    f"io_ext.cgroup_tenant_controls[{raw_tenant!r}] must set at "
                    f"least one control ({'/'.join(_TENANT_IO_CONTROL_KEYS)})"
                )
            self.cgroup_tenant_controls[str(raw_tenant)] = {
                str(key): value for key, value in raw_controls.items() if value is not None
            }

        self.io_cost_model: Optional[Mapping[str, Any]] = None
        self.io_cost_qos: Optional[Mapping[str, Any]] = None
        self.io_cost_tenant_weights: Dict[str, int] = {}
        raw_io_cost = io_ext_cfg.get("io_cost")
        if raw_io_cost is not None:
            if not isinstance(raw_io_cost, Mapping):
                raise ValueError(
                    "io_ext.io_cost must be a mapping with model/qos/tenant_weights"
                )
            self.io_cost_model = raw_io_cost.get("model")
            self.io_cost_qos = raw_io_cost.get("qos")
            if not isinstance(self.io_cost_model, Mapping) or not isinstance(self.io_cost_qos, Mapping):
                raise ValueError(
                    "io_ext.io_cost requires both 'model' and 'qos' mappings "
                    "(setup_iocost_at_root format; the root io.cost is written "
                    "once in setup())"
                )
            raw_weights = raw_io_cost.get("tenant_weights") or {}
            if not isinstance(raw_weights, Mapping):
                raise ValueError(
                    "io_ext.io_cost.tenant_weights must be a mapping tenant -> weight"
                )
            unknown_weights = [t for t in raw_weights if t not in self.tenants]
            if unknown_weights:
                raise ValueError(
                    f"io_ext.io_cost.tenant_weights: unknown tenants "
                    f"{unknown_weights}; selected tenants are {sorted(self.tenants)}"
                )
            self.io_cost_tenant_weights = {
                str(t): int(w) for t, w in raw_weights.items()
            }

        self.drop_caches_before_run = bool(parameters.get("drop_caches_before_run", True))

        spawn_margin = float(parameters.get("duration_margin_sec", 30.0))
        default_duration = self.num_phases * self.phase_length_sec + spawn_margin
        self.duration = int(parameters.get("duration", default_duration))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.t0_monotonic: Optional[float] = None
        self.plan: Optional[TimelinePlan] = None
        self._tenant_procs: Dict[str, subprocess.Popen] = {}
        self._tenant_records: Dict[str, Dict[str, Any]] = {}
        self._boundary_records: List[Dict[str, Any]] = []
        self._artifact_paths: List[str] = []
        self._errors: List[str] = []
        # Root io.cost model+qos applied ONCE in setup() (C4 arm evidence;
        # surfaced through prepare()/stat() as collected_data["io_cost_applied"]).
        self.io_cost_applied: Optional[Dict[str, Any]] = None
        # runtime transition state (mode machine mirrors the static validation)
        self._ioext_mode: str = self.io_ext_initial_state
        self._ioext_current_q: Dict[str, str] = dict(raw_current_q)
        self._ioext_loader_procs: List[subprocess.Popen] = []       # all transition loaders
        self._ioext_tenant_loaders: Dict[str, subprocess.Popen] = {}  # tenant -> current q loader
        self._ioext_arb_loaders: List[subprocess.Popen] = []
        # Static transition validation: walk the pre-declared action sequence
        # through the native/io_ext state machine BEFORE the run (illegal
        # pairs are rejected at config time, not mid-run).  Needs tenants +
        # cgroup_paths, hence placed after they are built above.
        self._validate_transition_sequence(dict(raw_current_q))

    # -- naming helpers ------------------------------------------------------

    def _seq_token(self) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in self.timeline_id)

    def _tenant_output_dir(self, tenant: str) -> str:
        return os.path.join(self.output_dir, f"tenant_{tenant}")

    # -- static transition validation -----------------------------------------

    def _validate_transition_sequence(self, current_q: Dict[str, str]) -> None:
        """Walk the pre-declared actions through the native/io_ext mode machine.

        Raises ValueError on any illegal pair (config-time rejection):
        - switch_native only from the native mode (an elevator switch while
          scx_io owns the device would silently bypass every attached policy);
        - swap_q only while io_ext is active;
        - attach_io_ext only from native; detach_io_ext only from io_ext;
        - the q_wedf<->q_sp_edf swap pair is banned everywhere (hotsplit
          NOTES 4.3, unfixed);
        - attach profiles must keep cleanup helpers off (forbidden while
          tenants are active) and must target scx_io.
        """
        mode = self.io_ext_initial_state
        current_q = dict(current_q)
        for boundary in sorted(self.phase_actions):
            for action in self.phase_actions[boundary]:
                action_type = str(action.get("type") or "none")
                if action_type in ("none", "switch_q"):
                    continue
                if action_type == "switch_native":
                    if mode != "native":
                        raise ValueError(
                            f"phase_actions[{boundary}]: switch_native requires native mode "
                            f"(current mode: {mode}); detach_io_ext first"
                        )
                    scheduler = str(action.get("scheduler") or "").strip()
                    if not scheduler:
                        raise ValueError(f"phase_actions[{boundary}]: switch_native requires 'scheduler'")
                    if scheduler == IOEXT_SCX_SCHEDULER:
                        raise ValueError(
                            f"phase_actions[{boundary}]: switch_native must not target "
                            f"{IOEXT_SCX_SCHEDULER} (use attach_io_ext)"
                        )
                    if is_process_scheduler_alias(scheduler):
                        raise ValueError(
                            f"phase_actions[{boundary}]: switch_native scheduler "
                            f"{scheduler!r} is a process-scheduler alias, not an elevator"
                        )
                    continue
                if action_type == "swap_q":
                    if mode != "io_ext":
                        raise ValueError(
                            f"phase_actions[{boundary}]: swap_q requires io_ext mode "
                            f"(attach_io_ext first or declare io_ext_initial_state)"
                        )
                    tenants = [str(t) for t in (action.get("tenants") or [])]
                    if not tenants:
                        raise ValueError(f"phase_actions[{boundary}]: swap_q requires non-empty 'tenants'")
                    unknown = [t for t in tenants if t not in self.tenants]
                    if unknown:
                        raise ValueError(f"phase_actions[{boundary}]: swap_q unknown tenants {unknown}")
                    q = str(action.get("q") or "").strip()
                    if not q:
                        raise ValueError(f"phase_actions[{boundary}]: swap_q requires 'q'")
                    for tenant in tenants:
                        from_q = str(action.get("from_q") or current_q.get(tenant) or "").strip()
                        if from_q and (from_q, q) in BANNED_Q_SWAP_PAIRS:
                            raise ValueError(
                                f"phase_actions[{boundary}]: swap_q pair {from_q}->{q} banned "
                                f"(q_wedf<->q_sp_edf service-share collapse, hotsplit NOTES 4.3)"
                            )
                    continue
                if action_type == "attach_io_ext":
                    if mode != "native":
                        raise ValueError(
                            f"phase_actions[{boundary}]: attach_io_ext requires native mode "
                            f"(current mode: {mode}); detach_io_ext first"
                        )
                    profile = self._validated_attach_profile(action, boundary)
                    scheduler = str(action.get("scheduler") or IOEXT_SCX_SCHEDULER)
                    if scheduler != IOEXT_SCX_SCHEDULER:
                        raise ValueError(
                            f"phase_actions[{boundary}]: attach_io_ext scheduler must be "
                            f"{IOEXT_SCX_SCHEDULER!r}, got {scheduler!r}"
                        )
                    mode = "io_ext"
                    for tenant, q in _profile_tenant_q(profile, self.cgroup_paths).items():
                        current_q[tenant] = q
                    continue
                if action_type == "detach_io_ext":
                    if mode != "io_ext":
                        raise ValueError(
                            f"phase_actions[{boundary}]: detach_io_ext requires io_ext mode"
                        )
                    target = str(action.get("scheduler") or self.scheduler or "none")
                    if target == IOEXT_SCX_SCHEDULER:
                        raise ValueError(
                            f"phase_actions[{boundary}]: detach_io_ext scheduler must be a "
                            f"native elevator, got {target!r}"
                        )
                    mode = "native"
                    current_q = {}
                    continue
        if mode != "native" and self.num_phases > 0:
            # ending the trajectory still inside io_ext is legal only when a
            # final detach is not required by the schedule; the runner must
            # stop the resident chain.  Recorded, not fatal.
            logger.info(
                "timeline %s: action sequence ends in io_ext mode; the final "
                "detach is left to the resident loader scene / runner", self.scene_id)

    def _validated_attach_profile(self, action: Mapping[str, Any], boundary: int) -> Dict[str, Any]:
        profile = action.get("io_ext")
        if not isinstance(profile, Mapping):
            raise ValueError(f"phase_actions[{boundary}]: attach_io_ext requires an 'io_ext' mapping")
        for key in ("cleanup_stale", "cleanup_bpffs_pins", "cleanup_stale_when_no_specs"):
            if key in profile and _config_bool(profile[key], False):
                raise ValueError(
                    f"phase_actions[{boundary}]: attach_io_ext io_ext.{key} must be explicitly "
                    f"false -- cleanup helpers are forbidden while tenants are active"
                )
        arbiters = profile.get("arbiters") or []
        cgroup_policies = profile.get("cgroup_policies") or []
        if not arbiters and not cgroup_policies:
            raise ValueError(
                f"phase_actions[{boundary}]: attach_io_ext io_ext profile needs "
                f"'arbiters' and/or 'cgroup_policies'"
            )
        return dict(profile)

    # -- Scene lifecycle -----------------------------------------------------

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if not os.path.exists(self.device_path):
            raise FileNotFoundError(f"block device does not exist: {self.device_path}")
        for tenant in sorted(self.tenants):
            setup_cgroup_v2(self.cgroup_paths[tenant], ("io",))
        if self.io_cost_model is not None or self.io_cost_qos is not None:
            # Root io.cost (C4 arm): written ONCE here, i.e. before any tenant
            # spawns (the per-tenant io.weight device lines follow at each
            # spawn via _apply_tenant_io_controls).  Failures propagate like
            # every other setup() step (setup_iocost_at_root raises
            # RuntimeError on a non-zero rc); the applied lines are kept as
            # evidence for prepare()/stat().
            self.io_cost_applied = setup_iocost_at_root(
                self.device_path,
                self.io_cost_model,
                self.io_cost_qos,
                root_cgroup_path=self.cgroup_root,
            )

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.t0_monotonic = None
        self.plan = None
        self._tenant_procs = {}
        self._tenant_records = {}
        self._boundary_records = []
        self._artifact_paths = []
        self._errors = []
        self.collected_data = {
            "scene_id": self.scene_id,
            "scene_name": self.scene_name,
            "timeline_id": self.timeline_id,
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "phase_length_sec": self.phase_length_sec,
            "num_phases": self.num_phases,
            "tenants": sorted(self.tenants),
            "tenant_windows": {t: list(w) for t, w in self.tenant_windows.items()},
            "cgroup_paths": dict(self.cgroup_paths),
            # Root io.cost applied in setup() (None when the C4 arm is not
            # configured); kept next to the cgroup paths it constrains.
            "io_cost_applied": self.io_cost_applied,
            "phase_active_contract": {
                str(phase): list(tenants)
                for phase, tenants in CONTRACT_PHASE_ACTIVE.items()
                if phase <= self.num_phases and all(t in self.tenants for t in tenants)
            },
            "service_preference": "A > C > B = D (recorded for later tuning; NOT implemented in this skeleton)",
            "boundaries": [],
            "events": [],
        }

    def _run(self) -> None:
        self.start_time = time.time()
        self.t0_monotonic = time.monotonic()
        self.plan = compute_timeline(
            self.t0_monotonic, self.phase_length_sec, self.num_phases, self.tenant_windows
        )
        self._record_event("run_start", boundary=0, details={
            "t0_monotonic": self.t0_monotonic,
            "t0_epoch": self.start_time,
            "phase_length_sec": self.phase_length_sec,
            "num_phases": self.num_phases,
            "boundaries_planned": self.plan.boundaries,
        })
        try:
            if self.drop_caches_before_run:
                self._drop_caches()
            set_scheduler(self.target_device, self.scheduler)

            num_phases = self.plan.num_phases
            for boundary in range(0, num_phases + 1):
                if self.should_stop():
                    break
                if boundary > 0:
                    # Absolute-deadline sleep: recompute remaining time from
                    # the pre-computed boundary every round, so drift cannot
                    # accumulate across phases.
                    self._sleep_until(self.plan.boundary_deadline(boundary))
                if self.should_stop():
                    break
                self._run_boundary(boundary)

            self._wait_tenants_settled()
            if self._errors and not self.should_stop():
                # Fail-visible: the timeline itself broke (missing process,
                # unexpected survivor, non-interrupt rc).  All collected
                # artifacts are already on disk; surface the failure so the
                # controller/validator cannot mistake it for a clean run.
                raise RuntimeError(f"timeline scene errors: {self._errors}")
        except Exception as exc:
            self._errors.append(f"run_error: {exc!r}")
            self._record_event("run_error", boundary=-1, details={"error": repr(exc)})
            raise
        finally:
            self.end_time = time.time()

    def _run_boundary(self, boundary: int) -> None:
        planned = self.plan.boundary_deadline(boundary)
        actual = time.monotonic()
        drift_sec = actual - planned
        exits = self.plan.exits_at(boundary)
        joins = self.plan.joins_at(boundary)
        record: Dict[str, Any] = {
            "boundary": boundary,
            "phase_ending": boundary,
            "phase_starting": boundary + 1 if boundary < self.plan.num_phases else None,
            "planned_monotonic": planned,
            "actual_monotonic": actual,
            "drift_sec": drift_sec,
            "exited_tenants": [],
            "spawned_tenants": [],
            "actions": [],
        }
        self._record_event("boundary_start", boundary=boundary, details={
            "drift_sec": drift_sec, "exits": exits, "joins": joins,
        })

        # Ordering rule: boundary 0 declared actions run BEFORE the first
        # spawn.  The opening attach_io_ext switches the elevator and starts
        # the resident loader chain; if it ran after the spawn, the opening
        # fio tenants would push several seconds of unprotected I/O before
        # the policy covers them (kvmhost DYN_valid_dyn_s101 20261104 lesson:
        # the reverse design also races the global policy scene against
        # cgroup creation).  Every other boundary keeps the original
        # exits -> joins -> actions order (transitions there must run
        # against live tenant fio processes, exits first).
        actions_before_spawn = bool(self.phase_actions.get(boundary)) and boundary == 0
        if actions_before_spawn:
            record["actions"] = self._run_phase_actions(boundary)

        for tenant in exits:
            record["exited_tenants"].append(self._exit_tenant(tenant, boundary))
        for tenant in joins:
            record["spawned_tenants"].append(self._spawn_tenant(tenant, boundary))

        expected_after = self.plan.expected_active_at_boundary(boundary)
        actual_after = tuple(sorted(t for t, proc in self._tenant_procs.items() if proc.poll() is None))
        record["expected_active_after"] = list(expected_after)
        record["actual_active_after"] = list(actual_after)
        if set(actual_after) - set(expected_after):
            # Unexpected survivor: fail visible.
            self._errors.append(
                f"boundary {boundary}: unexpected active tenants {sorted(set(actual_after) - set(expected_after))}"
            )
        if not actions_before_spawn:
            record["actions"] = self._run_phase_actions(boundary)

        self._boundary_records.append(record)
        self.collected_data["boundaries"] = self._boundary_records
        self._record_event("boundary_end", boundary=boundary, details={
            "drift_sec": drift_sec,
            "expected_active_after": record["expected_active_after"],
            "actual_active_after": record["actual_active_after"],
        })

    # -- tenant spawn/exit ----------------------------------------------------

    def _spawn_tenant(self, tenant: str, boundary: int) -> Dict[str, Any]:
        spec = self.tenants[tenant]
        span = self.plan.spans[tenant]
        out_dir = self._tenant_output_dir(tenant)
        os.makedirs(out_dir, exist_ok=True)
        seq = self._seq_token()
        name = f"rdyn_{seq}_t{tenant}"
        fio_json = os.path.join(out_dir, f"{name}_fio.json")
        hist_prefix = os.path.join(out_dir, name)
        bw_prefix = os.path.join(out_dir, f"{name}_bw")

        # Static C-arm cgroup io.* controls go on BEFORE the tenant's fio
        # command is built and wrapped: the controls must already be in
        # effect when the tenant starts issuing I/O, and a control that did
        # not land must fail the spawn here (strict read-back inside).
        io_controls_applied = self._apply_tenant_io_controls(tenant)
        if io_controls_applied:
            self._record_event("tenant_io_controls_applied", boundary=boundary, details={
                "tenant": tenant,
                "cgroup": self.cgroup_paths[tenant],
                "applied": io_controls_applied,
            })

        # Single continuous fio job for the whole tenant span; runtime carries
        # a small margin so fio never exits before the planned boundary, and
        # the scene SIGINTs it exactly at the boundary deadline.
        runtime_sec = span.span_sec + self.fio_runtime_margin_sec
        # fio target: the tenant's region FILE when the spec carries one
        # (reserved key, replaces the device path -- duplicate --filename is a
        # multi-file STRIPE in fio, never an override), else the device path.
        fio_target = str(spec.get("filename") or self.device_path)
        argv = [
            "fio",
            f"--name={name}",
            f"--filename={fio_target}",
            "--ioengine=io_uring",
            "--direct=1",
            f"--bs={spec['bs']}",
            f"--rw={spec['rw']}",
            f"--iodepth={spec['iodepth']}",
            f"--numjobs={spec['numjobs']}",
            f"--offset={spec['disk_layout']['offset']}",
            f"--size={spec['disk_layout']['size']}",
            f"--runtime={runtime_sec:.3f}",
            "--time_based=1",
            f"--randseed={int(spec['randseed'])}",
            "--randrepeat=1",
            # Optional per-tenant passthrough options from the tenant spec
            # (e.g. "rate": "1037M" for the frozen contract, "rw" tweaks).
            # Only simple scalar keys not explicitly built above are allowed;
            # values are stringified, so YAML numbers work too.
            *[
                f"--{key}={spec[key]}"
                for key in sorted(spec)
                if key not in _TENANT_SPEC_RESERVED_KEYS and isinstance(spec[key], (str, int, float, bool))
            ],
            "--output-format=json+",
            f"--output={fio_json}",
            f"--write_hist_log={hist_prefix}",
            "--log_hist_msec=1000",
            "--log_hist_coarseness=0",
            "--log_unix_epoch=1",
            f"--write_bw_log={bw_prefix}",
            "--log_avg_msec=1000",
            "--clat_percentiles=1",
            "--lat_percentiles=0",
            "--percentile_list=99:99.9",
        ]
        cmd = wrap_command_in_cgroup(argv, self.cgroup_paths[tenant])
        stderr_path = os.path.join(out_dir, f"{name}_stderr.log")

        spawn_record: Dict[str, Any] = {
            "tenant": tenant,
            "boundary": boundary,
            "planned_monotonic": span.start_monotonic,
            "actual_monotonic": time.monotonic(),
            "cgroup": self.cgroup_paths[tenant],
            "fio_target": fio_target,
            "fio_json": fio_json,
            "hist_log_prefix": hist_prefix,
            "bw_log_prefix": bw_prefix,
            "stderr_log": stderr_path,
            "argv": argv,
            "fio_runtime_sec": runtime_sec,
            "io_controls_applied": io_controls_applied,
        }
        spawn_record["spawn_drift_sec"] = spawn_record["actual_monotonic"] - span.start_monotonic
        self._record_event("tenant_spawn_begin", boundary=boundary, details={
            "tenant": tenant, "cgroup": self.cgroup_paths[tenant],
        })
        stderr_f = open(stderr_path, "w", encoding="utf-8")
        try:
            proc = popen_in_own_session(cmd, stdout=subprocess.DEVNULL, stderr=stderr_f, text=True)
        finally:
            stderr_f.close()
        self._tenant_procs[tenant] = proc
        self._tenant_records[tenant] = {
            "tenant": tenant,
            "spawn_record": spawn_record,
            "exit_record": None,
            "phases_active": [
                phase for phase in range(1, self.plan.num_phases + 1)
                if span.join_boundary < phase <= span.exit_boundary
            ],
        }
        self._record_event("tenant_spawn_end", boundary=boundary, details={
            "tenant": tenant, "pid": proc.pid, "spawn_drift_sec": spawn_record["spawn_drift_sec"],
        })
        return spawn_record

    @staticmethod
    def _wait_fio_json_complete(fio_json_path: str, timeout_sec: float) -> bool:
        """Wait for fio to flush its final JSON block after a drain stall.

        The file may carry the ``fio: terminating on signal ...`` preamble
        before the JSON body; consider it final once the tail parses as a
        fio JSON document.  Returns False if the deadline passes (caller
        keeps the scene error)."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                with open(fio_json_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
                body = raw[raw.find("{"):]
                if body.startswith("{"):
                    json.loads(body)
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        return False

    def _exit_tenant(self, tenant: str, boundary: int) -> Dict[str, Any]:
        proc = self._tenant_procs.get(tenant)
        span = self.plan.spans[tenant]
        exit_record: Dict[str, Any] = {
            "tenant": tenant,
            "boundary": boundary,
            "planned_monotonic": span.end_monotonic,
            "actual_monotonic": time.monotonic(),
        }
        exit_record["exit_drift_sec"] = exit_record["actual_monotonic"] - span.end_monotonic
        self._record_event("tenant_exit_begin", boundary=boundary, details={"tenant": tenant})
        if proc is None:
            exit_record["error"] = "tenant process missing at planned exit boundary"
            self._errors.append(f"tenant {tenant}: process missing at boundary {boundary}")
        elif proc.poll() is not None:
            # fio finished on its own before the boundary (margin should make
            # this rare); keep the rc and flag it.
            exit_record["exited_via"] = "natural"
            exit_record["returncode"] = proc.returncode
            if proc.returncode != 0:
                self._errors.append(
                    f"tenant {tenant}: fio exited early with rc={proc.returncode}"
                )
        else:
            # bm 20261106 (DIAG_f1_validation_sigint_hang.md + second whole-
            # box freeze 23:0x supervised, journal evidence): fio at these
            # boundaries self-exits within ~2-10s (runtime=window+2s margin;
            # measured natural end of tenant a: boundary+9.6s).  Signalling a
            # fio with IO in flight can wedge the ioext dispatch deeper than
            # any userspace recovery (SIGINT delivery itself froze the whole
            # box once) — so WAIT for the natural exit first and only signal
            # if it is genuinely overdue.  Natural exit also always writes a
            # complete stats JSON, eliminating the drain-stall class.
            grace_t0 = time.monotonic()
            natural_deadline = time.monotonic() + NATURAL_EXIT_GRACE_SEC
            while proc.poll() is None and time.monotonic() < natural_deadline:
                time.sleep(0.1)
            if proc.poll() is not None:
                exit_record["exited_via"] = "natural_grace"
                exit_record["natural_grace_sec"] = round(time.monotonic() - grace_t0, 3)
                exit_record["returncode"] = proc.returncode
                if proc.returncode != 0:
                    self._record_event("tenant_natural_grace_rc", boundary=boundary,
                                       details={"tenant": tenant,
                                                "returncode": proc.returncode})
            else:
                interrupt_process(proc, timeout_sec=10.0)
                exit_record["exited_via"] = "boundary_sigint"
                exit_record["natural_grace_sec"] = round(time.monotonic() - grace_t0, 3)
                exit_record["returncode"] = proc.returncode
                if proc.returncode not in BOUNDARY_SIGINT_RCS:
                    # bm 20261106 (DIAG_f1_validation_sigint_hang.md): the
                    # last in-flight request can wedge in the ioext dispatch
                    # path and is only released when the policy link
                    # detaches.  First observed case: released ~80ms after
                    # the SIGTERM'd wrapper died; the s101 rerun released
                    # only with the scene-cleanup loader stop (~2min later),
                    # so a plain drain wait both misses (30s) and risks the
                    # wedge going machine-wide when the run is ever
                    # unsupervised (f1_s102 19:21:05 journal evidence:
                    # whole-box freeze on tenant a's boundary SIGINT).
                    # Therefore: brief drain (10s) -> on still-stuck, stop
                    # the loader chain right here (same order as
                    # detach_io_ext) -> wait up to 50s more for fio's final
                    # JSON.  If it lands, downgrade to a warning; an empty
                    # loader list is a no-op.
                    rec_drain = self._tenant_records.get(tenant) or {}
                    fio_json = rec_drain.get("spawn_record", {}).get("fio_json")
                    drain_t0 = time.monotonic()
                    drained = self._wait_fio_json_complete(fio_json, 10.0) if fio_json else False
                    loaders_evacuated = False
                    if not drained and self._ioext_loader_procs:
                        stop_loader_chain(list(reversed(self._ioext_loader_procs)))
                        loaders_evacuated = True
                        self._ioext_tenant_loaders = {}
                        self._ioext_arb_loaders = []
                        self._ioext_loader_procs = []
                        self._record_event("drain_stall_loader_evacuate", boundary=boundary,
                                           details={"tenant": tenant})
                    if not drained:
                        # the detach releases the wedged request; give fio the
                        # final window to flush its stats block
                        drained = self._wait_fio_json_complete(fio_json, 50.0) if fio_json else False
                    exit_record["drain_stall_recovered"] = drained
                    exit_record["drain_wait_sec"] = round(time.monotonic() - drain_t0, 3)
                    exit_record["drain_loaders_evacuated"] = loaders_evacuated
                    if drained:
                        exit_record["exited_via"] = "boundary_sigint_drain_stall"
                        self._record_event("boundary_exit_drain_stall", boundary=boundary,
                                           details={"tenant": tenant,
                                                    "returncode": proc.returncode,
                                                    "fio_json": fio_json,
                                                    "loaders_evacuated": loaders_evacuated,
                                                    "drain_wait_sec": exit_record["drain_wait_sec"]})
                    else:
                        self._errors.append(
                            f"tenant {tenant}: unexpected rc={proc.returncode} after boundary SIGINT"
                        )
        exit_record["returncode"] = proc.returncode if proc is not None else None
        self._tenant_procs.pop(tenant, None)
        rec = self._tenant_records.get(tenant)
        if rec is not None:
            rec["exit_record"] = exit_record
        self._record_event("tenant_exit_end", boundary=boundary, details={
            "tenant": tenant,
            "returncode": exit_record.get("returncode"),
            "exit_drift_sec": exit_record["exit_drift_sec"],
        })
        return exit_record

    def _wait_tenants_settled(self) -> None:
        """Reap any tenant still alive after the final boundary (fail-visible)."""
        for tenant, proc in list(self._tenant_procs.items()):
            if proc.poll() is None:
                self._errors.append(f"tenant {tenant}: still running after final boundary; interrupting")
                interrupt_process(proc, timeout_sec=10.0)
            self._tenant_procs.pop(tenant, None)

    # -- tenant cgroup io controls (static C arms) -----------------------------

    def _apply_tenant_io_controls(self, tenant: str) -> Dict[str, Any]:
        """Apply this tenant's cgroup v2 io controls at spawn time, then verify.

        Merges ``io_ext.cgroup_tenant_controls[tenant]`` with
        ``io_ext.io_cost.tenant_weights[tenant]`` into ONE
        setup_cgroup_io_controls call: the iocost weight becomes an io.weight
        ``<MAJ:MIN> <weight>`` device line in the same write (io.weight is
        never written twice).  Returns {} when the arm configures no control
        for this tenant (N/X/F arms), otherwise the applied dict extended
        with the read-back verification evidence.  Any write failure or
        read-back mismatch raises (fail-visible): a silently skipped control
        would turn a C-arm run into an unthrottled run.
        """
        controls = self.cgroup_tenant_controls.get(tenant) or {}
        iocost_weight = self.io_cost_tenant_weights.get(tenant)
        if not controls and iocost_weight is None:
            return {}
        merged: Dict[str, Any] = dict(controls)
        if iocost_weight is not None:
            raw_weight = merged.get("io_weight")
            if raw_weight is None:
                merged["io_weight"] = {"device": int(iocost_weight)}
            elif isinstance(raw_weight, Mapping):
                weight_map = dict(raw_weight)
                weight_map["device"] = int(iocost_weight)
                merged["io_weight"] = weight_map
            else:
                # Scalar io_weight keeps its "default <w>" semantics and gains
                # the per-device override line inside the same single write.
                merged["io_weight"] = {"default": raw_weight, "device": int(iocost_weight)}
        applied = setup_cgroup_io_controls(
            cgroup_path=self.cgroup_paths[tenant],
            device_path=self.device_path,
            io_weight=merged.get("io_weight"),
            io_max=merged.get("io_max"),
            io_latency=merged.get("io_latency"),
            io_bfq_weight=merged.get("io_bfq_weight"),
        )
        applied["readback"] = self._verify_tenant_io_controls(tenant, applied)
        return applied

    def _verify_tenant_io_controls(self, tenant: str,
                                   applied: Mapping[str, Any]) -> Dict[str, Any]:
        """Strict read-back of the io.* files this spawn wrote (one sudo pass).

        common.py writes io.latency (and unlimited io.max lines) behind an
        ``if [ -f ]`` guard, i.e. a cgroup/kernel without the file silently
        skips the control.  This layer turns that into a fatal error and
        string-verifies the kernel's read-back against every expected line
        (whitespace-normalized containment, so the kernel's own read-back
        format -- e.g. io.latency's ``<MAJ:MIN> target=<usec>`` -- is the
        comparison source): a missing/unreadable file or a mismatching line
        raises RuntimeError instead of silently unthrottling the tenant.
        """
        expected: List[Tuple[str, List[str]]] = []
        if applied.get("io_weight_lines"):
            expected.append(("io.weight", [str(line) for line in applied["io_weight_lines"]]))
        if applied.get("io_max_line"):
            expected.append(("io.max", [str(applied["io_max_line"])]))
        if applied.get("io_latency_line"):
            expected.append(("io.latency", [str(applied["io_latency_line"])]))
        if applied.get("io_bfq_weight_line"):
            expected.append(("io.bfq.weight", [str(applied["io_bfq_weight_line"])]))
        if not expected:
            # No knob produced a line (nothing written): nothing to verify.
            return {"verified": []}
        cg_path = self.cgroup_paths[tenant]
        script_lines = ["set -e", f"test -d {shlex.quote(cg_path)}"]
        for name, _ in expected:
            path = shlex.quote(os.path.join(cg_path, name))
            # Missing io.* file is FATAL here (see docstring): test -f first
            # instead of common.py's tolerant `if [ -f ]` write guard.
            script_lines.append(f"test -f {path}")
            script_lines.append(f"printf '%s\\n' '##{name}'; cat {path}")
        result = run_sudo_shell("\n".join(script_lines), check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"tenant {tenant}: cgroup io control read-back failed on {cg_path} "
                f"(rc={result.returncode}; a required io.* file is missing or "
                f"unreadable, expected files {[name for name, _ in expected]}): "
                f"{(result.stderr or result.stdout).strip()[:400] or 'no command output'}"
            )
        names = {name for name, _ in expected}
        sections: Dict[str, List[str]] = {}
        current: Optional[str] = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("##") and line[2:] in names:
                current = line[2:]
                sections.setdefault(current, [])
            elif current is not None:
                sections[current].append(line)
        verified: List[Dict[str, Any]] = []
        for name, lines in expected:
            readback = " ".join(" ".join(sections.get(name, [])).split())
            for line in lines:
                if " ".join(line.split()) not in readback:
                    raise RuntimeError(
                        f"tenant {tenant}: cgroup io control read-back mismatch on "
                        f"{cg_path}/{name}: expected line {line!r} not in "
                        f"read-back {readback!r}"
                    )
            verified.append({"file": name, "readback": readback,
                             "expected_lines": list(lines)})
        return {"verified": verified}

    # -- phase action hooks ----------------------------------------------------

    def _run_phase_actions(self, boundary: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for action in self.phase_actions.get(boundary, []):
            action_type = str(action.get("type") or "none")
            if action_type == "none":
                results.append({"type": "none", "boundary": boundary, "executed": True})
            elif action_type == "switch_q":
                results.append(self._run_switch_q_action(action, boundary))
            elif action_type == "switch_native":
                results.append(self._run_transition("switch_native", action, boundary,
                                                    self._exec_switch_native))
            elif action_type == "swap_q":
                results.append(self._run_transition("swap_q", action, boundary,
                                                    self._exec_swap_q))
            elif action_type == "attach_io_ext":
                results.append(self._run_transition("attach_io_ext", action, boundary,
                                                    self._exec_attach_io_ext))
            elif action_type == "detach_io_ext":
                results.append(self._run_transition("detach_io_ext", action, boundary,
                                                    self._exec_detach_io_ext))
            else:  # guarded in __init__; kept for defense in depth
                raise ValueError(f"unsupported phase action type: {action_type!r}")
        return results

    # -- transition executors ------------------------------------------------
    #
    # Every transition: event pair (transition_begin/transition_end) with
    # type, start/end monotonic, rc, drain result, account snapshot delta;
    # tenant fio processes are NEVER touched; failures are fail-visible into
    # self._errors (they surface in collect()/metadata and fail the run at
    # the end of _run()); no cleanup helpers are ever invoked here.

    def _run_transition(self, transition_type: str, action: Mapping[str, Any],
                        boundary: int, executor) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "type": transition_type,
            "boundary": boundary,
            "start_monotonic": time.monotonic(),
            "executed": False,
            "rc": None,
        }
        self._record_event("transition_begin", boundary=boundary, details={
            "type": transition_type, "action": dict(action),
            "ioext_mode_before": self._ioext_mode,
        })
        try:
            if not self.allow_transition_exec:
                record["reason"] = (
                    "transition exec disabled (set allow_transition_exec=true to enable)"
                )
            else:
                executor(action, record)
                record["executed"] = True
                if record.get("rc") is None:
                    record["rc"] = 0
        except Exception as exc:
            record["rc"] = 1
            record["error"] = repr(exc)
            self._errors.append(
                f"boundary {boundary}: {transition_type} failed: {exc!r}"
            )
        record["end_monotonic"] = time.monotonic()
        record["duration_sec"] = record["end_monotonic"] - record["start_monotonic"]
        record["ioext_mode_after"] = self._ioext_mode
        self._record_event("transition_end", boundary=boundary, details={
            "type": transition_type,
            "rc": record.get("rc"),
            "duration_sec": record["duration_sec"],
            "drain": record.get("drain"),
            "error": record.get("error"),
        })
        return record

    # -- account snapshots (bpftool) ------------------------------------------

    def _bpftool_dump(self, *args: str, timeout_sec: float = 10.0) -> Any:
        if not os.path.isfile(self.bpftool_path):
            raise RuntimeError(f"bpftool not found: {self.bpftool_path}")
        result = subprocess.run(
            [self.bpftool_path, "-j", *args],
            capture_output=True, text=True, check=False, timeout=timeout_sec,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"bpftool {' '.join(args)} rc={result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:400]}"
            )
        return json.loads(result.stdout) if result.stdout.strip() else []

    def _ioext_account_snapshot(self) -> Dict[str, Dict[int, Dict[str, int]]]:
        """Snapshot the io_ext account: pinned policy_meta + tenant_state."""
        return {
            "policy_meta": self._policy_meta_snapshot(),
            "tenant_state": parse_ioext_map_dump(
                self._bpftool_dump("map", "dump", "name", "tenant_state"),
                IOEXT_TENANT_FIELDS),
        }

    def _policy_meta_snapshot(self) -> Dict[int, Dict[str, int]]:
        """Cheap meta-only snapshot (one bpftool dump) for hot-path polls."""
        return parse_ioext_map_dump(
            self._bpftool_dump("map", "dump", "pinned", IOEXT_POLICY_META_PIN),
            IOEXT_META_FIELDS)

    def _active_elevator(self) -> str:
        sched_device = resolve_scheduler_device(self.target_device)
        path = f"/sys/block/{sched_device}/queue/scheduler"
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            raise RuntimeError(f"cannot read {path}: {exc}") from exc
        # "none mq-deadline [kyber] bfq" -> active entry is bracketed
        for token in text.split():
            if token.startswith("[") and token.endswith("]"):
                return token[1:-1]
        return text.strip()

    # -- switch_native ---------------------------------------------------------

    def _exec_switch_native(self, action: Mapping[str, Any], record: Dict[str, Any]) -> None:
        if self._ioext_mode != "native":
            raise RuntimeError(
                f"switch_native requires native mode (current: {self._ioext_mode}); "
                f"attach_io_ext while scx_io owns the device is an unvalidated "
                f"arbiter-level transition"
            )
        scheduler = str(action.get("scheduler") or "").strip()
        record["scheduler"] = scheduler
        record["scheduler_before"] = self._active_elevator()
        set_scheduler(self.target_device, scheduler)
        record["scheduler_after"] = self._active_elevator()
        if record["scheduler_after"] != scheduler:
            raise RuntimeError(
                f"elevator switch incomplete: wanted {scheduler!r}, "
                f"active is {record['scheduler_after']!r}"
            )

    # -- swap_q ------------------------------------------------------------------

    def _exec_swap_q(self, action: Mapping[str, Any], record: Dict[str, Any]) -> None:
        if self._ioext_mode != "io_ext":
            raise RuntimeError(f"swap_q requires io_ext mode (current: {self._ioext_mode})")
        active = self._active_elevator()
        record["active_scheduler"] = active
        if active != IOEXT_SCX_SCHEDULER:
            raise RuntimeError(
                f"swap_q requires {IOEXT_SCX_SCHEDULER} on the device, active={active!r}"
            )
        tenants = [str(t) for t in (action.get("tenants") or [])]
        q = str(action.get("q") or "").strip()
        record.update({"target_tenants": tenants, "q": q})
        if not tenants or not q:
            raise RuntimeError("swap_q requires 'tenants' and 'q'")
        for tenant in tenants:
            from_q = self._ioext_current_q.get(tenant)
            if from_q and (from_q, q) in BANNED_Q_SWAP_PAIRS:
                raise RuntimeError(
                    f"swap_q pair {from_q}->{q} banned (q_wedf<->q_sp_edf "
                    f"service-share collapse, hotsplit NOTES 4.3)"
                )
        timeout = float(action.get("owner_token_timeout_sec", 5.0))
        tenant_results: Dict[str, Any] = {}
        for tenant in tenants:
            tenant_results[tenant] = self._swap_tenant_q(tenant, q, action, timeout)
        record["tenants"] = tenant_results
        record["queued_ok"] = all(r.get("queued_ok") for r in tenant_results.values())

    def _swap_tenant_q(self, tenant: str, q: str, action: Mapping[str, Any],
                       timeout: float) -> Dict[str, Any]:
        """Hot-swap ONE tenant's q: new loader up -> owner_token flip -> old loader down."""
        cgroup = self.cgroup_paths[tenant]
        loader_dir = str(action.get("loader_dir") or self.loader_dir)
        loader_bin = os.path.join(loader_dir, f"loader_{q}")
        if not os.path.isfile(loader_bin):
            raise RuntimeError(f"loader binary not found: {loader_bin}")
        proc = self._tenant_procs.get(tenant)
        if proc is None or proc.poll() is not None:
            raise RuntimeError(
                f"tenant {tenant}: fio not running -- swap_q is a hot-path "
                f"transition and refuses to run against an idle tenant"
            )
        cgid = os.stat(cgroup).st_ino
        result: Dict[str, Any] = {"tenant": tenant, "cgid": cgid, "q": q,
                                  "cgroup": cgroup, "loader": loader_bin}
        before = self._ioext_account_snapshot()
        result["queued_ok"], result["queue_depth"], result["pending_before"] = \
            self._poll_queued_request(before, cgid)

        prio_map = action.get("arb_prio_by_tenant") or {}
        weight_map = action.get("arb_weight_by_tenant") or {}
        prio = int(prio_map.get(tenant, action.get("arb_prio", 7)))
        weight = int(weight_map.get(tenant, action.get("arb_weight", 100)))
        result["arb_prio"], result["arb_weight"] = prio, weight

        out_dir = self._tenant_output_dir(tenant)
        os.makedirs(out_dir, exist_ok=True)
        stdout_path = os.path.join(out_dir, f"swap_{q}_{int(time.time())}.log")
        # the loader inherits the stream; keep our handle open until the
        # process is up, then close (the child owns its dup after fork/exec)
        with open(stdout_path, "w", encoding="utf-8") as stdout_f:
            new_proc = popen_in_own_process_group(
                [loader_bin, "-c", cgroup, "-p", str(prio), "-W", str(weight)],
                stdout=stdout_f, stderr=subprocess.STDOUT, text=True,
            )
        result["new_loader_pid"] = new_proc.pid
        result["new_loader_log"] = stdout_path
        self._ioext_loader_procs.append(new_proc)

        old_proc = self._ioext_tenant_loaders.get(tenant)
        old_token = (before["policy_meta"].get(cgid) or {}).get("owner_token")
        flipped = False
        token = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if new_proc.poll() is not None:
                break
            # meta-only poll: one bpftool dump per iteration (the full
            # account snapshot doubles the poll cost under load)
            meta = self._policy_meta_snapshot()
            token = (meta.get(cgid) or {}).get("owner_token")
            if token not in (None, old_token) and token not in (-1, 0):
                flipped = True
                break
            time.sleep(0.1)
        result["owner_token_flipped"] = flipped
        if not flipped:
            if new_proc.poll() is None:
                interrupt_process(new_proc, timeout_sec=5.0)
            raise RuntimeError(
                f"tenant {tenant}: owner_token did not flip within {timeout}s "
                f"after attaching {q} (LIFECYCLE)"
            )
        result["owner_token_after"] = token

        # replace tracking BEFORE stopping the old loader (the loader's own
        # exit-time deregistration checks ownership, verified by hotsplit:
        # old loader SIGTERM never deletes the new meta).
        self._ioext_tenant_loaders[tenant] = new_proc
        self._ioext_current_q[tenant] = q
        if old_proc is not None:
            result["old_loader_pid"] = old_proc.pid
            if old_proc.poll() is None:
                signal_process(old_proc, signal.SIGTERM)
                try:
                    old_proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    interrupt_process(old_proc, timeout_sec=5.0)
                    result["old_loader_sigterm"] = "timeout_interrupted"
            result["old_loader_rc"] = old_proc.returncode
        after = self._ioext_account_snapshot()
        # meta counters are zeroed by the new loader's upsert (NOTES 4.2), so
        # meta deltas across a swap are recorded but expected negative; the
        # tenant_state (arb-side) counters are the conservation baseline.
        result["account_delta"] = account_snapshot_delta(
            {cgid: before["policy_meta"].get(cgid, {})},
            {cgid: after["policy_meta"].get(cgid, {})},
        )
        result["tenant_state_delta"] = account_snapshot_delta(
            {cgid: before["tenant_state"].get(cgid, {})},
            {cgid: after["tenant_state"].get(cgid, {})},
        )
        return result

    def _poll_queued_request(self, snapshot: Dict[str, Dict[int, Dict[str, int]]],
                             cgid: int, attempts: int = 40, interval: float = 0.05) -> Tuple[bool, int, int]:
        """Best-effort queued-request confirmation (hotsplit checklist):
        poll queue_depth>0 || pending>0; bypass-side (q_none active) has no
        scheduler queue -- the negative is recorded honestly."""
        for _ in range(attempts):
            meta_row = snapshot["policy_meta"].get(cgid) or {}
            tenant_row = snapshot["tenant_state"].get(cgid) or {}
            qd = int(meta_row.get("queue_depth", 0) or 0)
            pend = int(tenant_row.get("pending", 0) or 0)
            if qd > 0 or pend > 0:
                return True, qd, pend
            if attempts > 1:
                time.sleep(interval)
        return False, 0, 0

    # -- attach_io_ext / detach_io_ext -----------------------------------------

    def _exec_attach_io_ext(self, action: Mapping[str, Any], record: Dict[str, Any]) -> None:
        if self._ioext_mode != "native":
            raise RuntimeError(f"attach_io_ext requires native mode (current: {self._ioext_mode})")
        scheduler = str(action.get("scheduler") or IOEXT_SCX_SCHEDULER)
        record["scheduler"] = scheduler
        record["scheduler_before"] = self._active_elevator()
        set_scheduler(self.target_device, scheduler)
        record["scheduler_after"] = self._active_elevator()
        if record["scheduler_after"] != scheduler:
            raise RuntimeError(
                f"scx_io switch incomplete: active={record['scheduler_after']!r}"
            )
        profile = self._validated_attach_profile(action, record["boundary"])
        params = {"io_ext": profile}
        transition_dir = os.path.join(self.output_dir, "io_ext_transitions")
        os.makedirs(transition_dir, exist_ok=True)
        procs = start_loader_chain(params, transition_dir, None)
        self._ioext_loader_procs.extend(procs)
        record["loaders_started"] = len(procs)
        # map per-tenant q loaders by scanning their argv for the tenant cgroups
        for proc in procs:
            argv = [str(a) for a in (proc.args if isinstance(proc.args, list) else [])]
            joined = " ".join(argv)
            for tenant, cgroup in self.cgroup_paths.items():
                if cgroup in joined:
                    self._ioext_tenant_loaders[tenant] = proc
                    self._ioext_current_q[tenant] = _argv_loader_name(argv) or \
                        self._ioext_current_q.get(tenant, "")
            for arg in argv:
                if os.path.basename(arg).startswith("loader_arb"):
                    self._ioext_arb_loaders.append(proc)
                    break
        # evidence: account readable after attach
        snap = self._ioext_account_snapshot()
        record["account_after"] = {
            "policy_meta_cgids": sorted(snap["policy_meta"], key=str),
            "tenant_state_cgids": sorted(snap["tenant_state"], key=str),
        }
        wanted = {os.stat(cg).st_ino for cg in self.cgroup_paths.values()
                  if os.path.isdir(cg)}
        covered = wanted & set(snap["policy_meta"])
        record["policy_meta_covered_cgids"] = len(covered)
        if wanted and len(covered) < len(wanted):
            missing = wanted - covered
            raise RuntimeError(
                f"attach incomplete: no policy_meta entry for cgids {sorted(missing)} "
                f"(attach evidence failed)"
            )
        self._ioext_mode = "io_ext"

    def _exec_detach_io_ext(self, action: Mapping[str, Any], record: Dict[str, Any]) -> None:
        if self._ioext_mode != "io_ext":
            raise RuntimeError(f"detach_io_ext requires io_ext mode (current: {self._ioext_mode})")
        target = str(action.get("scheduler") or self.scheduler or "none")
        drain_timeout = float(action.get("drain_timeout_sec", 30.0))
        record["scheduler_target"] = target
        before = self._ioext_account_snapshot()
        record["account_before_cgids"] = {
            "policy_meta": sorted(before["policy_meta"], key=str),
            "tenant_state": sorted(before["tenant_state"], key=str),
        }
        # stop loaders in reverse start order (q policies first, arbiter last)
        stop_loader_chain(list(reversed(self._ioext_loader_procs)))
        record["loaders_stopped"] = len(self._ioext_loader_procs)
        self._ioext_loader_procs = []
        self._ioext_tenant_loaders = {}
        self._ioext_arb_loaders = []
        # drain: wait for the account to close (or the maps to disappear with
        # the loaders -- recorded honestly via account_drained's note)
        deadline = time.monotonic() + drain_timeout
        drained = (False, "not_polled")
        while True:
            try:
                snap = self._ioext_account_snapshot()
                drained = account_drained(snap)
            except RuntimeError as exc:
                # maps gone entirely (loaders exited and unpinned) => drained
                drained = (True, f"maps_unreadable_after_exit: {exc}")
            if drained[0] or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        record["drain"] = {"ok": drained[0], "detail": drained[1],
                           "waited_sec": round(min(drain_timeout, time.monotonic()
                                                   - (deadline - drain_timeout)), 3)}
        if not drained[0]:
            self._errors.append(
                f"boundary {record['boundary']}: detach_io_ext drain failed: {drained[1]}"
            )
        # restore the native elevator
        set_scheduler(self.target_device, target)
        record["scheduler_after"] = self._active_elevator()
        if record["scheduler_after"] != target:
            raise RuntimeError(
                f"elevator restore incomplete: wanted {target!r}, "
                f"active is {record['scheduler_after']!r}"
            )
        self._ioext_mode = "native"
        self._ioext_current_q = {}

    def _run_switch_q_action(self, action: Mapping[str, Any], boundary: int) -> Dict[str, Any]:
        """Placeholder interface for the future io_ext q-switch policy.

        Records the full event (target cgroup, q name, resolved loader
        command).  A loader command is only invoked when ``loader_command`` is
        configured AND the scene-level ``allow_switch_q_exec`` flag is true;
        the skeleton never attaches a policy and never runs cleanup helpers.
        """
        target_tenants = [str(t) for t in (action.get("tenants") or sorted(self.tenants))]
        q_name = str(action.get("q") or "")
        raw_command = action.get("loader_command")
        loader_command = None
        if raw_command:
            loader_command = str(raw_command).format(
                cgroup_paths=self.cgroup_paths, target_device=self.target_device, q=q_name,
            )
        record: Dict[str, Any] = {
            "type": "switch_q",
            "boundary": boundary,
            "q": q_name,
            "target_tenants": target_tenants,
            "target_cgroups": [self.cgroup_paths.get(t) for t in target_tenants],
            "loader_command": loader_command,
            "executed": False,
        }
        if loader_command and self.allow_switch_q_exec:
            self._record_event("switch_q_exec_begin", boundary=boundary, details={"q": q_name})
            result = subprocess.run(
                [SUDO_BIN, BASH_BIN, "-lc", loader_command],
                capture_output=True, text=True, check=False,
            )
            record.update({
                "executed": True,
                "returncode": result.returncode,
                "stdout_tail": (result.stdout or "")[-1200:],
                "stderr_tail": (result.stderr or "")[-1200:],
            })
            if result.returncode != 0:
                self._errors.append(
                    f"boundary {boundary}: switch_q rc={result.returncode} stderr={result.stderr!r}"
                )
            self._record_event("switch_q_exec_end", boundary=boundary, details={
                "q": q_name, "returncode": result.returncode,
            })
        else:
            record["reason"] = (
                "skeleton placeholder: loader attach disabled "
                "(set allow_switch_q_exec=true and loader_command to enable)"
            )
        return record

    # -- sleeps / events / cleanup ---------------------------------------------

    def _sleep_until(self, deadline_monotonic: float) -> None:
        while not self.should_stop():
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return
            # Segmented sleep so stop requests are honored promptly; the
            # deadline is absolute, so retry rounds never accumulate drift.
            time.sleep(min(self.poll_interval_sec, remaining))

    def _drop_caches(self) -> None:
        subprocess.run(
            [SUDO_BIN, BASH_BIN, "-lc", "sync; printf '%s\\n' 3 > /proc/sys/vm/drop_caches"],
            capture_output=True, text=True, check=False,
        )

    def _record_event(self, event: str, boundary: int, details: Mapping[str, Any]) -> None:
        now_epoch = time.time()
        now_mono = time.monotonic()
        relative = None if self.t0_monotonic is None else now_mono - self.t0_monotonic
        self.collected_data.setdefault("events", []).append({
            "event": event,
            "boundary": boundary,
            "timestamp": now_epoch,
            "monotonic": now_mono,
            "relative_sec": relative,
            "details": dict(details),
        })

    def end(self) -> None:
        """Interrupt every tenant process and any transition-started loader; idempotent."""
        for tenant, proc in list(self._tenant_procs.items()):
            interrupt_process(proc, timeout_sec=10.0)
            self._tenant_procs.pop(tenant, None)
        if self._ioext_loader_procs:
            logger.warning(
                "timeline %s: stopping %d transition io_ext loader(s) at end() "
                "(mode=%s); prefer an explicit detach_io_ext boundary action",
                self.scene_id, len(self._ioext_loader_procs), self._ioext_mode)
            stop_loader_chain(list(reversed(self._ioext_loader_procs)))
            self._ioext_loader_procs = []
            self._ioext_tenant_loaders = {}
            self._ioext_arb_loaders = []
        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        for tenant in sorted(self.tenants):
            cleanup_cgroup(self.cgroup_paths[tenant])

    # -- results -----------------------------------------------------------------

    def collect(self) -> Dict[str, Any]:
        if self.start_time is None:
            raise RuntimeError("timeline scene has not started")
        if self.end_time is None:
            self.end_time = time.time()
        self.collected_data["start_time"] = self.start_time
        self.collected_data["end_time"] = self.end_time
        self.collected_data["duration_wall_sec"] = self.end_time - self.start_time
        self.collected_data["t0_monotonic"] = self.t0_monotonic
        self.collected_data["boundaries"] = self._boundary_records
        self.collected_data["tenants_runtime"] = {
            tenant: {
                "spawn_record": rec.get("spawn_record"),
                "exit_record": rec.get("exit_record"),
                "phases_active": rec.get("phases_active"),
            }
            for tenant, rec in self._tenant_records.items()
        }
        self.collected_data["errors"] = list(self._errors)
        if self._errors:
            # Fail visible: errors surface in collect()/metadata, never swallowed.
            self.collected_data["has_errors"] = True
        return self.collected_data

    def _hist_log_paths(self) -> List[str]:
        paths: List[str] = []
        for tenant in self.tenants:
            out_dir = self._tenant_output_dir(tenant)
            name = f"rdyn_{self._seq_token()}_t{tenant}"
            hist_prefix = os.path.join(out_dir, name)
            bw_prefix = os.path.join(out_dir, f"{name}_bw")
            for parent in (out_dir,):
                if not os.path.isdir(parent):
                    continue
                for entry in os.listdir(parent):
                    if entry.startswith(os.path.basename(hist_prefix) + "_clat_hist") \
                            or entry.startswith(os.path.basename(bw_prefix)):
                        paths.append(os.path.join(parent, entry))
        return sorted(set(paths))

    def stat(self) -> List[str]:
        if self.start_time is not None:
            self.collect()
        paths = [write_json_stat(self.output_dir, "timeline", self.scene_id, self.collected_data)]
        for tenant in sorted(self.tenants):
            rec = self._tenant_records.get(tenant) or {}
            spawn = rec.get("spawn_record") or {}
            for key in ("fio_json", "stderr_log"):
                path = spawn.get(key)
                if path and os.path.isfile(path):
                    paths.append(path)
        paths.extend(p for p in self._hist_log_paths() if os.path.isfile(p))
        self._artifact_paths = paths
        return paths

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        self.t0_monotonic = self.collected_data.get("t0_monotonic")
        self._boundary_records = list(self.collected_data.get("boundaries") or [])
        self.duration = float(self.collected_data.get("duration_wall_sec") or 0.0)

    def score(self) -> float:
        # Skeleton: no scoring contract yet.  The main experiment will score
        # per-phase p99 tail latency time series (from the clat_hist logs) with
        # throughput as supporting evidence; see the contract document.
        return 0.0
