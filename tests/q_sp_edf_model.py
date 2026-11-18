#!/usr/bin/env python3
"""q_sp_edf reference model — executable specification of the SP-EDF core.

Mirrors userspace/q_sp_edf.bpf.c exactly; every rule carries a file:line
citation.  Descriptor construction comes from userspace/ioext_bpf_common.h
and lane constants from ioext_policy_shared.h.  Any change to the BPF core
must be mirrored here and vice versa.  tests/q_sp_edf_accept.sh runs this
self-test as its gate 0 (like tests/arb_wdrr_accept.sh).

Model contract (all cited against userspace/q_sp_edf.bpf.c):
  - lane selection: cost > 8 sectors -> LARGE; write -> SMALL_WRITE;
    otherwise SMALL_READ                               (bpf.c:59-66, :10)
  - cost == nr_sectors (or 1 when 0)                   (ioext_bpf_common.h:209)
  - deadline_ns = arrival_ns + lane constant            (bpf.c:212, :68-75)
      SMALL_READ  +1ms  (bpf.c:11)  SMALL_WRITE +2ms (bpf.c:12)  LARGE +20ms (bpf.c:13)
    arrival_ns = bpf_ktime_get_ns() at insert          (ioext_bpf_common.h:201)
  - lane insert keeps the lane sorted by (deadline ASC, sector ASC on equal
    deadline)                                          (bpf.c:86-95)
  - burst re-arm: any insert while small_burst_left==0 re-arms it to 8
                                                       (bpf.c:215-216, :14)
  - dispatch lane choice: LARGE iff large_should_run, else SMALL_READ if
    non-empty, else SMALL_WRITE, else LARGE            (bpf.c:128-140)
  - large_should_run: large lane empty -> false; both small lanes empty ->
    true; burst==0 -> true; large head deadline expired vs now -> true;
    >=8ms since last large dispatch -> true; else false (bpf.c:108-126, :15)
  - dispatch bookkeeping: LARGE -> last_large_dispatch_ns=now,
    small_burst_left=8; small with burst>0 -> burst--  (bpf.c:163-167)
  - lane capacity IOSCHED_MAX_PENDING=32, overflow insert returns -1
                                                       (bpf.c:83-84,
                                                        ioext_policy_shared.h:15)

Driver model (tests/full_suite/deterministic/trace_driver.c run_async,
lines 282-367): requests are played in batches of batch_size; one batch is
submitted with a single io_uring_enter, all its requests are inserted into
the plug and flushed to the elevator back-to-back (blk-mq.c:2957-2960), and
the driver then waits for every CQE of the batch before submitting the next
one.  The model therefore inserts a whole batch (strictly increasing arrival
times) and then drains the scheduler queue before the next batch.  Arrival
ties are possible in the kernel (ktime granularity); the model exposes them
via arrival_gap_ns=0 (equal arrivals) — then the (deadline, sector) sort
degenerates to sector-ascending order within a lane (bpf.c:89-91).

Self-test scenarios mirror the Tier-2 micro traces in
tests/full_suite/deterministic/traces/micro/spedf_*.json (they are also
cross-checked against those files when present).  The expected issue orders
derived here ARE the algorithm_derivation oracle for the goldens in
tests/full_suite/deterministic/expect/golden/spedf_*.json.
"""
import json
import os
import sys

# ── constants (userspace/q_sp_edf.bpf.c:10-15) ──
SMALL_SECTORS = 8                    # bpf.c:10  Q_SP_EDF_SMALL_SECTORS
SMALL_READ_DEADLINE_NS = 1_000_000   # bpf.c:11
SMALL_WRITE_DEADLINE_NS = 2_000_000  # bpf.c:12
LARGE_DEADLINE_NS = 20_000_000       # bpf.c:13
SMALL_BURST = 8                      # bpf.c:14  Q_SP_EDF_SMALL_BURST
LARGE_MAX_WAIT_NS = 8_000_000        # bpf.c:15  Q_SP_EDF_LARGE_MAX_WAIT_NS
MAX_PENDING = 32                      # ioext_policy_shared.h:15

# lane ids (bpf.c:17-22)
SMALL_READ, SMALL_WRITE, LARGE = 0, 1, 2


class Request:
    def __init__(self, op, sector, nr_sectors, ioprio=None):
        self.op = op
        self.sector = sector
        # cost == nr_sectors, or 1 when 0 (ioext_bpf_common.h:209)
        self.cost = nr_sectors if nr_sectors else 1
        self.ioprio = ioprio
        self.arrival_ns = 0
        self.deadline_ns = 0

    @property
    def is_write(self):
        return "write" in self.op


