#!/usr/bin/env python3
"""arb_cost reference model — executable specification of the cost-weighted
DRR core (gate 0 of the arb_cost CI loop).

Mirrors userspace/ioext_arb_common.h (arb_cost_estimate / arb_cost_size_bucket
/ arb_cost_op_type / arb_drr_pick / arb_note_* hooks) and the arb_cost.bpf.c
hook wiring (dispatch-time cost charge, constant boost 1, strict mode).
Per ARB_POLICY_DEV_GUIDE.md §1 + §7 the model expresses the specification
BEFORE the BPF is trusted: any change to the BPF DRR core must be mirrored
here and vice versa.  The acceptance script tests/arb_cost_accept.sh runs
this self-test as its first gate (`timeout 120 python3 tests/arb_cost_model.py`).
Exit 0 = all PASS.

Model contract (identical to ioext_arb_common.h / ARB_POLICY_DEV_GUIDE.md §3):
  - eligibility: pending > 0 only (arbiter's own count from inserted_request;
    never policy_meta.queue_depth)
  - turn grant: deficit <= 0 and not yet refilled this round
    (refilled_round != current_round) → arm round budget
    quantum * effective_weight, capped at 2x, and serve immediately
    (refill IS the turn grant — classic DRR; at most one grant per round)
  - round budget floor: max(MIN_BUDGET, last_round_max_charge) — the
    workload-adaptive part (arb_hctx_state) rolls at every round advance
    (arb_note_charge_max fires from each arbiter's dispatched_request)
  - serve: deficit > 0 → keep selecting the same tenant; each dispatch
    charges cost = arb_cost_estimate(opf, nr_sectors) (dispatch time!)
  - exhaust: deficit <= 0 after this round's refill → cursor advances
  - round boundary: full scan, nobody servable → current_round += 1,
    return 0 (single NULL dispatch; blk-mq re-kicks on next completion)
  - empty tenant: pending == 0 → skip WITHOUT refill (no credit hoarding)
  - boost: cost arbiter uses a constant boost of 1 (cost_get_boost)
  - requeue: refund the charged cost + inflight--, NEVER pending++
    (the kernel re-inserts via blk_mq_insert_request → inserted_request;
    guide §6 pitfall 4 / ioext_arb_common.h requeue contract)
  - dispatch_failed: pending-- only (guide §6 pitfall 3)
  - charge unit is COST, not bytes (refine.md POL-04: dispatch-time cost
    charge replaces completion-time charging)

Acceptance encoded (refine.md POL-04 + §4 "4. COST 验收"): equal weights,
A = 4K reads (cost 64) vs B = 4K writes (cost 256) → charged_cost ratio ≈
1:1 while request counts are NOT 1:1 (~4:1).  The reverse (request-count
or byte fairness) is a spec failure of the implementation, never of this
model.

Structurally cloned from tests/arb_wdrr_model.py (same Tenant/Arbiter
shape, drive_backlogged, check() printer, MODEL PASS/FAIL + exit convention).
"""
import sys

# ---------------------------------------------------------------------------
# Constants (encode ioext_arb_common.h + arb_cost.bpf.c verbatim)
# ---------------------------------------------------------------------------

SECTOR = 512                  # arb_cost_size_bucket: bytes = nr_sectors * 512
QUANTUM = 1                   # ARB_COST_QUANTUM (arb_cost.bpf.c, S6): cost
                              # units per unit of effective weight — with the
                              # x64 table, budget(w) = w/64 dispatches of a
                              # 4K read, binding per visit
CAP_MULT = 2                  # ARB_WDRR_IDLE_CAP_MULT: refill cap = 2x budget
MIN_BUDGET = 64               # ARB_COST_MIN_BUDGET (arb_cost.bpf.c, S6): one
                              # 4K-read charge, cost-denominated (the model
                              # was stale at 1024/256KB vs the BPF's 8/256
                              # since the S6 gate commit; S6 restores sync)
