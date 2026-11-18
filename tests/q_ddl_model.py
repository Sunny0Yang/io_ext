#!/usr/bin/env python3
"""q_ddl reference model — executable specification of the two-lane
deadline-FIFO core (userspace/q_ddl.bpf.c).

FRAMING (do not chase legacy fidelity): q_ddl is an ORIGINAL iosched_ext
design (POL-05), not a port of the legacy deadline-iosched — that file was
removed upstream (block/deadline-iosched.c no longer exists); no legacy
behavioral contract applies.  Everything below is derived from
userspace/q_ddl.bpf.c with file:line citations; descriptor construction
comes from userspace/ioext_bpf_common.h, lane capacity from
userspace/ioext_policy_shared.h.  Any change to the BPF core must be
mirrored here and vice versa.  tests/q_ddl_accept.sh runs this self-test
as its gate 0.

Model contract (all cited against userspace/q_ddl.bpf.c):
  - two lanes: DDL_LAT (latency-critical) / DDL_BULK (best-effort)
                                                        (bpf.c:45-49, :51-56)
  - classification ddl_classify                       (bpf.c:104-118):
      ioprio class = (ioprio >> 13) & 0x7              (bpf.c:99-102, :37-39)
      RT (1)   -> LAT                                   (bpf.c:108-109)
      IDLE (3) -> BULK                                  (bpf.c:111-112)
      else: read && cost <= 8 sectors -> LAT           (bpf.c:114-115)
            otherwise                 -> BULK          (bpf.c:117)
    cost == nr_sectors (or 1 when 0)  (ioext_bpf_common.h:209);
    write flag from opf via iosched_rq_flags_from_opf
                                        (ioext_bpf_common.h:146-172,
                                         ioext_policy_shared.h:8, :374-377)
  - deadline_ns = arrival_ns + lane constant          (bpf.c:268, :120-123)
      LAT +1ms (bpf.c:29)  BULK +20ms (bpf.c:30);
    arrival_ns = bpf_ktime_get_ns() at insert         (ioext_bpf_common.h:201)
  - head bookkeeping: lat_head_enqueue_ns / lat_head_deadline /
    bulk_head_enqueue_ns track each lane head; refreshed after every
    successful insert and dispatch                      (bpf.c:153-168, :273,
                                                         :306-307)
  - pure per-lane FIFO: push_tail / pop_head arrays, NO sector ordering,
    NO C-LOOK, NO batch continuation                    (bpf.c:125-151)
  - lane capacity IOSCHED_MAX_PENDING=32 per lane, overflow insert
    returns -1 (kernel bypasses the policy for that request)
                                                        (bpf.c:128-134,
                                                         ioext_policy_shared.h:15)
  - dispatch precedence ddl_dispatch, now = bpf_ktime_get_ns at
    cgroup_dispatch (:303)                             (bpf.c:170-227):
      1. LAT head deadline missed (0 < hd <= now) AND emergency_burst < 3
         -> LAT; emergency_burst++, lat_burst++        (bpf.c:193-198, :34)
      2. BULK head waited >= 10ms -> BULK; BOTH burst counters reset
                                                        (bpf.c:200-205, :32)
      3. LAT urgent (hd <= now + 200us guard) AND lat_burst < 8 -> LAT;
         lat_burst++                                   (bpf.c:207-211, :31, :33)
      4. BULK pending -> BULK; BOTH counters reset     (bpf.c:213-218)
      5. LAT pending -> LAT; lat_burst++ (UNCAPPED)    (bpf.c:220-224)
    Counter lifetimes: lat_burst/emergency_burst are reset ONLY by a BULK
    dispatch (rules 2 and 4, bpf.c:201-202, :214-215); LAT dispatches only
    increment; inserts never touch them (unlike q_sp_edf, no insert-time
    re-arm exists).
  - merge: allow_merge = false always                  (bpf.c:340-344, POL-06)
    (scheduler-queue merges only; the plug can still pre-merge adjacent
    same-op bios before the elevator, so traces must keep extents
    non-adjacent — see golden notes)

KEY DISCRIMINATOR vs q_mq_deadline: in a fresh async-batch burst (all rules
1-3 cold — requests are µs old, LAT deadline is 1ms away, BULK head far from
the 10ms starvation bound), rule 4 fires before rule 5: **BULK drains first
(arrival FIFO), then LAT (arrival FIFO)** — the exact inverse of
mq-deadline's read-first C-LOOK.  A fresh mixed batch therefore issues
[W-lane FIFO..., R-lane FIFO...], which is neither sector-ascending
(C-LOOK) nor read-first (mqdd).

Rig testability boundaries (audit-verified, VM-confirmed for the siblings;
document — do NOT chase):
  - ioprio does NOT propagate through trace_driver's io_uring O_DIRECT path
    (see golden mqdd_prio_5 notes): RT/IDLE classification lanes are
    UNTESTABLE in this rig; only the read&&cost<=8 heuristic branch is
    observable.  The model still implements the ioprio branches and self-
    tests them host-side.
  - expire-based rules (1ms LAT deadline / 200us guard / 10ms BULK
    starvation) are structurally untestable in the DT rig: trace_driver's
    delay_us applies only between fully-harvested batches, and in-queue
    aging needs >32-deep per-lane queues which overflow to kernel bypass
    (bpf.c:128-134).  Rules 1-3 + counter lifetimes are therefore encoded
    here with injected-time self-tests; NO Tier-2 case exercises them.

KNOWN GAP — q_ddl structurally bypasses ALL requests as built (VM finding
2026-09-01, first q_ddl DT run, results/q_ddl_accept_20260901_163034):
q_ddl.bpf.c NEVER creates state_map entries — state_map is
BPF_MAP_TYPE_HASH keyed by (cgroup_id,hctx_idx) (bpf.c:79-84) with a
lookup-only helper (bpf.c:88-97) and NO bpf_map_update_elem anywhere, so
ddl_state_lookup always returns NULL and insert_request returns -1
(bpf.c:260-264) for every request.  The working siblings avoid this with
auto-zero BPF_MAP_TYPE_ARRAY state maps (lookups cannot miss).  Runtime
proof: policy_meta.dispatches==0 after fully-ISSUEd traces (note_dispatch
fires only on the accept path, bpf.c:310-311).  Consequence: the lanes and
the 5-rule precedence below are currently DEAD CODE in the shipped
loader_q_ddl; this model remains the INTENDED spec.  Tier-2 case M02
(ddl_cost_class_5) is the registered bypass detector (its golden differs
from submission order) and FAILs 5/5 until the source is fixed (ARRAY
state map keyed by hctx_idx mirroring sp_edf/wedf/kyber, or loader-side
HASH seeding to preserve POL-08 per-cgroup isolation) and the loader is
rebuilt — both outside this harness's ownership.

Driver model (tests/full_suite/deterministic/trace_driver.c run_async,
:282-367): one batch is submitted with a single io_uring_enter, all its
requests are inserted into the plug and flushed to the elevator back-to-back
(blk-mq elevator insert path), and the driver waits for every CQE before the
next batch.  The model therefore inserts a whole batch (strictly increasing
arrival times; arrival_gap_ns=0 models equal-arrival ties) and then drains
the scheduler queue before the next batch.  q_ddl is IMMUNE to arrival ties:
per-lane push_tail keeps submission order (stable append, no sorted insert),
so expected ISSUE orders hold for any arrival gap >= 0.

Self-test scenarios mirror the Tier-2 micro traces in
tests/full_suite/deterministic/traces/micro/ddl_*.json; the expected
issue orders derived here ARE the algorithm_derivation oracle for the
goldens in tests/full_suite/deterministic/expect/golden/ddl_*.json.
"""
import json
import os
import sys