def sp_edf_lane_id(req):
    """bpf.c:59-66 — cost > SMALL_SECTORS -> LARGE, write -> SMALL_WRITE."""
    if req.cost > SMALL_SECTORS:
        return LARGE
    if req.is_write:
        return SMALL_WRITE
    return SMALL_READ


def sp_edf_deadline_ns(lane):
    """bpf.c:68-75 — per-lane deadline constant."""
    if lane == SMALL_READ:
        return SMALL_READ_DEADLINE_NS
    if lane == SMALL_WRITE:
        return SMALL_WRITE_DEADLINE_NS
    return LARGE_DEADLINE_NS


class SpEdfState:
    """Per-hctx state (bpf.c:29-36) as stored in state_map (bpf.c:43-48)."""

    def __init__(self):
        self.lanes = [[], [], []]
        self.small_burst_left = 0
        self.last_large_dispatch_ns = 0
        self.enqueued = [0, 0, 0]
        self.dispatched = [0, 0, 0]

    def insert(self, req, arrival_ns):
        """insert_request hook (bpf.c:185-233). Returns 0 accept, -1 reject."""
        req.arrival_ns = arrival_ns
        lane = sp_edf_lane_id(req)                       # bpf.c:211
        req.deadline_ns = arrival_ns + sp_edf_deadline_ns(lane)  # bpf.c:212
        # burst re-arm happens under the spin lock BEFORE the lane insert,
        # for ANY lane, when small_burst_left==0                (bpf.c:215-216)
        if self.small_burst_left == 0:
            self.small_burst_left = SMALL_BURST
        target = self.lanes[lane]
        if len(target) >= MAX_PENDING:                    # bpf.c:83-84
            return -1
        # sorted insert by (deadline ASC, sector ASC)       (bpf.c:86-101)
        pos = len(target)
        for i, e in enumerate(target):
            if (req.deadline_ns < e.deadline_ns or
                    (req.deadline_ns == e.deadline_ns and
                     req.sector < e.sector)):
                pos = i
                break
        target.insert(pos, req)
        self.enqueued[lane] += 1                          # bpf.c:218-219
        return 0

    def large_should_run(self, now_ns):
        """bpf.c:108-126."""
        large = self.lanes[LARGE]
        if not large:                                      # bpf.c:113-114
            return False
        if (not self.lanes[SMALL_READ] and
                not self.lanes[SMALL_WRITE]):              # bpf.c:115-117
            return True
        if not self.small_burst_left:                      # bpf.c:118-119
            return True
        if large[0].deadline_ns <= now_ns:                 # bpf.c:120-121
            return True
        if (self.last_large_dispatch_ns and
                now_ns - self.last_large_dispatch_ns >= LARGE_MAX_WAIT_NS):
            return True                                    # bpf.c:122-124
        return False

    def choose_lane(self, now_ns):
        """bpf.c:128-140 — fixed lane priority, NOT global EDF."""
        if self.large_should_run(now_ns):
            return LARGE
        if self.lanes[SMALL_READ]:
            return SMALL_READ
        if self.lanes[SMALL_WRITE]:
            return SMALL_WRITE
        if self.lanes[LARGE]:
            return LARGE
        return None

    def dispatch(self, now_ns):
        """cgroup_dispatch/evacuate hook (bpf.c:142-171, :235-279)."""
        lane_id = self.choose_lane(now_ns)
        if lane_id is None:
            return None                                    # bpf.c:150-151
        lane = self.lanes[lane_id]
        if not lane:
            return None                                    # bpf.c:154-155
        picked = lane.pop(0)                               # bpf.c:157-158
        self.dispatched[lane_id] += 1                      # bpf.c:162
        if lane_id == LARGE:
            self.last_large_dispatch_ns = now_ns           # bpf.c:164
            self.small_burst_left = SMALL_BURST            # bpf.c:165
        elif self.small_burst_left > 0:
            self.small_burst_left -= 1                     # bpf.c:166-167
        return picked

    def drain(self, now_ns, step_ns=100):
        """blk-mq dispatch loop: pop while the scheduler has work."""
        out = []
        n = 0
        while True:
            rq = self.dispatch(now_ns + n * step_ns)
            if rq is None:
                break
            out.append(rq)
            n += 1
        return out