MAX_TENANTS = 16              # ARB_MAX_TENANTS
# Round-budget floor (S6): cost-denominated 64 plus a workload-adaptive
# part — floor = max(MIN_BUDGET, last_round_max_charge) (arb_hctx_state,
# rolled at every round advance), so a large-request stream keeps cost
# fairness (refill can always afford one max-charge request) while the
# small quantum binds per visit for weighted share.  Weights below 64
# are floor-compressed to the same budget (test_min_budget_floor);
# weight-ratio fixtures use >= 100 (case3) / >= 256 (case1/case2).

ARB_COST_OP_READ = 0          # ARB_COST_OP_READ
ARB_COST_OP_WRITE = 1         # ARB_COST_OP_WRITE

# arb_cost_estimate base_cost[ARB_COST_OPS][ARB_COST_BUCKETS]
# rows: READ, WRITE; cols: buckets 4K, 16K, 64K, 128K, >128K
# S6: table scaled x64 (4K read = 64) so ARB_COST_QUANTUM=1 gives
# budget(w) = w/64 dispatches of a 4K read; read:write ratio 4:1 and the
# size-bucket ratios are unchanged.
COST_TABLE = (
    (64, 256, 1024, 2048, 4096),       # READ  → 4K read cost 64, 128K read cost 2048
    (256, 1024, 4096, 8192, 16384),    # WRITE → 4K write cost 256, 128K write cost 8192
)


def arb_cost_size_bucket(nr_sectors):
    """Mirror ioext_arb_common.h arb_cost_size_bucket()."""
    nbytes = nr_sectors * SECTOR
    if nbytes <= 4096:
        return 0
    if nbytes <= 16384:
        return 1
    if nbytes <= 65536:
        return 2
    if nbytes <= 131072:
        return 3
    return 4


def arb_cost_op_type(opf):
    """Mirror ioext_arb_common.h arb_cost_op_type(): opf & 0xff == 1 → WRITE."""
    op = opf & 0xFF
    return ARB_COST_OP_WRITE if op == 1 else ARB_COST_OP_READ


def arb_cost_estimate(opf, nr_sectors):
    """Mirror ioext_arb_common.h arb_cost_estimate(): the arbiter's charge
    unit.  (The C fallback `return nr_sectors` for out-of-range op/bucket is
    unreachable through the inline helpers: op ∈ {0,1}, bucket ∈ {0..4}.)"""
    return COST_TABLE[arb_cost_op_type(opf)][arb_cost_size_bucket(nr_sectors)]


MAX_COST = COST_TABLE[ARB_COST_OP_WRITE][4]   # 16384: max single-request charge (4 x 4096)


# Request fixtures: (opf, nr_sectors).  opf low byte: 0 = READ, 1 = WRITE.
R4K = (0, 8)       # 4096B   → bucket 0 → READ cost 64
W4K = (1, 8)       # 4096B   → bucket 0 → WRITE cost 256
R16K = (0, 32)     # 16384B  → bucket 1 → READ cost 256
W16K = (1, 32)     # 16384B  → bucket 1 → WRITE cost 1024
R64K = (0, 128)    # 65536B  → bucket 2 → READ cost 1024
W64K = (1, 128)    # 65536B  → bucket 2 → WRITE cost 4096
R128K = (0, 256)   # 131072B → bucket 3 → READ cost 2048
W128K = (1, 256)   # 131072B → bucket 3 → WRITE cost 8192
R256K = (0, 512)   # 262144B → bucket 4 → READ cost 4096
W256K = (1, 512)   # 262144B → bucket 4 → WRITE cost 16384 (max charge)


# ---------------------------------------------------------------------------
# Tenant / Arbiter (mirror arb_tenant_state + arb_drr_pick + arb_note_*)
# ---------------------------------------------------------------------------

