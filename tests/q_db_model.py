#!/usr/bin/env python3
"""q_db reference model — executable specification of the DB-workload
two-lane size-threshold FIFO core (userspace/q_db.bpf.c).

Design source: results/isol_bench/rig_ycsb/D5_YCSB_DESIGN.md §3.
Everything below is derived from userspace/q_db.bpf.c with citations;
descriptor construction from userspace/ioext_bpf_common.h, lane
capacity from userspace/ioext_policy_shared.h.  Any change to the BPF
core must be mirrored here and vice versa.

Model contract (cited against userspace/q_db.bpf.c):
  - two lanes: DB_F (fast <=64KB) / DB_B (bulk >64KB)  (bpf.c:44-47)
  - classification db_classify                        (bpf.c:99-103):
      cost <= 128 sectors (64KB) -> F, else B         (bpf.c:41-43)
      direction-BLIND: reads and writes are equal (WAL small writes are
      foreground-critical)                            (bpf.c:26-27)
    cost == nr_sectors (or 1 when 0)  (ioext_bpf_common.h:209)
    arrival_ns = bpf_ktime_get_ns() at insert         (ioext_bpf_common.h:201)
  - per-lane capacity IOSCHED_MAX_PENDING=32, overflow insert returns -1
    (kernel bypasses the policy for that request)     (bpf.c:74, :105-112,
                                                      ioext_policy_shared.h:15)
  - dispatch precedence db_dispatch, now = bpf_ktime_get_ns() at
    cgroup_dispatch                                    (bpf.c:160):
      1. B pending AND (B head waited >= 10ms OR f_burst >= 32) -> B;
         f_burst reset                                (bpf.c:127-135, :42-43)
         (anti-starvation: guards the q_prio D4 write-starvation ->
          memtable backpressure -> foreground collapse failure mode)
      2. F pending -> F (strict priority); f_burst++ (bpf.c:137-140)
      3. B pending -> B (work-conserving fallback); f_burst reset
                                                      (bpf.c:142-145)
    f_burst counts CONSECUTIVE F dispatches: only a B dispatch (rules 1
    and 3) resets it; inserts never touch it.
  - merge: allow_merge = false always                  (bpf.c:213-216)

Design-threshold provenance (D5_YCSB_DESIGN.md §2-3, matrix CSV):
  foreground point reads ~13KB (27 sectors) sync low-QD; WAL writes small;
  background SST/compaction writes ~350KB (700 sectors) async bulk.  The
  64KB threshold sits inside the 27x separation band (13KB vs 350KB) —
  unlike q_prio's 4KB threshold which misfiled 13KB foreground reads into
  the slow lane (its D4 collapse root cause #1).

Rig testability boundaries (documented — do NOT chase):
  - the 10ms B starvation bound is structurally hard to hit in the DT rig
    (delay_us applies only between fully-harvested batches); encoded here
    with injected-time self-tests.
  - burst-cap rule (32 consecutive F) IS observable in-rig with a large
    enough F batch plus a pending B, but no Tier-2 trace is registered in
    this round; the model self-tests it host-side.

Driver model (tests/full_suite/deterministic/trace_driver.c run_async):
  one batch is submitted with a single io_uring_enter, all its requests
  are inserted back-to-back, and the driver waits for every CQE before
  the next batch.  The model therefore inserts a whole batch (strictly
  increasing arrival times; gap=0 models equal-arrival ties) and then
  drains the scheduler queue before the next batch.  q_db is IMMUNE to
  arrival ties: per-lane push_tail keeps submission order (stable append).
"""
import os
import re
import sys

# ── constants (userspace/q_db.bpf.c:41-43) ──
F_MAX_SECTORS = 128                 # bpf.c:41  Q_DB_F_MAX_SECTORS (64KB)
B_STARVATION_NS = 10_000_000        # bpf.c:42  Q_DB_B_STARVATION_NS (10ms)
F_BURST_MAX = 32                    # bpf.c:43  Q_DB_F_BURST_MAX
MAX_PENDING = 32                    # ioext_policy_shared.h:15

# lane ids (bpf.c:45-48)
DB_F, DB_B = 0, 1
LANE_NAME = {DB_F: "F", DB_B: "B"}

# rule ids (see db_dispatch in this model)
RULE_FORCED_B = 1                   # anti-starvation forced B
RULE_F = 2                         # strict-priority F
RULE_FALLBACK_B = 3                # work-conserving B