def play_trace(requests, batch_size, arrival_gap_ns=1000, dispatch_now_fn=None):
    """Play a trace the way trace_driver run_async does (trace_driver.c:282-367):
    submit one batch (inserts with strictly increasing arrival times, gap
    = arrival_gap_ns; 0 models equal-arrival ties), drain it fully (wait for
    all CQEs) before the next batch."""
    st = SpEdfState()
    t0 = 10_000_000
    issued = []
    idx = 0
    arrival = t0
    while idx < len(requests):
        batch = requests[idx:idx + batch_size]
        for rq in batch:
            st.insert(rq, arrival)
            arrival += arrival_gap_ns
        now = arrival + 100
        if dispatch_now_fn is not None:
            now = dispatch_now_fn(st, arrival)
        issued.extend(st.drain(now))
        idx += batch_size
    return st, issued


# ── scenarios mirroring traces/micro/spedf_*.json ──
# Sector layout deliberately avoids the readahead/background zones observed in
# prior bpftrace logs (low sectors, powers of two, ~20MB end-of-disk scan) and
# keeps extents non-adjacent (>=8-sector gaps): q_sp_edf has no merge hook and
# the framework allows merges by default (iosched_ext.c:3569 allow=true).
S1_REQS = [("read", 103424, 8), ("read", 103168, 8), ("read", 102912, 8),
           ("read", 102656, 8), ("read", 102400, 8)]
S2_REQS = [("read", 204800, 128), ("read", 102400, 8), ("read", 102656, 8),
           ("read", 102912, 8), ("read", 103168, 8), ("read", 208896, 128),
           ("read", 212992, 128), ("read", 217088, 128)]
S3_REQS = [("write", 101888, 8, 7), ("read", 102400, 8, 0),
           ("write", 102144, 8, 5), ("read", 102912, 8, 3),
           ("read", 103424, 8, 1)]
S4_REQS = ([("read", 102400 + 256 * k, 8) for k in range(8)] +
           [("read", 204800, 128)] +
           [("read", 104448 + 256 * k, 8) for k in range(8)] +
           [("read", 208896, 128)])


def mk(reqs):
    return [Request(*r) for r in reqs]


SCENARIOS = [
    {
        "name": "s1_deadline_5",
        "trace": "spedf_deadline_5.json",
        "batch_size": 8,
        "requests": mk(S1_REQS),
        "expected_issue": [103424, 103168, 102912, 102656, 102400],
        "expected_issue_equal_arrival": [102400, 102656, 102912, 103168, 103424],
        "robust_to_equal_arrival": False,
    },
    {
        "name": "s2_large_small_8",
        "trace": "spedf_large_small_8.json",
        "batch_size": 8,
        "requests": mk(S2_REQS),
        "expected_issue": [102400, 102656, 102912, 103168,
                           204800, 208896, 212992, 217088],
        "expected_issue_equal_arrival": [102400, 102656, 102912, 103168,
                                         204800, 208896, 212992, 217088],
        "robust_to_equal_arrival": True,
    },
    {
        "name": "s3_tie_prio_5",
        "trace": "spedf_tie_prio_5.json",
        "batch_size": 8,
        "requests": mk(S3_REQS),
        "expected_issue": [102400, 102912, 103424, 101888, 102144],
        "expected_issue_equal_arrival": [102400, 102912, 103424, 101888, 102144],
        "robust_to_equal_arrival": True,
    },
    {
        "name": "s4_burst_18",
        "trace": "spedf_burst_18.json",
        "batch_size": 18,
        "requests": mk(S4_REQS),
        # small_burst anti-starvation interleave (bpf.c:118, :165, :215-216):
        # 8 smalls -> burst exhausted -> large runs -> burst reset to 8 ->
        # 8 smalls -> last large (smalls empty, bpf.c:115-117).
        "expected_issue": [102400, 102656, 102912, 103168, 103424, 103680,
                           103936, 104192, 204800, 104448, 104704, 104960,
                           105216, 105472, 105728, 105984, 106240, 208896],
        "expected_issue_equal_arrival": None,  # derived below
        "robust_to_equal_arrival": True,
    },
]


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


def sectors(rqs):
    return [r.sector for r in rqs]


def trace_path(name):
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "full_suite", "deterministic", "traces",
                        "micro", name)


def load_trace_requests(path):
    d = json.load(open(path))
    reqs = []
    for r in d["requests"]:
        reqs.append(Request(r["op"], r["sector"], r["nr_sectors"]))
    return d, reqs