class Tenant:
    def __init__(self, cgid, weight):
        self.cgid = cgid
        self.weight = weight
        self.deficit = 0
        self.refilled_round = -1
        self.pending = 0
        self.inflight = 0
        self.dispatches = 0
        # shared struct tracks both; the cost charge path never touches
        # completed_bytes (arb_note_dispatched_cost updates completed_cost only)
        self.completed_bytes = 0
        self.completed_cost = 0
        self.requeues = 0
        # requeued_bytes is the shared struct's field name; arb_note_requeue
        # accumulates the refund there even when the refund is in COST units
        self.requeued_bytes = 0
        self.dispatch_log = []
        self.serve_selects = 0
        self.refill_arms = 0
        self.wrap_selects = 0      # struct parity; current arb_drr_pick never
                                   # increments it (C parity — keep at 0)
        self.skip_pending_zero = 0

    def budget(self, floor_max=0):
        """Round budget = QUANTUM * effective_weight, floored at
        max(MIN_BUDGET, floor_max = last_round_max_charge)
        (arb_drr_pick refill arithmetic; boost is constant 1 for cost)."""
        ew = self.weight           # * cost_get_boost() == 1
        if ew == 0:
            ew = 1
        floor = max(MIN_BUDGET, floor_max)
        if QUANTUM * ew < floor:
            ew = (floor + QUANTUM - 1) // QUANTUM
        return QUANTUM * ew


class CostArbiter:
    """Per-hctx cost-DRR core: slots[0..15] hold cgids (0 = empty), cursor
    points at the last selected slot.  Round semantics mirror arb_drr_pick:
    refill == turn grant, at most once per round.  The budget floor is
    workload-adaptive (arb_hctx_state): max(MIN_BUDGET,
    last_round_max_charge), rolled at every round advance."""

    def __init__(self):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.cursor = 0
        self.current_round = 0
        self.round_max_charge = 0
        self.last_round_max_charge = 0

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    def pick(self):
        base = self.cursor
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.pending == 0:
                ts.skip_pending_zero += 1
                continue
            if ts.deficit > 0:
                # Turn in progress: keep serving this tenant.
                self.cursor = idx
                ts.serve_selects += 1
                return cgid
            if ts.refilled_round != self.current_round:
                # New turn: grant this round's budget and serve it
                # (refill IS the turn grant).
                budget = ts.budget(self.last_round_max_charge)
                ts.deficit = min(ts.deficit + budget, budget * CAP_MULT)
                ts.refilled_round = self.current_round
                ts.refill_arms += 1
                self.cursor = idx
                return cgid
            # Already had its turn this round: move the cursor past it.
            self.cursor = (idx + 1) % MAX_TENANTS
        # Round complete (or nothing eligible): advance the round and
        # roll the adaptive floor window (arb_drr_pick round advance).
        self.last_round_max_charge = self.round_max_charge
        self.round_max_charge = 0
        self.current_round += 1
        return 0

    def advance_cursor(self):
        """Mirror arb_drr_advance_cursor (dispatch_failed hook wiring)."""
        self.cursor = (self.cursor + 1) % MAX_TENANTS

    # framework hook handlers (mirror arb_note_*)

    def note_inserted(self, cgid):
        self.tenants[cgid].pending += 1

    def note_dispatched(self, cgid, cost):
        """Mirror arb_note_dispatched_cost: dispatch-time cost charge."""
        ts = self.tenants[cgid]
        ts.deficit -= cost
        if ts.pending > 0:
            ts.pending -= 1
        ts.inflight += 1
        ts.completed_cost += cost
        ts.dispatches += 1
        ts.dispatch_log.append(cost)
        if cost > self.round_max_charge:
            self.round_max_charge = cost

    def note_requeue(self, cgid, refund):
        """Mirror arb_note_requeue: refund only; never pending++ (the kernel
        re-inserts the request, re-firing inserted_request — guide §6 pitfall 4).
        Refund must mirror what dispatched_request charged (same cost unit)."""
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1
        if refund:
            ts.deficit += refund
            ts.requeued_bytes += refund
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1

    def note_dispatch_failed(self, cgid):
        """Mirror arb_note_dispatch_failed: retire one pending entry only
        (the framework popped the rq before validating it — guide §6 pitfall 3)."""
        ts = self.tenants[cgid]
        if ts.pending > 0:
            ts.pending -= 1

    # BPF hook wiring (arb_cost.bpf.c): refund/charge recompute the estimate
    # from (opf, nr_sectors) so both sides use the same unit.

    def dispatched_request(self, cgid, opf, nr_sectors):
        self.note_dispatched(cgid, arb_cost_estimate(opf, nr_sectors))

    def requeue_refund(self, cgid, opf, nr_sectors):
        self.note_requeue(cgid, arb_cost_estimate(opf, nr_sectors))

    def dispatch_failed(self, cgid):
        """arb_cost.bpf.c dispatch_failed = arb_note_dispatch_failed
        + arb_drr_advance_cursor."""
        self.note_dispatch_failed(cgid)
        self.advance_cursor()


