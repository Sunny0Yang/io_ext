#!/usr/bin/env python3
# sc_injector_bm.py — S-CHAOS hot-switch / fault-injection driver (runs on kvmhost, root).
#
# Serially drives policy switches on one cgroup:
#   kill current loader (SIGTERM) -> observe detach completion -> spawn next.
# Detach completion is observed by TWO independent channels (deployed kernel
# 7.3.1-ioext3+ gates its "teardown complete" pr_info under IOEXT_DEBUG, so
# dmesg is NOT a per-event signal there):
#   1. kprobe tap file: events 'p:ioext_spre scx_io_policy_prepare_teardown' and
#      'p:ioext_sfree scx_io_policy_free' created by sc_run_chaos_bm.sh.
#      prepare->free span = kernel-side quiesce+drain duration (T_drain_kernel);
#      kill->free span = end-to-end switch latency (T_switch_e2e).
#   2. bpftool meta-map poll: entry for our cgroup id in the pinned
#      /sys/fs/bpf/ioext_policy_meta disappearing == policy torn down (the
#      stock wait_policy_quiesced uses the same condition).
# All timestamps: CLOCK_MONOTONIC + CLOCK_BOOTTIME (dmesg/tracing share boot clock).
import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time

KPROBE_RE = re.compile(r"\s(\d+\.\d+): ioext_(spre|sfree):")

def now_mono():
    return time.clock_gettime(time.CLOCK_MONOTONIC)

def now_boot():
    return time.clock_gettime(time.CLOCK_BOOTTIME)

BOOT_OFFSET = now_boot() - now_mono()   # constant offset, measured at start

def boot_of_mono(t_mono):
    return t_mono + BOOT_OFFSET

class KprobeTap:
    """Tail the kprobe trace_pipe capture file."""
    def __init__(self, path):
        self.path = path
        self.pos = 0

    def poll(self):
        """Return list of new (ts, which) events since last poll, in order.
        NO clock filtering: the tracing clock (sched_clock) may differ from
        CLOCK_BOOTTIME by a constant if the VM ever suspended — pairing is by
        order instead (the injector switches strictly serially)."""
        out = []
        try:
            with open(self.path, "r") as f:
                f.seek(self.pos)
                new = f.read()
                self.pos = f.tell()
        except FileNotFoundError:
            return out
        for line in new.splitlines():
            m = KPROBE_RE.search(line)
            if m:
                out.append((float(m.group(1)), m.group(2)))
        return out

class MetaPoller:
    """Poll the pinned per-cgroup policy meta map; entry gone == torn down."""
    def __init__(self, cgroup_path):
        self.cgid = os.stat(cgroup_path).st_ino   # cgroup v2 inode == cgroup id

    def entry_present(self):
        try:
            out = subprocess.run(
                ["bpftool", "map", "dump", "pinned",
                 "/sys/fs/bpf/ioext_policy_meta", "--json"],
                capture_output=True, timeout=2)
            if out.returncode != 0:
                return False    # map gone entirely -> nothing active
            entries = json.loads(out.stdout.decode() or "[]")
            for e in entries:
                key = e.get("key", [])
                if len(key) >= 8:
                    val = int.from_bytes(
                        bytes(int(b, 16) for b in key[:8]), "little")
                    if val == self.cgid:
                        return True
            return False
        except Exception:
            return None         # poll failure: treat as unknown

