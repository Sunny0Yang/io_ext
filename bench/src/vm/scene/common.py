"""Shared helpers for io-ext VM scenes.

The helpers intentionally stay small and stdlib-only.  They provide the
framework glue that every io-ext workload needs: switch the block scheduler,
prepare cgroup v2, start/stop the arbiter + queue loader chain, and wrap a
workload command so the shell writes the child subshell's ``$BASHPID`` into the
configured cgroup before ``exec``-ing the real workload.
"""

from __future__ import annotations

import glob
import io
import json
import logging
import os
import re
import shlex
import signal
import stat
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

logger = logging.getLogger("io_ext_common")


Command = Union[str, Sequence[str]]
DEFAULT_BPFFS_PIN_PATHS = [
    "/sys/fs/bpf/ioext_cgroup_queues",
    "/sys/fs/bpf/ioext_active_cgroups",
    "/sys/fs/bpf/ioext_policy_meta",
    "/sys/fs/bpf/ioext_bfq_tenant_state",
    "/sys/fs/bpf/ioext_prio_config",
]
DEFAULT_ROOT_CGROUP_PATH = "/sys/fs/cgroup"
# Gate on the attach-complete marker written by the arbiter loader AFTER
# link-create succeeds (ioext_loader_common.h ARBITER_ATTACH_READY_PATH —
# kernel publishes current_arbiter only at link-create).  The old
# pin-based gate was unsound: /sys/fs/bpf/ioext_active_cgroups is created
# BEFORE the BPF load, so requests could be inserted before the arbiter
# existed and were then never charged to the arbiter ledger — permanently
# invisible to a strict pick (STALL_FORENSICS.md §4.1.1 seed-1 / leak B1).
DEFAULT_ARBITER_READY_PATHS = ["/tmp/ioext_arbiter_ready"]
_CGROUP_CONTROLLER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEVICE_MAJOR_MINOR_RE = re.compile(r"^[0-9]+:[0-9]+$")
PROCESS_SCHEDULER_ALIASES = {
    "default",
    "eevdf",
    "eevdf/default",
    "process-default",
    "process_default",
}
SUDO_BIN = "/usr/bin/sudo"
BASH_BIN = "/bin/bash"
STDBUF_BIN = "/usr/bin/stdbuf"
PKILL_BIN = "/usr/bin/pkill"
KILL_BIN = "/usr/bin/kill"
_SYSFS_DEVICE_LOCKS: Dict[str, threading.RLock] = {}
_SYSFS_DEVICE_LOCKS_GUARD = threading.Lock()
_SYSFS_WRITE_ATTEMPTS = 5
_SYSFS_WRITE_RETRY_SEC = 0.05
_PROCESS_METADATA: weakref.WeakKeyDictionary[subprocess.Popen[Any], Dict[str, Any]] = weakref.WeakKeyDictionary()

# PIDs of loader chains this runner process started and has not stopped yet
# (the sudo wrappers spawned by start_loader_chain).  The no-specs stale sweep
# (``cleanup_stale_when_no_specs``) uses them to tell "stale loader left over
# by a crashed run" from "live loader owned by a sibling scene of THIS run"
# (e.g. the homo-v2 tenant scenes vs. the sequence's global policy scene).
_LIVE_LOADER_PIDS: set = set()


def _process_metadata(proc: subprocess.Popen[Any]) -> Dict[str, Any]:
    return _PROCESS_METADATA.setdefault(proc, {})


def _pid_alive(pid: int) -> bool:
    try:
        return pid > 0 and os.path.exists(f"/proc/{pid:d}")
    except (OSError, ValueError):
        return False


def live_loader_pids() -> List[int]:
    """PIDs of loader chains this runner started that are still alive.

    Self-healing: dead pids are dropped from the registry so a crashed scene
    cannot pin its dead loader in the protection set forever.
    """
    alive: List[int] = []
    for pid in list(_LIVE_LOADER_PIDS):
        if _pid_alive(pid):
            alive.append(pid)
        else:
            _LIVE_LOADER_PIDS.discard(pid)
    return alive


def _iter_process_entries() -> List[tuple]:
    """One /proc sweep: (pid, ppid, space-joined cmdline) per process.

    Mirrors ``pkill -f`` matching inputs: kernel threads (empty cmdline) and
    unreadable entries are skipped.  /proc/<pid>/stat ppid is parsed after the
    final ``)`` because comm may contain spaces and parentheses.
    """
    entries: List[tuple] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return entries
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not cmdline:
            continue
        ppid = 0
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                stat_text = fh.read().decode("utf-8", "replace")
            rparen = stat_text.rfind(")")
            if rparen >= 0:
                stat_fields = stat_text[rparen + 1:].split()
                if len(stat_fields) >= 2:
                    ppid = int(stat_fields[1])
        except (OSError, ValueError):
            pass
        entries.append((pid, ppid, cmdline))
    return entries


def _descendant_pid_closure(roots: Iterable[int], entries: List[tuple]) -> set:
    """roots plus every process whose parent chain reaches a root pid."""
    children: Dict[int, List[int]] = {}
    for pid, ppid, _cmdline in entries:
        children.setdefault(ppid, []).append(pid)
    protected = {int(pid) for pid in roots if int(pid) > 0}
    stack = list(protected)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, ()):
            if child not in protected:
                protected.add(child)
                stack.append(child)
    return protected


@dataclass
class LoaderSpec:
    """One long-lived io-ext loader process."""

    path: str
    args: List[str]
    name: str
    env: Dict[str, str]
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    scope: str = "legacy"
    policy_cgroup_path: Optional[str] = None


def expand_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    expanded = os.path.expanduser(os.path.expandvars(value))
    if "$" in expanded:
        return expanded
    return os.path.abspath(expanded)


def expand_value(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    return value


def _safe_cgroup_path(path: str) -> str:
    expanded = expand_path(path)
    if not expanded or "$" in expanded:
        raise ValueError(f"unsafe cgroup path: {path!r}")
    normalized = os.path.normpath(expanded)
    root = os.path.realpath(DEFAULT_ROOT_CGROUP_PATH)
    if normalized != root and not normalized.startswith(root + os.sep):
        raise ValueError(f"cgroup path must be under {root}: {path!r}")
    resolved = os.path.realpath(normalized)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"cgroup path escapes {root}: {path!r}")
    return normalized