# ── constants (userspace/q_ddl.bpf.c:29-35) ──
LAT_DEADLINE_NS = 1_000_000        # bpf.c:29  Q_DDL_LAT_DEADLINE_NS
BULK_DEADLINE_NS = 20_000_000      # bpf.c:30  Q_DDL_BULK_DEADLINE_NS
GUARD_NS = 200_000                 # bpf.c:31  Q_DDL_GUARD_NS
BULK_STARVATION_NS = 10_000_000    # bpf.c:32  Q_DDL_BULK_STARVATION_NS
BURST_MAX = 8                      # bpf.c:33  Q_DDL_BURST_MAX
EMERGENCY_MAX = 3                  # bpf.c:34  Q_DDL_EMERGENCY_MAX
SMALL_SECTOR_THRESH = 8            # bpf.c:35  Q_DDL_SMALL_SECTOR_THRESH
MAX_PENDING = 32                   # ioext_policy_shared.h:15

# ioprio classes (bpf.c:37-43)
IOPRIO_CLASS_SHIFT = 13            # bpf.c:38
IOPRIO_CLASS_RT = 1                # bpf.c:41
IOPRIO_CLASS_IDLE = 3              # bpf.c:43

# lane ids (bpf.c:45-49)
DDL_LAT, DDL_BULK = 0, 1
LANE_NAME = {DDL_LAT: "LAT", DDL_BULK: "BULK"}