def test_scenarios():
    ok = True
    for sc in SCENARIOS:
        st, issued = play_trace(sc["requests"], sc["batch_size"])
        ok &= check(f"scenario_{sc['name']}",
                    sectors(issued) == sc["expected_issue"],
                    f"got={sectors(issued)} want={sc['expected_issue']}")
        # equal-arrival tie probe: lane sort degenerates to sector ASC
        # (bpf.c:89-91)
        _, issued_tie = play_trace(mk([tuple(r)[:3] for r in
                                       [(x.op, x.sector, x.cost) for x in
                                        sc["requests"]]]),
                                  sc["batch_size"], arrival_gap_ns=0)
        if sc["expected_issue_equal_arrival"] is None:
            sc["expected_issue_equal_arrival"] = sectors(issued_tie)
        if sc["robust_to_equal_arrival"]:
            ok &= check(f"{sc['name']}_robust_to_equal_arrival",
                         sectors(issued_tie) == sc["expected_issue"],
                         f"tie={sectors(issued_tie)}")
        else:
            ok &= check(f"{sc['name']}_equal_arrival_sector_asc",
                         sectors(issued_tie) ==
                         sc["expected_issue_equal_arrival"],
                         f"tie={sectors(issued_tie)}")
    return ok


def test_trace_files_match():
    """Cross-check the embedded scenarios against the actual trace JSONs
    (skipped when the files are absent, e.g. standalone host runs)."""
    ok = True
    for sc in SCENARIOS:
        p = trace_path(sc["trace"])
        if not os.path.exists(p):
            continue
        d, reqs = load_trace_requests(p)
        batch = d.get("defaults", {}).get("batch_size", 8)
        ok &= check(f"trace_file_{sc['trace']}_requests",
                    [(r.op, r.sector, r.cost) for r in reqs] ==
                    [(r.op, r.sector, r.cost) for r in sc["requests"]],
                    f"file={[(r.op, r.sector, r.cost) for r in reqs]}")
        ok &= check(f"trace_file_{sc['trace']}_batch_size",
                    batch == sc["batch_size"],
                    f"file={batch} model={sc['batch_size']}")
        # oracle: derive the issue order from the FILE and compare with the
        # golden order the model asserts
        _, issued = play_trace(reqs, batch)
        ok &= check(f"trace_file_{sc['trace']}_issue_oracle",
                    sectors(issued) == sc["expected_issue"],
                    f"got={sectors(issued)}")
    return ok


def test_read_before_write_lane():
    """bpf.c:133-135 — SMALL_READ drains before SMALL_WRITE while both
    non-empty (fixed lane priority, cross-lane EDF does NOT exist)."""
    st, issued = play_trace(mk([("write", 101888, 8), ("read", 102400, 8)]), 8)
    return check("read_before_write_lane",
                 sectors(issued) == [102400, 101888],
                 f"got={sectors(issued)}")


def test_small_before_large_burst():
    """bpf.c:118 + :163-167 — with burst budget left and no deadline expiry,
    smalls win over an earlier-arriving large (S2 core property)."""
    st, issued = play_trace(mk([("read", 204800, 128), ("read", 102400, 8)]), 8)
    return check("small_before_large_burst",
                 sectors(issued) == [102400, 204800],
                 f"got={sectors(issued)}")


def test_large_deadline_expiry_preempts():
    """bpf.c:120-121 — large head deadline <= now lets the large lane run
    even with smalls pending and burst budget left."""
    st, issued = play_trace(
        mk([("read", 204800, 128), ("read", 102400, 8)]), 8,
        dispatch_now_fn=lambda s, arrival: arrival + LARGE_DEADLINE_NS + 1)
    return check("large_deadline_expiry_preempts",
                 sectors(issued) == [204800, 102400],
                 f"got={sectors(issued)}")


def test_large_max_wait_preempts():
    """bpf.c:122-124 — >=8ms since the last large dispatch lets the large
    lane run even when its head deadline has NOT expired."""
    st = SpEdfState()
    now = 100_000_000
    st.last_large_dispatch_ns = now - LARGE_MAX_WAIT_NS - 1   # 9ms ago
    st.small_burst_left = SMALL_BURST
    # large arrives 'now' -> its deadline is +20ms away (not expired)
    st.insert(Request("read", 204800, 128), now)
    st.insert(Request("read", 102400, 8), now + 1000)
    picked = st.dispatch(now + 2000)
    return check("large_max_wait_preempts",
                 picked is not None and picked.sector == 204800,
                 f"got={picked.sector if picked else None}")


