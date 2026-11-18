#!/usr/bin/env python3
"""汇总 io_ycsb_suite 下各 job 的 metrics.json 为 Markdown 表格。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_metrics(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def collect_metrics(suite_root: Path, extra_dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_from(root: Path, prefix: str = "") -> None:
        if not root.is_dir():
            return
        for metrics in sorted(root.rglob("metrics.json")):
            job = metrics.parent.name
            key = f"{prefix}:{job}"
            if key in seen:
                continue
            data = load_metrics(metrics)
            if not data:
                continue
            seen.add(key)
            row = dict(data)
            row["job_id"] = prefix or job
            if prefix and job != prefix:
                row["job_id"] = f"{prefix}/{job}"
            row["_metrics_path"] = str(metrics)
            rows.append(row)

    for extra in extra_dirs:
        add_from(extra, extra.name)
    add_from(suite_root / "jobs")
    if not extra_dirs and (suite_root / "legacy_import").is_dir():
        add_from(suite_root / "legacy_import", "legacy_import")
    return rows


def fmt_tp(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return str(v)


def status_mark(s: str) -> str:
    return "PASS" if s == "pass" else "FAIL"


def render_stable_table(rows: list[dict[str, Any]]) -> str:
    stable = [r for r in rows if r.get("suite") == "stable"]
    stable.sort(key=lambda r: (r.get("scheduler_kind", ""), r.get("scheduler", ""), r.get("policy") or ""))

    lines = [
        "## Stable 吞吐量 (ops/s)",
        "",
        "| 类型 | 调度器/策略 | Load | Run | 状态 | Job |",
        "|------|-------------|------|-----|------|-----|",
    ]
    for r in stable:
        kind = r.get("scheduler_kind", "")
        if kind == "ioext":
            label = f"ioext:{r.get('policy', '')}"
        else:
            label = f"baseline:{r.get('scheduler', '')}"
        lines.append(
            f"| {kind} | {label} | {fmt_tp(r.get('load_throughput_ops_sec'))} | "
            f"{fmt_tp(r.get('throughput_ops_sec'))} | {status_mark(r.get('status', ''))} | "
            f"`{r.get('job_id', '')}` |"
        )
    return "\n".join(lines) + "\n"


def render_hot_table(rows: list[dict[str, Any]]) -> str:
    hot = [r for r in rows if r.get("suite") == "hot_switch"]
    hot.sort(key=lambda r: (r.get("tag", ""), r.get("from_policy", ""), r.get("to_policy", "")))

    lines = [
        "## Hot-switch 吞吐量 (ops/s)",
        "",
        "| 场景 | From | To | Run | 状态 | Job |",
        "|------|------|-----|-----|------|-----|",
    ]
    for r in hot:
        tag = r.get("tag", "")
        from_p = r.get("from_policy", "-")
        to_p = r.get("to_policy", "-")
        if tag == "full":
            from_p = "chain"
            to_p = "chain"
        elif tag.startswith("hot_into_q_none_"):
            from_p = tag.replace("hot_into_q_none_", "")
            to_p = "q_none"
        elif tag.startswith("hot_from_q_none_"):
            from_p = "q_none"
            to_p = tag.replace("hot_from_q_none_", "")
        lines.append(
            f"| {tag} | {from_p} | {to_p} | {fmt_tp(r.get('throughput_ops_sec'))} | "
            f"{status_mark(r.get('status', ''))} | `{r.get('job_id', '')}` |"
        )
    return "\n".join(lines) + "\n"


def render_q_none_focus(rows: list[dict[str, Any]]) -> str:
    hot = [r for r in rows if r.get("suite") == "hot_switch"]
    into_rows = [r for r in hot if (r.get("tag") or "").startswith("hot_into_q_none_")]
    from_rows = [r for r in hot if (r.get("tag") or "").startswith("hot_from_q_none_")]

    lines = [
        "## q_none 热切换专项",
        "",
        "### 其他策略 → q_none",
        "",
        "| From | Throughput | 状态 |",
        "|------|------------|------|",
    ]
    for r in sorted(into_rows, key=lambda x: x.get("tag", "")):
        frm = r.get("from_policy") or r.get("tag", "").replace("hot_into_q_none_", "")
        lines.append(
            f"| {frm} | {fmt_tp(r.get('throughput_ops_sec'))} | {status_mark(r.get('status', ''))} |"
        )

    lines += [
        "",
        "### q_none → 其他策略",
        "",
        "| To | Throughput | 状态 |",
        "|----|------------|------|",
    ]
    for r in sorted(from_rows, key=lambda x: x.get("tag", "")):
        to = r.get("to_policy") or r.get("tag", "").replace("hot_from_q_none_", "")
        lines.append(
            f"| {to} | {fmt_tp(r.get('throughput_ops_sec'))} | {status_mark(r.get('status', ''))} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-root", required=True, type=Path)
    ap.add_argument("--include-legacy", type=Path, default=None)
    args = ap.parse_args()

    extras = [args.include_legacy] if args.include_legacy else []
    rows = collect_metrics(args.suite_root, extras)

    out = [
        f"# IO YCSB 测试汇总",
        "",
        f"Suite: `{args.suite_root}`",
        f"Jobs: {len(rows)}",
        "",
        render_stable_table(rows),
        render_hot_table(rows),
        render_q_none_focus(rows),
    ]
    print("\n".join(out))


if __name__ == "__main__":
    main()