class Request:
    def __init__(self, op, sector, nr_sectors):
        self.op = op
        self.sector = sector
        # cost == nr_sectors, or 1 when 0 (ioext_bpf_common.h:209)
        self.cost = nr_sectors if nr_sectors else 1
        self.arrival_ns = 0

    @property
    def is_write(self):
        # IOSCHED_REQF_WRITE from opf (ioext_bpf_common.h:154-159);
        # classification is direction-blind, kept only for readability
        return "write" in self.op

    def __repr__(self):
        return f"{'W' if self.is_write else 'R'}{self.sector}(nr={self.cost})"


def db_classify(req):
    """bpf.c:99-103 — size only: cost <= 128 sectors -> F, else B."""
    return DB_F if req.cost <= F_MAX_SECTORS else DB_B


class DbState:
    """Per-hctx local state (bpf.c:50-60) stored in the ARRAY state_map
    (bpf.c:62-70; per-hctx, shared across cgroups — root-policy rig)."""

    def __init__(self):
        self.lanes = {DB_F: [], DB_B: []}
        self.f_burst = 0
        self.enqueued = {DB_F: 0, DB_B: 0}
        self.dispatched = {DB_F: 0, DB_B: 0}
        self.last_rule = None

    # ── insert_request hook (bpf.c:167-208) ──
    def insert(self, req, arrival_ns):
        """Returns 0 accept, -1 overflow (bpf.c:105-112)."""
        req.arrival_ns = arrival_ns
        lane = db_classify(req)                       # bpf.c:192
        target = self.lanes[lane]
        if len(target) >= MAX_PENDING:                 # bpf.c:106
            return -1
        target.append(req)                             # push_tail bpf.c:108-109
        self.enqueued[lane] += 1                       # bpf.c:110
        return 0

    # ── dispatch hook (bpf.c:116-150); now_ns from bpf_ktime_get_ns ──
    def dispatch(self, now_ns):
        b_starved = False
        if self.lanes[DB_B]:                          # bpf.c:127-131
            b_wait = now_ns - self.lanes[DB_B][0].arrival_ns
            if b_wait >= B_STARVATION_NS:
                b_starved = True

        # rule 1: forced anti-starvation B (starve timeout OR burst cap)
        if self.lanes[DB_B] and (b_starved or self.f_burst >= F_BURST_MAX):
            self.f_burst = 0
            self.last_rule = RULE_FORCED_B
            return self._pop(DB_B)

        # rule 2: F strict priority, f_burst++
        if self.lanes[DB_F]:
            self.f_burst += 1
            self.last_rule = RULE_F
            return self._pop(DB_F)

        # rule 3: B work-conserving fallback, f_burst reset
        if self.lanes[DB_B]:
            self.f_burst = 0
            self.last_rule = RULE_FALLBACK_B
            return self._pop(DB_B)

        self.last_rule = None
        return None

    def _pop(self, lane):
        req = self.lanes[lane].pop(0)
        self.dispatched[lane] += 1
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
    """Play a trace the way trace_driver run_async does: submit one batch
    (inserts with strictly increasing arrival times; gap=0 models
    equal-arrival ties), drain it fully before the next batch.
    dispatch_now_fn(st, arrival) lets self-tests inject time (rule 1)."""
    st = DbState()
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


def sectors(rqs):
    return [r.sector for r in rqs]


def mk(reqs):
    return [Request(*r) for r in reqs]


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ── source cross-check: model constants must equal the BPF file ──
def bpf_source_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "..", "userspace", "io", "q_db.bpf.c")


