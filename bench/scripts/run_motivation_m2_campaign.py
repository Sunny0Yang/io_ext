#!/usr/bin/env python3
"""Offline YCSB-A/FIO campaign on an explicitly authorized disposable test disk."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import signal
import subprocess
import time


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv):
    return command(argv, stdout=subprocess.PIPE).stdout.strip()


def snapshot(cgroup):
    return {name: (cgroup / name).read_text() for name in ("io.stat", "io.max", "cgroup.procs")
            if (cgroup / name).is_file()}


def ycsb_stats(text):
    values = {}
    for line in text.splitlines():
        match = re.match(r"\[([^]]+)\],\s*([^,]+),\s*([0-9.eE+-]+)\s*$", line)
        if match:
            values.setdefault(match[1], {})[match[2].strip()] = float(match[3])
    return values


def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="/dev/sdb")
    parser.add_argument("--expected-size", type=int, default=103079215104)
    parser.add_argument("--allow-format", action="store_true")
    parser.add_argument("--rates", default="0,32,64,128,max")
    parser.add_argument("--schedulers", default="none")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--fio-iodepth", type=int, default=16)
    parser.add_argument("--fio-numjobs", type=int, default=1)
    parser.add_argument(
        "--fio-profiles",
        default="",
        help="Comma-separated FIO shapes as iodepth x numjobs, e.g. 32x1,64x1,16x4",
    )
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.allow_format:
        parser.error("root and explicit --allow-format are required")
    dev = Path(args.device)
    if str(dev) != "/dev/sdb":
        parser.error("this kvmhost campaign is restricted to /dev/sdb")
    assert int(output(["blockdev", "--getsize64", str(dev)])) == args.expected_size
    assert not list(Path("/sys/class/block/sdb/holders").iterdir())
    assert not output(["findmnt", "-rn", "-T", "/", "-o", "SOURCE"]).startswith(str(dev))
    locks = []
    for name in ("paper_sdb_executor", "rig_iox_executor", "motivation_m2_executor"):
        handle = open(f"/tmp/{name}.lock", "a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(handle)
    for procname in ("fio", "db_bench", "java"):
        assert subprocess.run(["pgrep", "-x", procname], stdout=subprocess.DEVNULL).returncode == 1, procname
    args.output.mkdir(parents=True, exist_ok=False)
    bundle = args.bundle.resolve()
    java = bundle / "runtime/java-21-openjdk-amd64/bin/java"
    ycsb = bundle / "assets/ycsb/bin/ycsb"
    fio = bundle / "bin/fio"
    fio_prefix = [str(bundle / "lib/ld-linux-x86-64.so.2"), "--library-path", str(bundle / "lib"), str(fio)]
    assert java.is_file() and ycsb.is_file() and fio.is_file()
    env = dict(os.environ, JAVA_HOME=str(java.parent.parent), PATH=f"{java.parent}:/usr/bin:/bin:/usr/sbin:/sbin")
    mount = Path("/mnt/motivation_m2_offline")
    current = subprocess.run(["findmnt", "-rn", "-S", str(dev), "-o", "TARGET"], capture_output=True, text=True)
    # Only campaign-created mount points may be detached automatically.
    for path in current.stdout.splitlines():
        assert path.startswith("/mnt/motivation_"), path
    original_scheduler = re.search(r"\[([^]]+)\]", Path("/sys/block/sdb/queue/scheduler").read_text()).group(1)
    available = Path("/sys/block/sdb/queue/scheduler").read_text().replace("[", "").replace("]", "").split()
    schedulers = args.schedulers.split(",")
    assert set(schedulers) <= set(available)
    cgroups = [Path("/sys/fs/cgroup/motivation_m2_offline_" + role) for role in ("ycsb", "fio")]
    Path("/sys/fs/cgroup/cgroup.subtree_control").write_text("+io")
    for cg in cgroups:
        cg.mkdir(exist_ok=True)
        assert not (cg / "cgroup.procs").read_text().strip()
        assert all("=max" in field for line in (cg / "io.max").read_text().splitlines() for field in line.split()[1:])
    def parse_rate(token):
        token = token.strip().lower()
        if token == "max":
            return "max"
        return int(token)

    def parse_profile(token):
        token = token.strip().lower()
        if "x" not in token:
            parser.error(f"invalid FIO profile {token!r}; use iodepth x numjobs")
        depth_s, jobs_s = token.split("x", 1)
        depth, jobs = int(depth_s), int(jobs_s)
        assert depth > 0 and jobs > 0
        return depth, jobs

    rates = [parse_rate(r) for r in args.rates.split(",")]
    if args.fio_profiles:
        profiles = [parse_profile(p) for p in args.fio_profiles.split(",") if p.strip()]
    else:
        profiles = [(args.fio_iodepth, args.fio_numjobs)]
    cases = []
    for rep in range(1, args.repeats + 1):
        for scheduler in schedulers:
            for rate in rates:
                if not rate:
                    cases.append((scheduler, rate, None, rep))
                    continue
                for profile in profiles:
                    cases.append((scheduler, rate, profile, rep))
    random.Random(20261031).shuffle(cases)
    metadata = {"arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "host": output(["hostname"]), "kernel": output(["uname", "-r"]),
                "guest_time_utc": output(["date", "-u", "+%FT%TZ"]), "original_scheduler": original_scheduler,
                "available_schedulers": available, "case_order": cases,
                "java": command([str(java), "-version"], capture_output=True).stderr,
                "fio_version": output(fio_prefix + ["--version"]),
                "mount_before": current.stdout, "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "measurement": "whole YCSB run P99; FIO 1s bandwidth samples restricted to run wall-clock window; no cross-run pooled percentiles"}
    (args.output / "campaign.json").write_text(json.dumps(metadata, indent=2))
    try:
        for index, (scheduler, rate, profile, rep) in enumerate(cases):
            rate_label = "max" if rate == "max" else f"{rate}M"
            profile_label = ""
            if profile:
                profile_label = f"_qd{profile[0]}n{profile[1]}"
            case = args.output / f"{index:02d}_{scheduler}_{rate_label}{profile_label}_r{rep}"
            case.mkdir()
            offered = -1 if rate == "max" else rate
            result = {
                "scheduler": scheduler,
                "offered_mib_s": offered,
                "repeat": rep,
                "fio_iodepth": profile[0] if profile else None,
                "fio_numjobs": profile[1] if profile else None,
                "status": "running",
            }
            foreground = background = None
            print(f"START {case.name}", flush=True)
            try:
                command(["sync"])
                targets = subprocess.run(["findmnt", "-rn", "-S", str(dev), "-o", "TARGET"], capture_output=True, text=True)
                for target in targets.stdout.splitlines():
                    assert target.startswith("/mnt/motivation_"), target
                    command(["umount", target])
                with (case / "mkfs.log").open("w") as log:
                    command(["mkfs.ext4", "-F", "-E", "lazy_itable_init=0,lazy_journal_init=0", str(dev)], stdout=log, stderr=subprocess.STDOUT)
                mount.mkdir(exist_ok=True)
                command(["mount", "-o", "noatime,nodiratime", str(dev), str(mount)])
                Path("/sys/block/sdb/queue/scheduler").write_text(scheduler)
                result["mount"] = output(["findmnt", "-rn", "-T", str(mount), "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"])
                result["device_major_minor"] = output(["lsblk", "-dn", "-o", "MAJ:MIN", str(dev)])
                db = mount / "database"
                db.mkdir()
                fio_file = mount / "background.dat"
                # Allocate and initialize the background file before any measured application work.
                with (case / "prefill.log").open("w") as log:
                    command(fio_prefix + ["--name=prefill", f"--filename={fio_file}", "--rw=write", "--bs=1m", "--size=1g", "--direct=1", "--ioengine=libaio", "--iodepth=16"], stdout=log, stderr=subprocess.STDOUT)
                base = ["/usr/bin/python3", str(ycsb), "-jvm-args", "-Xms2048m -Xmx2048m"]
                props = ["rocksdb", "-s", "-P", str(bundle / "assets/ycsb/workloads/workloada"), "-threads", "16",
                         "-p", f"rocksdb.dir={db}", "-p", "recordcount=100000", "-p", "fieldcount=10", "-p", "fieldlength=1024"]
                def in_cgroup(argv, cg):
                    script = f'printf "%s\\n" "$BASHPID" > {shlex.quote(str(cg / "cgroup.procs"))}; exec ' + shlex.join(argv)
                    return ["/bin/bash", "-c", script]
                load_cmd = in_cgroup(base + ["load"] + props, cgroups[0])
                result["load_command"] = load_cmd
                with (case / "load.txt").open("w") as log:
                    command(load_cmd, env=env, cwd=bundle / "assets/ycsb", stdout=log, stderr=subprocess.STDOUT, timeout=120)
                load = ycsb_stats((case / "load.txt").read_text())
                assert load.get("INSERT", {}).get("Return=OK") == 100000, load
                result["before_run_io"] = [snapshot(cg) for cg in cgroups]
                if rate:
                    iodepth, numjobs = profile
                    fio_args = fio_prefix + ["--name=background", f"--filename={fio_file}", "--rw=randwrite", "--bs=4k", "--direct=1",
                        "--ioengine=libaio", f"--iodepth={iodepth}", f"--numjobs={numjobs}", "--size=1g", "--time_based=1",
                        f"--runtime={args.seconds + 45}", "--output-format=json",
                        f"--output={case / 'fio.json'}", f"--write_bw_log={case / 'background'}", "--log_avg_msec=1000", "--log_unix_epoch=1"]
                    if rate != "max":
                        fio_args.insert(-4, f"--rate={rate * 1024 * 1024}")
                    result["fio_rate_mode"] = "unlimited" if rate == "max" else "capped"
                    result["fio_command"] = fio_args
                    with (case / "fio.stderr").open("w") as log:
                        background = subprocess.Popen(in_cgroup(fio_args, cgroups[1]), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    time.sleep(5)
                    assert background.poll() is None, "FIO exited before foreground"
                result["foreground_start_epoch"] = time.time()
                result["foreground_start_monotonic"] = time.monotonic()
                run_cmd = in_cgroup(base + ["run"] + props + ["-p", "operationcount=100000000", "-p", f"maxexecutiontime={args.seconds}"], cgroups[0])
                result["run_command"] = run_cmd
                with (case / "run.txt").open("w") as log:
                    foreground = subprocess.Popen(run_cmd, env=env, cwd=bundle / "assets/ycsb", stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    foreground.wait(timeout=args.seconds + 45)
                result["foreground_end_epoch"] = time.time()
                result["foreground_end_monotonic"] = time.monotonic()
                assert foreground.returncode == 0, foreground.returncode
                stats = ycsb_stats((case / "run.txt").read_text())
                for op in ("READ", "UPDATE"):
                    assert stats.get(op, {}).get("Return=OK", 0) == stats.get(op, {}).get("Operations", -1), stats
                    assert stats[op]["Operations"] > 0
                result["application"] = stats
                result["after_run_io"] = [snapshot(cg) for cg in cgroups]
                if background:
                    assert background.poll() is None, "FIO ended before foreground; window invalid"
                    background.wait(timeout=60)
                    assert background.returncode == 0, background.returncode
                    fio_data = json.loads((case / "fio.json").read_text())
                    assert all(job["error"] == 0 for job in fio_data["jobs"])
                    begin = result["foreground_start_epoch"] * 1000
                    end = result["foreground_end_epoch"] * 1000
                    samples = []
                    for path in case.glob("background_bw*.log"):
                        for line in path.read_text().splitlines():
                            fields = [float(x.strip()) for x in line.split(",")]
                            # A complete 1s bucket must be contained inside the foreground window.
                            if fields[0] - 1000 >= begin and fields[0] <= end:
                                samples.append(fields[1] / 1024)
                    assert len(samples) >= args.seconds * .7, len(samples)
                    result["background_achieved_mib_s"] = sum(samples) / len(samples)
                    result["background_window_samples"] = len(samples)
                    result["background_window_samples_mib"] = list(samples)
                else:
                    result["background_achieved_mib_s"] = 0.0
                result["status"] = "success"
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = repr(exc)
                raise
            finally:
                stop(foreground)
                stop(background)
                (case / "result.json").write_text(json.dumps(result, indent=2))
                print(f"END {case.name} {result['status']}", flush=True)
    finally:
        Path("/sys/block/sdb/queue/scheduler").write_text(original_scheduler)
        command(["sync"])
        if subprocess.run(["mountpoint", "-q", str(mount)]).returncode == 0:
            command(["umount", str(mount)])


if __name__ == "__main__":
    main()