def ensure_output_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def run_sudo_shell(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([SUDO_BIN, BASH_BIN, "-lc", script], capture_output=True, text=True, check=check)


def ensure_bpffs_mounted() -> None:
    result = run_sudo_shell("mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"failed to mount bpffs at /sys/fs/bpf: {(result.stderr or result.stdout).strip()}")


def is_process_scheduler_alias(scheduler: Optional[str]) -> bool:
    if scheduler is None:
        return True
    return str(scheduler).strip().lower() in PROCESS_SCHEDULER_ALIASES


def validate_block_device_name(target_device: str) -> str:
    device = str(target_device).strip()
    if not device or device in {".", ".."} or "/" in device:
        raise ValueError(f"invalid block device name: {target_device!r}")
    return device


def resolve_block_device_major_minor(device_path: str) -> str:
    """Return the decimal MAJ:MIN pair for a block device node."""

    if not device_path:
        raise ValueError("device_path is required")
    expanded = expand_path(device_path) or device_path
    st = os.stat(expanded)
    if not stat.S_ISBLK(st.st_mode):
        raise ValueError(f"not a block device: {expanded}")
    return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"


def resolve_scheduler_device(target_device: str) -> str:
    """Return block device name that owns queue/scheduler sysfs for *target_device*."""
    target_device = validate_block_device_name(target_device)
    sched_path = f"/sys/block/{target_device}/queue/scheduler"
    if os.path.exists(sched_path):
        return target_device
    # NVMe multipath exposes namespace as nvmeXnY without queue; controller is nvmeXc0nY.
    m = re.match(r"^nvme(\d+)n(\d+)$", target_device)
    if m:
        ctrl = f"nvme{m.group(1)}c0n{m.group(2)}"
        if os.path.exists(f"/sys/block/{ctrl}/queue/scheduler"):
            logger.info("NVMe multipath: scheduler for %s via %s", target_device, ctrl)
            return ctrl
    return target_device


def set_scheduler(target_device: str, scheduler: Optional[str]) -> None:
    if not scheduler or is_process_scheduler_alias(str(scheduler)):
        return
    sched_device = resolve_scheduler_device(target_device)
    sched_path = f"/sys/block/{sched_device}/queue/scheduler"
    with _sysfs_device_lock(sched_device):
        active_scheduler = _active_scheduler(_read_sysfs_text(sched_path))
        if active_scheduler != str(scheduler):
            _write_sysfs_with_retry(
                sched_path,
                f"{scheduler}\n",
                f"failed to set scheduler {scheduler!r} on {target_device}",
            )
            logger.info(
                "scheduler switch %s -> %s on %s: kernel resets device-level "
                "queue knobs (e.g. nr_requests) to defaults on elevator switch",
                active_scheduler,
                scheduler,
                target_device,
            )
        env_nr = os.environ.get("BLOCK_LAYER_NR_REQUESTS", "").strip()
        if env_nr.isdigit():
            set_nr_requests(target_device, int(env_nr))


def set_scheduler_tunables(target_device: str, tunables: Optional[Mapping[str, Any]]) -> None:
    if not tunables:
        return
    sched_device = resolve_scheduler_device(target_device)
    with _sysfs_device_lock(sched_device):
        for name, value in tunables.items():
            tunable = str(name).strip()
            if not _CGROUP_CONTROLLER_RE.fullmatch(tunable):
                raise ValueError(f"invalid scheduler tunable name: {name!r}")
            path = f"/sys/block/{sched_device}/queue/iosched/{tunable}"
            if not os.path.isfile(path):
                raise RuntimeError(f"scheduler tunable is unavailable: {path}")
            expected = str(value).strip()
            if _read_sysfs_text(path) == expected:
                continue
            _write_sysfs_with_retry(
                path,
                f"{expected}\n",
                f"failed to set scheduler tunable {tunable}={expected} on {target_device}",
                expected_value=expected,
            )


def set_d2fq_setup(target_device: str, cfg: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Activate the D2FQ (FAST'21 native port) state on one block queue.

    cfg keys:
      enable:   int 0/1 written to /sys/block/<dev>/queue/d2fq_en (default 1)
      sysctls:  mapping of /proc/sys/kernel/<name> values; every name must
                start with "d2fq_" (e.g. d2fq_thrsh, d2fq_force_all).
    Returns the asserted controls for arm-state evidence, or None when the
    arm carries no d2fq block.
    """
    if not cfg:
        return None
    if not isinstance(cfg, Mapping):
        raise ValueError("io_ext.d2fq must be a mapping")
    sched_device = resolve_scheduler_device(target_device)
    controls: Dict[str, Any] = {"sysctls": {}}
    with _sysfs_device_lock(sched_device):
        for name, value in (cfg.get("sysctls") or {}).items():
            tunable = str(name).strip()
            if not tunable.startswith("d2fq_"):
                raise ValueError(f"d2fq sysctl name must start with d2fq_: {name!r}")
            path = f"/proc/sys/kernel/{tunable}"
            if not os.path.isfile(path):
                raise RuntimeError(f"d2fq sysctl is unavailable: {path}")
            expected = str(value).strip()
            if _read_sysfs_text(path) != expected:
                _write_sysfs_with_retry(
                    path,
                    f"{expected}\n",
                    f"failed to set d2fq sysctl {tunable}={expected}",
                    expected_value=expected,
                )
            controls["sysctls"][tunable] = expected
        enable = int(cfg.get("enable", 1))
        en_path = f"/sys/block/{sched_device}/queue/d2fq_en"
        if not os.path.exists(en_path):
            raise RuntimeError(f"d2fq sysfs is unavailable (kernel lacks the D2FQ port): {en_path}")
        if _read_sysfs_text(en_path) != str(enable):
            _write_sysfs_with_retry(
                en_path,
                f"{enable}\n",
                f"failed to set d2fq_en={enable} on {target_device}",
                expected_value=str(enable),
            )
        controls["d2fq_en"] = str(enable)
    return controls


def clear_d2fq_setup(target_device: str) -> None:
    """Restore queue/d2fq_en=0 (best effort, teardown path)."""
    sched_device = resolve_scheduler_device(target_device)
    en_path = f"/sys/block/{sched_device}/queue/d2fq_en"
    if not os.path.exists(en_path):
        return
    try:
        _write_sysfs_with_retry(en_path, "0\n", f"failed to clear d2fq_en on {target_device}",
                                expected_value="0")
    except Exception as exc:  # pragma: no cover - teardown best effort
        logger.warning("clear_d2fq_setup: %s", exc)


def set_nr_requests(target_device: str, nr: int) -> None:
    """Constrain blk-mq software queue depth to force scheduler arbitration."""
    sched_device = resolve_scheduler_device(target_device)
    nr_path = f"/sys/block/{sched_device}/queue/nr_requests"
    if not os.path.exists(nr_path):
        logger.warning("nr_requests sysfs missing for %s (%s)", target_device, nr_path)
        return
    value = int(nr)
    if value <= 0:
        raise ValueError(f"nr_requests must be positive, got {value!r}")
    with _sysfs_device_lock(sched_device):
        current = _read_sysfs_text(nr_path)
        if current == str(value):
            return
        _write_sysfs_with_retry(
            nr_path,
            f"{value}\n",
            f"failed to set nr_requests={value} on {target_device}",
            expected_value=str(value),
        )
    logger.info("set nr_requests=%s on %s (%s)", value, target_device, nr_path)


def _sysfs_device_lock(device: str) -> threading.RLock:
    with _SYSFS_DEVICE_LOCKS_GUARD:
        return _SYSFS_DEVICE_LOCKS.setdefault(device, threading.RLock())


def _read_sysfs_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            return stream.read().strip()
    except OSError:
        return None


def _active_scheduler(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"\[([^]]+)\]", text)
    return match.group(1) if match else None


def _write_sysfs_with_retry(
    path: str,
    value: str,
    error_prefix: str,
    expected_value: Optional[str] = None,
) -> None:
    last_detail = ""
    for attempt in range(_SYSFS_WRITE_ATTEMPTS):
        result = subprocess.run(
            [SUDO_BIN, "tee", path],
            input=value,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        last_detail = (result.stderr or result.stdout).strip()
        if expected_value is not None and _read_sysfs_text(path) == expected_value:
            return
        if "busy" not in last_detail.lower() or attempt + 1 == _SYSFS_WRITE_ATTEMPTS:
            break
        time.sleep(_SYSFS_WRITE_RETRY_SEC * (attempt + 1))
    raise RuntimeError(f"{error_prefix}: {last_detail}")


def apply_block_device_tuning(
    target_device: str,
    system_cfg: Optional[Mapping[str, Any]] = None,
    null_blk_cfg: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply optional block-layer contention knobs before workload setup."""
    applied: Dict[str, Any] = {}
    system_cfg = system_cfg or {}
    nr = system_cfg.get("nr_requests")
    if nr is None and null_blk_cfg:
        nr = null_blk_cfg.get("nr_requests")
    env_nr = os.environ.get("BLOCK_LAYER_NR_REQUESTS", "").strip()
    if nr is None and env_nr.isdigit():
        nr = int(env_nr)
    if nr is not None:
        set_nr_requests(target_device, int(nr))
        applied["nr_requests"] = int(nr)
    return applied


def _cgroup_ancestor_levels(cg_path: str) -> List[str]:
    """Cgroup dirs from the v2 root's first child down to ``cg_path`` itself.

    ``/sys/fs/cgroup/a/b/c`` -> [``/sys/fs/cgroup/a``, ``/sys/fs/cgroup/a/b``,
    ``/sys/fs/cgroup/a/b/c``]. Flat paths yield a single element, matching the
    pre-hierarchy behavior.
    """
    root = os.path.realpath(DEFAULT_ROOT_CGROUP_PATH)
    rel = os.path.relpath(cg_path, root)
    if rel in (".", ""):
        return []
    levels: List[str] = []
    current = root
    for part in rel.split(os.sep):
        if part in ("", "."):
            continue
        current = os.path.join(current, part)
        levels.append(current)
    return levels


def setup_cgroup_v2(cgroup_path: Optional[str], controllers: Iterable[str] = ("io",)) -> None:
    """Mount cgroup v2 if necessary, delegate controllers, and create cgroup.

    Multi-level (hierarchical) paths are supported: ``cgroup.subtree_control``
    is enabled on every ancestor of the target so the leaf ends up with the
    controller files (io.max etc.). Flat single-level paths behave exactly as
    before (only the v2 root's subtree_control is touched).
    """

    if not cgroup_path:
        return
    cg_path = _safe_cgroup_path(cgroup_path)
    wanted_controllers = [str(ctrl).strip().lstrip("+") for ctrl in controllers]
    wanted_controllers = [ctrl for ctrl in wanted_controllers if ctrl]
    invalid_controllers = [ctrl for ctrl in wanted_controllers if not _CGROUP_CONTROLLER_RE.fullmatch(ctrl)]
    if invalid_controllers:
        raise ValueError(f"invalid cgroup controller names: {invalid_controllers}")
    root = os.path.realpath(DEFAULT_ROOT_CGROUP_PATH)
    levels = _cgroup_ancestor_levels(cg_path)

    def _enable_lines(target: str) -> List[str]:
        return [
            f"printf '%s\\n' {shlex.quote('+' + ctrl)} > {shlex.quote(os.path.join(target, 'cgroup.subtree_control'))} 2>/dev/null || true"
            for ctrl in wanted_controllers
        ]

    script_lines = [
        "mountpoint -q /sys/fs/cgroup || mount -t cgroup2 none /sys/fs/cgroup || true",
        *_enable_lines(root),
        f"mkdir -p {shlex.quote(cg_path)}",
    ]
    # Enable the controllers on each intermediate ancestor (below the v2 root)
    # so the leaf receives the io.* control files. Best-effort per level: an
    # ancestor already carrying the controller (e.g. from another live scene)
    # must not fail the write.
    for level in levels[:-1]:
        script_lines.extend(_enable_lines(level))
    script = "\n".join(script_lines)
    result = run_sudo_shell(script, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"failed to prepare cgroup {cg_path}: {(result.stderr or result.stdout).strip()}")
    if wanted_controllers:
        check_targets = [root, *levels[:-1]]
        check_script = "\n".join(
            [
                "set -e",
                *[
                    f"enabled=$(cat {shlex.quote(os.path.join(t, 'cgroup.subtree_control'))} 2>/dev/null || true); "
                    f"printf '%s\\n' \"$enabled\" | grep -qw {shlex.quote(ctrl)}"
                    for t in check_targets
                    for ctrl in wanted_controllers
                ],
                *(
                    [f"test -f {shlex.quote(os.path.join(cg_path, 'io.max'))}"]
                    if "io" in wanted_controllers and levels
                    else []
                ),
            ]
        )
        check = run_sudo_shell(check_script, check=False)
        if check.returncode != 0:
            raise RuntimeError(
                "failed to enable cgroup v2 controllers "
                f"{wanted_controllers} along the ancestor chain of {cg_path}"
            )


def setup_cgroup_io_controls(
    cgroup_path: Optional[str],
    device_path: str,
    io_weight: Any = None,
    io_max: Any = None,
    io_latency: Any = None,
    io_prio_class: Any = None,
    io_bfq_weight: Any = None,
) -> Dict[str, Any]:
    """Apply cgroup v2 IO controller settings for one workload cgroup.

    ``io.weight`` accepts a scalar default cgroup weight or a mapping with
    ``default``/``default_weight`` and ``device``/``device_weight``/``weight``.
    ``io.max`` accepts a mapping containing any of ``rbps``, ``wbps``, ``riops``,
    and ``wiops``. Values may be non-negative integers or the string ``max``.
    ``io_latency`` accepts a mapping ``{target: int}`` or ``{rbps: ..., target: int}``
    (the rbps key is accepted for upstream-compat but ignored; only ``target``
    in microseconds is written as ``{MAJ:MIN} target={usec}``).
    ``io_prio_class`` accepts a string label
    (``no-change|promote-to-rt|restrict-to-be|idle|none-to-rt``) or an int 0..3.

    io.cost (model + qos) is NOT handled here because it must be written at the
    cgroup root (``/sys/fs/cgroup/io.cost.{model,qos}``); use
    :func:`setup_iocost_at_root` for that.
    """

    has_any = (
        io_weight is not None
        or io_max is not None
        or io_latency is not None
        or io_prio_class is not None
        or io_bfq_weight is not None
    )
    if not cgroup_path or not has_any:
        return {}

    cg_path = _safe_cgroup_path(cgroup_path)
    major_minor = resolve_block_device_major_minor(device_path)
    weight_lines = _format_io_weight_lines(io_weight, major_minor)
    max_line = _format_io_max_line(io_max, major_minor)
    max_line_is_unlimited = _io_max_line_is_unlimited(max_line)
    latency_line = _format_io_latency_line(io_latency, major_minor)
    prio_line = _format_io_prio_class_line(io_prio_class)
    bfq_weight_line = _format_io_bfq_weight_line(io_bfq_weight)
    if not weight_lines and not max_line and not latency_line and not prio_line and not bfq_weight_line:
        return {"device_path": device_path, "device_major_minor": major_minor}

    script_lines = ["set -e", f"test -d {shlex.quote(cg_path)}"]
    if weight_lines:
        script_lines.append(f"test -f {shlex.quote(os.path.join(cg_path, 'io.weight'))}")
        for line in weight_lines:
            script_lines.append(
                f"printf '%s\\n' {shlex.quote(line)} > {shlex.quote(os.path.join(cg_path, 'io.weight'))}"
            )
    if max_line:
        io_max_path = shlex.quote(os.path.join(cg_path, "io.max"))
        io_max_write = f"printf '%s\\n' {shlex.quote(max_line)} > {io_max_path}"
        if max_line_is_unlimited:
            script_lines.append(f"if [ -f {io_max_path} ]; then {io_max_write}; fi")
        else:
            script_lines.append(f"test -f {io_max_path}")
            script_lines.append(io_max_write)
    if latency_line:
        io_lat_path = shlex.quote(os.path.join(cg_path, "io.latency"))
        script_lines.append(f"if [ -f {io_lat_path} ]; then printf '%s\\n' {shlex.quote(latency_line)} > {io_lat_path}; fi")
    if prio_line:
        io_prio_path = shlex.quote(os.path.join(cg_path, "io.prio.class"))
        script_lines.append(f"if [ -f {io_prio_path} ]; then printf '%s\\n' {shlex.quote(prio_line)} > {io_prio_path}; fi")

    if bfq_weight_line:
        io_bfq_weight_path = shlex.quote(os.path.join(cg_path, "io.bfq.weight"))
        script_lines.append(f"test -f {io_bfq_weight_path}")
        script_lines.append(f"printf '%s\\n' {shlex.quote(bfq_weight_line)} > {io_bfq_weight_path}")

    result = run_sudo_shell("\n".join(script_lines), check=False)
    if result.returncode != 0:
        requested_controls = {
            "io.weight": weight_lines,
            "io.max": max_line,
            "io.latency": latency_line,
            "io.prio.class": prio_line,
            "io.bfq.weight": bfq_weight_line,
        }
        raise RuntimeError(
            f"failed to apply cgroup IO controls on {cg_path} (rc={result.returncode}, "
            f"requested={requested_controls}): {(result.stderr or result.stdout).strip() or 'no command output'}"
        )

    applied: Dict[str, Any] = {
        "cgroup_path": cg_path,
        "device_path": device_path,
        "device_major_minor": major_minor,
        "io_weight_lines": weight_lines,
        "io_max_line": max_line,
        "io_max_unlimited": max_line_is_unlimited,
    }
    if latency_line:
        applied["io_latency_line"] = latency_line
    if prio_line:
        applied["io_prio_class_line"] = prio_line
    if bfq_weight_line:
        applied["io_bfq_weight_line"] = bfq_weight_line
    return applied


def setup_iocost_at_root(
    device_path: str,
    io_cost_model: Any,
    io_cost_qos: Any,
    root_cgroup_path: str = DEFAULT_ROOT_CGROUP_PATH,
) -> Dict[str, Any]:
    """Write io.cost.model and io.cost.qos at the cgroup root.

    ``io_cost_model`` accepts a mapping with any of:
    ``ctrl`` (default ``user``), ``model`` (default ``linear``), and the six
    rate fields ``rbps, rseqiops, rrandiops, wbps, wseqiops, wrandiops``.
    ``io_cost_qos`` accepts a mapping with ``enable`` (bool, default True),
    ``ctrl``, ``rpct``, ``rlat``, ``wpct``, ``wlat``, ``min``, ``max``.
    """

    if io_cost_model is None and io_cost_qos is None:
        return {}
    root_path = _safe_cgroup_path(root_cgroup_path)
    major_minor = resolve_block_device_major_minor(device_path)
    model_line = _format_iocost_model_line(io_cost_model, major_minor) if io_cost_model else None
    qos_line = _format_iocost_qos_line(io_cost_qos, major_minor) if io_cost_qos else None
    if not model_line and not qos_line:
        return {"device_major_minor": major_minor}

    script_lines = ["set -e", f"test -d {shlex.quote(root_path)}"]
    if model_line:
        model_path = shlex.quote(os.path.join(root_path, "io.cost.model"))
        script_lines.append(f"if [ -f {model_path} ]; then printf '%s\\n' {shlex.quote(model_line)} > {model_path}; fi")
    if qos_line:
        qos_path = shlex.quote(os.path.join(root_path, "io.cost.qos"))
        script_lines.append(f"if [ -f {qos_path} ]; then printf '%s\\n' {shlex.quote(qos_line)} > {qos_path}; fi")
    result = run_sudo_shell("\n".join(script_lines), check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to apply io.cost at root {root_path}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return {
        "root_cgroup_path": root_path,
        "device_major_minor": major_minor,
        "io_cost_model_line": model_line,
        "io_cost_qos_line": qos_line,
    }


def _format_io_weight_lines(io_weight: Any, major_minor: str) -> List[str]:
    if io_weight is None:
        return []
    if isinstance(io_weight, Mapping):
        lines: List[str] = []
        default_weight = _mapping_get_any(io_weight, ("default", "default_weight"))
        if default_weight is not None:
            lines.append(f"default {_normalize_io_weight(default_weight)}")
        device_weight = _mapping_get_any(io_weight, ("device", "device_weight", "weight"))
        if device_weight is not None:
            lines.append(f"{major_minor} {_normalize_io_weight(device_weight)}")
        devices = io_weight.get("devices")
        if devices is not None:
            if not isinstance(devices, Mapping):
                raise ValueError("io_weight.devices must be a mapping of MAJ:MIN to weight")
            for device_key, weight in devices.items():
                device_major_minor = str(device_key).strip()
                if device_major_minor in {"target", "target_device", "device"}:
                    device_major_minor = major_minor
                if not _DEVICE_MAJOR_MINOR_RE.fullmatch(device_major_minor):
                    raise ValueError(f"io_weight device key must be MAJ:MIN, got {device_key!r}")
                lines.append(f"{device_major_minor} {_normalize_io_weight(weight)}")
        if not lines:
            raise ValueError(
                "io_weight mapping must contain default/default_weight, "
                "device/device_weight/weight, or devices"
            )
        return lines
    return [f"default {_normalize_io_weight(io_weight)}"]


def _format_io_bfq_weight_line(io_bfq_weight: Any) -> Optional[str]:
    if io_bfq_weight is None:
        return None
    value = io_bfq_weight
    if isinstance(io_bfq_weight, Mapping):
        value = _mapping_get_any(io_bfq_weight, ("default", "default_weight", "weight"))
        if value is None:
            raise ValueError("io_bfq_weight mapping must contain default/default_weight/weight")
    if isinstance(value, bool):
        raise ValueError("io.bfq.weight must be an integer from 1 to 1000")
    try:
        weight = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"io.bfq.weight must be an integer from 1 to 1000, got {value!r}") from exc
    if weight < 1 or weight > 1000:
        raise ValueError(f"io.bfq.weight must be in [1, 1000], got {weight}")
    return f"default {weight}"


def _format_io_max_line(io_max: Any, major_minor: str) -> Optional[str]:
    if io_max is None:
        return None
    if not isinstance(io_max, Mapping):
        raise ValueError("io_max must be a mapping with rbps/wbps/riops/wiops keys")
    field_aliases = {
        "rbps": ("rbps", "read_bps"),
        "wbps": ("wbps", "write_bps"),
        "riops": ("riops", "read_iops"),
        "wiops": ("wiops", "write_iops"),
    }
    fields: List[str] = []
    for field, aliases in field_aliases.items():
        value = _mapping_get_any(io_max, aliases)
        if value is not None:
            fields.append(f"{field}={_normalize_io_max_value(value)}")
    if not fields:
        return None
    configured_device = io_max.get("device") or io_max.get("major_minor") or major_minor
    configured_device = str(configured_device).strip()
    if configured_device in {"target", "target_device"}:
        configured_device = major_minor
    if not _DEVICE_MAJOR_MINOR_RE.fullmatch(configured_device):
        raise ValueError(f"io_max device must be MAJ:MIN, got {configured_device!r}")
    return f"{configured_device} {' '.join(fields)}"


def _mapping_get_any(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalize_io_weight(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("io.weight must be an integer from 1 to 10000")
    try:
        weight = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"io.weight must be an integer from 1 to 10000, got {value!r}") from exc
    if weight < 1 or weight > 10000:
        raise ValueError(f"io.weight must be in [1, 10000], got {weight}")
    return weight


def _normalize_io_max_value(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("io.max values must be non-negative integers or 'max'")
    if isinstance(value, str) and value.strip().lower() == "max":
        return "max"
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"io.max values must be non-negative integers or 'max', got {value!r}") from exc
    if limit < 0:
        raise ValueError(f"io.max values must be non-negative integers or 'max', got {limit}")
    return str(limit)


def _io_max_line_is_unlimited(io_max_line: Optional[str]) -> bool:
    if not io_max_line:
        return False
    parts = io_max_line.split()[1:]
    return bool(parts) and all(part.endswith("=max") for part in parts)


_IO_PRIO_CLASS_LABELS = {
    0: "no-change",
    1: "promote-to-rt",
    2: "restrict-to-be",
    3: "idle",
    "no-change": "no-change",
    "promote-to-rt": "promote-to-rt",
    "restrict-to-be": "restrict-to-be",
    "idle": "idle",
    "none-to-rt": "none-to-rt",
    "rt": "promote-to-rt",
    "be": "restrict-to-be",
}


def _format_io_prio_class_line(io_prio_class: Any) -> Optional[str]:
    if io_prio_class is None:
        return None
    if isinstance(io_prio_class, str):
        key = io_prio_class.strip().lower()
    elif isinstance(io_prio_class, bool):
        return None
    else:
        try:
            key = int(io_prio_class)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"io.prio.class must be a label or int 0..3, got {io_prio_class!r}") from exc
    if key not in _IO_PRIO_CLASS_LABELS:
        raise ValueError(f"invalid io.prio.class value: {io_prio_class!r}")
    return _IO_PRIO_CLASS_LABELS[key]


def _format_io_latency_line(io_latency: Any, major_minor: str) -> Optional[str]:
    if io_latency is None:
        return None
    if isinstance(io_latency, Mapping):
        target = _mapping_get_any(io_latency, ("target", "target_us", "latency", "latency_us"))
    elif isinstance(io_latency, bool):
        return None
    else:
        target = io_latency
    if target is None:
        return None
    try:
        target_us = int(str(target).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"io.latency target must be a non-negative integer (usec), got {target!r}") from exc
    if target_us < 0:
        raise ValueError(f"io.latency target must be >= 0, got {target_us}")
    return f"{major_minor} target={target_us}"


_IOCOST_MODEL_RATE_FIELDS = ("rbps", "rseqiops", "rrandiops", "wbps", "wseqiops", "wrandiops")


def _format_iocost_model_line(io_cost_model: Any, major_minor: str) -> Optional[str]:
    if io_cost_model is None:
        return None
    if not isinstance(io_cost_model, Mapping):
        raise ValueError("io_cost_model must be a mapping with rate fields")
    ctrl = str(io_cost_model.get("ctrl", "user"))
    model_name = str(io_cost_model.get("model", "linear"))
    parts = [f"{major_minor} ctrl={ctrl} model={model_name}"]
    for field in _IOCOST_MODEL_RATE_FIELDS:
        value = _mapping_get_any(io_cost_model, (field,))
        if value is None:
            raise ValueError(f"io_cost_model.{field} is required")
        try:
            rate = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"io_cost_model.{field} must be an integer, got {value!r}") from exc
        if rate < 0:
            raise ValueError(f"io_cost_model.{field} must be >= 0, got {rate}")
        parts.append(f"{field}={rate}")
    return " ".join(parts)


_IOCOST_QOS_FIELDS = ("rpct", "rlat", "wpct", "wlat", "min", "max")
_IOCOST_QOS_DEFAULTS = {"rpct": 95.0, "rlat": 1_000_000, "wpct": 95.0, "wlat": 1_000_000, "min": 50.0, "max": 150.0}


def _format_iocost_qos_line(io_cost_qos: Any, major_minor: str) -> Optional[str]:
    if io_cost_qos is None:
        return None
    if not isinstance(io_cost_qos, Mapping):
        raise ValueError("io_cost_qos must be a mapping")
    enable = _config_bool(io_cost_qos.get("enable", True), True)
    ctrl = str(io_cost_qos.get("ctrl", "user"))
    parts = [f"{major_minor} enable={'1' if enable else '0'} ctrl={ctrl}"]
    for field in _IOCOST_QOS_FIELDS:
        value = _mapping_get_any(io_cost_qos, (field,))
        if value is None:
            value = _IOCOST_QOS_DEFAULTS[field]
        try:
            num = float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"io_cost_qos.{field} must be a number, got {value!r}") from exc
        if field in ("rlat", "wlat"):
            parts.append(f"{field}={int(num)}")
        else:
            parts.append(f"{field}={num}")
    return " ".join(parts)


def cleanup_cgroup(cgroup_path: Optional[str]) -> None:
    if not cgroup_path:
        return
    cg_path = _safe_cgroup_path(cgroup_path)
    root = os.path.realpath(DEFAULT_ROOT_CGROUP_PATH)
    if cg_path == root:
        return
    # Bottom-up teardown: disable io in subtree_control (best-effort — fails
    # harmlessly while children still exist), rmdir, then repeat for the now
    # possibly-empty parent. Stops at the cgroup v2 root so shared ancestors
    # of other namespaces are never touched; flat paths behave as before
    # (single rmdir of the leaf).
    script = "\n".join(
        [
            f"lvl={shlex.quote(cg_path)}",
            f"while [ \"$lvl\" != {shlex.quote(root)} ] && [ -n \"$lvl\" ]; do",
            "  printf '%s\\n' '-io' > \"$lvl/cgroup.subtree_control\" 2>/dev/null || true",
            "  rmdir \"$lvl\" 2>/dev/null || true",
            "  lvl=$(dirname \"$lvl\")",
            "done",
        ]
    )
    run_sudo_shell(script, check=False)


def cleanup_stale_loaders(patterns: Optional[Iterable[str]], protect_pids: Optional[Iterable[int]] = None) -> None:
    """Kill loader processes whose cmdline matches ``patterns``.

    Without ``protect_pids`` this is the blind ``pkill -9 -f`` sweep (scene
    startup hygiene that must also clear unknown leftovers from crashed runs).

    With ``protect_pids`` (loader chains this runner started and still
    considers live — see :func:`live_loader_pids`) the sweep is scoped: the
    protected pids AND their descendants (sudo wrapper -> loader) are spared;
    only genuinely stale matches are killed.  This is what tenant scenes with
    ``cleanup_stale_when_no_specs`` need: their hygiene must not tear down the
    live loaders of a sibling scene of the same run (e.g. the sequence's
    global policy scene), which would silently strip every io-ext policy from
    the measured workload.
    """
    protect = {int(pid) for pid in (protect_pids or ()) if int(pid) > 0}
    if not protect:
        for pattern in patterns or []:
            pattern = str(pattern).strip()
            if not pattern:
                continue
            subprocess.run([SUDO_BIN, PKILL_BIN, "-9", "-f", pattern], capture_output=True, text=True)
        return

    entries = _iter_process_entries()
    protected = _descendant_pid_closure(protect, entries)
    own_pid = os.getpid()
    victims: List[tuple] = []
    for pid, _ppid, cmdline in entries:
        if pid in protected or pid == own_pid:
            continue
        for pattern in patterns or []:
            pattern = str(pattern).strip()
            if not pattern:
                continue
            try:
                matched = re.search(pattern, cmdline) is not None
            except re.error:
                matched = pattern in cmdline
            if matched:
                victims.append((pid, cmdline))
                break
    for pid, _cmdline in victims:
        subprocess.run([SUDO_BIN, KILL_BIN, "-9", str(pid)], capture_output=True, text=True)
    if victims:
        logger.info(
            "io-ext stale loader sweep: killed %d stale process(es), protected %d live loader pid(s) (incl. descendants)",
            len(victims),
            len(protected),
        )
    else:
        logger.info(
            "io-ext stale loader sweep: no stale processes, protected %d live loader pid(s) (incl. descendants)",
            len(protected),
        )


def cleanup_bpffs_pins(paths: Optional[Iterable[str]]) -> None:
    safe_paths = [_safe_bpffs_pin_path(str(path)) for path in (paths or []) if str(path).strip()]
    if not safe_paths:
        return
    ensure_bpffs_mounted()
    run_sudo_shell("rm -rf " + " ".join(shlex.quote(path) for path in safe_paths), check=False)


def _safe_bpffs_pin_path(path: str) -> str:
    expanded = expand_path(path)
    if not expanded or "$" in expanded:
        raise ValueError(f"unsafe BPFFS pin path: {path!r}")
    normalized = os.path.normpath(expanded)
    if normalized in ("/", "/sys", "/sys/fs", "/sys/fs/bpf"):
        raise ValueError(f"refusing broad BPFFS cleanup path: {path!r}")
    if not normalized.startswith("/sys/fs/bpf/ioext_"):
        raise ValueError(f"BPFFS cleanup path must be under /sys/fs/bpf/ioext_*: {path!r}")
    return normalized


def file_tail(path: Optional[str], max_chars: int = 1200) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    except OSError:
        return ""
    return data[-max_chars:].strip()


def _file_tail(path: Optional[str], max_chars: int = 1200) -> str:
    return file_tail(path, max_chars=max_chars)


_ARBITER_SCOPES = {"arbiter", "global_arbiter", "global-arbiter"}
_ROOT_POLICY_SCOPES = {"root", "root_policy", "root-policy", "global_policy", "global-policy"}
_CGROUP_POLICY_SCOPES = {"cgroup", "cgroup_policy", "cgroup-policy", "tenant", "per_cgroup", "per-cgroup"}


def _as_loader_items(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    return list(value)


def _normalize_loader_scope(scope: Any, default_scope: str) -> str:
    raw_scope = scope if scope is not None else default_scope
    normalized = str(raw_scope).strip().lower().replace(" ", "_")
    return normalized or default_scope


def _resolve_root_cgroup_path(io_ext: Mapping[str, Any]) -> str:
    root_cfg = io_ext.get("root_cgroup") or {}
    raw_path = None
    if isinstance(root_cfg, Mapping):
        raw_path = root_cfg.get("path")
    raw_path = raw_path or io_ext.get("root_cgroup_path") or DEFAULT_ROOT_CGROUP_PATH
    return _safe_cgroup_path(str(expand_value(raw_path)))


def _expand_loader_template(
    value: Any,
    scene_cgroup_path: Optional[str],
    root_cgroup_path: str,
    policy_cgroup_path: Optional[str],
) -> str:
    expanded = str(expand_value(value))
    replacements = {
        "{cgroup_path}": scene_cgroup_path or "",
        "{root_cgroup_path}": root_cgroup_path,
        "{policy_cgroup_path}": policy_cgroup_path or "",
    }
    for token, replacement in replacements.items():
        expanded = expanded.replace(token, replacement)
    return expanded


def _item_cgroup_path(
    item: Mapping[str, Any],
    scope: str,
    scene_cgroup_path: Optional[str],
    root_cgroup_path: str,
) -> Optional[str]:
    raw_path = item.get("policy_cgroup_path") or item.get("cgroup_path")
    cgroup_cfg = item.get("cgroup") or {}
    if raw_path is None and isinstance(cgroup_cfg, Mapping):
        raw_path = cgroup_cfg.get("path")

    if raw_path is None:
        if scope in _ROOT_POLICY_SCOPES:
            raw_path = root_cgroup_path
        elif scope in _CGROUP_POLICY_SCOPES or scope == "legacy":
            raw_path = scene_cgroup_path

    if raw_path is None:
        return None
    default_policy_path = root_cgroup_path if scope in _ROOT_POLICY_SCOPES else scene_cgroup_path
    tokenized = _expand_loader_template(raw_path, scene_cgroup_path, root_cgroup_path, default_policy_path)
    if not tokenized:
        return None
    return _safe_cgroup_path(tokenized)


def _loader_entries(io_ext: Mapping[str, Any], parameters: Mapping[str, Any]) -> List[tuple[Any, str]]:
    entries: List[tuple[Any, str]] = []
    for key in ("arbiters", "arbiter"):
        entries.extend((item, "arbiter") for item in _as_loader_items(io_ext.get(key)))
    for key in ("root_policies", "root_policy", "global_policies", "global_policy"):
        entries.extend((item, "root_policy") for item in _as_loader_items(io_ext.get(key)))
    for key in ("cgroup_policies", "cgroup_policy"):
        entries.extend((item, "cgroup_policy") for item in _as_loader_items(io_ext.get(key)))

    # Generic policy list: each item may set scope=root_policy/cgroup_policy/arbiter.
    entries.extend((item, "cgroup_policy") for item in _as_loader_items(io_ext.get("policies")))

    legacy_loader_cfgs = io_ext.get("loaders") or io_ext.get("loader_chain") or parameters.get("loader_chain") or []
    if not legacy_loader_cfgs and parameters.get("loader_path"):
        legacy_loader_cfgs = [{"path": parameters["loader_path"], "name": "loader"}]
    entries.extend((item, "legacy") for item in _as_loader_items(legacy_loader_cfgs))
    return entries


def _resolve_loader_specs(
    parameters: Mapping[str, Any], output_dir: str, cgroup_path: Optional[str]
) -> List[LoaderSpec]:
    raw_io_ext = parameters.get("io_ext") or {}
    io_ext = raw_io_ext if isinstance(raw_io_ext, Mapping) else {}
    root_cgroup_path = _resolve_root_cgroup_path(io_ext)

    specs: List[LoaderSpec] = []
    for idx, (item, default_scope) in enumerate(_loader_entries(io_ext, parameters)):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, Mapping):
            raise ValueError(f"invalid loader specification at index {idx}: {item!r}")

        scope = _normalize_loader_scope(item.get("scope"), default_scope)
        policy_cgroup_path = None if scope in _ARBITER_SCOPES else _item_cgroup_path(
            item, scope, cgroup_path, root_cgroup_path
        )

        raw_path = item.get("path") or item.get("loader_path")
        if not raw_path:
            raise ValueError(f"loader specification {idx} misses path")
        templated_path = _expand_loader_template(raw_path, cgroup_path, root_cgroup_path, policy_cgroup_path)
        path = expand_path(templated_path) or templated_path
        name = str(item.get("name") or os.path.basename(path) or f"loader{idx}")
        raw_args_value = item.get("args")
        if raw_args_value is None:
            raw_args = ["{policy_cgroup_path}"] if scope in (_ROOT_POLICY_SCOPES | _CGROUP_POLICY_SCOPES) else []
        elif isinstance(raw_args_value, (str, int, float)):
            raw_args = [raw_args_value]
        else:
            raw_args = list(raw_args_value)
        args = [
            _expand_loader_template(arg, cgroup_path, root_cgroup_path, policy_cgroup_path)
            for arg in raw_args
        ]
        raw_env = item.get("env") or {}
        if not isinstance(raw_env, Mapping):
            raise ValueError(f"loader specification {idx} env must be a mapping")
        env = {str(k): str(expand_value(v)) for k, v in raw_env.items()}
        specs.append(
            LoaderSpec(
                path=path,
                args=args,
                name=name,
                env=env,
                stdout_path=os.path.join(output_dir, f"io_ext_{idx}_{name}.out"),
                stderr_path=os.path.join(output_dir, f"io_ext_{idx}_{name}.err"),
                scope=scope,
                policy_cgroup_path=policy_cgroup_path,
            )
        )
    return specs


def start_loader_chain(
    parameters: Mapping[str, Any], output_dir: str, cgroup_path: Optional[str]
) -> List[subprocess.Popen[str]]:
    """Start loader processes in the configured order; arbiters should be first."""

    specs = _resolve_loader_specs(parameters, output_dir, cgroup_path)
    procs: List[subprocess.Popen[str]] = []
    io_ext = parameters.get("io_ext") or {}
    has_arbiter = any(_looks_like_arbiter(spec) for spec in specs)

    if not specs:
        if io_ext.get("cleanup_stale_when_no_specs", False):
            stale_patterns = io_ext.get("stale_loader_patterns") or ["loader_arb_", "loader_q_"]
            # Scope the sweep to genuinely stale processes: loaders this
            # runner started for a sibling scene of the same run (typically
            # the sequence's global policy scene) are live policy owners and
            # must survive — killing them here silently strips every io-ext
            # policy from the workload this scene is about to measure.
            protect = live_loader_pids()
            if protect:
                logger.info(
                    "io-ext no-specs stale sweep: protecting %d live loader pid(s) from sibling scene(s) of this run",
                    len(protect),
                )
            cleanup_stale_loaders(stale_patterns, protect_pids=protect)
        if io_ext.get("cleanup_bpffs_pins"):
            if live_loader_pids():
                # Live loaders own the ioext_* BPFFS pins; removing them would
                # break tooling access (bpftool/sidecar) to the running
                # policies.  Truly stale pins (no live loader) still sweep.
                logger.info("io-ext BPFFS pin cleanup skipped: live loader(s) hold ioext_* pins")
            else:
                ensure_bpffs_mounted()
                cleanup_bpffs_pins(io_ext.get("bpffs_pin_paths") or DEFAULT_BPFFS_PIN_PATHS)
        return []

    if io_ext.get("cleanup_stale", True):
        stale_patterns = io_ext.get("stale_loader_patterns")
        if stale_patterns is None:
            stale_patterns = ["loader_arb_", "loader_q_"]
        cleanup_stale_loaders(stale_patterns)

    ensure_bpffs_mounted()
    if io_ext.get("cleanup_bpffs_pins"):
        cleanup_bpffs_pins(io_ext.get("bpffs_pin_paths") or DEFAULT_BPFFS_PIN_PATHS)

    # Stale attach-complete marker: a loader killed with SIGKILL (the
    # stale-sweep / wedge-recovery path) cannot unlink it.  The loader
    # runs as root, so removal must go through sudo — the same mechanism
    # as cleanup_bpffs_pins.  Guarding the ready gate on a stale marker
    # would let the chain start against no arbiter at all.
    run_sudo_shell(
        "rm -f " + shlex.quote(DEFAULT_ARBITER_READY_PATHS[0]),
        check=False,
    )

    env_base = os.environ.copy()
    start_gap_sec = float(io_ext.get("loader_start_gap_sec", 0.5))
    arbiter_ready_timeout_sec = float(io_ext.get("arbiter_ready_timeout_sec", 5.0))
    arbiter_ready_paths = [
        str(expand_value(path)) for path in (io_ext.get("arbiter_ready_paths") or DEFAULT_ARBITER_READY_PATHS)
    ]
    sudo_cmd = [SUDO_BIN]
    if _config_bool(io_ext.get("loader_sudo_noninteractive"), True):
        sudo_cmd.append("-n")
    for spec in specs:
        loader_base = os.path.basename(spec.path)
        skip_stdbuf = loader_base.startswith("loader_") or _config_bool(
            os.environ.get("IOEXT_SKIP_STDBUF"), False
        )
        if (
            not skip_stdbuf
            and STDBUF_BIN
            and os.path.isfile(STDBUF_BIN)
            and os.access(STDBUF_BIN, os.X_OK)
        ):
            cmd = [*sudo_cmd, STDBUF_BIN, "-oL", spec.path, *spec.args]
        else:
            cmd = [*sudo_cmd, spec.path, *spec.args]
        env = env_base.copy()
        env.update(spec.env)
        stdout_f = None
        stderr_f = None
        try:
            stdout_f = open(spec.stdout_path, "w", encoding="utf-8") if spec.stdout_path else subprocess.DEVNULL
            stderr_f = open(spec.stderr_path, "w", encoding="utf-8") if spec.stderr_path else subprocess.DEVNULL
            proc = popen_in_own_process_group(cmd, stdout=stdout_f, stderr=stderr_f, text=True, env=env)
        except Exception:
            for stream in (stdout_f, stderr_f):
                if isinstance(stream, io.IOBase):
                    try:
                        stream.close()
                    except OSError:
                        logger.debug("failed to close io-ext loader log stream after startup error", exc_info=True)
            stop_loader_chain(procs)
            raise
        _PROCESS_METADATA[proc] = {
            "streams": (stdout_f, stderr_f),
            "stdout_path": spec.stdout_path,
            "stderr_path": spec.stderr_path,
            "name": spec.name,
            "own_session": True,
        }
        procs.append(proc)
        _LIVE_LOADER_PIDS.add(proc.pid)
        time.sleep(start_gap_sec)
        _raise_if_loader_exited(proc, spec.name, procs)
        if _looks_like_arbiter(spec):
            _wait_for_paths(arbiter_ready_paths, arbiter_ready_timeout_sec, spec.name, procs)

    if procs:
        time.sleep(float(io_ext.get("loader_settle_sec", 2.0)))
        for proc in procs:
            _raise_if_loader_exited(proc, str(_process_metadata(proc).get("name") or "loader"), procs)
    return procs


def assert_loader_chain_alive(procs: Iterable[subprocess.Popen[str]]) -> None:
    proc_list = list(procs)
    for proc in proc_list:
        _raise_if_loader_exited(proc, str(_process_metadata(proc).get("name") or "loader"), proc_list)


def _looks_like_arbiter(spec: LoaderSpec) -> bool:
    if spec.scope in _ARBITER_SCOPES:
        return True
    base = os.path.basename(spec.path)
    name = spec.name.lower()
    return name.startswith("arb") or base.startswith("loader_arb") or base.startswith("loader_ar_")


def _bpffs_path_ready(path: str) -> bool:
    expanded = expand_path(path) or path
    if os.path.exists(expanded):
        return True
    # BPFFS pins are root-owned (mode 700 mount); unprivileged stat() fails.
    if expanded.startswith("/sys/fs/bpf/"):
        result = subprocess.run(
            [SUDO_BIN, "test", "-e", expanded],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    return False


def _wait_for_paths(paths: Iterable[str], timeout_sec: float, loader_name: str, procs: List[subprocess.Popen[str]]) -> None:
    wanted = [expand_path(path) or path for path in paths if str(path).strip()]
    if not wanted:
        return
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if all(_bpffs_path_ready(path) for path in wanted):
            return
        for proc in procs:
            _raise_if_loader_exited(proc, str(_process_metadata(proc).get("name") or "loader"), procs)
        time.sleep(0.1)
    details = []
    for proc in procs:
        process_metadata = _process_metadata(proc)
        stdout_path = process_metadata.get("stdout_path")
        stderr_path = process_metadata.get("stderr_path")
        details.append(
            {
                "name": process_metadata.get("name") or "loader",
                "pid": proc.pid,
                "returncode": proc.poll(),
                "stdout": stdout_path,
                "stderr": stderr_path,
                "stdout_tail": _file_tail(stdout_path),
                "stderr_tail": _file_tail(stderr_path),
            }
        )
    stop_loader_chain(procs)
    raise RuntimeError(
        f"io-ext arbiter {loader_name} did not signal attach-ready (required paths missing): {wanted}; "
        f"loader_details={details!r}"
    )


def _raise_if_loader_exited(
    proc: subprocess.Popen[str], loader_name: str, procs: List[subprocess.Popen[str]]
) -> None:
    rc = proc.poll()
    if rc is None:
        return
    process_metadata = _process_metadata(proc)
    stdout_path = process_metadata.get("stdout_path")
    stderr_path = process_metadata.get("stderr_path")
    stdout_tail = _file_tail(stdout_path)
    stderr_tail = _file_tail(stderr_path)
    stop_loader_chain(procs)
    raise RuntimeError(
        f"io-ext loader {loader_name} exited during startup with returncode={rc}; "
        f"stdout={stdout_path} stderr={stderr_path}; "
        f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
    )


def stop_loader_chain(procs: Iterable[subprocess.Popen[str]], timeout_sec: float = 10.0) -> None:
    proc_list = list(procs)
    for proc in proc_list:
        if proc.poll() is None:
            signal_process(proc, signal.SIGINT)
    deadline = time.time() + timeout_sec
    for proc in proc_list:
        remaining = max(0.1, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_process(proc, timeout_sec=2.0)
        _LIVE_LOADER_PIDS.discard(proc.pid)
        for stream in _process_metadata(proc).get("streams", ()):
            try:
                if hasattr(stream, "close"):
                    stream.close()
            except AttributeError:
                pass


def popen_in_own_session(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a process in its own session so cleanup can signal the group."""

    proc = subprocess.Popen(command, start_new_session=True, **kwargs)
    _PROCESS_METADATA[proc] = {"own_session": True}
    return proc


def popen_in_own_process_group(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a process group while preserving the caller's controlling TTY.

    Long-lived io-ext loaders are launched through sudo.  Using setsid via
    start_new_session=True detaches sudo from the tty that holds its timestamp,
    so sudo can block on an invisible password prompt before the loader creates
    BPFFS pins.  A separate process group is enough for cleanup and keeps sudo
    in the same tty session.
    """

    try:
        proc = subprocess.Popen(command, process_group=0, **kwargs)
    except TypeError:
        proc = subprocess.Popen(command, preexec_fn=os.setpgrp, **kwargs)
    _PROCESS_METADATA[proc] = {"own_session": True}
    return proc


def _env_prefix(env: Optional[Mapping[str, Any]]) -> str:
    if not env:
        return ""
    parts = []
    for key, value in env.items():
        if value is None:
            continue
        parts.append(f"{shlex.quote(str(key))}={shlex.quote(str(value))}")
    return "env " + " ".join(parts) + " " if parts else ""


def command_to_shell(command: Command) -> str:
    if isinstance(command, str):
        return command
    return shlex.join([str(part) for part in command])


def wrap_command_in_cgroup(
    command: Command, cgroup_path: Optional[str], env: Optional[Mapping[str, Any]] = None, use_sudo: bool = True
) -> Command:
    """Return a command that runs inside cgroup_path via $BASHPID + exec.

    If cgroup_path is unset the original command is returned, with environment
    variables prefixed for string/list commands.  When cgroup_path is set the
    command is executed by a root shell so it can write cgroup.procs.
    """

    if not cgroup_path:
        if not env:
            return command
        shell_cmd = _env_prefix(env) + command_to_shell(command)
        return [BASH_BIN, "-lc", shell_cmd]

    cg_path = _safe_cgroup_path(cgroup_path)
    inner: Command = command
    if isinstance(command, Sequence) and not isinstance(command, str):
        parts = [str(part) for part in command]
        if use_sudo and parts and parts[0] in {"sudo", SUDO_BIN}:
            parts = parts[1:]
        inner = parts
    inner_cmd = _env_prefix(env) + command_to_shell(inner)
    script = "\n".join(
        [
            "set -e",
            "mountpoint -q /sys/fs/cgroup || mount -t cgroup2 none /sys/fs/cgroup || true",
            "printf '%s\\n' '+io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true",
            f"mkdir -p {shlex.quote(cg_path)}",
            f"( printf '%s\\n' \"$BASHPID\" > {shlex.quote(cg_path)}/cgroup.procs; exec {inner_cmd} )",
        ]
    )
    return [SUDO_BIN, BASH_BIN, "-lc", script] if use_sudo else [BASH_BIN, "-lc", script]


def signal_process(proc: Optional[subprocess.Popen[Any]], sig: signal.Signals) -> None:
    if not proc or proc.poll() is not None:
        return
    if _process_metadata(proc).get("own_session", False):
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        result = subprocess.run(
            [SUDO_BIN, "-n", KILL_BIN, f"-{int(sig)}", "--", f"-{pgid}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        return


def loader_log_paths(procs: Iterable[subprocess.Popen[str]]) -> List[Dict[str, Optional[str]]]:
    logs: List[Dict[str, Optional[str]]] = []
    for proc in procs:
        logs.append(
            {
                "name": _process_metadata(proc).get("name"),
                "stdout": _process_metadata(proc).get("stdout_path"),
                "stderr": _process_metadata(proc).get("stderr_path"),
            }
        )
    return logs


def terminate_process(proc: Optional[subprocess.Popen[Any]], timeout_sec: float = 5.0) -> None:
    if not proc or proc.poll() is not None:
        return
    signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        signal_process(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            logger.warning("process group for pid %s did not exit after SIGKILL", proc.pid)


def interrupt_process(proc: Optional[subprocess.Popen[Any]], timeout_sec: float = 10.0) -> None:
    if not proc or proc.poll() is not None:
        return
    signal_process(proc, signal.SIGINT)
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        terminate_process(proc, timeout_sec=2.0)


def write_json_stat(output_dir: str, prefix: str, scene_id: str, data: Mapping[str, Any]) -> str:
    ensure_output_dir(output_dir)
    path = os.path.join(output_dir, f"{prefix}_{scene_id}_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    return path


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_output_paths(path: Optional[str], patterns: Optional[Iterable[str]] = None) -> List[str]:
    if not path:
        return []
    expanded = expand_path(path) or path
    if os.path.isfile(expanded):
        return [expanded]
    if not os.path.isdir(expanded):
        return []
    result: List[str] = []
    for pattern in patterns or ["**/*"]:
        result.extend(p for p in glob.glob(os.path.join(expanded, pattern), recursive=True) if os.path.isfile(p))
    return sorted(set(result))


def read_cgroup_memory_snapshot(cgroup_path: Optional[str]) -> Dict[str, Any]:
    """Read cgroup v2 memory/swap counters for benchmark evidence."""

    if not cgroup_path:
        return {"cgroup_path": cgroup_path, "exists": False, "error": "cgroup_path is empty"}
    cg_path = _safe_cgroup_path(cgroup_path)
    snapshot: Dict[str, Any] = {
        "cgroup_path": cg_path,
        "exists": os.path.isdir(cg_path),
        "timestamp": time.time(),
    }
    if not snapshot["exists"]:
        return snapshot

    scalar_files = {
        "memory_current": "memory.current",
        "memory_max": "memory.max",
        "memory_peak": "memory.peak",
        "memory_swap_current": "memory.swap.current",
        "memory_swap_max": "memory.swap.max",
        "memory_swap_peak": "memory.swap.peak",
    }
    for key, filename in scalar_files.items():
        value = _read_cgroup_text(os.path.join(cg_path, filename))
        if value is not None:
            snapshot[key] = _parse_cgroup_scalar(value)

    for key, filename in {
        "memory_events": "memory.events",
        "memory_swap_events": "memory.swap.events",
        "memory_stat": "memory.stat",
        "memory_pressure": "memory.pressure",
    }.items():
        value = _read_cgroup_text(os.path.join(cg_path, filename))
        if value is not None:
            snapshot[key] = _parse_cgroup_kv(value)
    return snapshot


def _read_cgroup_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _parse_cgroup_scalar(value: str) -> Any:
    value = value.strip()
    if value == "max":
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _parse_cgroup_kv(text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        values = parts[1:]
        if len(values) == 1:
            parsed[key] = _parse_cgroup_scalar(values[0])
        else:
            parsed[key] = values
    return parsed


def fio_agg_bw_mbps(fio_json_path: str) -> Optional[float]:
    metrics = fio_json_metrics(fio_json_path)
    value = metrics.get("agg_bw_mbps")
    return float(value) if isinstance(value, (int, float)) else None


def fio_json_metrics(fio_json_path: str) -> Dict[str, Any]:
    """Parse low-overhead FIO JSON metrics used by paper experiments.

    FIO already records bandwidth, IOPS, completion-latency percentiles and
    per-job CPU utilization in its own accounting path.  Reading those fields
    from the final JSON file avoids adding per-I/O tracing overhead to the
    workload scene.  ``cpu_cycles_per_io`` is an estimate derived from FIO's
    ``usr_cpu`` + ``sys_cpu`` percentage and the current CPU MHz; real hardware
    PMU cycles can be added later via an explicit perf monitor if the VM exposes
    CAP_PERFMON.
    """

    data = read_json(fio_json_path)
    if not isinstance(data, Mapping):
        return {}
    return fio_metrics_from_data(data)


def fio_metrics_from_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        return {}

    total_bytes_s = 0.0
    total_iops = 0.0
    total_ios = 0.0
    p99_ns_values: List[float] = []
    p999_ns_values: List[float] = []
    clat_mean_weighted_ns = 0.0
    clat_mean_weight = 0.0
    cpu_percent_total = 0.0
    cpu_percent_seen = False
    runtime_ms = 0.0
    context_switches_total = 0.0
    context_switches_seen = False
    direction_bytes_s: Dict[str, float] = {"read": 0.0, "write": 0.0, "trim": 0.0}
    aggregate_percentiles_us: Dict[str, float] = {}
    per_job_metrics: List[Dict[str, Any]] = []

    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        job_runtime_ms = _float_or_none(job.get("job_runtime"))
        if job_runtime_ms is not None:
            runtime_ms = max(runtime_ms, job_runtime_ms)

        user_cpu = _float_or_none(job.get("usr_cpu"))
        sys_cpu = _float_or_none(job.get("sys_cpu"))
        if user_cpu is not None or sys_cpu is not None:
            cpu_percent_total += float(user_cpu or 0.0) + float(sys_cpu or 0.0)
            cpu_percent_seen = True

        context_switches = _float_or_none(job.get("ctx"))
        if context_switches is not None:
            context_switches_total += context_switches
            context_switches_seen = True

        job_metrics: Dict[str, Any] = {"jobname": str(job.get("jobname") or "")}
        job_directions: Dict[str, Dict[str, Any]] = {}

        for rw in ("read", "write", "trim"):
            section = job.get(rw)
            if not isinstance(section, Mapping):
                continue
            if section.get("bw_bytes") is not None:
                section_bytes_s = _float_or_none(section.get("bw_bytes")) or 0.0
            elif section.get("bw") is not None:
                section_bytes_s = (_float_or_none(section.get("bw")) or 0.0) * 1024.0
            else:
                section_bytes_s = 0.0
            total_bytes_s += section_bytes_s
            direction_bytes_s[rw] += section_bytes_s

            iops = _float_or_none(section.get("iops"))
            if iops is not None:
                total_iops += iops
            ios = _float_or_none(section.get("total_ios"))
            if ios is not None:
                total_ios += ios

            direction_metrics: Dict[str, Any] = {
                "bw_mbps": section_bytes_s / (1024.0 * 1024.0),
                "iops": float(iops or 0.0),
                "total_ios": float(ios or 0.0),
            }
            percentiles_us = _fio_percentiles_us(section)
            if percentiles_us:
                direction_metrics["clat_percentiles_us"] = percentiles_us
                for percentile, value in percentiles_us.items():
                    aggregate_percentiles_us[percentile] = max(
                        value, aggregate_percentiles_us.get(percentile, value)
                    )
            job_directions[rw] = direction_metrics

            p99_ns = _fio_percentile_ns(section, 99.0)
            if p99_ns is not None:
                p99_ns_values.append(p99_ns)
            p999_ns = _fio_percentile_ns(section, 99.9)
            if p999_ns is not None:
                p999_ns_values.append(p999_ns)

            clat_ns = section.get("clat_ns")
            if isinstance(clat_ns, Mapping):
                mean_ns = _float_or_none(clat_ns.get("mean"))
                if mean_ns is not None:
                    weight = ios if ios and ios > 0 else 1.0
                    clat_mean_weighted_ns += mean_ns * weight
                    clat_mean_weight += weight

        job_metrics["directions"] = job_directions
        job_metrics["context_switches"] = float(context_switches or 0.0)
        per_job_metrics.append(job_metrics)

    metrics: Dict[str, Any] = {}
    if total_bytes_s > 0:
        metrics["agg_bw_mbps"] = total_bytes_s / (1024.0 * 1024.0)
    metrics["direction_bw_mbps"] = {
        direction: bytes_s / (1024.0 * 1024.0)
        for direction, bytes_s in direction_bytes_s.items()
    }
    metrics["per_job_metrics"] = per_job_metrics
    if aggregate_percentiles_us:
        metrics["clat_percentiles_us"] = aggregate_percentiles_us
    if total_iops > 0:
        metrics["iops"] = total_iops
        metrics["throughput_ops_per_sec"] = total_iops
    if total_ios > 0:
        metrics["total_ios"] = total_ios
    if context_switches_seen:
        metrics["context_switches_total"] = context_switches_total
        if total_ios > 0:
            metrics["context_switches_per_io"] = context_switches_total / total_ios
    if p99_ns_values:
        metrics["p99_latency_us"] = max(p99_ns_values) / 1000.0
    if p999_ns_values:
        p999_us = max(p999_ns_values) / 1000.0
        metrics["p999_latency_us"] = p999_us
        metrics["p99_9_latency_us"] = p999_us
    if clat_mean_weight > 0:
        metrics["avg_clat_us"] = (clat_mean_weighted_ns / clat_mean_weight) / 1000.0

    runtime_sec = runtime_ms / 1000.0 if runtime_ms > 0 else 0.0
    if runtime_sec <= 0 and total_iops > 0 and total_ios > 0:
        runtime_sec = total_ios / total_iops
    if cpu_percent_seen:
        metrics["cpu_percent_total"] = cpu_percent_total
    if cpu_percent_seen and runtime_sec > 0 and total_ios > 0:
        cpu_sec = runtime_sec * cpu_percent_total / 100.0
        metrics["cpu_sec"] = cpu_sec
        metrics["cpu_sec_per_io"] = cpu_sec / total_ios
        cpu_hz = cpu_hz_from_proc()
        if cpu_hz is not None:
            cycles = cpu_sec * cpu_hz
            metrics["cpu_cycles"] = cycles
            metrics["cpu_cycles_per_io"] = cycles / total_ios
            metrics["cpu_cycles_per_io_source"] = "fio_cpu_percent_times_proc_cpu_mhz"
    return metrics


def _fio_percentiles_us(section: Mapping[str, Any]) -> Dict[str, float]:
    for latency_key in ("clat_ns", "lat_ns"):
        latency = section.get(latency_key)
        if not isinstance(latency, Mapping):
            continue
        percentiles = latency.get("percentile")
        if not isinstance(percentiles, Mapping):
            continue
        parsed: Dict[str, float] = {}
        for key, value in percentiles.items():
            percentile = _float_or_none(key)
            latency_ns = _float_or_none(value)
            if percentile is None or latency_ns is None:
                continue
            parsed[f"{percentile:g}"] = latency_ns / 1000.0
        return parsed
    return {}


def _fio_percentile_ns(section: Mapping[str, Any], target_percentile: float) -> Optional[float]:
    for latency_key in ("clat_ns", "lat_ns"):
        latency = section.get(latency_key)
        if not isinstance(latency, Mapping):
            continue
        percentiles = latency.get("percentile")
        if not isinstance(percentiles, Mapping):
            continue
        for key, value in percentiles.items():
            percentile = _float_or_none(key)
            if percentile is None:
                continue
            if abs(percentile - target_percentile) <= 0.0001:
                parsed = _float_or_none(value)
                if parsed is not None:
                    return parsed
    return None


def cpu_hz_from_proc() -> Optional[float]:
    """Return the first CPU's current MHz as Hz, if procfs exposes it."""

    text = ""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        if line.lower().startswith("cpu mhz") and ":" in line:
            mhz = _float_or_none(line.split(":", 1)[1].strip())
            if mhz and mhz > 0:
                return mhz * 1_000_000.0
    return None


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_first_float(text: str, keys: Iterable[str]) -> Optional[float]:
    import re

    key_alt = "|".join(re.escape(key) for key in keys)
    match = re.search(rf"(?:{key_alt})[^0-9+-.]*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