def test_source_constants_match():
    """Guard against model/BPF drift: parse the #define lines from
    q_db.bpf.c and require exact agreement with the model constants."""
    path = bpf_source_path()
    if not os.path.exists(path):
        return check("source_cross_check_skipped_no_file", False,
                     f"path={path}")
    src = open(path).read()
    defs = dict(re.findall(
        r"#define\s+(Q_DB_\w+)\s+(\d+)", src))
    ok = check("source_has_all_defines",
               set(defs) == {"Q_DB_F_MAX_SECTORS", "Q_DB_B_STARVATION_NS",
                             "Q_DB_F_BURST_MAX"},
               f"found={sorted(defs)}")
    ok &= check("source_F_MAX_SECTORS_matches_model",
                int(defs.get("Q_DB_F_MAX_SECTORS", -1)) == F_MAX_SECTORS,
                f"bpf={defs.get('Q_DB_F_MAX_SECTORS')}")
    ok &= check("source_B_STARVATION_matches_model",
                int(defs.get("Q_DB_B_STARVATION_NS", -1)) == B_STARVATION_NS,
                f"bpf={defs.get('Q_DB_B_STARVATION_NS')}")
    ok &= check("source_F_BURST_MAX_matches_model",
                int(defs.get("Q_DB_F_BURST_MAX", -1)) == F_BURST_MAX,
                f"bpf={defs.get('Q_DB_F_BURST_MAX')}")
    ok &= check("source_max_pending_matches_model",
                "IOSCHED_MAX_PENDING" in src and MAX_PENDING == 32,
                "ioext_policy_shared.h:15")
    return ok


# ── (a) size-classification boundary (THE design contract) ─────────────
def test_size_boundary():
    """bpf.c:99-103 — cost <= 128 sectors -> F, else B; direction-blind.
    13KB foreground read (27 sectors) must be F — the q_prio D4 fix;
    350KB compaction write (700 sectors) must be B; exact 64KB boundary
    on both sides and both directions."""
    ok = True
    ok &= check("classify_fg_read_13KB_is_F",
                db_classify(Request("read", 2560, 27)) == DB_F,
                "q_prio's 4KB threshold misfiled this (D4 root cause)")
    ok &= check("classify_wal_write_4KB_is_F",
                db_classify(Request("write", 2560, 8)) == DB_F)
    ok &= check("classify_wal_write_64KB_is_F",
                db_classify(Request("write", 2560, 128)) == DB_F)
    ok &= check("classify_sst_write_350KB_is_B",
                db_classify(Request("write", 2560, 700)) == DB_B)
    ok &= check("boundary_read_128sec_is_F",
                db_classify(Request("read", 2560, 128)) == DB_F)
    ok &= check("boundary_write_128sec_is_F",
                db_classify(Request("write", 2560, 128)) == DB_F)
    ok &= check("boundary_read_129sec_is_B",
                db_classify(Request("read", 2560, 129)) == DB_B)
    ok &= check("boundary_write_129sec_is_B",
                db_classify(Request("write", 2560, 129)) == DB_B)
    ok &= check("direction_blind_read_write_same_class",
                db_classify(Request("read", 2560, 200)) ==
                db_classify(Request("write", 2560, 200)) == DB_B)
    ok &= check("classify_cost0_reads_as_1_is_F",
                db_classify(Request("read", 2560, 0)) == DB_F,
                "cost=nr_sectors?1 (ioext_bpf_common.h:209)")
    return ok


# ── (b) F strict priority over fresh B ──────────────────────────────────
def test_f_strict_priority():
    """Fresh mixed burst (no starvation, burst counter cold): rule 2
    drains the F lane arrival-FIFO first, then rule 3 drains B — the
    foreground traffic never queues behind a 350KB bulk write."""
    reqs = mk([("write", 4608, 700), ("write", 3072, 700),   # B bulk
               ("read", 5120, 27), ("read", 3584, 27),        # F reads
               ("write", 2560, 8)])                           # F WAL write
    st, issued, rule_seq = play_trace(reqs, 8)
    ok = check("mixed_burst_f_first_then_b",
               sectors(issued) == [5120, 3584, 2560, 4608, 3072],
               f"got={sectors(issued)}")
    ok &= check("mixed_burst_rules_f_f_f_b_b",
                rule_seq == [RULE_F, RULE_F, RULE_F,
                             RULE_FALLBACK_B, RULE_FALLBACK_B],
                f"got={rule_seq}")
    ok &= check("f_burst_reset_by_fallback_b",
                st.f_burst == 0, f"f_burst={st.f_burst}")
    # equal-arrival ties keep the same order (stable per-lane append)
    _, issued_tie, rule_tie = play_trace(reqs, 8, arrival_gap_ns=0)
    ok &= check("f_priority_robust_to_equal_arrival",
                sectors(issued_tie) == [5120, 3584, 2560, 4608, 3072] and
                rule_tie == rule_seq,
                f"tie={sectors(issued_tie)}/{rule_tie}")
    return ok