# ---------------------------------------------------------------------------
# Saturated drive
# ---------------------------------------------------------------------------

def drive_backlogged(arb, requests, rounds=None, total=None, fast=True):
    """Keep every tenant backlogged at exactly one pending request (re-insert
    after each dispatch, like the framework re-queueing issued IO), mirroring
    arb_wdrr_model.drive_backlogged.  `requests` maps cgid -> (opf, nr_sectors);
    every request of a tenant inside one drive is identical, and its cost is
    arb_cost_estimate(opf, nr_sectors) — exactly what arb_cost.bpf.c charges.

    Stops when `rounds` DRR rounds have fully completed (the round-boundary
    NULL pick fired) or `total` dispatches were made (one of the two must be
    given — a backlogged drive never idles).  A pick returning 0 is a round
    boundary: blk-mq retries on the next completion kick, so the model
    retries too.

    fast=True collapses each run of same-cost dispatches inside one turn
    into O(1) arithmetic: n = min(ceil(deficit/cost), remaining) requests
    advance at once, keeping per-request fidelity at turn boundaries.
    Bookkeeping is identical to the per-request loop (pending oscillates
    1→0→1 per dispatch, inflight nets to 0, dispatch_log/serve_selects grow
    by n, round_max_charge rises to the batch's cost) — proven bit-exact by
    test_fastforward_parity.  Performance: the S6 quantum makes turns tiny
    (budget(w) = w cost units), so this is pure parity machinery.
    """
    if rounds is None and total is None:
        raise ValueError("drive_backlogged needs a rounds or total stop condition")
    costs = {cg: arb_cost_estimate(opf, sec)
             for cg, (opf, sec) in requests.items()}
    for cg in requests:
        # backlog invariant: exactly one pending request per listed tenant
        # (idempotent top-up so a drive may resume on a non-empty arbiter)
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    served = {cg: 0 for cg in requests}
    done = 0
    while (rounds is None or arb.current_round < rounds) and \
            (total is None or done < total):
        cg = arb.pick()
        if cg == 0:
            continue            # round boundary; retry like blk-mq's next kick
        ts = arb.tenants[cg]
        cost = costs[cg]
        if not fast:
            arb.note_dispatched(cg, cost)
            arb.note_completed(cg)
            served[cg] += 1
            done += 1
            if ts.pending == 0:
                arb.note_inserted(cg)
            continue
        # fast-forward this turn: the real pick above stands in for the first
        # dispatch's pick; the remaining n-1 dispatches each had a serve pick
        # in per-request mode → serve_selects += n-1 keeps telemetry identical.
        n = -(-ts.deficit // cost)          # ceil(deficit / cost) ≥ 1
        if total is not None:
            n = min(n, total - done)
        ts.deficit -= n * cost
        ts.completed_cost += n * cost
        ts.dispatches += n
        ts.dispatch_log.extend([cost] * n)
        if cost > arb.round_max_charge:
            arb.round_max_charge = cost
        ts.serve_selects += n - 1
        served[cg] += n
        done += n
        # per-request mode re-inserts after each dispatch (pending ends at 1)
        # and completes each dispatch (inflight ends at 0): net no change.
    return served


def drive_shallow_queues(arb, requests, total_dispatches, queue_cap=16,
                         device_latency=8, jitter=15, seed=0):
    """Hardware saturated regime (session6_20260920 W4 dump), cost-charged
    variant of arb_wdrr_model.drive_shallow_queues.  Each dispatched
    request returns to the queue after device_latency + jitter slots
    (completion round-trip; the jitter models completion batching across
    the 48 (tenant, hctx) pipeline queues).  fio keeps outstanding
    (queued + in transit) at queue_cap, so the framework queue is the
    RESIDUAL behind the transit pipeline and pending hits zero
    periodically: visits end pending-bound unless the round budget is
    small enough to bind first.  The pre-S6 quantum (1024 cost units with
    the unscaled table) degenerated exactly this regime on hardware
    (refills settled at ~1/w, weighted Jain 0.63); the S6 quantum must
    keep the budget binding per visit."""
    served = {cg: 0 for cg in requests}
    returns = []           # [due_at, cgid] completions back into the queue
    rng = __import__("random").Random(seed)

    costs = {cg: arb_cost_estimate(opf, sec)
             for cg, (opf, sec) in requests.items()}

    for cg in requests:
        for _ in range(queue_cap):
            arb.note_inserted(cg)

    clock = 0
    n = 0
    idle_slots = 0
    while n < total_dispatches:
        clock += 1
        for e in [e for e in returns if e[0] <= clock]:
            returns.remove(e)
            if arb.tenants[e[1]].pending < queue_cap:
                arb.note_inserted(e[1])
        cg = arb.pick()
        if cg == 0:
            if all(arb.tenants[t].pending == 0 for t in requests):
                idle_slots += 1
                if idle_slots > 6 * queue_cap:
                    raise RuntimeError("drive_shallow_queues starved")
            continue
        arb.note_dispatched(cg, costs[cg])
        returns.append([clock + device_latency + rng.randint(0, jitter), cg])
        served[cg] += 1
        n += 1
        idle_slots = 0
    return served


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Test-only subclass for the fast-forward parity check (scaled-down budgets)
# ---------------------------------------------------------------------------

class SmallTenant(Tenant):
    """Test-only: identical floor arithmetic with tiny quantum/min-budget, so
    the per-request vs fast-forward parity check stays cheap.  The real
    arbiter code paths (pick / drive) are used unchanged — only budget() is
    scaled, which is what arb_drr_pick consults for the refill."""

    SMALL_QUANTUM = 64
    SMALL_MIN_BUDGET = 512

    def budget(self, floor_max=0):
        ew = self.weight
        if ew == 0:
            ew = 1
        floor = max(self.SMALL_MIN_BUDGET, floor_max)
        if self.SMALL_QUANTUM * ew < floor:
            ew = (floor + self.SMALL_QUANTUM - 1) // self.SMALL_QUANTUM
        return self.SMALL_QUANTUM * ew


def _snapshot(arb, served):
    """Full comparable state: arbiter position + every tenant field."""
    snap = {"round": arb.current_round, "cursor": arb.cursor,
            "rmax": arb.round_max_charge,
            "lrmax": arb.last_round_max_charge,
            "served": served}
    for cg in sorted(arb.tenants):
        ts = arb.tenants[cg]
        snap[cg] = (ts.deficit, ts.pending, ts.inflight, ts.completed_cost,
                    ts.dispatches, ts.requeues, ts.requeued_bytes,
                    ts.completed_bytes, ts.dispatch_log, ts.serve_selects,
                    ts.refill_arms, ts.wrap_selects, ts.skip_pending_zero,
                    ts.refilled_round)
    return snap


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cost_estimate_table():
    """Encode arb_cost_estimate's table + bucket boundaries verbatim."""
    cases = [
        ((0, 1), 64),        # 512B read  → bucket 0
        ((0, 8), 64),        # 4K read    → bucket 0 boundary (bytes == 4096)
        ((0, 9), 256),       # 4608B read → bucket 1
        ((0, 32), 256),      # 16K read   → bucket 1 boundary
        ((0, 33), 1024),     # 16896B read → bucket 2
        ((0, 128), 1024),    # 64K read   → bucket 2 boundary
        ((0, 129), 2048),    # 66048B read → bucket 3
        ((0, 256), 2048),    # 128K read  → bucket 3 boundary
        ((0, 257), 4096),    # 132096B read → bucket 4
        ((0, 512), 4096),    # 256K read  → bucket 4
        ((1, 8), 256),       # 4K write
        ((1, 32), 1024),     # 16K write
        ((1, 128), 4096),    # 64K write
        ((1, 256), 8192),    # 128K write
        ((1, 512), 16384),   # 256K write → max single-request cost
        ((1, 2048), 16384),  # 1MB write → bucket 4 (no wrong else bucket)
        ((0x101, 512), 16384),  # opf flags: op = opf & 0xff == 1 → WRITE
    ]
    bad = [(rq, want, arb_cost_estimate(*rq)) for rq, want in cases
           if arb_cost_estimate(*rq) != want]
    return check("cost_estimate_table", not bad, f"mismatches={bad}")


def test_case1_cost_ratio():
    """THE POL-04 acceptance case (refine.md §4 '4. COST 验收').

    Equal weights, A issues 4K READs (cost 64), B issues 4K WRITEs (cost
    256).  Weight 256 sits well above the S6 floor (64): QUANTUM*256 = 256
    cost units per round, and the adaptive floor (max of 64 and
    last_round_max_charge) stays at 256 after B's write turns (charge
    256 <= budget 256), so neither tenant is bumped.  Expected after ≥2
    full DRR rounds under saturation: charged_cost 1:1 (cost fairness)
    while request counts run ~4:1 (NOT request-count fairness) and bytes
    likewise ~4:1.
    """
    arb = CostArbiter()
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: W4K}, rounds=2)
    ca, cb = a.completed_cost, b.completed_cost
    cost_ratio = ca / cb
    req_ratio = served[100] / served[200]
    byte_ratio = (served[100] * 4096) / (served[200] * 4096)
    ok = check("case1_cost_ratio_1to1", 0.95 <= cost_ratio <= 1.05,
               f"charged_cost ratio={cost_ratio:.3f} A={ca} B={cb}")
    ok &= check("case1_request_ratio_4", 3.5 <= req_ratio <= 4.5,
                f"request ratio={req_ratio:.3f}")
    ok &= check("case1_not_request_fair", req_ratio > 3,
                f"request ratio={req_ratio:.3f} must NOT be ~1 "
                f"(request-count fairness would be a POL-04 failure)")
    ok &= check("case1_bytes_ratio_4", 3.5 <= byte_ratio <= 4.5,
                f"bytes ratio={byte_ratio:.3f}")
    return ok


