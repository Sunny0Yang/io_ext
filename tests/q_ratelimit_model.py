#!/usr/bin/env python3
"""q_ratelimit reference model — executable specification of the token bucket.

Mirrors userspace/q_ratelimit.bpf.c (insert/cgroup_dispatch/evacuate hooks
and the q_rl_cfg / q_rl_bucket / q_rl_queue state), the same way
tests/arb_wdrr_model.py mirrors the arb_wdrr DRR core.  Any change to the
BPF policy must be mirrored here and vice versa; the acceptance script
tests/q_ratelimit_accept.sh runs this self-test as its gate 0.

Model contract (identical to q_ratelimit.bpf.c):
  - refill : tokens += dt * rate (dt clamped at 1s — exact because the
             bucket refills to cap within 0.5s of idle), then clamped to
             cap = rate * 0.5s (simplified io.max burst window)
  - last_ns==0 means "bucket never touched": first touch sets last_ns=now
             with no retroactive refill
  - dispatch: peek FIFO head; tokens >= head.bytes -> pop and deduct the
             ACTUAL popped bytes (peek/pop straddle race), else NULL +
             null_returns++ (blk-mq 3ms self-kick retries)
  - insert : always accepts (push {rq_ptr, bytes}); DRAINING defers to parent
  - evacuate: DRAINING-gated — returns 0 while the policy is active (resists
             the non-strict generic-fallback steal, iosched_ext.c:3388),
             drains FIFO (no token deduction) after the loader marks drain
  - lost   : enqueued - dispatches - evac_pops, must be 0 at quiesce
  - deduction floors at 0 (bounded one-request overshoot per peek/pop race)
"""
import sys

NS = 1
US = 1000
MS = 1000 * 1000
SEC = 1000 * 1000 * 1000
DT_CLAMP_NS = SEC        # Q_RL_DT_CLAMP_NS
CAP_WINDOW_NS = SEC // 2  # burst cap = rate * 0.5s
MIB = 1048576


class Entry:
    def __init__(self, rq_ptr, nbytes):
        self.rq_ptr = rq_ptr
        self.bytes = nbytes


class Bucket:
    def __init__(self, rate_bytes_per_sec, cap_bytes=None):
        self.rate = rate_bytes_per_sec
        self.cap = cap_bytes if cap_bytes is not None \
            else rate_bytes_per_sec * CAP_WINDOW_NS // SEC
        self.tokens = 0
        self.last_ns = None
        self.enqueued = 0
        self.dispatches = 0
        self.evac_pops = 0
        self.null_returns = 0
        self.push_fails = 0
        self.queue = []

    # --- stats mirror of struct q_rl_bucket ---
    @property
    def lost(self):
        return self.enqueued - self.dispatches - self.evac_pops

    def pending(self):
        return len(self.queue)


class Policy:
    """q_ratelimit instance: one bucket per cgroup, one FIFO queue."""

    def __init__(self, rate_bytes_per_sec, cap_bytes=None):
        self.rate = rate_bytes_per_sec
        self.cap_bytes = cap_bytes
        self.buckets = {}
        self.draining = False
        self.inserts = 0

    def bucket(self, cgid):
        if cgid not in self.buckets:
            self.buckets[cgid] = Bucket(self.rate, self.cap_bytes)
        return self.buckets[cgid]

    # --- struct_ops/insert_request ---
    def insert(self, cgid, rq_ptr, nbytes):
        if self.draining:
            return "DEFER"
        b = self.bucket(cgid)
        b.queue.append(Entry(rq_ptr, nbytes))
        b.enqueued += 1
        self.inserts += 1
        return "ACCEPT"

    # --- struct_ops/cgroup_dispatch ---
    def dispatch(self, cgid, now_ns):
        b = self.bucket(cgid)
        # refill under the bucket lock.  `now` is sampled before the lock:
        # a lock-order inversion (now < last_ns) must refill ZERO, never
        # underflow (u64 underflow clamped at the 1s dt would award a
        # phantom rate*1s refill per inversion — the 20261029 smoke bug).
        if b.last_ns is None:
            b.last_ns = now_ns
        if now_ns > b.last_ns:
            dt = now_ns - b.last_ns
            if dt > DT_CLAMP_NS:
                dt = DT_CLAMP_NS
        else:
            dt = 0
        b.last_ns = now_ns
        b.tokens += dt * self.rate // SEC
        if b.cap and b.tokens > b.cap:
            b.tokens = b.cap
        tokens_now = b.tokens

        if not b.queue:
            return 0  # ordinary no-work NULL (not rate-limited)

        head = b.queue[0]
        if head.bytes > tokens_now:
            b.null_returns += 1
            return 0  # rate-limited NULL -> 3ms self-kick retry

        popped = b.queue.pop(0)
        b.tokens = popped.bytes <= b.tokens and b.tokens - popped.bytes or 0
        b.dispatches += 1
        return popped.rq_ptr

    # --- struct_ops/evacuate (DRAINING-gated) ---
    def evacuate(self, cgid):
        if not self.draining:
            return 0  # active policy: resist generic-fallback steal
        b = self.bucket(cgid)
        if not b.queue:
            return 0
        popped = b.queue.pop(0)
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


