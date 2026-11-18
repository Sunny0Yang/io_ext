#!/usr/bin/env bash
# Snapshot native blk-mq scheduler sysfs for D1 debug runs (kyber / mq-deadline / bfq).
set -euo pipefail

OUT_DIR="${1:?usage: dump_native_scheduler_stats.sh <output_dir> [sample_idx]}"
IDX="${2:-0}"
TARGET_DEVICE="${TARGET_DEVICE:-sda}"
STATS_DIR="$OUT_DIR/native_scheduler_stats"
mkdir -p "$STATS_DIR"

python3 - <<'PY' "$STATS_DIR/sample_${IDX}.json" "$TARGET_DEVICE"
import glob
import json
import os
import sys
import time
from pathlib import Path

out_path, device = sys.argv[1], sys.argv[2]
ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
tag = "ok"
reasons: list[str] = []

def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None

def read_int(path: str) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None

def active_scheduler(dev: str) -> str | None:
    sched = read_text(f"/sys/block/{dev}/queue/scheduler")
    if not sched:
        return None
    active = [part.strip("[]") for part in sched.split() if part.startswith("[")]
    return active[0] if active else sched

def collect_kyber(dev: str) -> dict:
    fields = {}
    for path in sorted(glob.glob(f"/sys/block/{dev}/mq/*/kyber_*")):
        name = Path(path).name
        text = read_text(path)
        if text is not None:
            fields[name] = text
    for path in sorted(glob.glob(f"/sys/block/{dev}/mq/*/domain*")):
        name = Path(path).name
        text = read_text(path)
        if text is not None:
            fields[name] = text
    return fields

def collect_mq_deadline(dev: str) -> dict:
    fields: dict = {}
    patterns = [
        f"/sys/block/{dev}/mq/*/deadline_*",
        f"/sys/block/{dev}/mq/*/iosched/*",
        f"/sys/block/{dev}/queue/iosched/*",
        f"/sys/kernel/debug/block/{dev}/sched/*",
        f"/sys/kernel/debug/block/{dev}/hctx*/sched/*",
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            name = str(Path(path).relative_to(Path(path).parts[0]))
            text = read_text(path)
            if text is not None:
                fields[name] = text
    return fields

def collect_bfq(dev: str) -> dict:
    fields = {}
    for path in sorted(glob.glob(f"/sys/block/{dev}/queue/iosched/*")):
        text = read_text(path)
        if text is not None:
            fields[Path(path).name] = text
    return fields

scheduler = active_scheduler(device)
payload: dict = {
    "timestamp": ts,
    "target_device": device,
    "scheduler": scheduler,
    "nr_requests": read_int(f"/sys/block/{device}/queue/nr_requests"),
    "read_ahead_kb": read_int(f"/sys/block/{device}/queue/read_ahead_kb"),
    "kyber": {},
    "mq_deadline": {},
    "bfq": {},
    "deadline_miss": {},
}

if scheduler == "kyber":
    payload["kyber"] = collect_kyber(device)
    if not payload["kyber"]:
        reasons.append("kyber_sysfs_missing")
elif scheduler == "mq-deadline":
    payload["mq_deadline"] = collect_mq_deadline(device)
    miss_keys = {k: v for k, v in payload["mq_deadline"].items()
                 if any(token in k.lower() for token in ("miss", "starved", "batch", "sorted"))}
    payload["deadline_miss"] = miss_keys
    if not payload["mq_deadline"]:
        reasons.append("mq_deadline_sysfs_missing")
elif scheduler == "bfq":
    payload["bfq"] = collect_bfq(device)
    if not payload["bfq"]:
        reasons.append("bfq_sysfs_missing")
elif scheduler in (None, "none"):
    reasons.append("scheduler_none_no_native_stats")
else:
    reasons.append(f"unsupported_scheduler:{scheduler}")

if reasons:
    tag = "native_stats_unavailable"
    payload["unavailable_reason"] = "; ".join(reasons)

payload["tag"] = tag
Path(out_path).write_text(json.dumps(payload, indent=2))
PY

echo "$STATS_DIR"