def test_case2_weight_ratio():
    """Weights 256 vs 768 (both above the floor, no compression), 4K reads.
    One full round: A serves QUANTUM*256 = 256 cost (4 x R4K@64), B serves
    QUANTUM*768 = 768 cost (12 x R4K@64) → ratio 768/256 = 3.0 exactly."""
    arb = CostArbiter()
    a, b = Tenant(100, 256), Tenant(200, 768)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=1)
    cost_ratio = b.completed_cost / a.completed_cost
    req_ratio = served[200] / served[100]
    ok = check("case2_weight_cost_ratio_3", 2.9 <= cost_ratio <= 3.1,
               f"B/A charged_cost={cost_ratio:.3f} expected ~3.0")
    ok &= check("case2_weight_request_ratio_3", 2.9 <= req_ratio <= 3.1,
                f"B/A request ratio={req_ratio:.3f} expected ~3.0")
    return ok


def test_case3_shallow_queue_weight_ratio():
    """The hardware saturated regime (20260919 W4 run): shallow queues,
    completion-latency-bound refill.  Pre-S6 constants (1024-cost quantum,
    262144-cost floor) degenerate this to equal share; the S6 quantum must
    keep the budget binding per visit so service splits by weight (served
    cost ratio == dispatch ratio here: uniform R4K charge of 64)."""
    arb = CostArbiter()
    a, b = Tenant(100, 100), Tenant(300, 300)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_shallow_queues(arb, {100: R4K, 300: R4K}, 40000)
    ca, cb = served[100] * 64, served[300] * 64
    ratio = cb / ca
    return check("case3_shallow_queue_weight_ratio", 2.0 <= ratio <= 4.0,
                 f"ratio={ratio:.3f} expected~3 (window [2.0,4.0])")


