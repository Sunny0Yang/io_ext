#!/usr/bin/env python3
"""q_iomax reference model — executable specification of the 4-channel token bucket.

Mirrors userspace/q_iomax.bpf.c (insert/cgroup_dispatch/evacuate hooks and
the q_iomax_cfg / q_iomax_bucket / q_iomax_queue_r / q_iomax_queue_w state),
the same way tests/q_ratelimit_model.py mirrors q_ratelimit.  Any change to
the BPF policy must be mirrored here and vice versa.

Model contract (identical to q_iomax.bpf.c, io.max semantics):
  - channels: riops/wiops/rbps/wbps per cgroup, each independent;
    rate 0 = unlimited ("max" in io.max): no refill, no clamp, never gates.
  - direction: opcode LSB parity with blk-throttle's bio_data_dir()
    (op_is_write): REQ_OP_READ -> read FIFO, WRITE/DISCARD/ZONE_APPEND/
    WRITE_ZEROES -> write FIFO; REQ_OP_FLUSH(2) is even -> read FIFO,
    exactly like blk-throttle charges it.
  - queues: one FIFO per direction (throtl_service_queue.queued[READ/WRITE]):
    a rate-limited read head does NOT block queued writes.
  - refill : per channel, tokens += dt * rate / 1e9 with the FRACTIONAL
    part carried across calls (blk-throttle carryover analogue) — a naive
    floor would starve a <333/s channel at the ~3ms blk-mq retry cadence;
    dt clamped at 1s (exact: the bucket refills to cap within 0.5s), then
    tokens clamped to cap = rate * 0.5s (loader floors iops caps at 1,
    bps caps at one 512B sector).
  - last_ns==0 means "bucket never touched": first touch sets last_ns=now
    with no retroactive refill; a lock-order inversion (now < last_ns)
    refills ZERO (no u64 underflow -> phantom refill, q_ratelimit bug).
  - dispatch: peek BOTH heads; a head is admissible when its own bps
    channel has tokens >= head bytes AND its own iops channel has
    tokens >= 1 (tg_dispatch_time parity: bps first then iops, AND);
    serve the preferred admissible direction round-robin, fall through
    to the other when the preferred is empty or blocked
    (work-conserving); pop and deduct the ACTUAL popped request from
    its own channels, floored at 0 (peek/pop straddle race).
  - insert : always accepts, into the direction FIFO; DRAINING defers to
    parent.
  - evacuate: DRAINING-gated — returns 0 while the policy is active
    (resists the non-strict generic-fallback steal, iosched_ext.c:3388),
    drains the read FIFO then the write FIFO (no token deduction) after
    the loader marks drain.
  - lost   : enqueued - dispatches - evac_pops, must be 0 at quiesce.
  - known boundary (cap = rate*0.5s convention, inherited from q_ratelimit):
    a bps channel whose rate*0.5s < the head request size starves that
    request size (io.max's slice accrual would admit one IO per
    size/rate seconds).  Keep rbps/wbps >= 2 * max request bytes.
"""
import sys

NS = 1
US = 1000
MS = 1000 * 1000
SEC = 1000 * 1000 * 1000
DT_CLAMP_NS = SEC           # Q_IOMAX_DT_CLAMP_NS
CAP_WINDOW_NS = SEC // 2    # burst cap = rate * 0.5s
SECTOR = 512                # Q_IOMAX_MIN_BURST_BYTES (loader cap floor)
MIB = 1048576

# opcode LSB encodes the data transfer direction (blk_types.h op_is_write)
REQ_OP_READ = 0
REQ_OP_WRITE = 1
REQ_OP_FLUSH = 2
REQ_OP_DISCARD = 3


def op_is_write(opf):
    """bio_data_dir parity: blk-throttle charges by the opcode LSB."""
    return bool(opf & 1)