def test_steady_rate_4k():
    """Continuous QD-saturated 4K stream held at exactly rate (±2%)."""
    rate = 64 * MIB
    pol = Policy(rate)
    cgid = 1
    reqs = 0
    dispatched_bytes = 0
    rid = 0
    window_ns = 10 * SEC
    # backlogged generator: keep >= 256 queued, dispatch at 1ms cadence
    for t in range(0, window_ns, MS):
        while pol.bucket(cgid).pending() < 256:
            pol.insert(cgid, rid, 4096)
            rid += 1
        for _ in range(64):  # multiple dispatch attempts per tick
            rq = pol.dispatch(cgid, t)
            if rq:
                dispatched_bytes += 4096
                reqs += 1
            else:
                break
    bw = dispatched_bytes / (window_ns / SEC)
    err = abs(bw - rate) / rate
    return check("steady_rate_4k_64MiB", err <= 0.02,
                 f"achieved={bw / MIB:.2f}MiB/s err={err:.4f}")


def test_burst_clamp():
    """Idle bucket clamps the instantaneous burst to cap (+ one request)."""
    rate = 32 * MIB
    pol = Policy(rate)
    cgid = 1
    cap = rate * CAP_WINDOW_NS // SEC
    n = cap // 4096
    for rid in range(n + 1):
        pol.insert(cgid, rid + 1, 4096)   # rid 0 is falsy: start at 1
    pol.dispatch(cgid, 0)          # touch at t=0: dt=0, tokens stay 0
    rq = pol.dispatch(cgid, SEC)   # idle 1s: refill clamped to cap
    burst = 4096 if rq else 0
    while True:
        rq = pol.dispatch(cgid, SEC)   # same instant: dt=0, tokens frozen
        if not rq:
            break
        burst += 4096
    return check("burst_clamp_half_second",
                 burst == cap and pol.bucket(cgid).pending() == 1,
                 f"burst={burst} cap={cap}")


