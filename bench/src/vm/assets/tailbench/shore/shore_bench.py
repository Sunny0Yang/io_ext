#!/usr/bin/env python3
"""Tiny TailBench/Shore-style VM smoke helper.

This is not a replacement for upstream Shore-MT. It is a deterministic bench
entrypoint for framework smoke tests: read an input file, optionally issue sync
filesystem I/O on a block-backed file, and emit TailBench-compatible lats.bin
triplets.
"""

from __future__ import annotations

import argparse
import os
import struct
import time
from pathlib import Path

IO_MODES = ("memory", "sync_read", "sync_write", "sync_rw")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a tiny Shore-style smoke bench")
    parser.add_argument("--data-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-reqs", type=int, default=int(os.environ.get("TBENCH_MAXREQS", "100")))
    parser.add_argument("--warmup-reqs", type=int, default=int(os.environ.get("TBENCH_WARMUPREQS", "10")))
    parser.add_argument("--qps", type=float, default=float(os.environ.get("TBENCH_QPS", "100")))
    parser.add_argument("--block-size", type=int, default=4096)
    parser.add_argument("--io-file", default=os.environ.get("TBENCH_IO_FILE"))
    parser.add_argument(
        "--io-mode",
        default=os.environ.get("TBENCH_IO_MODE", "memory"),
        choices=IO_MODES,
    )
    return parser.parse_args()


def _sync_block_io(io_path: Path, offset: int, block_size: int, mode: str) -> None:
    file_size = io_path.stat().st_size
    if file_size < block_size:
        raise RuntimeError(f"io file too small for block_size={block_size}: {io_path}")
    pos = offset % (file_size - block_size + 1)
    fd = os.open(str(io_path), os.O_RDWR)
    try:
        if mode in ("sync_read", "sync_rw"):
            os.pread(fd, block_size, pos)
        if mode in ("sync_write", "sync_rw"):
            os.pwrite(fd, b"\xab" * block_size, pos)
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    args = parse_args()
    data_path = Path(args.data_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = data_path.read_bytes()
    if not payload:
        raise RuntimeError(f"data file is empty: {data_path}")

    io_path = Path(args.io_file) if args.io_file else None
    io_mode = str(args.io_mode or "memory").strip().lower()
    if io_mode != "memory":
        if io_path is None:
            raise RuntimeError("--io-file is required when --io-mode is not memory")
        if not io_path.is_file():
            raise RuntimeError(f"io file does not exist: {io_path}")

    total_reqs = max(0, args.warmup_reqs) + max(1, args.max_reqs)
    sleep_sec = 0.0 if args.qps <= 0 else min(0.01, 1.0 / args.qps)
    samples: list[tuple[int, int, int]] = []
    checksum = 0
    cursor = 0

    for index in range(total_reqs):
        enqueue_ns = time.monotonic_ns()
        start_ns = time.monotonic_ns()
        if io_mode != "memory" and io_path is not None:
            _sync_block_io(io_path, cursor, args.block_size, io_mode)
            checksum ^= cursor & 0xFFFFFFFF
        else:
            offset = cursor % len(payload)
            chunk = payload[offset : offset + args.block_size]
            if not chunk:
                chunk = payload[: min(args.block_size, len(payload))]
            checksum ^= sum(chunk) & 0xFFFFFFFF
        cursor += args.block_size + 131
        service_ns = max(1, time.monotonic_ns() - start_ns)
        sojourn_ns = max(service_ns, time.monotonic_ns() - enqueue_ns)
        if index >= args.warmup_reqs:
            samples.append((0, service_ns, sojourn_ns))
        if sleep_sec:
            time.sleep(sleep_sec)

    lats_bin = output_dir / "lats.bin"
    with lats_bin.open("wb") as f:
        for queue_ns, service_ns, sojourn_ns in samples:
            f.write(struct.pack("<QQQ", queue_ns, service_ns, sojourn_ns))

    summary = output_dir / "shore_bench_summary.txt"
    summary.write_text(
        f"completed={len(samples)} warmup={args.warmup_reqs} io_mode={io_mode} "
        f"io_file={io_path or ''} checksum={checksum} lats_bin={lats_bin}\n",
        encoding="utf-8",
    )
    print(
        f"SHORE_BENCH_DONE completed={len(samples)} io_mode={io_mode} "
        f"lats_bin={lats_bin} checksum={checksum}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