class Injector:
    def __init__(self, a):
        self.a = a
        self.tap = KprobeTap(a.kprobe_tap)
        self.meta = MetaPoller(a.cgroup)
        self.local_cycle = [s for s in a.local_cycle.split(",") if s]
        self.null_modes = [int(x) for x in a.null_modes.split(",") if x != ""]
        self.stall_list = [int(x) for x in a.stall_ms_list.split(",") if x != ""]
        self.arb_cycle = [s for s in a.arbiter_cycle.split(",") if s]
        self.local_idx = self.null_idx = self.stall_idx = self.arb_idx = 0
        self.ev_count = 0
        self.cur_local = None      # (name, proc)
        self.cur_arb = None

    def log(self, msg):
        line = "[%.6f] %s" % (now_mono(), msg)
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()

    def spawn(self, binary, extra_args, tag):
        path = os.path.join(self.a.bin_dir, binary)
        if not os.path.exists(path):
            self.log("FATAL: loader missing: %s" % path)
            sys.exit(3)
        proc = subprocess.Popen([path, "-c", self.a.cgroup] + extra_args,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        for _ in range(8):            # attach errors exit within ~40ms
            time.sleep(0.005)
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace")
                self.log("FATAL: %s exited rc=%s during attach: %s"
                         % (tag, proc.returncode, err.strip()[:500]))
                sys.exit(3)
        return proc

    def kill_and_wait(self, proc, name, budget_s):
        t_kill_mono = now_mono()
        t_kill_boot = boot_of_mono(t_kill_mono)
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = t_kill_mono + budget_s
        while now_mono() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.002)
        if proc.poll() is None:
            self.log("FATAL: loader %s did not exit in %.1fs -> SIGKILL + fail-stop"
                     % (name, budget_s))
            proc.kill()
            sys.exit(4)
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        return t_kill_mono, t_kill_boot, err

    def observe_detach(self, budget_s):
        """Join serial kprobe events by ORDER (the injector switches strictly
        serially and waits for meta-clean, so the next complete spre->sfree
        pair belongs to this event).  Timestamps stay in the tracing clock and
        are never compared against CLOCK_BOOTTIME — the VM may suspend, making
        the two clocks differ by a constant (suspend time); the pair itself is
        same-clock so T_drain is exact."""
        deadline = now_mono() + budget_s
        pre_ts, free_ts = None, None
        while now_mono() < deadline:
            for ts, which in self.tap.poll():
                if which == "spre" and pre_ts is None:
                    pre_ts = ts
                elif which == "sfree" and pre_ts is not None and free_ts is None:
                    free_ts = ts
                    break
            if free_ts is not None:
                break
            time.sleep(0.002)
        return pre_ts, free_ts

    def observe_meta_clean(self, budget_s):
        deadline = now_mono() + budget_s
        while now_mono() < deadline:
            present = self.meta.entry_present()
            if present is False:
                return now_boot()
            time.sleep(0.02)
        return None

    def record(self, kind, p_from, p_to, fault_param, t_kill_mono, t_kill_boot,
               t_exit_boot, pre_ts, free_ts, meta_clean_ts, note):
        # Decomposed, suspend-proof metrics:
        #   T_to_detach  = loader SIGTERM -> loader exit (boot clock, exact)
        #   T_drain      = kprobe prepare_teardown -> policy_free (tracing
        #                  clock, exact, same-clock pair)
        #   T_switch_e2e = T_to_detach + T_drain  (accurate: detach happens
        #                  just before loader exit, verified ~2ms earlier)
        to_detach_ms = ((t_exit_boot - t_kill_boot) * 1000.0
                        if t_exit_boot else None)
        drain_ms = (free_ts - pre_ts) * 1000.0 if (free_ts and pre_ts) else None
        e2e_ms = None
        if to_detach_ms is not None and drain_ms is not None:
            e2e_ms = to_detach_ms + drain_ms
        row = {
            "seq": self.ev_count,
            "kind": kind,
            "policy_from": p_from or "",
            "policy_to": p_to or "",
            "fault_param": fault_param or "",
            "ts_mono_issue": "%.6f" % t_kill_mono,
            "ts_boot_issue": "%.6f" % t_kill_boot,
            "ts_boot_loader_exit": ("%.6f" % t_exit_boot) if t_exit_boot else "",
            "ts_boot_pre": ("%.6f" % pre_ts) if pre_ts else "",
            "ts_boot_free": ("%.6f" % free_ts) if free_ts else "",
            "ts_boot_meta_clean": ("%.6f" % meta_clean_ts) if meta_clean_ts else "",
            "e2e_ms": ("%.3f" % e2e_ms) if e2e_ms is not None else "",
            "drain_kernel_ms": ("%.3f" % drain_ms) if drain_ms is not None else "",
            "note": (note or "").strip()[:400],
        }
        self.w.writerow(row)
        self.csvf.flush()
        self.log("ev#%d %s %s->%s %s e2e_ms=%s drain_ms=%s meta_clean=%s %s"
                 % (row["seq"], kind, p_from, p_to, fault_param,
                    row["e2e_ms"], row["drain_kernel_ms"],
                    "y" if meta_clean_ts else "n", row["note"][:60]))

    def switch_local(self, name, binary, extra_args, kind, fault_param):
        p_from = self.cur_local[0] if self.cur_local else None
        note = ""
        if self.cur_local:
            old_name = self.cur_local[0]
            budget_s = 15.0
            if fault_param and "stall_ms=" in fault_param:
                budget_s += 2.0 * int(fault_param.split("=")[1]) / 1000.0
            t_kill_mono, t_kill_boot, err = self.kill_and_wait(
                self.cur_local[1], old_name, budget_s)
            t_exit_boot = boot_of_mono(now_mono())
            pre_ts, free_ts = self.observe_detach(max(40.0, 5.0 + budget_s))
            meta_ts = self.observe_meta_clean(5.0)
            m = re.search(r"sc_chaos:.*", err)
            if m:
                note = m.group(0)
            self.record(kind, old_name, name, fault_param, t_kill_mono,
                        t_kill_boot, t_exit_boot, pre_ts, free_ts, meta_ts, note)
        proc = self.spawn(binary, extra_args, name)
        self.cur_local = (name, proc)

    def switch_arb(self, name):
        kind = "arbiter_switch" if self.cur_arb else "arbiter_attach"
        p_from = self.cur_arb if self.cur_arb else None
        if self.cur_arb:
            t_kill_mono, t_kill_boot, _ = self.kill_and_wait(self.cur_arb[1],
                                                             p_from, 15.0)
            t_exit_boot = boot_of_mono(now_mono())
            pre_ts, free_ts = self.observe_detach(40.0)
            self.record(kind, p_from, name, "arbiter", t_kill_mono, t_kill_boot,
                        t_exit_boot, pre_ts, free_ts, None, "")
        proc = self.spawn("loader_" + name, [], name)
        self.cur_arb = (name, proc)

    def run(self):
        self.csvf = open(self.a.out, "w", newline="")
        self.w = csv.DictWriter(self.csvf, fieldnames=[
            "seq", "kind", "policy_from", "policy_to", "fault_param",
            "ts_mono_issue", "ts_boot_issue", "ts_boot_loader_exit",
            "ts_boot_pre", "ts_boot_free", "ts_boot_meta_clean",
            "e2e_ms", "drain_kernel_ms", "note"])
        self.w.writeheader()
        self.logf = open(self.a.log, "w") if self.a.log else None

        meta = {"cmd": sys.argv, "boot_offset_mono_to_boot": BOOT_OFFSET,
                "start_mono": now_mono(), "start_boot": now_boot(),
                "cgroup_id": self.meta.cgid, "args": vars(self.a)}
        with open(self.a.out + ".meta.json", "w") as f:
            json.dump(meta, f, indent=1)

        self.log("injector start: cgroup=%s(cgid=%d) period=%dms duration=%ds"
                 % (self.a.cgroup, self.meta.cgid, self.a.period_ms,
                    self.a.duration_s))

        deadline = now_mono() + self.a.duration_s
        next_local_at = now_mono() + 0.5     # let fio warm up briefly
        next_arb_at = (now_mono() + 0.5) if self.a.arbiter_period_ms > 0 else None

        if self.a.arbiter:
            self.switch_arb(self.a.arbiter)

        while now_mono() < deadline:
            did = False
            if next_arb_at is not None and now_mono() >= next_arb_at:
                self.arb_idx += 1
                self.switch_arb(self.arb_cycle[self.arb_idx % len(self.arb_cycle)])
                next_arb_at = now_mono() + self.a.arbiter_period_ms / 1000.0
                did = True
            if now_mono() >= next_local_at:
                self.ev_count += 1
                do_null = (self.a.fault_null_every > 0 and
                           self.ev_count % self.a.fault_null_every == 0)
                do_stall = (self.a.fault_stall_every > 0 and
                            self.ev_count % self.a.fault_stall_every == 0)
                if do_null:
                    mode = self.null_modes[self.null_idx % len(self.null_modes)]
                    self.null_idx += 1
                    self.switch_local("q_inject_null", "loader_q_inject_null",
                                      ["--mode", str(mode)], "fault_null",
                                      "mode=%d" % mode)
                elif do_stall:
                    ms = self.stall_list[self.stall_idx % len(self.stall_list)]
                    self.stall_idx += 1
                    self.switch_local("q_inject_stall", "loader_q_inject_stall",
                                      ["--stall-ms", str(ms)], "fault_stall",
                                      "stall_ms=%d" % ms)
                else:
                    name = self.local_cycle[self.local_idx % len(self.local_cycle)]
                    self.local_idx += 1
                    self.switch_local(name, "loader_" + name, [], "local_switch", "")
                period = self.a.period_ms / 1000.0
                next_local_at = max(now_mono() + period, now_mono() + 0.01)
                did = True
            if not did:
                time.sleep(0.002)

        # final detach: kill current policy, spawn nothing (clean scene teardown)
        if self.cur_local:
            old_name = self.cur_local[0]
            t_kill_mono, t_kill_boot, err = self.kill_and_wait(
                self.cur_local[1], old_name, 15.0)
            t_exit_boot = boot_of_mono(now_mono())
            pre_ts, free_ts = self.observe_detach(40.0)
            meta_ts = self.observe_meta_clean(5.0)
            self.record("final_detach", old_name, "", "", t_kill_mono,
                        t_kill_boot, t_exit_boot, pre_ts, free_ts, meta_ts, "")
        if self.cur_arb:
            try:
                subprocess.run(["pkill", "-TERM", "-x",
                                "loader_" + self.cur_arb[0]],
                               capture_output=True, timeout=5)
                self.record("final_detach", self.cur_arb[0], "", "arbiter",
                            now_mono(), now_boot(), boot_of_mono(now_mono()),
                            None, None, None, "arbiter final detach")
            except Exception as e:
                self.log("WARN: arbiter final detach failed: %s" % e)
        self.log("injector done: %d local events" % self.ev_count)
        self.csvf.close()
        if self.logf:
            self.logf.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cgroup", required=True)
    p.add_argument("--bin-dir", default="/opt/safety_chaos/bin")
    p.add_argument("--kprobe-tap", default="/opt/safety_chaos/results/kprobe_tap.log")
    p.add_argument("--duration-s", type=float, required=True)
    p.add_argument("--period-ms", type=int, default=100)
    p.add_argument("--local-cycle", default="q_fifo,q_none,q_wedf")
    p.add_argument("--fault-null-every", type=int, default=0)
    p.add_argument("--null-modes", default="0,1,2,3")
    p.add_argument("--fault-stall-every", type=int, default=0)
    p.add_argument("--stall-ms-list", default="200,900")
    p.add_argument("--arbiter", default="")
    p.add_argument("--arbiter-cycle", default="")
    p.add_argument("--arbiter-period-ms", type=int, default=0)
    p.add_argument("--out", required=True)
    p.add_argument("--log", default="")
    a = p.parse_args()

    Injector(a).run()

if __name__ == "__main__":
    main()