class Request:
    def __init__(self, op, sector, nr_sectors, ioprio=0):
        self.op = op
        self.sector = sector
        # cost == nr_sectors, or 1 when 0 (ioext_bpf_common.h:209)
        self.cost = nr_sectors if nr_sectors else 1
        self.ioprio = ioprio
        self.arrival_ns = 0
        self.deadline_ns = 0

    @property
    def is_write(self):
        # IOSCHED_REQF_WRITE from opf (ioext_policy_shared.h:8,
        # ioext_bpf_common.h:154-159); trace op string carries the opf
        return "write" in self.op

    def __repr__(self):
        return (f"{'W' if self.is_write else 'R'}{self.sector}"
                f"(nr={self.cost})")


def ddl_ioprio_class(ioprio):
    """bpf.c:99-102 — class bits live at ioprio >> 13."""
    return (ioprio >> IOPRIO_CLASS_SHIFT) & 0x7


def ddl_classify(req):
    """bpf.c:104-118 — RT->LAT, IDLE->BULK, else read && cost<=8 -> LAT."""
    cls = ddl_ioprio_class(req.ioprio)
    if cls == IOPRIO_CLASS_RT:                       # bpf.c:108-109
        return DDL_LAT
    if cls == IOPRIO_CLASS_IDLE:                     # bpf.c:111-112
        return DDL_BULK
    if not req.is_write and req.cost <= SMALL_SECTOR_THRESH:  # bpf.c:114-115
        return DDL_LAT
    return DDL_BULK                                  # bpf.c:117


def ddl_deadline_ns(lane):
    """bpf.c:120-123 — per-lane deadline constant."""
    return LAT_DEADLINE_NS if lane == DDL_LAT else BULK_DEADLINE_NS


class DdlState:
    """Per-(cgroup_id, hctx_idx) state (bpf.c:58-66) as stored in
    state_map (bpf.c:79-84)."""

    def __init__(self):
        self.lanes = {DDL_LAT: [], DDL_BULK: []}
        self.lat_burst = 0
        self.emergency_burst = 0
        self.lat_head_enqueue_ns = 0
        self.lat_head_deadline = 0
        self.bulk_head_enqueue_ns = 0
        self.last_dispatch_ns = 0
        self.enqueued = {DDL_LAT: 0, DDL_BULK: 0}
        self.dispatched = {DDL_LAT: 0, DDL_BULK: 0}
        # observability only (not kernel state): which rule fired last
        self.last_rule = None

    # ── head bookkeeping (bpf.c:153-168) ──
    def update_heads(self):
        lat = self.lanes[DDL_LAT]
        if lat:                                        # bpf.c:155-157
            self.lat_head_enqueue_ns = lat[0].arrival_ns
            self.lat_head_deadline = lat[0].deadline_ns
        else:                                          # bpf.c:158-161
            self.lat_head_enqueue_ns = 0
            self.lat_head_deadline = 0
        bulk = self.lanes[DDL_BULK]
        if bulk:                                       # bpf.c:163-164
            self.bulk_head_enqueue_ns = bulk[0].arrival_ns
        else:                                          # bpf.c:165-167
            self.bulk_head_enqueue_ns = 0

    # ── insert_request hook (bpf.c:241-288) ──
    def insert(self, req, arrival_ns):
        """Returns 0 accept, -1 overflow (bpf.c:128-134, :276-279)."""
        req.arrival_ns = arrival_ns
        lane = ddl_classify(req)                       # bpf.c:267
        req.deadline_ns = arrival_ns + ddl_deadline_ns(lane)  # bpf.c:268
        target = self.lanes[lane]
        if len(target) >= MAX_PENDING:                 # bpf.c:128
            return -1
        target.append(req)                             # push_tail bpf.c:130
        self.enqueued[lane] += 1                       # bpf.c:132
        self.update_heads()                           # bpf.c:273
        return 0

    # ── dispatch hook (bpf.c:170-227); caller refreshes heads (bpf.c:306) ──
    def dispatch(self, now_ns):
        lat_missed = False
        lat_urgent = False
        bulk_starved = False

        if self.lanes[DDL_LAT]:                        # bpf.c:177-185
            hd = self.lat_head_deadline
            if hd > 0:                                 # bpf.c:179
                if hd <= now_ns:                       # bpf.c:180-181
                    lat_missed = True
                elif hd <= now_ns + GUARD_NS:          # bpf.c:182-183
                    lat_urgent = True

        if self.lanes[DDL_BULK]:                       # bpf.c:187-191
            bulk_wait = now_ns - self.bulk_head_enqueue_ns
            if bulk_wait >= BULK_STARVATION_NS:        # bpf.c:189-190
                bulk_starved = True

        # rule 1: LAT emergency (deadline missed, budget left)  bpf.c:193-198
        if lat_missed and self.emergency_burst < EMERGENCY_MAX:
            self.emergency_burst += 1
            self.lat_burst += 1
            self.last_dispatch_ns = now_ns
            self.last_rule = 1
            return self._pop(DDL_LAT)

        # rule 2: BULK starvation bail-out (resets BOTH counters) bpf.c:200-205
        if bulk_starved:
            self.emergency_burst = 0
            self.lat_burst = 0
            self.last_dispatch_ns = now_ns
            self.last_rule = 2
            return self._pop(DDL_BULK)

        # rule 3: LAT urgent within 200us guard, burst budget left
        #                                               bpf.c:207-211
        if lat_urgent and self.lat_burst < BURST_MAX:
            self.lat_burst += 1
            self.last_dispatch_ns = now_ns
            self.last_rule = 3
            return self._pop(DDL_LAT)

        # rule 4: BULK pending (resets BOTH counters)  bpf.c:213-218
        if self.lanes[DDL_BULK]:
            self.lat_burst = 0
            self.emergency_burst = 0
            self.last_dispatch_ns = now_ns
            self.last_rule = 4
            return self._pop(DDL_BULK)

        # rule 5: LAT fallback, lat_burst++ UNCAPPED    bpf.c:220-224
        if self.lanes[DDL_LAT]:
            self.lat_burst += 1
            self.last_dispatch_ns = now_ns
            self.last_rule = 5
            return self._pop(DDL_LAT)

        self.last_rule = None                          # bpf.c:226
        return None

    def _pop(self, lane):
        """ddl_pop_head — pure FIFO (bpf.c:136-151) + head refresh
        (caller-side bpf.c:306-307 folded here)."""
        req = self.lanes[lane].pop(0)
        self.dispatched[lane] += 1                     # bpf.c:148
        self.update_heads()
        return req

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