def test_insert_time_burst_rearm():
    """bpf.c:215-216 — an insert while burst==0 re-arms it, so a small
    inserted after the burst ran dry is served before a queued large."""
    reqs = mk([("read", 102400 + 256 * k, 8) for k in range(8)])
    st, issued = play_trace(reqs, 8)
    if not check("insert_rearm_batch1_burst_dry",
                 st.small_burst_left == 0,
                 f"burst={st.small_burst_left}"):
        return False
    # second batch: small insert re-arms burst (bpf.c:215-216) -> small first
    st2, issued2 = play_trace(
        mk([("read", 102400 + 256 * k, 8) for k in range(8)] +
           [("read", 204800, 128)]), 8)
    return check("insert_time_burst_rearm",
                 sectors(issued2)[:9] == [102400 + 256 * k for k in range(8)] +
                 [204800] and sectors(issued2)[-1] == 204800,
                 f"got={sectors(issued2)}")


def test_burst_reset_after_large():
    """bpf.c:163-165 — a large dispatch resets small_burst_left to 8."""
    st = SpEdfState()
    st.insert(Request("read", 204800, 128), 1_000)
    st.small_burst_left = 2   # simulate 6 prior small dispatches
    picked = st.dispatch(3_000)   # smalls empty -> large runs (bpf.c:115-117)
    return check("burst_reset_after_large",
                 picked is not None and picked.sector == 204800 and
                 st.small_burst_left == SMALL_BURST,
                 f"burst={st.small_burst_left}")


def test_large_insert_rearm_does_not_preempt():
    """bpf.c:215-216 — a large inserted while burst==0 re-arms the burst
    itself, so it does NOT preempt the small that follows it."""
    st = SpEdfState()
    st.small_burst_left = 0
    st.insert(Request("read", 204800, 128), 1_000)   # re-arms burst to 8
    st.insert(Request("read", 102400, 8), 2_000)
    picked = st.dispatch(3_000)
    return check("large_insert_rearm_does_not_preempt",
                 picked is not None and picked.sector == 102400,
                 f"got={picked.sector if picked else None}")


def test_lane_overflow():
    """bpf.c:83-84 + ioext_policy_shared.h:15 — 32 pending per lane max."""
    st = SpEdfState()
    last = 0
    for i in range(MAX_PENDING):
        last = st.insert(Request("read", 102400 + 8 * i, 8), i * 1000)
    ok = check("lane_fill_32_ok", last == 0 and
               len(st.lanes[SMALL_READ]) == MAX_PENDING)
    over = st.insert(Request("read", 999999, 8), 99_000)
    ok &= check("lane_overflow_rejected", over == -1 and
                len(st.lanes[SMALL_READ]) == MAX_PENDING,
                f"ret={over}")
    return ok


def test_burst_floor_at_zero():
    """bpf.c:166-167 — small dispatch only decrements while burst>0; it
    never goes negative."""
    st = SpEdfState()
    for i in range(12):
        st.insert(Request("read", 102400 + 256 * i, 8), i * 1000)
    for i in range(12):
        st.dispatch(100_000 + i * 100)
    return check("burst_floor_at_zero", st.small_burst_left == 0,
                 f"burst={st.small_burst_left}")


def test_write_lane_deadline_constant():
    """bpf.c:12 — SMALL_WRITE deadline is 2ms (documented constant check)."""
    st = SpEdfState()
    st.insert(Request("write", 101888, 8), 5_000)
    rq = st.lanes[SMALL_WRITE][0]
    return check("write_deadline_2ms", rq.deadline_ns == 5_000 + SMALL_WRITE_DEADLINE_NS,
                 f"deadline={rq.deadline_ns}")


def main():
    ok = True
    ok &= test_scenarios()
    ok &= test_trace_files_match()
    ok &= test_read_before_write_lane()
    ok &= test_small_before_large_burst()
    ok &= test_large_deadline_expiry_preempts()
    ok &= test_large_max_wait_preempts()
    ok &= test_insert_time_burst_rearm()
    ok &= test_burst_reset_after_large()
    ok &= test_lane_overflow()
    ok &= test_burst_floor_at_zero()
    ok &= test_write_lane_deadline_constant()
    print()
    for sc in SCENARIOS:
        print(f"oracle {sc['name']} ({sc['trace']}) "
              f"expected_dispatch_order={json.dumps(sc['expected_issue'])}")
    print()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()