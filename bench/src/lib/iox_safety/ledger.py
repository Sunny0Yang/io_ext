"""io_ext 策略安全探索账本（safety exploration ledger）。

对 io_ext BPF 策略探索实验（virtme guest 内 attach/repl/new 策略）产生的
故障/拦截/恢复事件做分类记账。事件类别与计数规则见
``docs/experiments/iox_policy_safety_taxonomy.md``（本模块的规范来源）。

落盘结构（root 是实验数据根目录，campaign_id 是一次探索战役的命名空间）::

    <root>/<campaign_id>/
      attempts/<attempt_id>/manifest.json   # 不可覆盖（二次写入报 ManifestExistsError）
      attempts/<attempt_id>/final.json      # 结束状态 + outcome 分母字段（一次写入）
      events/<attempt_id>.jsonl             # flock 保护的追加式 JSONL（行级原子）
      HALT_<attempt_id>.flag                # KERNEL error/critical 触发的停机标记
      summary.json / summary.csv            # derive_summary() 只读派生

设计要点：

- manifest.json 用 ``O_CREAT | O_EXCL`` 创建，任何覆盖尝试直接报错；
  结束信息（结束时间、最终状态、分母 outcome）写在独立的 ``final.json``，
  同样只允许写一次。
- 事件追加使用行级原子写（循环 ``os.write`` 拼完整行）+ ``flock(LOCK_EX)``
  包住 open→write→fsync 全程，多进程并发追加不产生交叉坏行。
- ``derive_summary()`` 是只读派生：扫描全部 events/*.jsonl 与 attempts/ 重建
  汇总，禁止手改 summary 计数。
- 任何 KERNEL 事件 severity ∈ {error, critical}（或显式 ``panic=True``）时写
  ``HALT_<attempt_id>.flag``，供上层 runner 轮询停机。
- 观测缺失显式建模：无 evidence 的事件强制 ``confidence="unknown"``，汇总单独
  报 unknown 计数，不允许沉默被当作"无异常"。

Importable as ``lib.iox_safety.*`` when ``PYTHONPATH`` includes ``src``
(the ioext-bench convention).
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# 常量与 schema
# ---------------------------------------------------------------------------

#: 八类事件（taxonomy 文档 §1）。
EVENT_TYPES: tuple[str, ...] = (
    "BUILD",      # 编译/链接/skeleton 生成失败 —— 部署前拦截，不是运行时防护
    "VERIFY",     # BPF verifier 拒绝 —— 部署前拦截，不等于运行安全
    "ATTACH",     # verifier 通过后 attach 阶段的失败/成功边界
    "POLICY",     # 策略自身逻辑异常（QoS/统计偏离），不涉及内核完整性
    "LIFECYCLE",  # 尝试生命周期里程碑（build/load/attach/repl/detach/结束）
    "KERNEL",     # WARN/OOPS/BUG/panic/lockdep/RCU stall/lockup
    "RECOVERY",   # 恢复动作（回滚/兜底 detach/重启）
    "INFRA",      # 与策略无关的基础设施故障（VM/串口/心跳/磁盘）
)

#: severity 枚举。
SEVERITIES: tuple[str, ...] = ("info", "warning", "error", "critical")

#: confidence 枚举。
CONFIDENCES: tuple[str, ...] = ("observed", "inferred", "unknown")

#: recovery 字段允许的取值（None 表示尚无恢复动作）。
RECOVERY_STATES: tuple[str, ...] = ("attempted", "succeeded", "failed", "not_needed")

#: 触发 HALT 标记的 severity 集合。
HALT_SEVERITIES: frozenset[str] = frozenset({"error", "critical"})

#: KERNEL incident 去重的默认窗口（秒），见 taxonomy §3。
DEFAULT_INCIDENT_WINDOW_S = 60.0

#: final.json 的 outcome 分母字段（taxonomy §2 第 5 条）。
OUTCOME_FIELDS: tuple[str, ...] = (
    "build_passed",
    "verifier_passed",
    "attach_succeeded",
    "actually_ran",
    "hot_swaps",
    "run_seconds",
)

#: append_event 允许写入的事件字段（白名单，未知键报 SchemaError）。
EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "campaign_id",
    "attempt_id",
    "event_type",
    "severity",
    "name",
    "evidence",
    "detected_at",
    "monotonic",
    "recovery",
    "confidence",
    "panic",
    "notes",
)


class LedgerError(Exception):
    """账本操作基类错误。"""


class SchemaError(LedgerError):
    """事件/manifest/finalize 字段不符合 schema。"""


class ManifestExistsError(LedgerError):
    """manifest.json（或 final.json）已存在，拒绝覆盖。"""


class UnknownAttemptError(LedgerError):
    """attempt_id 不存在。"""


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_iso_utc(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field} must be an ISO-8601 UTC string, got {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SchemaError(f"{field} must carry a UTC timezone: {value!r}")
    return value


def _sha256_file(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def campaign_dir(root: str, campaign_id: str) -> str:
    return os.path.join(str(root), campaign_id)


def attempts_dir(root: str, campaign_id: str) -> str:
    return os.path.join(campaign_dir(root, campaign_id), "attempts")


def events_dir(root: str, campaign_id: str) -> str:
    return os.path.join(campaign_dir(root, campaign_id), "events")


def attempt_dir(root: str, campaign_id: str, attempt_id: str) -> str:
    return os.path.join(attempts_dir(root, campaign_id), attempt_id)


def events_path(root: str, campaign_id: str, attempt_id: str) -> str:
    return os.path.join(events_dir(root, campaign_id), f"{attempt_id}.jsonl")


def halt_flag_path(root: str, campaign_id: str, attempt_id: str) -> str:
    return os.path.join(campaign_dir(root, campaign_id), f"HALT_{attempt_id}.flag")


def list_attempt_ids(root: str, campaign_id: str) -> list[str]:
    base = attempts_dir(root, campaign_id)
    if not os.path.isdir(base):
        return []
    return sorted(
        entry
        for entry in os.listdir(base)
        if os.path.isfile(os.path.join(base, entry, "manifest.json"))
    )


def make_attempt_id(now: datetime | None = None) -> str:
    """UTC 时间戳 + 短随机后缀，例如 ``20261103T154512Z-a3f1b2``。"""
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _write_json_exclusive(path: str, payload: dict[str, Any]) -> None:
    """以 O_CREAT|O_EXCL 写一个 JSON 文件；已存在则抛 ManifestExistsError。"""
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ManifestExistsError(f"refusing to overwrite existing file: {path}") from exc
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# attempt 管理
# ---------------------------------------------------------------------------


def create_attempt(
    root: str,
    campaign_id: str,
    metadata: dict[str, Any] | None = None,
    attempt_id: str | None = None,
    source_paths: list[str] | None = None,
) -> dict[str, Any]:
    """创建 ``attempts/<attempt_id>/`` 与不可覆盖的 ``manifest.json``。

    ``metadata`` 中的已知键被提取为 manifest 顶层字段（schema）：

    - ``parent_candidate``: 本尝试基于的父候选策略标识；
    - ``kernel_hash`` / ``loader_hash``: 内核镜像与 loader 哈希。传文件路径则
      自动求 sha256 并记为 ``{"path":..., "sha256":...}``；传其它值原样记录；
    - ``build_command``: 构建命令（字符串或 argv 列表）；
    - ``workload_seed``: 工作负载随机种子；
    - ``source_paths``: 参与构建的源码/配置文件列表（自动求 sha256 记入
      ``source_hashes``；也可通过 ``source_paths`` 参数传入）；
    - 其余任意键原样保留在 ``metadata`` 子字典。

    ``started_at_utc`` 与 ``created_at_utc`` 在此写入；结束时间用
    :func:`finalize_attempt` 记录（manifest 不可覆盖）。

    返回 manifest 内容（dict）。attempt_id 缺省自动生成；重复创建同一
    attempt_id 抛 :class:`ManifestExistsError`。
    """
    metadata = dict(metadata or {})
    if attempt_id is None:
        attempt_id = make_attempt_id()
    base = attempt_dir(root, campaign_id, attempt_id)
    manifest_path = os.path.join(base, "manifest.json")
    if os.path.exists(manifest_path):
        raise ManifestExistsError(f"attempt already exists: {manifest_path}")
    os.makedirs(base, exist_ok=True)

    known = {
        "parent_candidate": metadata.pop("parent_candidate", None),
        "kernel_hash": metadata.pop("kernel_hash", None),
        "loader_hash": metadata.pop("loader_hash", None),
        "build_command": metadata.pop("build_command", None),
        "workload_seed": metadata.pop("workload_seed", None),
    }
    paths = list(source_paths or metadata.pop("source_paths", []) or [])
    for key in ("kernel_hash", "loader_hash"):
        value = known[key]
        if isinstance(value, str) and os.path.isfile(value):
            known[key] = {"path": value, "sha256": _sha256_file(value)}

    created_at = _utc_now_iso()
    manifest: dict[str, Any] = {
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "created_at_utc": created_at,
        "started_at_utc": created_at,
        "status": "open",
        "parent_candidate": known["parent_candidate"],
        "source_hashes": {path: _sha256_file(path) for path in paths},
        "kernel_hash": known["kernel_hash"],
        "loader_hash": known["loader_hash"],
        "build_command": known["build_command"],
        "workload_seed": known["workload_seed"],
        "metadata": metadata,
    }
    _write_json_exclusive(manifest_path, manifest)
    os.makedirs(events_dir(root, campaign_id), exist_ok=True)
    return manifest


def read_manifest(root: str, campaign_id: str, attempt_id: str) -> dict[str, Any]:
    path = os.path.join(attempt_dir(root, campaign_id, attempt_id), "manifest.json")
    if not os.path.isfile(path):
        raise UnknownAttemptError(f"no manifest for attempt {attempt_id!r} in campaign {campaign_id!r}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _require_attempt(root: str, campaign_id: str, attempt_id: str) -> None:
    if not os.path.isfile(os.path.join(attempt_dir(root, campaign_id, attempt_id), "manifest.json")):
        raise UnknownAttemptError(f"unknown attempt: {attempt_id!r}")


def finalize_attempt(
    root: str,
    campaign_id: str,
    attempt_id: str,
    status: str,
    outcome: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """记录尝试结束信息（``final.json``，一次写入，不可覆盖）。

    status 取 ``completed`` / ``failed`` / ``aborted`` / ``unknown``。
    ``outcome`` 只接受 :data:`OUTCOME_FIELDS` 中的分母字段（build_passed /
    verifier_passed / attach_succeeded / actually_ran / hot_swaps /
    run_seconds），这些字段构成 :func:`derive_summary` 的分母。
    """
    if status not in ("completed", "failed", "aborted", "unknown"):
        raise SchemaError(f"invalid finalize status: {status!r}")
    _require_attempt(root, campaign_id, attempt_id)
    bad = sorted(set(outcome or {}) - set(OUTCOME_FIELDS))
    if bad:
        raise SchemaError(f"unknown outcome fields: {bad}; allowed: {list(OUTCOME_FIELDS)}")
    payload = {
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "status": status,
        "finished_at_utc": _utc_now_iso(),
        "outcome": dict(outcome or {}),
        "notes": notes,
    }
    _write_json_exclusive(os.path.join(attempt_dir(root, campaign_id, attempt_id), "final.json"), payload)
    return payload


# ---------------------------------------------------------------------------
# 事件追加
# ---------------------------------------------------------------------------


def _validate_event(campaign_id: str, attempt_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """schema 校验并补默认值；返回可直接序列化的事件 dict。"""
    if not isinstance(event, dict):
        raise SchemaError("event must be a dict")
    unknown_fields = sorted(set(event) - set(EVENT_FIELDS))
    if unknown_fields:
        raise SchemaError(f"unknown event fields: {unknown_fields}; allowed: {list(EVENT_FIELDS)}")

    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        raise SchemaError(f"event_type must be one of {list(EVENT_TYPES)}, got {event_type!r}")

    severity = event.get("severity")
    if severity not in SEVERITIES:
        raise SchemaError(f"severity must be one of {list(SEVERITIES)}, got {severity!r}")

    evidence = event.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence):
        raise SchemaError("evidence must be a list of non-empty path strings")

    recovery = event.get("recovery")
    if recovery is not None and recovery not in RECOVERY_STATES:
        raise SchemaError(f"recovery must be None or one of {list(RECOVERY_STATES)}, got {recovery!r}")

    panic = event.get("panic", False)
    if not isinstance(panic, bool):
        raise SchemaError("panic must be a boolean")

    confidence = event.get("confidence")
    if confidence is None:
        # 无证据路径的事件不允许自称 observed（taxonomy §1 INFRA 规则）。
        confidence = "observed" if evidence else "unknown"
    if confidence not in CONFIDENCES:
        raise SchemaError(f"confidence must be one of {list(CONFIDENCES)}, got {confidence!r}")
    if confidence == "observed" and not evidence:
        raise SchemaError('confidence="observed" requires at least one evidence path')

    detected_at = _validate_iso_utc(event.get("detected_at") or _utc_now_iso(), "detected_at")

    monotonic = event.get("monotonic")
    if monotonic is None:
        monotonic = time.monotonic()
    if not isinstance(monotonic, (int, float)) or isinstance(monotonic, bool):
        raise SchemaError(f"monotonic must be a number, got {monotonic!r}")

    name = event.get("name", "")
    notes = event.get("notes", "")
    if not isinstance(name, str):
        raise SchemaError("name must be a string")
    if not isinstance(notes, str):
        raise SchemaError("notes must be a string")

    for key, value in (("campaign_id", campaign_id), ("attempt_id", attempt_id)):
        if event.get(key) not in (None, value):
            raise SchemaError(f"{key} mismatch: event carries {event[key]!r}, ledger target is {value!r}")

    return {
        "event_id": event.get("event_id") or uuid.uuid4().hex,
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "event_type": event_type,
        "severity": severity,
        "name": name,
        "evidence": list(evidence),
        "detected_at": detected_at,
        "monotonic": float(monotonic),
        "recovery": recovery,
        "confidence": confidence,
        "panic": panic,
        "notes": notes,
    }


def append_event(
    root: str,
    campaign_id: str,
    attempt_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    """追加一条事件到 ``events/<attempt_id>.jsonl``（flock + 行级原子写）。

    schema：``event_type`` 必属 :data:`EVENT_TYPES` 八类；``severity`` ∈
    info/warning/error/critical；``evidence`` 为非空字符串路径列表；
    ``detected_at`` ISO-8601 UTC（缺省取当前 UTC）；``monotonic`` 数值（缺省
    取当前）；``recovery`` ∈ None/attempted/succeeded/failed/not_needed；
    ``confidence`` ∈ observed/inferred/unknown（缺省 observed，但无 evidence
    时强制且默认 unknown；自称 observed 而无 evidence 报错）；``panic`` 布尔；
    ``notes`` 字符串。未知字段抛 :class:`SchemaError`。

    任何 KERNEL 事件 severity ∈ {error, critical}（或 ``panic=True``）时写
    HALT 停机标记。返回落盘后的完整事件 dict。
    """
    _require_attempt(root, campaign_id, attempt_id)
    record = _validate_event(campaign_id, attempt_id, event)
    path = events_path(root, campaign_id, attempt_id)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # flock 包住 open→write→fsync 全程；对同一 fd 加锁保证多进程并发追加时
    # 每条 JSON 行完整落盘，不产生交叉坏行。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            written = 0
            while written < len(line):
                written += os.write(fd, line[written:])
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    if record["event_type"] == "KERNEL" and (
        record["severity"] in HALT_SEVERITIES or record["panic"]
    ):
        write_halt_flag(root, campaign_id, attempt_id, record)
    return record


def write_halt_flag(
    root: str,
    campaign_id: str,
    attempt_id: str,
    event: dict[str, Any],
) -> str:
    """写入 ``HALT_<attempt_id>.flag`` 停机标记（内容为事件 id 与时间）。

    已存在时追加本次触发记录，保留首个触发时间。返回标记文件路径。上层
    runner 轮询 ``HALT_*.flag`` 决定停机。
    """
    path = halt_flag_path(root, campaign_id, attempt_id)
    entry = {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "severity": event.get("severity"),
        "panic": event.get("panic"),
        "detected_at": event.get("detected_at"),
        "written_at_utc": _utc_now_iso(),
    }
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return path


def read_events(root: str, campaign_id: str, attempt_id: str) -> tuple[list[dict[str, Any]], int]:
    """读取一个 attempt 的全部合法事件；返回 ``(events, malformed_line_count)``。"""
    path = events_path(root, campaign_id, attempt_id)
    events: list[dict[str, Any]] = []
    malformed = 0
    if not os.path.isfile(path):
        return events, malformed
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("not a JSON object")
            except ValueError:
                malformed += 1
                continue
            events.append(item)
    return events, malformed


# ---------------------------------------------------------------------------
# 派生汇总
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def _cluster_kernel_incidents(events: list[dict[str, Any]], window_s: float) -> list[dict[str, Any]]:
    """taxonomy §3：同 attempt 内 KERNEL 事件按 monotonic 链式聚类成 incident。"""
    kernels = sorted(
        (e for e in events if e.get("event_type") == "KERNEL"),
        key=lambda e: e.get("monotonic", 0.0),
    )
    incidents: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for item in kernels:
        if current and (item.get("monotonic", 0.0) - current[-1].get("monotonic", 0.0)) < window_s:
            current.append(item)
        else:
            if current:
                incidents.append(_incident_from(current))
            current = [item]
    if current:
        incidents.append(_incident_from(current))
    return incidents


def _incident_from(members: list[dict[str, Any]]) -> dict[str, Any]:
    max_severity = max(members, key=lambda e: _SEVERITY_RANK.get(e.get("severity"), -1)).get("severity")
    monotonics = [e.get("monotonic", 0.0) for e in members]
    return {
        "attempt_id": members[0].get("attempt_id"),
        "event_type": "KERNEL",
        "first_event_id": members[0].get("event_id"),
        "last_event_id": members[-1].get("event_id"),
        "member_event_ids": [e.get("event_id") for e in members],
        "member_count": len(members),
        "max_severity": max_severity,
        "names": sorted({e.get("name", "") for e in members if e.get("name")}),
        "started_monotonic": min(monotonics),
        "duration_s": round(max(monotonics) - min(monotonics), 6),
        "has_panic": any(e.get("panic") for e in members),
    }


def derive_summary(
    root: str,
    campaign_id: str,
    incident_window_s: float = DEFAULT_INCIDENT_WINDOW_S,
) -> dict[str, Any]:
    """扫描全部 events/*.jsonl 与 attempts/，重建 summary（只读派生）。

    覆盖写 ``summary.json`` 与 ``summary.csv`` —— 它们是派生物，任何时候以
    重新运行为准，禁止手改计数（taxonomy §2 第 7 条）。返回 summary dict。

    内容：按类别×severity 计数、尝试总数/完成数/HALT 数、KERNEL incident
    去重（``incident_window_s`` 窗口）、unknown 计数、各类别涉及的 attempt
    数、以及来自 final.json 的分母（build_passed / verifier_passed /
    attach_succeeded / actually_ran / hot_swaps / run_seconds）。
    """
    base = campaign_dir(root, campaign_id)
    attempt_ids = list_attempt_ids(root, campaign_id)

    counts: dict[str, dict[str, int]] = {et: {sev: 0 for sev in SEVERITIES} for et in EVENT_TYPES}
    totals: dict[str, int] = {et: 0 for et in EVENT_TYPES}
    unknown_counts: dict[str, int] = {et: 0 for et in EVENT_TYPES}
    attempts_affected: dict[str, set[str]] = {et: set() for et in EVENT_TYPES}
    per_attempt_event_counts: dict[str, int] = {attempt_id: 0 for attempt_id in attempt_ids}
    confidence_totals: dict[str, int] = {conf: 0 for conf in CONFIDENCES}

    all_incidents: list[dict[str, Any]] = []
    malformed_lines = 0
    total_events = 0

    for attempt_id in attempt_ids:
        events, bad = read_events(root, campaign_id, attempt_id)
        malformed_lines += bad
        total_events += len(events)
        per_attempt_event_counts[attempt_id] = len(events)
        for item in events:
            event_type = item.get("event_type")
            if event_type not in EVENT_TYPES:
                malformed_lines += 1
                continue
            severity = item.get("severity") if item.get("severity") in SEVERITIES else "unknown"
            counts[event_type][severity] = counts[event_type].get(severity, 0) + 1
            totals[event_type] += 1
            if item.get("confidence") == "unknown":
                unknown_counts[event_type] += 1
            confidence_totals[item.get("confidence", "unknown")] = (
                confidence_totals.get(item.get("confidence", "unknown"), 0) + 1
            )
            attempts_affected[event_type].add(item.get("attempt_id", attempt_id))
        all_incidents.extend(_cluster_kernel_incidents(events, incident_window_s))

    # attempts 完成状态与分母（outcome 由上层 runner 通过 finalize_attempt 写入）。
    attempts_completed = 0
    attempts_with_halt = 0
    denominators: dict[str, int | float] = {field: 0 for field in OUTCOME_FIELDS}
    finalize_statuses: dict[str, str] = {}
    for attempt_id in attempt_ids:
        final_path = os.path.join(attempt_dir(root, campaign_id, attempt_id), "final.json")
        if os.path.isfile(final_path):
            with open(final_path, encoding="utf-8") as handle:
                final = json.load(handle)
            status = final.get("status", "unknown")
            finalize_statuses[attempt_id] = status
            if status == "completed":
                attempts_completed += 1
            for field in OUTCOME_FIELDS:
                value = final.get("outcome", {}).get(field)
                if value is None:
                    continue
                if isinstance(value, bool):
                    if value:
                        denominators[field] += 1
                elif isinstance(value, (int, float)):
                    denominators[field] += value
        if os.path.isfile(halt_flag_path(root, campaign_id, attempt_id)):
            attempts_with_halt += 1

    summary: dict[str, Any] = {
        "campaign_id": campaign_id,
        "derived_at_utc": _utc_now_iso(),
        "incident_window_s": incident_window_s,
        "attempts": {
            "total": len(attempt_ids),
            "completed": attempts_completed,
            "with_events": sum(1 for n in per_attempt_event_counts.values() if n > 0),
            "with_halt_flag": attempts_with_halt,
        },
        "events": {
            "total": total_events,
            "malformed_lines": malformed_lines,
            "by_type_severity": counts,
            "totals_by_type": totals,
            "unknown_by_type": unknown_counts,
            "unknown_total": sum(unknown_counts.values()),
            "confidence_totals": confidence_totals,
            "attempts_affected_by_type": {k: sorted(v) for k, v in attempts_affected.items()},
            "per_attempt_event_counts": per_attempt_event_counts,
        },
        "incidents": {
            "total": len(all_incidents),
            "window_s": incident_window_s,
            "with_panic": sum(1 for i in all_incidents if i["has_panic"]),
            "list": all_incidents,
        },
        "denominators": denominators,
        "note": (
            "Derived output; do not hand-edit counts, rerun derive_summary() instead. "
            "BUILD/VERIFY rejections are pre-deployment gates, not runtime protection. "
            "VM isolation protects the host, not io_ext itself. Absence of KERNEL "
            "incidents is only claimable within the observation window covered by "
            "guest heartbeat + serial evidence; unknown-confidence periods stay unknown."
        ),
    }

    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    with open(os.path.join(base, "summary.csv"), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["event_type", "severity", "count", "attempts_affected", "unknown"])
        for event_type in EVENT_TYPES:
            for severity in SEVERITIES:
                writer.writerow(
                    [
                        event_type,
                        severity,
                        counts[event_type][severity],
                        len(attempts_affected[event_type]),
                        unknown_counts[event_type] if severity == SEVERITIES[0] else "",
                    ]
                )
        writer.writerow([])
        writer.writerow(["metric", "value"])
        writer.writerow(["attempts_total", len(attempt_ids)])
        writer.writerow(["attempts_completed", attempts_completed])
        writer.writerow(["attempts_with_halt_flag", attempts_with_halt])
        writer.writerow(["events_total", total_events])
        writer.writerow(["events_malformed_lines", malformed_lines])
        writer.writerow(["events_unknown_total", summary["events"]["unknown_total"]])
        writer.writerow(["kernel_incidents_total", len(all_incidents)])
        writer.writerow(["kernel_incidents_with_panic", summary["incidents"]["with_panic"]])
        for field in OUTCOME_FIELDS:
            writer.writerow([f"denominator_{field}", denominators[field]])

    return summary


__all__ = [
    "EVENT_TYPES",
    "SEVERITIES",
    "CONFIDENCES",
    "RECOVERY_STATES",
    "HALT_SEVERITIES",
    "DEFAULT_INCIDENT_WINDOW_S",
    "EVENT_FIELDS",
    "OUTCOME_FIELDS",
    "LedgerError",
    "SchemaError",
    "ManifestExistsError",
    "UnknownAttemptError",
    "make_attempt_id",
    "create_attempt",
    "read_manifest",
    "finalize_attempt",
    "append_event",
    "write_halt_flag",
    "read_events",
    "derive_summary",
    "list_attempt_ids",
    "campaign_dir",
    "attempt_dir",
    "events_path",
    "halt_flag_path",
]