def test_min_budget_floor():
    """Floor compression is SPEC (not a bug): weights 32 and 48 both bump
    to ew = ceil(MIN_BUDGET/QUANTUM) = 64 → equal budgets 64.  Weights
    below 64 are cost-indistinguishable under the cost-denominated floor;
    weight-ratio tests must therefore use weights >= 64 (case1/case2 use
    256+, case3 uses 100/300)."""
    arb = CostArbiter()
    a, b = Tenant(100, 32), Tenant(200, 48)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=1)
    ok = check("min_budget_floor_equal_budgets",
               a.budget() == b.budget() == MIN_BUDGET == 64,
               f"A budget={a.budget()} B budget={b.budget()}")
    ratio = a.completed_cost / b.completed_cost
    ok &= check("min_budget_floor_ratio_1", ratio == 1.0,
                f"1-round cost ratio={ratio:.3f} "
                f"(A={a.completed_cost} B={b.completed_cost})")
    return ok


def test_requeue_refund_no_leak():
    """Requeue refunds the exact charged cost + inflight--, never pending++
    (guide §6 pitfall 4: the kernel re-inserts and re-fires inserted_request).
    Charge unit is cost: completed_bytes stays 0, refund lands in the shared
    struct's requeued_bytes field (cost units — field-name quirk, C parity)."""
    arb = CostArbiter()
    a = Tenant(100, 1024)
    arb.register(a, 0)
    cost = arb_cost_estimate(*W4K)          # 256
    peak_pending = 0

    # three plain dispatches; `armed` = deficit at turn arm: the first
    # pick is the refill arming the full budget (QUANTUM*1024 = 1024 =
    # 4 x the W4K charge of 256, so the whole 5-dispatch sequence fits in
    # one armed turn, no mid-test refill)
    arb.note_inserted(100)
    peak_pending = max(peak_pending, a.pending)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()                     # round boundary: retry
    armed = a.deficit
    arb.note_dispatched(cg, cost)
    arb.note_completed(cg)
    for _ in range(2):
        arb.note_inserted(100)
        peak_pending = max(peak_pending, a.pending)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()                 # round boundary: retry
        arb.note_dispatched(cg, cost)
        arb.note_completed(cg)

    # dispatch then requeue with the exact charged cost
    arb.note_inserted(100)
    peak_pending = max(peak_pending, a.pending)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()
    arb.note_dispatched(cg, cost)           # charge
    arb.note_requeue(cg, cost)              # refund, NO pending++
    pending_after_requeue = a.pending       # must be 0, not 1
    arb.note_inserted(cg)                   # framework re-insert
    peak_pending = max(peak_pending, a.pending)
    arb.note_dispatched(arb.pick(), cost)   # re-dispatch, charge again
    arb.note_completed(cg)

    charged = 5 * cost                      # 5 dispatches × cost 256
    refunded = cost
    net = a.completed_cost - a.requeued_bytes
    deficit_ok = a.deficit == armed - charged + refunded
    ok = check("requeue_no_pending_increment",
               pending_after_requeue == 0 and peak_pending == 1,
               f"pending after requeue={pending_after_requeue} "
               f"peak pending={peak_pending}")
    ok &= check("requeue_refund_no_leak",
                a.pending == 0 and a.inflight == 0 and net == charged - refunded
                and deficit_ok and a.completed_bytes == 0
                and a.requeued_bytes == refunded and a.requeues == 1,
                f"pending={a.pending} inflight={a.inflight} net={net} "
                f"deficit={a.deficit} expected={armed - charged + refunded}")
    return ok