def default_iops_cap(rate):
    """Loader convention: cap = rate * 0.5s floored at 1 IO when limited."""
    return max(rate // 2, 1) if rate else 0


def default_bps_cap(rate):
    """Loader convention: cap = rate * 0.5s floored at one 512B sector."""
    return max(rate // 2, SECTOR) if rate else 0


class Entry:
    def __init__(self, rq_ptr, nbytes):
        self.rq_ptr = rq_ptr
        self.bytes = nbytes


class Channel:
    """One token bucket channel (tokens + fractional carry)."""

    def __init__(self, rate, cap):
        self.rate = rate
        self.cap = cap
        self.tokens = 0
        self.carry = 0

    def refill(self, dt_ns):
        if not self.rate:
            return  # unlimited channel stays inert
        self.carry += dt_ns * self.rate
        whole = self.carry // SEC
        self.carry -= whole * SEC
        self.tokens += whole
        if self.cap and self.tokens > self.cap:
            self.tokens = self.cap

    def admit_one(self):
        return (not self.rate) or self.tokens >= 1

    def admit_bytes(self, nbytes):
        return (not self.rate) or self.tokens >= nbytes


class Bucket:
    """q_iomax per-cgroup state: 4 channels + 2 direction FIFOs."""

    def __init__(self, riops, wiops, rbps, wbps, caps=None):
        caps = caps or {}
        self.riops = Channel(riops, caps.get("riops", default_iops_cap(riops)))
        self.wiops = Channel(wiops, caps.get("wiops", default_iops_cap(wiops)))
        self.rbps = Channel(rbps, caps.get("rbps", default_bps_cap(rbps)))
        self.wbps = Channel(wbps, caps.get("wbps", default_bps_cap(wbps)))
        self.last_ns = None
        self.enqueued = 0
        self.dispatches = 0
        self.evac_pops = 0
        self.null_returns = 0
        self.push_fails = 0
        self.read_dispatches = 0
        self.write_dispatches = 0
        self.rr_turn = 0  # 0: try read first, 1: try write first
        self.queue_r = []
        self.queue_w = []

    @property
    def lost(self):
        return self.enqueued - self.dispatches - self.evac_pops

    def pending(self):
        return len(self.queue_r) + len(self.queue_w)


class Policy:
    """q_iomax instance: one 4-channel bucket set per cgroup."""

    def __init__(self, riops=0, wiops=0, rbps=0, wbps=0, caps=None):
        self.cfg = (riops, wiops, rbps, wbps, caps)
        self.buckets = {}
        self.draining = False

    def bucket(self, cgid):
        if cgid not in self.buckets:
            self.buckets[cgid] = Bucket(*self.cfg)
        return self.buckets[cgid]

    # --- struct_ops/insert_request ---
    def insert(self, cgid, rq_ptr, nbytes, opf=REQ_OP_READ):
        if self.draining:
            return "DEFER"
        b = self.bucket(cgid)
        if op_is_write(opf):
            b.queue_w.append(Entry(rq_ptr, nbytes))
        else:
            b.queue_r.append(Entry(rq_ptr, nbytes))
        b.enqueued += 1
        return "ACCEPT"

    # --- struct_ops/cgroup_dispatch ---
    def dispatch(self, cgid, now_ns):
        b = self.bucket(cgid)
        # refill under the bucket lock; `now` is sampled before the lock:
        # a lock-order inversion (now < last_ns) must refill ZERO, never
        # underflow (the q_ratelimit 20261029 smoke bug).
        if b.last_ns is None:
            b.last_ns = now_ns
        if now_ns > b.last_ns:
            dt = now_ns - b.last_ns
            if dt > DT_CLAMP_NS:
                dt = DT_CLAMP_NS
        else:
            dt = 0
        b.last_ns = now_ns
        for ch in (b.riops, b.wiops, b.rbps, b.wbps):
            ch.refill(dt)

        head_r = b.queue_r[0] if b.queue_r else None
        head_w = b.queue_w[0] if b.queue_w else None
        if head_r is None and head_w is None:
            return 0  # ordinary no-work NULL (not rate-limited)

        # admission = AND over the head's own two channels
        ok_r = head_r is not None and b.rbps.admit_bytes(head_r.bytes) \
            and b.riops.admit_one()
        ok_w = head_w is not None and b.wbps.admit_bytes(head_w.bytes) \
            and b.wiops.admit_one()
        if not ok_r and not ok_w:
            b.null_returns += 1
            return 0  # rate-limited NULL -> 3ms self-kick retry

        take_w = ok_w if b.rr_turn else not ok_r
        if take_w:
            popped = b.queue_w.pop(0)
            if b.wiops.tokens:
                b.wiops.tokens -= 1
            b.wbps.tokens = b.wbps.tokens - popped.bytes \
                if popped.bytes <= b.wbps.tokens else 0
            b.write_dispatches += 1
            b.rr_turn = 0
        else:
            popped = b.queue_r.pop(0)
            if b.riops.tokens:
                b.riops.tokens -= 1
            b.rbps.tokens = b.rbps.tokens - popped.bytes \
                if popped.bytes <= b.rbps.tokens else 0
            b.read_dispatches += 1
            b.rr_turn = 1
        b.dispatches += 1
        return popped.rq_ptr

    # --- struct_ops/evacuate (DRAINING-gated) ---
    def evacuate(self, cgid):
        if not self.draining:
            return 0  # active policy: resist generic-fallback steal
        b = self.bucket(cgid)
        if b.queue_r:
            popped = b.queue_r.pop(0)
        elif b.queue_w:
            popped = b.queue_w.pop(0)
        else:
            return 0
        b.evac_pops += 1
        return popped.rq_ptr

    def mark_draining(self):
        self.draining = True


# ----------------------------------------------------------------------
def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


def run_backlogged(pol, cgid, opf, nbytes, window_ns, tick=MS, backlog=256):
    """QD-saturated stream: keep >= backlog queued, dispatch per tick."""
    rid = 1
    done = 0
    for t in range(0, window_ns, tick):
        b = pol.bucket(cgid)
        while b.pending() < backlog:
            pol.insert(cgid, rid, nbytes, opf)
            rid += 1
        while pol.dispatch(cgid, t):
            done += 1
    return done


def test_steady_rate_4k():
    """Continuous QD-saturated 4K stream held at rate on either channel."""
    ok = True
    for name, kw, op in (("read", {"rbps": 64 * MIB}, REQ_OP_READ),
                         ("write", {"wbps": 64 * MIB}, REQ_OP_WRITE)):
        pol = Policy(**kw)
        cgid = 1
        done = run_backlogged(pol, cgid, op, 4096, 10 * SEC)
        bw = done * 4096 / 10.0
        err = abs(bw - 64 * MIB) / (64 * MIB)
        ok = check(f"steady_rate_4k_64MiB_{name}", err <= 0.02,
                   f"achieved={bw / MIB:.2f}MiB/s err={err:.4f}") and ok
    return ok


def test_low_rate_iops_carry():
    """riops=100 at the 3ms blk-mq retry cadence: exact 100 IOPS.

    The carry is load-bearing: the naive per-call floor
    dt*rate//1e9 == 0 at 3ms for rate=100 would starve the channel
    forever (this is why q_iomax_refill() carries the fraction).
    """
    rate = 100
    pol = Policy(riops=rate)
    cgid = 1
    done = run_backlogged(pol, cgid, REQ_OP_READ, 4096, 10 * SEC, tick=3 * MS)
    naive = sum((3 * MS) * rate // SEC
                for _ in range(10 * SEC // (3 * MS)))
    ok1 = check("low_rate_iops_carry_100iops", abs(done - 999) <= 20,
                f"done={done} expected~999")
    ok2 = check("naive_floor_refill_would_starve", naive == 0,
                f"naive={naive}")
    return ok1 and ok2


def test_iops_and_bps_and_gate():
    """Admission is the AND of the head's own channels: tighter one wins."""
    pol = Policy(riops=1000, rbps=8 * MIB)
    cgid = 1
    done_4k = run_backlogged(pol, cgid, REQ_OP_READ, 4096, 10 * SEC)
    # 1000 IOPS * 4K = 4MiB/s < 8MiB/s -> iops binds
    ok1 = check("and_gate_iops_binds_4k", abs(done_4k - 9999) <= 100,
                f"done={done_4k} expected~9999 (iops=1000/s)")

    pol = Policy(riops=1000, rbps=8 * MIB)
    done_64k = run_backlogged(pol, cgid, REQ_OP_READ, 64 * 1024, 10 * SEC)
    # 1000 IOPS * 64K = 64MiB/s > 8MiB/s -> bps binds (~128/s)
    ok2 = check("and_gate_bps_binds_64k", abs(done_64k - 1280) <= 30,
                f"done={done_64k} expected~1280 (8MiB/s / 64K)")
    return ok1 and ok2


def test_channel_independence():
    """A rate-limited read head must NOT block unlimited writes (io.max
    keeps one FIFO per direction), and writes must not charge read
    channels or vice versa."""
    # 1: blocked read head, 200 unlimited writes behind it all flow.
    pol = Policy(rbps=8 * MIB)
    cgid = 1
    pol.insert(cgid, 1, 4096, REQ_OP_READ)
    for rid in range(2, 202):
        pol.insert(cgid, rid, 4096, REQ_OP_WRITE)
    b = pol.bucket(cgid)
    while pol.dispatch(cgid, 0):
        pass
    ok1 = check("no_cross_direction_hol",
               b.write_dispatches == 200 and b.read_dispatches == 0
               and b.pending() == 1,
               f"wr={b.write_dispatches} rd={b.read_dispatches} "
               f"pending={b.pending()}")
    ok2 = check("writes_do_not_charge_read_channels",
                b.rbps.tokens == 0 and b.riops.tokens == 0,
                f"rbps_tok={b.rbps.tokens} riops_tok={b.riops.tokens}")
    rq = pol.dispatch(cgid, SEC)  # 1s -> 4096 read bytes accrued
    ok3 = check("blocked_read_released_after_refill",
                rq == 1 and b.pending() == 0 and b.read_dispatches == 1)

    # 2: unlimited reads do not consume limited write-channel tokens.
    pol = Policy(wiops=100)
    for rid in range(1, 301):
        pol.insert(cgid, rid, 4096, REQ_OP_READ)
    b = pol.bucket(cgid)
    reads_at_start = 0
    while pol.dispatch(cgid, 0):
        reads_at_start += 1
    ok4 = check("unlimited_reads_served_instantly",
                reads_at_start == 300 and b.wiops.tokens == 0,
                f"reads={reads_at_start} wiops_tok={b.wiops.tokens}")
    for rid in range(1, 301):
        pol.insert(cgid, rid, 4096, REQ_OP_WRITE)
    writes = 0
    for t in range(0, 3 * SEC, MS):
        while pol.dispatch(cgid, t):
            writes += 1
    ok5 = check("write_channel_independent_from_reads",
                abs(writes - 299) <= 10 and b.read_dispatches == 300,
                f"writes={writes} expected~299 (100 IOPS * 3s)")
    return ok1 and ok2 and ok3 and ok4 and ok5


def test_burst_clamp_half_second():
    """Idle bucket clamps the instantaneous burst to cap = rate*0.5s."""
    # bps channel: cap = 32MiB * 0.5s = 16MiB = 4096 * 4K exactly
    rate = 32 * MIB
    pol = Policy(rbps=rate)
    cgid = 1
    cap = default_bps_cap(rate)
    n = cap // 4096
    for rid in range(n + 1):
        pol.insert(cgid, rid + 1, 4096)  # rid 0 is falsy: start at 1
    pol.dispatch(cgid, 0)         # touch at t=0: dt=0, tokens stay 0
    rq = pol.dispatch(cgid, SEC)  # idle 1s: refill clamped to cap
    burst = 4096 if rq else 0
    while pol.dispatch(cgid, SEC):  # same instant: dt=0, tokens frozen
        burst += 4096
    ok1 = check("burst_clamp_bps_half_second",
                burst == cap and pol.bucket(cgid).pending() == 1,
                f"burst={burst} cap={cap}")

    # iops channel: cap = 100 * 0.5s = 50 IOs
    pol = Policy(riops=100)
    for rid in range(51):
        pol.insert(cgid, rid + 1, 4096)
    pol.dispatch(cgid, 0)
    rq = pol.dispatch(cgid, SEC)
    burst = 1 if rq else 0
    while pol.dispatch(cgid, SEC):
        burst += 1
    ok2 = check("burst_clamp_iops_half_second",
                burst == 50 and pol.bucket(cgid).pending() == 1,
                f"burst={burst} cap=50")

    # loader cap floors (a limited channel always has cap >= 1 IO / sector)
    ok3 = check("cap_floors",
                default_iops_cap(1) == 1 and default_bps_cap(100) == SECTOR
                and default_bps_cap(MIB) == MIB // 2,
                f"iops1={default_iops_cap(1)} bps100={default_bps_cap(100)}")
    return ok1 and ok2 and ok3


def test_dt_clamp_and_inversion():
    """dt > 1s refills exactly to cap; a lock-order inversion refills
    zero and must not phantom-accrue tokens OR carry."""
    rate = 64 * MIB
    pol = Policy(rbps=rate)
    cgid = 1
    b = pol.bucket(cgid)
    pol.dispatch(cgid, 0)          # touch at t=0 (empty queue)
    pol.dispatch(cgid, 120 * SEC)  # long idle: dt clamped to 1s
    cap = default_bps_cap(rate)
    ok1 = check("dt_clamp_refills_to_cap",
                b.rbps.tokens == cap and b.rbps.carry == 0,
                f"tokens={b.rbps.tokens} carry={b.rbps.carry} cap={cap}")
    tokens_before = b.rbps.tokens
    carry_before = b.rbps.carry
    pol.dispatch(cgid, 50 * MS)  # inversion: caller's now is in the past
    ok2 = check("inversion_refills_zero",
                b.rbps.tokens == tokens_before and b.rbps.carry == carry_before,
                f"tokens={b.rbps.tokens} carry={b.rbps.carry}")
    pol.insert(cgid, 1, 4096)
    rq = pol.dispatch(cgid, 150 * MS)  # normal refill after inversion
    ok3 = check("post_inversion_dispatch_ok", rq == 1)
    return ok1 and ok2 and ok3


def test_drain_gate_and_conservation():
    """Evacuate resists while active; drains after DRAINING; lost == 0."""
    pol = Policy(rbps=4 * MIB, wbps=4 * MIB)
    cgid = 1
    for rid in range(15):
        op = REQ_OP_READ if rid % 3 else REQ_OP_WRITE
        pol.insert(cgid, rid + 1, 4096, op)
    stolen = pol.evacuate(cgid)
    b = pol.bucket(cgid)
    ok1 = check("evacuate_resists_when_active",
                stolen == 0 and b.pending() == 15)
    pol.dispatch(cgid, 0)          # touch at t=0 (starved NULL)
    rq = pol.dispatch(cgid, 10 * SEC)
    ok2 = check("dispatch_works_while_active", rq == 2)
    pol.mark_draining()
    drained = []
    while True:
        rq = pol.evacuate(cgid)
        if not rq:
            break
        drained.append(rq)
    ok3 = check("drain_releases_all",
                len(drained) == 14 and b.pending() == 0)
    ok4 = check("accounting_conservation_lost_zero", b.lost == 0,
                f"enqueued={b.enqueued} dispatches={b.dispatches} "
                f"evac={b.evac_pops} lost={b.lost}")
    return ok1 and ok2 and ok3 and ok4


def test_null_returns_semantics():
    """null_returns counts only token-starved NULLs, not empty NULLs."""
    pol = Policy(rbps=8 * MIB)
    cgid = 1
    b = pol.bucket(cgid)
    rq = pol.dispatch(cgid, 0)
    ok1 = check("empty_null_not_counted",
                rq == 0 and b.null_returns == 0)
    pol.insert(cgid, 1, 4096)
    rq = pol.dispatch(cgid, 0)
    ok2 = check("starved_null_counted",
                rq == 0 and b.null_returns == 1)
    rq = pol.dispatch(cgid, 100 * MS)  # 8MiB * 0.1s = 838860 tokens
    ok3 = check("refilled_dispatch",
                rq == 1 and b.null_returns == 1)
    return ok1 and ok2 and ok3


def test_direction_classification():
    """Opcode-LSB direction parity with blk-throttle's bio_data_dir()."""
    ok1 = check("op_lsb_direction",
                not op_is_write(REQ_OP_READ) and op_is_write(REQ_OP_WRITE)
                and op_is_write(REQ_OP_DISCARD)
                and not op_is_write(REQ_OP_FLUSH),
                "READ->R WRITE->W DISCARD->W FLUSH->R (blk-throttle parity)")
    pol = Policy(rbps=4096)
    cgid = 1
    pol.insert(cgid, 1, 4096, REQ_OP_FLUSH)    # even opcode -> read FIFO
    pol.insert(cgid, 2, 4096, REQ_OP_DISCARD)  # odd opcode  -> write FIFO
    b = pol.bucket(cgid)
    ok2 = check("flush_routed_read_discard_routed_write",
                len(b.queue_r) == 1 and len(b.queue_w) == 1,
                f"qr={len(b.queue_r)} qw={len(b.queue_w)}")
    return ok1 and ok2


def test_rr_and_failopen():
    """All-unlimited policy: pure FIFO per direction + strict RR across
    the two backlogged directions (work-conserving fallback included)."""
    pol = Policy()  # all channels unlimited
    cgid = 1
    seq = []
    for rid in range(1, 4):
        pol.insert(cgid, rid * 10, 4096, REQ_OP_READ)     # R ids 10,20,30
        pol.insert(cgid, rid * 10 + 1, 4096, REQ_OP_WRITE)  # W ids 11,21,31
    b = pol.bucket(cgid)
    while True:
        rq = pol.dispatch(cgid, 0)
        if not rq:
            break
        seq.append("R" if rq % 10 == 0 else "W")
    ok1 = check("rr_alternates_when_both_backlogged",
                "".join(seq) == "RWRWRW", f"seq={''.join(seq)}")
    ok2 = check("failopen_all_dispatched",
                b.dispatches == 6 and b.pending() == 0
                and b.lost == 0,
                f"dispatches={b.dispatches}")
    return ok1 and ok2


def test_bytes_accounting():
    """A 64K read deducts exactly 64K bytes + 1 IO from its channels."""
    pol = Policy(riops=1000, rbps=8 * MIB)
    cgid = 1
    pol.insert(cgid, 1, 64 * 1024)
    pol.dispatch(cgid, 0)   # touch
    rq = pol.dispatch(cgid, SEC)  # refill to caps: 500 IOs / 4MiB
    b = pol.bucket(cgid)
    ok = check("bytes_and_iops_deducted_exactly",
               rq == 1 and b.riops.tokens == 499
               and b.rbps.tokens == 4 * MIB - 64 * 1024,
               f"rq={rq} riops_tok={b.riops.tokens} "
               f"rbps_tok={b.rbps.tokens}")
    return ok


def main():
    tests = [
        test_steady_rate_4k,
        test_low_rate_iops_carry,
        test_iops_and_bps_and_gate,
        test_channel_independence,
        test_burst_clamp_half_second,
        test_dt_clamp_and_inversion,
        test_drain_gate_and_conservation,
        test_null_returns_semantics,
        test_direction_classification,
        test_rr_and_failopen,
        test_bytes_accounting,
    ]
    ok = True
    for t in tests:
        ok = t() and ok
    print("MODEL " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