# ── (c) rule 1a: B starvation timeout (>= 10ms B-head wait) ─────────────
def test_b_starvation_timeout():
    """B head aged past 10ms -> forced B even with F pending; f_burst
    reset.  Boundary: 9.9ms -> not starved (F first); 10ms -> starved."""
    ok = True
    # aged 10ms exactly (b_wait >= 10ms, bpf.c:131)
    st = DbState()
    t0 = 1_000_000
    st.insert(Request("write", 300000, 700), t0)             # B head
    st.insert(Request("read", 200000, 27), t0 + 5_000)        # F arrives later
    rq = st.dispatch(t0 + B_STARVATION_NS)
    ok &= check("starved_b_head_10ms_forced_b",
                rq is not None and rq.sector == 300000 and
                st.last_rule == RULE_FORCED_B, f"rule={st.last_rule}")
    # one tick before the bound -> F strict priority wins
    st2 = DbState()
    st2.insert(Request("write", 300000, 700), t0)
    st2.insert(Request("read", 200000, 27), t0 + 5_000)
    rq2 = st2.dispatch(t0 + B_STARVATION_NS - 1)
    ok &= check("starvation_boundary_just_under_10ms_is_F",
                rq2 is not None and rq2.sector == 200000 and
                st2.last_rule == RULE_F, f"rule={st2.last_rule}")
    # forced B resets f_burst (simulated history)
    st3 = DbState()
    st3.insert(Request("write", 300000, 700), t0)
    st3.insert(Request("read", 200000, 27), t0 + 1_000)
    st3.f_burst = 20   # simulated history — must be reset by rule 1
    st3.dispatch(t0 + B_STARVATION_NS + 1)
    ok &= check("forced_b_resets_f_burst",
                st3.f_burst == 0, f"f_burst={st3.f_burst}")
    # after the forced B, remaining F resumes strict priority
    rq3 = st3.dispatch(t0 + B_STARVATION_NS + 2)
    ok &= check("forced_b_then_F_resumes",
                rq3 is not None and rq3.sector == 200000 and
                st3.last_rule == RULE_F and st3.f_burst == 1)
    return ok


# ── (d) rule 1b: burst cap (>= 32 consecutive F dispatches) ────────────
def test_burst_cap():
    """B pending + F flood: exactly F_BURST_MAX consecutive F dispatches,
    then rule 1 forces one B, then F resumes — the D4 anti-starvation
    contract (at most 32 F dispatches between consecutive B dispatches).
    Lane capacity discipline: batches keep each lane <= 32 pending, so a
    second F batch is inserted after the first batch has partially
    drained (fresh requests keep arriving while the queue drains)."""
    ok = True
    st = DbState()
    t0 = 1_000_000
    # batch 1: 32 F (lane-full, exactly IOSCHED_MAX_PENDING) + 2 B bulk
    # writes, all fresh (no time starvation possible at these gaps)
    f_reqs = [Request("read", 200000 + 256 * i, 27) for i in range(32)]
    b_reqs = [Request("write", 300000 + 256 * i, 700) for i in range(2)]
    for i, rq in enumerate(f_reqs + b_reqs):
        st.insert(rq, t0 + i * 1000)
    now = t0 + 40_000
    rule_seq = []
    issued = []
    for _ in range(33):
        rq = st.dispatch(now)
        if rq is None:
            break
        issued.append(rq)
        rule_seq.append(st.last_rule)
    # dispatches 1-32: F (f_burst 1..32); 33: forced B (f_burst reset)
    ok &= check("burst_cap_32F_then_forcedB",
                rule_seq == [RULE_F] * 32 + [RULE_FORCED_B],
                f"got={rule_seq[:33]}")
    ok &= check("burst_cap_first_batch_issue_order",
                sectors(issued) ==
                [200000 + 256 * i for i in range(32)] + [300000],
                f"got={sectors(issued)}")
    # batch 2: 8 more F arrive while B[1] is still queued (F lane now has
    # room); they resume strict priority ahead of the aged-but-below-
    # 10ms... aged far below the burst cap, so F wins until the lane
    # empties, then rule 3 drains the last bulk write.
    f2 = [Request("read", 400000 + 256 * i, 27) for i in range(8)]
    for i, rq in enumerate(f2):
        st.insert(rq, t0 + 100_000 + i * 1000)
    now2 = t0 + 120_000
    for _ in range(9):
        rq = st.dispatch(now2)
        if rq is None:
            break
        issued.append(rq)
        rule_seq.append(st.last_rule)
    expected_order = ([200000 + 256 * i for i in range(32)] + [300000] +
                      [400000 + 256 * i for i in range(8)] + [300256])
    expected_rules = ([RULE_F] * 32 + [RULE_FORCED_B] +
                      [RULE_F] * 8 + [RULE_FALLBACK_B])
    ok &= check("burst_cap_issue_order",
                sectors(issued) == expected_order,
                f"got={sectors(issued)}")
    ok &= check("burst_cap_rule_sequence",
                rule_seq == expected_rules, f"got={rule_seq}")
    # 42 dispatches total; f_burst ends at 0 (the trailing fallback B
    # reset it after the second 8-F run reached only 8)
    ok &= check("burst_cap_f_burst_lifecycle",
                len(issued) == 42 and st.f_burst == 0,
                f"n={len(issued)} f_burst={st.f_burst}")
    ok &= check("burst_cap_final_counters",
                st.dispatched[DB_F] == 40 and st.dispatched[DB_B] == 2,
                f"F={st.dispatched[DB_F]} B={st.dispatched[DB_B]}")
    return ok