def play_trace(requests, batch_size, arrival_gap_ns=1000,
               dispatch_now_fn=None):
    """Play a trace the way trace_driver run_async does (trace_driver.c
    :282-367): submit one batch (inserts with strictly increasing arrival
    times; gap=0 models equal-arrival ties), drain it fully before the
    next batch.  dispatch_now_fn(state, arrival) lets self-tests inject
    time (rules 1-3)."""
    st = DdlState()
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


# ── scenarios mirroring traces/micro/ddl_*.json ──
# Sector layout: 2560..5120 (above the partition-scan zone 0..2048, matching
# the Phase-2-validated mqdd_rev_5 sector family), no power-of-two sector,
# same-op extents separated by >=504 sectors (plug merges need adjacent
# same-op bios; q_ddl's allow_merge=false additionally kills
# scheduler-queue merges, bpf.c:340-344).  BULK count kept <=3 so a fresh
# burst's BULK drain stays microsecond-scale and rules 1-3 stay cold.
S1_REQS = [("write", 4608, 8), ("write", 3072, 8),
           ("read", 5120, 8), ("read", 3584, 8), ("read", 2560, 8)]
S2_REQS = [("read", 2560, 64), ("read", 5120, 8), ("read", 3584, 64),
           ("read", 4608, 8), ("read", 3072, 8)]
S3_REQS = [("read", 5120, 8), ("read", 4608, 8), ("read", 3584, 8),
           ("read", 3072, 8), ("read", 2560, 8)]


def mk(reqs):
    return [Request(*r) for r in reqs]


