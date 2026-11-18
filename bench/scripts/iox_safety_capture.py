#!/usr/bin/env python3
"""Evidence-capture helpers for the io_ext policy exploration safety ledger.

Companion of ``src/lib/iox_safety/`` and
``docs/experiments/iox_policy_safety_taxonomy.md``.  This script only
captures evidence and generates parameters; it never starts a VM itself and
never runs kernel experiments.

Subcommands
-----------
run-captured     Run a command under a timeout, archiving stdout/stderr,
                 return code and wall/monotonic timestamps into the attempt
                 directory (``<attempt_dir>/evidence/<tag>.*``).
heartbeat        Guest-side helper: write a heartbeat JSON file (wall clock +
                 monotonic) every ``--interval-sec`` until SIGTERM/SIGINT, or
                 ``--count`` beats when given (useful for tests/smoke).
check-heartbeat  Host-side helper: report heartbeat staleness.  A missing
                 heartbeat file is reported as ``unknown`` — never as
                 "healthy" (absence of evidence is not evidence of health).
serial-args      Print the parameter additions that route guest console /
                 serial output into a host file.  Research summary (verified
                 against ``vng --version`` = virtme-ng 1.40 on this host):
                 vng has NO ``--serial`` option.  Options are:
                   (a) ``vng --qemu-opts "-serial file:PATH"`` plus
                       ``vng --append "console=ttyS0"`` — appends an extra
                       QEMU serial device backed by a host file; kernel
                       console output reaches it only if the kernel cmdline
                       directs console to ttyS0.
                   (b) ``vng --console [PORT]`` (VSOCK console server) and a
                       host-side ``socat`` proxy that tees the transcript to
                       a file.
                   (c) Bypass vng and run QEMU directly with
                       ``-serial file:PATH``.
                 If none of these is wired up, serial evidence is missing and
                 any "no panic" claim must be recorded as
                 ``confidence: "unknown"`` (see taxonomy §4).

All output written by this script is evidence, not verdicts: classification
(event_type/severity/confidence) is the caller's job via
``lib.iox_safety.append_event``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# run-captured
# ---------------------------------------------------------------------------


def cmd_run_captured(args: argparse.Namespace) -> int:
    """Run one command under a timeout and archive stdout/stderr/rc/timestamps."""
    attempt_dir = Path(args.attempt_dir)
    evidence = attempt_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    started_mono = time.monotonic()
    stdout_path = evidence / f"{args.tag}.stdout.log"
    stderr_path = evidence / f"{args.tag}.stderr.log"
    timed_out = False
    killed = False
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(args.command, stdout=out, stderr=err, start_new_session=True)
        try:
            rc: int | None = proc.wait(timeout=args.timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                killed = True
                rc = proc.wait(timeout=10)
    meta = {
        "tag": args.tag,
        "command": list(args.command),
        "started_at": started_wall,
        "started_at_utc": datetime.fromtimestamp(started_wall, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "started_monotonic": started_mono,
        "ended_at": time.time(),
        "ended_at_utc": _utc_now_iso(),
        "duration_sec": time.monotonic() - started_mono,
        "returncode": rc,
        "timed_out": timed_out,
        "killed_after_terminate": killed,
        "timeout_sec": args.timeout_sec,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "captured_at_utc": _utc_now_iso(),
    }
    meta_path = evidence / f"{args.tag}.meta.json"
    _write_once(meta_path, meta)
    print(json.dumps(meta, indent=2))
    return 124 if timed_out else 0


def _write_once(path: Path, payload: dict) -> None:
    """O_EXCL write; never overwrite existing evidence."""
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def _beat_payload(seq: int, start_mono: float, stopped: bool = False) -> dict:
    return {
        "seq": seq,
        "wall": time.time(),
        "wall_utc": _utc_now_iso(),
        "monotonic": time.monotonic() - start_mono,
        "hostname": os.uname().nodename,
        "stopped": stopped,
    }


def _write_beat(path: Path, payload: dict) -> None:
    """Atomic swap: readers never see a partial heartbeat file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload) + "\n")
    os.replace(tmp, path)


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Guest-side heartbeat writer: periodic wall+monotonic beats to a file.

    The file must live on a directory shared with the host (e.g. the virtme
    shared CWD) so the host-side ``check-heartbeat`` can stat it.
    """
    path = Path(args.heartbeat_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stop = {"flag": False}

    def _stop(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start = time.monotonic()
    seq = 0
    _write_beat(path, _beat_payload(seq, start))
    while not stop["flag"]:
        if args.count is not None and seq >= args.count - 1:
            break
        time.sleep(args.interval_sec)
        if stop["flag"]:
            break
        seq += 1
        _write_beat(path, _beat_payload(seq, start))
    _write_beat(path, _beat_payload(seq, start, stopped=True))
    return 0


def cmd_check_heartbeat(args: argparse.Namespace) -> int:
    """Host-side staleness check; missing file => unknown, never "healthy"."""
    path = Path(args.heartbeat_path)
    if not path.exists():
        result = {
            "status": "unknown",
            "reason": "no heartbeat file; absence of evidence is not evidence of health",
            "heartbeat_path": str(path),
            "checked_at_utc": _utc_now_iso(),
        }
        print(json.dumps(result))
        return 3
    age = time.time() - path.stat().st_mtime
    try:
        content = json.loads(path.read_text())
    except ValueError:
        content = path.read_text().strip()
    status = "stale" if age > args.max_age_sec else "alive"
    result = {
        "status": status,
        "age_sec": round(age, 3),
        "max_age_sec": args.max_age_sec,
        "content": content,
        "heartbeat_path": str(path),
        "checked_at_utc": _utc_now_iso(),
    }
    print(json.dumps(result))
    return 0 if status == "alive" else 4


# ---------------------------------------------------------------------------
# serial-args (research + parameter generation only; never starts a VM)
# ---------------------------------------------------------------------------


def cmd_serial_args(args: argparse.Namespace) -> int:
    qemu_serial = f"-serial file:{args.serial_log}"
    plans = {
        "a_vng_qemu_opts": {
            "description": (
                "Append an extra QEMU serial device backed by a host file. "
                "Kernel console output reaches it only if the guest kernel "
                "cmdline directs the console to ttyS0."
            ),
            "vng_args": [
                "--qemu-opts",
                qemu_serial,
                "--append",
                "console=ttyS0",
            ],
            "limits": (
                "vng also creates its own console/chardev devices; if the "
                "combination conflicts on your vng build, use plan b or c."
            ),
        },
        "b_vng_vsock_console": {
            "description": (
                "vng --console [PORT] exposes the guest console over VSOCK; "
                "proxy it to a host file, e.g.: socat "
                "'VSOCK-LISTEN:<port>,fork' - | tee <serial-log>"
            ),
            "vng_args": ["--console"],
            "limits": "Needs socat on the host; interactive transcript, not a raw ttyS0 device.",
        },
        "c_plain_qemu": {
            "description": (
                "Bypass vng and run QEMU directly with -serial file:PATH "
                "(plus -append 'console=ttyS0' when using a direct kernel "
                "boot). Full control, more setup."
            ),
            "qemu_args": [qemu_serial],
        },
    }
    result = {
        "verified_on": {
            "tool": "vng (virtme-ng)",
            "version": "1.40",
            "has_native_serial_option": False,
            "checked_how": "`vng --help` contains no --serial option; --qemu-opts, --append and --console [PORT] are available",
        },
        "serial_log": args.serial_log,
        "kernel_append": "console=ttyS0",
        "plans": plans,
        "fallback_rule": (
            "If none of these is wired up, serial evidence is MISSING: any "
            '"no panic" claim for the affected window must be recorded with '
            'confidence "unknown" (taxonomy §4), not as a clean result.'
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run-captured", help="run a command with timeout, archive evidence")
    p_run.add_argument("command", nargs="+")
    p_run.add_argument("--attempt-dir", required=True, help="target attempt directory")
    p_run.add_argument("--tag", required=True, help="evidence file tag, e.g. 'build' or 'attach'")
    p_run.add_argument("--timeout-sec", type=float, default=120.0)
    p_run.set_defaults(func=cmd_run_captured)

    p_hb = sub.add_parser("heartbeat", help="guest-side heartbeat writer")
    p_hb.add_argument("--heartbeat-path", required=True)
    p_hb.add_argument("--interval-sec", type=float, default=1.0)
    p_hb.add_argument(
        "--count",
        type=int,
        default=None,
        help="write this many beats then exit (default: run until SIGTERM/SIGINT)",
    )
    p_hb.set_defaults(func=cmd_heartbeat)

    p_chk = sub.add_parser("check-heartbeat", help="host-side heartbeat staleness check")
    p_chk.add_argument("--heartbeat-path", required=True)
    p_chk.add_argument("--max-age-sec", type=float, default=5.0)
    p_chk.set_defaults(func=cmd_check_heartbeat)

    p_ser = sub.add_parser("serial-args", help="print vng/QEMU serial-capture parameter plans")
    p_ser.add_argument("--serial-log", required=True, help="host path for the serial capture file")
    p_ser.set_defaults(func=cmd_serial_args)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