# ── (e) work-conserving fallback ────────────────────────────────────────
def test_work_conserving():
    """B-only queue drains arrival-FIFO IMMEDIATELY (rule 3) — no
    starvation wait, no burst requirement; and an empty lane never
    blocks the other."""
    ok = True
    st, issued, rule_seq = play_trace(
        mk([("write", 4608, 700), ("write", 3072, 700),
            ("write", 3584, 129)]), 8)
    ok &= check("b_only_drains_fifo_immediately",
                sectors(issued) == [4608, 3072, 3584] and
                rule_seq == [RULE_FALLBACK_B] * 3,
                f"got={sectors(issued)}/{rule_seq}")
    st2, issued2, rule_seq2 = play_trace(
        mk([("read", 5120, 27), ("read", 3584, 27)]), 8)
    ok &= check("f_only_drains_fifo_immediately",
                sectors(issued2) == [5120, 3584] and
                rule_seq2 == [RULE_F, RULE_F],
                f"got={sectors(issued2)}/{rule_seq2}")
    # f_burst is UNCAPPED on a pure F flood (only B dispatches reset it;
    # a cap would stall a B-free system — there is nothing to protect)
    st3, issued3, rule_seq3 = play_trace(
        mk([("read", 102400 + 256 * i, 27) for i in range(40)]), 8)
    ok &= check("f_flood_uncapped_f_burst",
                sectors(issued3) == [102400 + 256 * i for i in range(40)] and
                rule_seq3 == [RULE_F] * 40 and st3.f_burst == 40,
                f"f_burst={st3.f_burst}")
    return ok


# ── (f) per-lane overflow: 33rd insert -> -1 (fail-open bypass) ─────────
def test_lane_overflow():
    """bpf.c:105-112 — 32 pending per LANE; the 33rd insert into that lane
    returns -1 (kernel bypasses), while the OTHER lane still accepts."""
    ok = True
    st = DbState()
    ret = 0
    for i in range(MAX_PENDING):
        ret = st.insert(Request("read", 102400 + 256 * i, 27), i * 1000)
        if ret != 0:
            ok = False
            break
    ok &= check("lane_fill_32_ok",
                ret == 0 and len(st.lanes[DB_F]) == MAX_PENDING)
    over = st.insert(Request("read", 999999, 27), 99_000)
    ok &= check("lane_overflow_33rd_rejected",
                over == -1 and len(st.lanes[DB_F]) == MAX_PENDING,
                f"ret={over}")
    other = st.insert(Request("write", 888888, 700), 100_000)
    ok &= check("lane_overflow_other_lane_still_accepts",
                other == 0 and len(st.lanes[DB_B]) == 1, f"ret={other}")
    return ok