SCENARIOS = [
    {
        "name": "s1_bulk_first_5",
        "trace": "ddl_bulk_first_5.json",
        "golden": "ddl_bulk_first_5.json",
        "batch_size": 8,
        "requests": mk(S1_REQS),
        # fresh burst: rules 1-3 cold -> rule 4 drains BULK FIFO first,
        # then rule 5 drains LAT FIFO — the anti-mqdd discriminator
        "expected_issue": [4608, 3072, 5120, 3584, 2560],
        "expected_rules": [4, 4, 5, 5, 5],
    },
    {
        "name": "s2_cost_class_5",
        "trace": "ddl_cost_class_5.json",
        "golden": "ddl_cost_class_5.json",
        "batch_size": 8,
        "requests": mk(S2_REQS),
        # nr=64 reads are BULK (cost 64 > 8), nr=8 reads are LAT:
        # cost boundary, no writes involved
        "expected_issue": [2560, 3584, 5120, 4608, 3072],
        "expected_rules": [4, 4, 5, 5, 5],
    },
    {
        "name": "s3_lat_fifo_desc_5",
        "trace": "ddl_lat_fifo_desc_5.json",
        "golden": "ddl_lat_fifo_desc_5.json",
        "batch_size": 8,
        "requests": mk(S3_REQS),
        # pure-LAT rule-5 drain: ISSUE == submission order (arrival FIFO),
        # NOT sector-ascending — the stark anti-C-LOOK pair vs mqdd_rev_5
        # (same shape, opposite expectation)
        "expected_issue": [5120, 4608, 3584, 3072, 2560],
        "expected_rules": [5, 5, 5, 5, 5],
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


def play_trace_with_rules(requests, batch_size, arrival_gap_ns=1000,
                          dispatch_now_fn=None):
    """play_trace + record which precedence rule fired per dispatch."""
    st = DdlState()
    t0 = 10_000_000
    issued = []
    rule_seq = []
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
        while True:
            rq = st.dispatch(now)
            if rq is None:
                break
            issued.append(rq)
            rule_seq.append(st.last_rule)
        idx += batch_size
    return st, issued, rule_seq


def trace_dir(name):
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "full_suite", "deterministic", name)


# ── (h) scenarios + trace/golden cross-check ─────────────────────────────
def test_scenarios():
    ok = True
    for sc in SCENARIOS:
        st, issued, rule_seq = play_trace_with_rules(
            sc["requests"], sc["batch_size"])
        ok &= check(f"scenario_{sc['name']}",
                    sectors(issued) == sc["expected_issue"],
                    f"got={sectors(issued)} want={sc['expected_issue']}")
        ok &= check(f"scenario_{sc['name']}_rules",
                    rule_seq == sc["expected_rules"],
                    f"got={rule_seq} want={sc['expected_rules']}")
        # q_ddl is immune to equal-arrival ties (stable per-lane append,
        # no sorted insert — bpf.c:125-151): same order at gap 0.
        _, issued_tie, rule_tie = play_trace_with_rules(
            sc["requests"], sc["batch_size"], arrival_gap_ns=0)
        ok &= check(f"{sc['name']}_robust_to_equal_arrival",
                    sectors(issued_tie) == sc["expected_issue"] and
                    rule_tie == sc["expected_rules"],
                    f"tie={sectors(issued_tie)}/{rule_tie}")
    return ok


def test_trace_files_match():
    """(h) Cross-check: the model derives the SAME expected orders as the
    goldens for M01/M02/M03 — from the actual trace files, then compared
    against the actual golden files (skipped when absent)."""
    ok = True
    for sc in SCENARIOS:
        tp = os.path.join(trace_dir("traces/micro"), sc["trace"])
        gp = os.path.join(trace_dir("expect/golden"), sc["golden"])
        if not os.path.exists(tp):
            continue
        d = json.load(open(tp))
        reqs = [Request(r["op"], r["sector"], r["nr_sectors"])
                for r in d["requests"]]
        batch = d.get("defaults", {}).get("batch_size", 8)
        ok &= check(f"trace_file_{sc['trace']}_requests",
                    [(r.op, r.sector, r.cost) for r in reqs] ==
                    [(r.op, r.sector, r.cost) for r in sc["requests"]],
                    f"file={[(r.op, r.sector, r.cost) for r in reqs]}")
        ok &= check(f"trace_file_{sc['trace']}_batch_size",
                    batch == sc["batch_size"], f"file={batch}")
        # oracle: derive the issue order from the FILE
        _, issued, _ = play_trace_with_rules(reqs, batch)
        ok &= check(f"trace_file_{sc['trace']}_issue_oracle",
                    sectors(issued) == sc["expected_issue"],
                    f"got={sectors(issued)}")
        # golden cross-check: the golden's expected_dispatch_order must
        # equal the model-derived order
        if os.path.exists(gp):
            g = json.load(open(gp))
            golden_order = [e["sector"] for e in g["expected_dispatch_order"]]
            ok &= check(f"golden_{sc['golden']}_matches_model",
                        golden_order == sectors(issued),
                        f"golden={golden_order} model={sectors(issued)}")
            ok &= check(f"golden_{sc['golden']}_expected_merge_false",
                        g.get("expected_merge") is False,
                        f"got={g.get('expected_merge')}")
    return ok


# ── (b) cost boundary ────────────────────────────────────────────────────
def test_cost_boundary():
    """bpf.c:114-115 — read && cost<=8 -> LAT; everything else BULK.
    Boundary: nr=8 read LAT, nr=9 / nr=16 reads BULK, writes always BULK
    (S2 core property)."""
    ok = True
    ok &= check("classify_read_nr8_is_LAT",
                ddl_classify(Request("read", 2560, 8)) == DDL_LAT)
    ok &= check("classify_read_nr1_is_LAT",
                ddl_classify(Request("read", 2560, 1)) == DDL_LAT)
    ok &= check("classify_read_nr9_is_BULK",
                ddl_classify(Request("read", 2560, 9)) == DDL_BULK)
    ok &= check("classify_read_nr16_is_BULK",
                ddl_classify(Request("read", 2560, 16)) == DDL_BULK)
    ok &= check("classify_read_nr64_is_BULK",
                ddl_classify(Request("read", 2560, 64)) == DDL_BULK)
    ok &= check("classify_write_nr8_is_BULK",
                ddl_classify(Request("write", 2560, 8)) == DDL_BULK)
    ok &= check("classify_write_nr1_is_BULK",
                ddl_classify(Request("write", 2560, 1)) == DDL_BULK)
    ok &= check("classify_cost0_reads_as_1_LAT",
                ddl_classify(Request("read", 2560, 0)) == DDL_LAT,
                "cost=nr_sectors?1 (ioext_bpf_common.h:209)")
    # M02 exact shape: interleaved arrival, nr=64 reads classified BULK
    # and dispatched BEFORE the 4KB LAT reads despite arriving between them
    st, issued, rule_seq = play_trace_with_rules(mk(S2_REQS), 8)
    ok &= check("m02_bulk_before_lat_interleaved",
                sectors(issued) == [2560, 3584, 5120, 4608, 3072],
                f"got={sectors(issued)}")
    ok &= check("m02_rules", rule_seq == [4, 4, 5, 5, 5],
                f"got={rule_seq}")
    return ok


# ── (a) fresh mixed burst: BULK lane first ──────────────────────────────
def test_fresh_burst_bulk_first():
    """M01 core: fresh async-batch burst, rules 1-3 cold -> rule 4 drains
    the BULK lane (arrival FIFO), then rule 5 drains LAT (arrival FIFO) —
    neither sector-ascending (C-LOOK) nor read-first (mqdd)."""
    st, issued, rule_seq = play_trace_with_rules(mk(S1_REQS), 8)
    ok = check("m01_bulk_first_then_lat_fifo",
               sectors(issued) == [4608, 3072, 5120, 3584, 2560],
               f"got={sectors(issued)}")
    ok &= check("m01_rules_bulk_bulk_lat_lat_lat",
                rule_seq == [4, 4, 5, 5, 5], f"got={rule_seq}")
    # lane counters: 2 rule-4 BULK resets leave lat_burst=0, then 3 rule-5
    # LAT increments -> 3 (uncapped path, but <8 here)
    ok &= check("m01_counter_lifetimes",
                st.lat_burst == 3 and st.emergency_burst == 0,
                f"lat_burst={st.lat_burst} emergency={st.emergency_burst}")
    # cross-lane check: reads submitted BEFORE some writes still issue
    # AFTER every write (BULK lane wins while non-empty) — already covered
    # by the order assertion above (writes 4608/3072 submitted before reads
    # yet BULK would also win if interleaved; see s2 interleave test).
    return ok


# ── (c) rule 1: LAT emergency dispatch + emergency_burst cap ────────────
def test_rule1_emergency_then_rule4():
    """Injected now = arrival+2ms: LAT head deadline (arrival+1ms) missed ->
    rule-1 LAT dispatches until emergency_burst==EMERGENCY_MAX(3), then rule
    4 BULK (rule 2 needs 10ms bulk wait, rule 3 needs lat_burst<8 — both
    blocked).  Pre-state: lat_burst saturated by a genuine rule-5 pure-LAT
    flood (counters are ONLY reset by BULK dispatch — bpf.c:201-202,
    :214-215)."""
    st = DdlState()
    t0 = 1_000_000
    # phase 1: pure-LAT flood -> rule 5 x10, lat_burst=10 (>= BURST_MAX),
    # emergency_burst stays 0
    flood = [Request("read", 102400 + 256 * i, 8) for i in range(10)]
    for i, rq in enumerate(flood):
        st.insert(rq, t0 + i * 1000)
    out = st.drain(t0 + 10 * 1000 + 100)
    ok = check("rule1_pre_flood_rule5_drain",
               sectors(out) == [102400 + 256 * i for i in range(10)] and
               st.lat_burst == 10 and st.emergency_burst == 0,
               f"lat_burst={st.lat_burst} emergency={st.emergency_burst}")
    # phase 2: fresh mixed batch (4 LAT + 2 BULK), inject now = +2ms
    t1 = t0 + 5_000_000
    lats = [Request("read", 200000 + 256 * i, 8) for i in range(4)]
    bulks = [Request("write", 300000 + 256 * i, 8) for i in range(2)]
    for i, rq in enumerate(lats + bulks):
        st.insert(rq, t1 + i * 1000)
    now = t1 + 2_000_000
    seq, rule_seq = [], []
    for _ in range(3):
        rq = st.dispatch(now)
        seq.append(rq)
        rule_seq.append(st.last_rule)
    # dispatches 1-3: rule 1 (emergency_burst 1,2,3; lat_burst 11,12,13)
    ok &= check("rule1_three_emergency_lat",
                rule_seq == [1, 1, 1] and st.emergency_burst == 3,
                f"rules={rule_seq} emergency={st.emergency_burst}")
    for _ in range(3):
        rq = st.dispatch(now)
        seq.append(rq)
        rule_seq.append(st.last_rule)
        if rq is None:
            break
    # dispatch 4: emergency cap + lat_burst>=8 + bulk_wait=2ms<10ms ->
    # rule 4 BULK, BOTH counters reset
    ok &= check("rule1_cap_then_rule4_bulk",
                rule_seq[3] == 4 and st.lat_burst == 0 and
                st.emergency_burst == 0,
                f"rules={rule_seq} lat_burst={st.lat_burst}")
    # dispatch 5: counters reset -> rule 1 fires again for the last LAT
    ok &= check("rule1_rearms_after_bulk_reset",
                rule_seq[4] == 1 and seq[4].sector == 200000 + 256 * 3,
                f"rules={rule_seq} got={[r.sector for r in seq]}")
    # dispatch 6: LAT empty -> rule 4 drains last BULK
    ok &= check("rule1_last_bulk_rule4",
                rule_seq[5] == 4 and seq[5].sector == 300000 + 256,
                f"rules={rule_seq}")
    ok &= check("rule1_full_sequence",
                [r.sector for r in seq] ==
                [200000, 200256, 200512, 300000, 200768, 300256],
                f"got={[r.sector for r in seq]}")
    return ok


# ── (d) rule 2: BULK starvation bail-out ────────────────────────────────
def test_rule2_bulk_starvation():
    """Injected now with BULK head aged 11ms -> rule 2 BULK even though LAT
    is pending, resetting BOTH counters (bpf.c:200-205).  LAT arrives late
    (deadline in the future) so rule 1 does not shadow rule 2."""
    st = DdlState()
    t0 = 1_000_000
    st.insert(Request("write", 300000, 8), t0)             # BULK, aged head
    st.insert(Request("read", 200000, 8), t0 + 10_500_000)  # LAT, hd in future
    st.lat_burst = 5
    st.emergency_burst = 2   # simulated history — must be reset by rule 2
    now = t0 + 11_000_000
    # BULK waited 11ms >= 10ms (bpf.c:188-190); LAT hd = t0+11.5ms > now
    # (no rule 1) and > now+200us (no rule 3)
    rq = st.dispatch(now)
    ok = check("rule2_starved_bulk_dispatched",
               rq is not None and rq.sector == 300000 and
               st.last_rule == 2,
               f"rule={st.last_rule} rq={rq}")
    ok &= check("rule2_resets_both_counters",
                st.lat_burst == 0 and st.emergency_burst == 0,
                f"lat_burst={st.lat_burst} emergency={st.emergency_burst}")
    # after the bail-out the LAT urgent/missed logic resumes normally
    rq2 = st.dispatch(now + 600_000)   # now'=t0+11.6ms > LAT hd (11.5ms)
    ok &= check("rule2_then_rule1_for_expired_lat",
                rq2 is not None and rq2.sector == 200000 and
                st.last_rule == 1, f"rule={st.last_rule}")
    return ok


# ── (e) rule 3: LAT urgent within the 200us guard ────────────────────────
def test_rule3_urgent_guard():
    """Injected now = arrival+900us: LAT hd (arrival+1ms) is within the
    200us guard -> rule 3 LAT dispatch preempts a pending BULK while
    lat_burst < 8 (bpf.c:207-211)."""
    st = DdlState()
    t0 = 1_000_000
    st.insert(Request("read", 200000, 8), t0)    # LAT, hd = t0+1ms
    st.insert(Request("write", 300000, 8), t0)  # BULK, would win via rule 4
    now = t0 + 900_000                          # hd <= now+200us -> urgent
    rq = st.dispatch(now)
    ok = check("rule3_urgent_lat_preempts_bulk",
               rq is not None and rq.sector == 200000 and
               st.last_rule == 3 and st.lat_burst == 1,
               f"rule={st.last_rule} lat_burst={st.lat_burst}")
    # guard precision: at now+700us the head is 300us away — outside the
    # 200us guard -> rule 4 BULK first (bpf.c:182-183 boundary)
    st2 = DdlState()
    st2.insert(Request("read", 200000, 8), t0)
    st2.insert(Request("write", 300000, 8), t0)
    rq2 = st2.dispatch(t0 + 700_000)
    ok &= check("rule3_guard_boundary_700us_bulk_first",
                rq2 is not None and rq2.sector == 300000 and
                st2.last_rule == 4, f"rule={st2.last_rule}")
    return ok


# ── (f) pure-LAT flood: rule 5 uncapped drain ────────────────────────────
def test_pure_lat_flood_uncapped():
    """12 LAT reads, fresh drain: rule 5 fires for every dispatch with NO
    burst cap (bpf.c:220-224 has no lat_burst check) — lat_burst reaches 12
    > BURST_MAX(8); order is arrival FIFO (anti-C-LOOK: M03/Mqd_rev pair)."""
    reqs = [Request("read", 102400 + 256 * i, 8) for i in range(12)]
    st, issued, rule_seq = play_trace_with_rules(reqs, 8)
    ok = check("rule5_uncapped_drain",
               sectors(issued) == [102400 + 256 * i for i in range(12)] and
               rule_seq == [5] * 12 and st.lat_burst == 12,
               f"lat_burst={st.lat_burst} rules={rule_seq[:3]}...")
    ok &= check("rule5_emergency_never_touched",
                st.emergency_burst == 0,
                f"emergency={st.emergency_burst}")
    return ok


# ── (g) overflow: 33rd insert -> -1 ──────────────────────────────────────
def test_lane_overflow():
    """bpf.c:128-134 + ioext_policy_shared.h:15 — 32 pending per LANE;
    the 33rd insert into that lane returns -1 (kernel bypasses), while the
    OTHER lane still accepts."""
    st = DdlState()
    ok = True
    ret = 0
    for i in range(MAX_PENDING):
        ret = st.insert(Request("read", 102400 + 256 * i, 8), i * 1000)
        if ret != 0:
            ok = False
            break
    ok &= check("lane_fill_32_ok",
                ret == 0 and len(st.lanes[DDL_LAT]) == MAX_PENDING)
    over = st.insert(Request("read", 999999, 8), 99_000)
    ok &= check("lane_overflow_33rd_rejected",
                over == -1 and len(st.lanes[DDL_LAT]) == MAX_PENDING,
                f"ret={over}")
    # per-lane isolation: BULK lane still accepts while LAT is full
    other = st.insert(Request("write", 888888, 8), 100_000)
    ok &= check("lane_overflow_other_lane_still_accepts",
                other == 0 and len(st.lanes[DDL_BULK]) == 1, f"ret={other}")
    return ok


# ── ioprio branches (rig-untestable, model-verified) ─────────────────────
def test_ioprio_classification():
    """bpf.c:108-112 — RT class -> LAT even for writes; IDLE -> BULK even
    for tiny reads.  Rig note: trace_driver's io_uring O_DIRECT path does
    not propagate ioprio (mqdd_prio_5 golden notes), so these branches are
    host-verified ONLY — documented, not chased in Tier-2 cases."""
    rt = (IOPRIO_CLASS_RT << IOPRIO_CLASS_SHIFT) | 4
    idle = (IOPRIO_CLASS_IDLE << IOPRIO_CLASS_SHIFT) | 7
    ok = check("ioprio_rt_write_is_LAT",
               ddl_classify(Request("write", 2560, 8, rt)) == DDL_LAT)
    ok &= check("ioprio_rt_large_write_is_LAT",
                ddl_classify(Request("write", 2560, 64, rt)) == DDL_LAT)
    ok &= check("ioprio_idle_small_read_is_BULK",
                ddl_classify(Request("read", 2560, 8, idle)) == DDL_BULK)
    ok &= check("ioprio_none_falls_to_heuristic",
                ddl_classify(Request("read", 2560, 8, 0)) == DDL_LAT and
                ddl_classify(Request("write", 2560, 8, 0)) == DDL_BULK)
    return ok


# ── deadline constants ──────────────────────────────────────────────────
def test_deadline_constants():
    """bpf.c:268 + :120-123 — LAT gets +1ms, BULK gets +20ms."""
    st = DdlState()
    st.insert(Request("read", 2560, 8), 5_000)
    st.insert(Request("write", 4608, 8), 5_000)
    ok = check("lat_deadline_is_1ms",
                st.lanes[DDL_LAT][0].deadline_ns == 5_000 + LAT_DEADLINE_NS)
    ok &= check("bulk_deadline_is_20ms",
                st.lanes[DDL_BULK][0].deadline_ns ==
                5_000 + BULK_DEADLINE_NS)
    return ok


def main():
    ok = True
    ok &= test_scenarios()          # (a) + (h) trace cross-check + ties
    ok &= test_trace_files_match()  # (h) golden cross-check
    ok &= test_cost_boundary()      # (b)
    ok &= test_fresh_burst_bulk_first()  # (a) M01 exact
    ok &= test_rule1_emergency_then_rule4()  # (c)
    ok &= test_rule2_bulk_starvation()       # (d)
    ok &= test_rule3_urgent_guard()         # (e)
    ok &= test_pure_lat_flood_uncapped()     # (f)
    ok &= test_lane_overflow()              # (g)
    ok &= test_ioprio_classification()
    ok &= test_deadline_constants()
    print()
    for sc in SCENARIOS:
        print(f"oracle {sc['name']} ({sc['trace']}) "
              f"expected_dispatch_order={json.dumps(sc['expected_issue'])}")
    print()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()