def test_overshoot_bound():
    """deficit may go transiently negative, bounded by ONE max request cost
    (16384 = WRITE bucket 4): pick only selects at deficit > 0, so after any
    dispatch deficit > 0 - 16384 >= -16383.  Two constructions:
    (1) real constants, per-request pre-roll to a sub-request deficit;
    (2) small-budget subclass, per-request mixed sizes
    across many turns (also exercises negative-deficit refill)."""
    worst = None

    # construction 1: real constants, per-request pre-roll: 3 of the 4
    # R4K dispatches the weight-256 round budget (256 cost) affords →
    # deficit 64, then the max-cost request arrives (W256K)
    arb = CostArbiter()
    a = Tenant(100, 256)
    arb.register(a, 0)
    drive_backlogged(arb, {100: R4K}, total=3, fast=False)
    small_armed = a.deficit                 # 256 - 3*64 == 64
    arb.note_inserted(100)                  # a big request arrives (W256K)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()
    arb.dispatched_request(cg, *W256K)      # charge 16384 at deficit 64
    arb.note_completed(cg)
    worst = a.deficit                       # 64 - 16384 = -16320
    ok = check("overshoot_bound",
               worst >= -MAX_COST and small_armed == 64,
               f"worst deficit={worst} bound=-{MAX_COST} "
               f"(armed={small_armed})")

    # construction 2: small budgets, per-request mixed request sizes
    arb2 = CostArbiter()
    s = SmallTenant(100, 9)                 # budget 64*9 = 576
    arb2.register(s, 0)
    pattern = [W256K, R4K, W128K, R64K, W4K, R128K]
    for i in range(2000):
        arb2.note_inserted(100)
        cg = arb2.pick()
        while cg == 0:
            cg = arb2.pick()
        arb2.dispatched_request(cg, *pattern[i % len(pattern)])
        arb2.note_completed(cg)
        worst = min(worst, s.deficit)
    ok &= check("overshoot_bound_mixed", worst >= -MAX_COST,
                f"worst deficit={worst} bound=-{MAX_COST}")
    return ok