# ── (g) counter lifetime: consecutive semantics ────────────────────────
def test_consecutive_f_semantics():
    """f_burst counts CONSECUTIVE F dispatches: a fallback B (rule 3, F
    momentarily empty) resets it, so 20 F + B + 20 F never triggers the
    burst cap even though 40 F dispatches happened in total."""
    ok = True
    st = DbState()
    t0 = 1_000_000
    # batch 1: 20 F then 1 B (lane below capacity), drained fully
    reqs = ([Request("read", 200000 + 256 * i, 27) for i in range(20)] +
            [Request("write", 300000, 700)])
    for i, rq in enumerate(reqs):
        st.insert(rq, t0 + i * 1000)
    rule_seq = []
    for _ in range(25):
        rq = st.dispatch(t0 + 30_000)
        if rq is None:
            break
        rule_seq.append(st.last_rule)
    # 20 F (burst 20) -> F lane empty -> B dispatched by rule 3 (reset)
    ok &= check("sandwich_batch1_20F_then_fallback_B",
                rule_seq == [RULE_F] * 20 + [RULE_FALLBACK_B] and
                st.f_burst == 0,
                f"rules={rule_seq[:5]}... f_burst={st.f_burst}")
    # batch 2: 20 more F; cumulative F dispatches reach 40 but the
    # in-between B reset the budget — burst grows only to 20, no forced B
    reqs2 = [Request("read", 400000 + 256 * i, 27) for i in range(20)]
    for i, rq in enumerate(reqs2):
        st.insert(rq, t0 + 100_000 + i * 1000)
    for _ in range(25):
        rq = st.dispatch(t0 + 130_000)
        if rq is None:
            break
        rule_seq.append(st.last_rule)
    ok &= check("sandwich_b_resets_burst_budget",
                rule_seq == [RULE_F] * 20 + [RULE_FALLBACK_B] +
                [RULE_F] * 20 and st.f_burst == 20 and
                st.dispatched[DB_F] == 40,
                f"rules={rule_seq[:5]}...{rule_seq[-5:]} f_burst={st.f_burst}")
    # true consecutiveness: exactly 32 F with a B waiting behind — the
    # 33rd dispatch is the FORCED B (f_burst hit 32); nothing follows it
    st2 = DbState()
    reqs3 = ([Request("read", 200000 + 256 * i, 27) for i in range(32)] +
             [Request("write", 300000, 700)])
    for i, rq in enumerate(reqs3):
        st2.insert(rq, t0 + i * 1000)
    seq2 = []
    for _ in range(34):
        rq = st2.dispatch(t0 + 40_000)
        if rq is None:
            break
        seq2.append(st2.last_rule)
    ok &= check("exactly_32_consecutive_F_then_forced_B",
                seq2 == [RULE_F] * 32 + [RULE_FORCED_B] and
                len(seq2) == 33 and st2.f_burst == 0,
                f"n={len(seq2)} got={seq2[:33]}")
    return ok


# ── (h) workloada-shaped scenario: YCSB mix ─────────────────────────────
def test_wa_shaped_scenario():
    """YCSB workloada block-layer shape (D5 §2): foreground 13KB reads +
    small WAL writes vs ~350KB compaction writes, one batch (16-thread
    sync QD window).  F traffic issues in arrival order ahead of every
    fresh bulk write; bulk FIFO behind."""
    reqs = mk([("read", 5000, 27), ("write", 6000, 8),      # F read, F WAL
               ("write", 7000, 700),                        # B compaction
               ("read", 8000, 27), ("read", 9000, 27),      # F reads
               ("write", 9500, 700)])                       # B compaction
    st, issued, rule_seq = play_trace(reqs, 8)
    ok = check("wa_shape_f_arrival_fifo_then_b_fifo",
               sectors(issued) == [5000, 6000, 8000, 9000, 7000, 9500],
               f"got={sectors(issued)}")
    ok &= check("wa_shape_rules",
                rule_seq == [RULE_F] * 4 + [RULE_FALLBACK_B] * 2,
                f"got={rule_seq}")
    ok &= check("wa_shape_lane_counts",
                st.enqueued[DB_F] == 4 and st.enqueued[DB_B] == 2)
    return ok


def main():
    ok = True
    ok &= test_source_constants_match()   # model/BPF drift guard
    ok &= test_size_boundary()            # (a) 64KB boundary, direction-blind
    ok &= test_f_strict_priority()        # (b) F strict priority + ties
    ok &= test_b_starvation_timeout()      # (c) 10ms B-head timeout
    ok &= test_burst_cap()                # (d) 32-F burst cap
    ok &= test_work_conserving()          # (e) work-conserving + uncapped F
    ok &= test_lane_overflow()            # (f) 32/lane fail-open
    ok &= test_consecutive_f_semantics()  # (g) counter lifetimes
    ok &= test_wa_shaped_scenario()       # (h) workloada shape
    print()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()