def test_head_of_line_blocking():
    """A 350K bulk head blocks 4K followers until tokens accrue (FIFO)."""
    rate = 8 * MIB
    pol = Policy(rate)
    cgid = 1
    pol.insert(cgid, 100, 350 * 1024)
    pol.insert(cgid, 101, 4096)
    rq = pol.dispatch(cgid, 0)
    ok1 = check("bulk_head_blocks", rq == 0 and
                pol.bucket(cgid).null_returns == 1)
    # ceil: floor division would leave tokens one byte short of the head
    need_ns = -(-(350 * 1024) * SEC // rate)
    rq = pol.dispatch(cgid, need_ns)
    ok2 = check("bulk_head_dispatched_in_order",
                rq == 100 and pol.bucket(cgid).pending() == 1)
    rq = pol.dispatch(cgid, need_ns + SEC // 10)
    ok3 = check("follower_dispatched_after_head", rq == 101)
    return ok1 and ok2 and ok3


def test_dt_clamp_exactness():
    """dt > 1s refills exactly to cap (clamp is semantics-preserving)."""
    rate = 64 * MIB
    pol = Policy(rate)
    cgid = 1
    pol.dispatch(cgid, 0)           # touch at t=0 (empty queue)
    pol.dispatch(cgid, 120 * SEC)   # long idle: dt clamped to 1s
    cap = rate * CAP_WINDOW_NS // SEC
    return check("dt_clamp_refills_to_cap", pol.bucket(cgid).tokens == cap,
                 f"tokens={pol.bucket(cgid).tokens} cap={cap}")


def test_drain_gate_and_conservation():
    """Evacuate resists while active; drains after DRAINING; lost == 0."""
    rate = 4 * MIB
    pol = Policy(rate)
    cgid = 1
    for rid in range(10):
        pol.insert(cgid, rid + 1, 4096)   # rid 0 is falsy: start at 1
    stolen = pol.evacuate(cgid)
    ok1 = check("evacuate_resists_when_active", stolen == 0 and
                pol.bucket(cgid).pending() == 10)
    # touch at t=0, then normal dispatch drains one request at rate
    pol.dispatch(cgid, 0)
    rq = pol.dispatch(cgid, 10 * SEC)
    ok2 = check("dispatch_works_while_active", rq == 1)
    pol.mark_draining()
    drained = []
    while True:
        rq = pol.evacuate(cgid)
        if not rq:
            break
        drained.append(rq)
    b = pol.bucket(cgid)
    ok3 = check("drain_releases_all", len(drained) == 9 and
                b.pending() == 0)
    ok4 = check("accounting_conservation_lost_zero", b.lost == 0,
                f"enqueued={b.enqueued} dispatches={b.dispatches} "
                f"evac={b.evac_pops} lost={b.lost}")
    return ok1 and ok2 and ok3 and ok4


def test_null_returns_semantics():
    """null_returns counts only token-starved NULLs, not empty NULLs."""
    pol = Policy(64 * MIB)
    cgid = 1
    rq = pol.dispatch(cgid, 0)
    ok1 = check("empty_null_not_counted", rq == 0 and
                pol.bucket(cgid).null_returns == 0)
    pol.insert(cgid, 1, 4096)
    rq = pol.dispatch(cgid, 0)
    ok2 = check("starved_null_counted", rq == 0 and
                pol.bucket(cgid).null_returns == 1)
    rq = pol.dispatch(cgid, 100 * MS)  # 64MiB * 0.1s = 6.7MB tokens
    ok3 = check("refilled_dispatch", rq == 1 and
                pol.bucket(cgid).null_returns == 1)
    return ok1 and ok2 and ok3


def test_lock_order_inversion_refills_zero():
    """now sampled pre-lock can invert vs lock order; dt must be 0 then.

    Mirrors the 20261029 vng smoke bug: a plain u64 subtraction underflows
    on inversion, the 1s clamp turns it into a phantom rate*1s refill and
    the effective refill measured ~2.8x the configured rate.
    """
    rate = 64 * MIB
    pol = Policy(rate)
    cgid = 1
    pol.insert(cgid, 1, 4096)
    pol.dispatch(cgid, 0)                       # touch at t=0
    pol.dispatch(cgid, 100 * MS)                # normal refill: 6.7MB
    tokens_before = pol.bucket(cgid).tokens
    # inversion: caller's now is 50ms in the past
    rq = pol.dispatch(cgid, 50 * MS)
    ok1 = check("inversion_refills_zero",
                pol.bucket(cgid).tokens == tokens_before)
    # and the inverted call must still admit correctly afterwards
    pol.dispatch(cgid, 150 * MS)
    ok2 = check("post_inversion_dispatch_ok", True)
    return ok1 and ok2


def main():
    tests = [
        test_steady_rate_4k,
        test_burst_clamp,
        test_head_of_line_blocking,
        test_dt_clamp_exactness,
        test_lock_order_inversion_refills_zero,
        test_drain_gate_and_conservation,
        test_null_returns_semantics,
    ]
    ok = True
    for t in tests:
        ok = t() and ok
    print("MODEL " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())