def test_dispatch_failed_pending():
    """Invalid-rq path: the framework popped the rq before validating it, so
    dispatch_failed retires one pending entry (only).  An emptied tenant must
    never be picked — strict mode returns NULL instead (pick() == 0)."""
    arb = CostArbiter()
    a = Tenant(100, 256)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    assert cg == 100
    armed = a.deficit
    arb.dispatch_failed(100)                # note_dispatch_failed + advance
    ok = check("dispatch_failed_retires_pending",
               a.pending == 0 and a.deficit == armed,
               f"pending={a.pending} deficit={a.deficit} armed={armed} "
               f"(dispatch_failed must touch pending ONLY)")
    ok &= check("dispatch_failed_empty_not_picked", arb.pick() == 0,
                "empty tenant must not be picked (strict NULL)")
    return ok


def test_work_conservation_idle_cap():
    """Only tenant A active: A gets all the service (work conservation), idle
    B is skipped WITHOUT refill (no credit hoarding), and A's deficit never
    exceeds budget*CAP_MULT after repeated refills (idle credit cap)."""
    arb = CostArbiter()
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K}, rounds=3)
    # 3 rounds x budget QUANTUM*256 = 256 cost = 768 cost = 12 x R4K@64
    expected = 3 * (QUANTUM * 256 // arb_cost_estimate(*R4K))
    ok = check("work_conservation",
               b.dispatches == 0 and b.completed_cost == 0
               and served[100] == expected
               and a.dispatches == served[100],
               f"A dispatches={a.dispatches} (expected {expected}) "
               f"B dispatches={b.dispatches}")
    ok &= check("idle_no_credit_hoard",
                b.deficit == 0 and b.refill_arms == 0 and b.pending == 0,
                f"B deficit={b.deficit} refills={b.refill_arms}")
    ok &= check("idle_credit_cap",
                a.deficit <= a.budget() * CAP_MULT,
                f"A deficit={a.deficit} cap={a.budget() * CAP_MULT}")
    return ok


def test_fastforward_parity():
    """Fast-forward must reproduce the per-request loop bit-exactly: run the
    same small scenario (scaled-down budgets via SmallTenant) both ways and
    compare every piece of state — including a mid-turn `total` cut."""
    def run(fast):
        arb = CostArbiter()
        a, b = SmallTenant(100, 8), SmallTenant(200, 9)
        arb.register(a, 0)
        arb.register(b, 1)
        served = drive_backlogged(arb, {100: R4K, 200: W128K}, rounds=3,
                                  fast=fast)
        served.update(drive_backlogged(arb, {100: W64K, 200: R4K}, total=777,
                                       fast=fast))
        return _snapshot(arb, served)

    slow, quick = run(fast=False), run(fast=True)
    diffs = []
    for key in slow:
        if slow[key] != quick[key]:
            diffs.append(key)
    return check("fastforward_parity", not diffs,
                 f"state mismatch in {diffs}: "
                 f"per-request={ {k: slow[k] for k in diffs} } "
                 f"fast={ {k: quick[k] for k in diffs} }")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_cost_estimate_table()
    ok &= test_case1_cost_ratio()
    ok &= test_case2_weight_ratio()
    ok &= test_case3_shallow_queue_weight_ratio()
    ok &= test_min_budget_floor()
    ok &= test_requeue_refund_no_leak()
    ok &= test_overshoot_bound()
    ok &= test_dispatch_failed_pending()
    ok &= test_work_conservation_idle_cap()
    ok &= test_fastforward_parity()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()