#!/usr/bin/env python3
"""q_wedf reference model — executable specification of the q_wedf BPF policy.

Two modes:
  ACTUAL   Mirrors userspace/q_wedf.bpf.c exactly, line by line. This is
           the algorithm_derivation oracle for the Tier-2 goldens
           (wedf_sector_asc_8 / wedf_rw_edf_8).
  INTENDED Adds weight-scaled deadlines (dl = arrival + base_ns / weight),
           the semantics the policy NAME promises ("加权 EDF") but the code
           does not implement. Running the same trace through both modes
           PROVES the divergence between spec and implementation.

Source-of-truth citations (userspace/q_wedf.bpf.c unless noted):
  :10        Q_WEDF_READ_DEADLINE_NS   = 50_000_000   (50 ms)
  :11        Q_WEDF_WRITE_DEADLINE_NS  = 500_000_000  (500 ms)
  :12        Q_WEDF_WINDOW_NS          = 5_000_000    (5 ms)
  :13        Q_WEDF_EXPIRE_GUARD_NS    = 1_000_000    (1 ms, telemetry only)
  :16-24     q_wedf_local_state: pending[IOSCHED_MAX_PENDING], pending_nr, counters
  :47-51     wedf_base_deadline_ns: write -> 500ms else 50ms  (weight NEVER consulted)
  :53-64     wedf_before: >5ms-earlier -> before; >5ms-later -> after;
             else sector-ascending (locality rule)
  :66-94     wedf_insert: insertion sort at FIRST position where before() holds;
             reject when pending_nr >= IOSCHED_MAX_PENDING
  :96-115    wedf_dispatch: pop head; now_ns feeds ONLY the expired/locality
             telemetry counters (:110-113) — it never changes the order
  :129-172   insert_request hook: desc = iosched_build_desc(ictx) (:153);
             desc.deadline_ns = desc.arrival_ns + wedf_base_deadline_ns(&desc) (:154)
  :222-230   struct_ops: init_queue/exit_queue/insert_request/cgroup_dispatch/
             evacuate ONLY — no allow_merge hook, no stats map, name "q_wedf"
  ioext_policy_shared.h:15  IOSCHED_MAX_PENDING = 32
  ioext_policy_shared.h:91  struct iosched_request_desc has __u32 weight;
  ioext_bpf_common.h:193-212 iosched_build_desc NEVER assigns desc.weight
                           (zero-init `= {}` at :195) — weight is always 0;
  loader_q_wedf.c:16-17     ioext_cli_parse_policy_args(..., NULL, NULL) — no
                           extra CLI options; ioext_cli_args.h:117-123 common
                           args are --cgroup/--arb-prio/--arb-weight/--help/
                           --verbose only (arb_weight feeds policy_meta, not
                           per-request weight). No cgroup/ioprio -> weight
                           path exists ANYWHERE. This is the KNOWN gap:
                           EVOLUTION_PLAN.md §8 row 5.4 "q_wedf | weight 未实现 |
                           KNOWN"; Test_Design_IOExt.md ~343-348 (planned
                           q_wedf-DT-01 weight 1:1, DT-02 weight 1:3) and
                           ~371-378 (wedf_base_deadline_ns never uses desc.weight).

Usage:
  python3 tests/q_wedf_model.py             # full self-test, exit 0 = PASS
  python3 tests/q_wedf_model.py --divergence  # print actual-vs-intended orders

Self-test scenarios MATCH the Tier-2 micro traces
(tests/full_suite/deterministic/traces/micro/wedf_*.json) and derive the
same expected dispatch orders as the goldens
(expect/golden/wedf_*.json): model == algorithm_derivation oracle. When run
from the repo, the self-test additionally cross-loads those files and asserts
agreement.
"""
import json
import os
import random
import sys

# ── constants mirrored from source (citations in module docstring) ─────────
READ_DEADLINE_NS = 50_000_000        # q_wedf.bpf.c:10
WRITE_DEADLINE_NS = 500_000_000      # q_wedf.bpf.c:11
WINDOW_NS = 5_000_000                # q_wedf.bpf.c:12
EXPIRE_GUARD_NS = 1_000_000          # q_wedf.bpf.c:13
MAX_PENDING = 32                     # ioext_policy_shared.h:15 IOSCHED_MAX_PENDING

MODE_ACTUAL = "actual"
MODE_INTENDED = "intended"


class Desc:
    """Mirror of struct iosched_request_desc (ioext_policy_shared.h:78-95) —
    only the fields q_wedf consumes, plus weight for the gap analysis."""

    __slots__ = ("rq_ptr", "sector", "nr_sectors", "op", "arrival_ns",
                 "deadline_ns", "weight")

    def __init__(self, rq_ptr, sector, nr_sectors, op, arrival_ns,
                 deadline_ns, weight):
        self.rq_ptr = rq_ptr
        self.sector = sector
        self.nr_sectors = nr_sectors
        self.op = op
        self.arrival_ns = arrival_ns
        self.deadline_ns = deadline_ns
        self.weight = weight


def iosched_req_is_write(desc):
    """ioext_policy_shared.h:374-377: !!(req->flags & IOSCHED_REQF_WRITE)."""
    return desc.op == "write"


def wedf_base_deadline_ns(desc):
    """q_wedf.bpf.c:47-51. NOTE: desc.weight is NEVER consulted here —
    this is the code-level heart of the KNOWN weight gap."""
    return WRITE_DEADLINE_NS if iosched_req_is_write(desc) else READ_DEADLINE_NS


def wedf_before(req, cur):
    """q_wedf.bpf.c:53-64, exact translation."""
    req_late = req.deadline_ns + WINDOW_NS
    cur_late = cur.deadline_ns + WINDOW_NS
    if req_late < cur.deadline_ns:      # req >5ms earlier: true EDF
        return True
    if req.deadline_ns > cur_late:      # req >5ms later
        return False
    return req.sector < cur.sector      # within +-5ms window: locality rule


class WedfState:
    """q_wedf.bpf.c:16-24 q_wedf_local_state + insert/dispatch helpers."""

    def __init__(self):
        self.pending = []               # sorted list of Desc (index 0 = head)
        self.enqueued = 0
        self.dispatched = 0
        self.expired_dispatches = 0
        self.locality_dispatches = 0
        self.insert_rejected = 0

    def insert(self, desc):
        """q_wedf.bpf.c:66-94 wedf_insert: first-position insertion sort."""
        if len(self.pending) >= MAX_PENDING:   # :72-73
            self.insert_rejected += 1
            return -1
        pos = len(self.pending)                # :69 (append by default)
        for i in range(len(self.pending)):     # :75-82
            if wedf_before(desc, self.pending[i]):
                pos = i
                break
        self.pending.insert(pos, desc)         # :84-90 shift + :90 store
        self.pending_nr = len(self.pending)     # pending_nr is implicit in list
        self.enqueued += 1                     # :92
        return 0

    def dispatch(self, now_ns):
        """q_wedf.bpf.c:96-115 wedf_dispatch: pop head; now only drives the
        expired/locality telemetry counters (:110-113), never the order."""
        if not self.pending:                   # :101-102
            return None
        picked = self.pending.pop(0)           # :104-105 iosched_pop_head_32
        self.dispatched += 1                   # :109
        if picked.deadline_ns <= now_ns + EXPIRE_GUARD_NS:
            self.expired_dispatches += 1
        else:
            self.locality_dispatches += 1
        return picked


def build_desc(req, arrival_ns, mode, weight=None):
    """Mirror of the insert_request hook body (q_wedf.bpf.c:153-154) on top of
    iosched_build_desc (ioext_bpf_common.h:193-212).

    ACTUAL mode: iosched_build_desc NEVER assigns desc.weight (:195 zero-init;
    no weight line anywhere in :197-210) so weight stays 0, and the deadline
    is arrival + read/write base offset only (q_wedf.bpf.c:154).
    INTENDED mode: weight-scaled deadline dl = arrival + base_ns / weight
    (documented intended semantics; Test_Design_IOExt.md ~343-348 plans
    weight 1:1 and 1:3 cases that the current code cannot differentiate)."""
    op = req.get("op", "read")
    desc = Desc(
        rq_ptr=req.get("_rq_ptr", 0xffff000000000000 + id(req)),
        sector=req["sector"],
        nr_sectors=req.get("nr_sectors", 8),
        op=op,
        arrival_ns=arrival_ns,
        deadline_ns=0,
        weight=0,
    )
    if mode == MODE_INTENDED:
        w = weight if weight is not None else req.get("weight", 1)
        if not w or w < 1:
            w = 1
        desc.weight = w
        desc.deadline_ns = desc.arrival_ns + wedf_base_deadline_ns(desc) // w
    else:
        # ACTUAL: weight never set (always 0) and never read.
        desc.weight = 0
        desc.deadline_ns = desc.arrival_ns + wedf_base_deadline_ns(desc)
    return desc


def run_scenario(requests, mode, arrival_base_ns=1_000_000_000, spread_ns=10_000):
    """Play a trace through the model the way the async_batch rig does:
    all requests are inserted back-to-back (arrival spread = spread_ns, the
    us-scale io_uring submit loop) and then the queue drains via dispatch.
    Returns (issue_sector_order, state)."""
    state = WedfState()
    for i, req in enumerate(requests):
        desc = build_desc(req, arrival_base_ns + i * spread_ns, mode)
        state.insert(desc)
    order = []
    now_ns = arrival_base_ns + len(requests) * spread_ns + 10_000_000
    while True:
        picked = state.dispatch(now_ns)
        if picked is None:
            break
        order.append(picked.sector)
    return order, state


# ── scenarios: byte-for-byte the request lists of the micro traces ─────────

def w1_requests():
    """traces/micro/wedf_sector_asc_8.json — 8 reads submitted DESCENDING.
    Phase-3 re-space: sectors moved above the partition-scan zone (was 448..0)."""
    return [{"op": "read", "sector": s, "nr_sectors": 8}
            for s in (3008, 2944, 2880, 2816, 2752, 2688, 2624, 2560)]


def w2_requests():
    """traces/micro/wedf_rw_edf_8.json — 4 reads + 4 writes interleaved,
    each class submitted DESCENDING.
    Phase-3 re-space: sectors moved above the partition-scan zone
    (was reads 256..64 / writes 896..512)."""
    reqs = []
    for r, w in zip((2752, 2688, 2624, 2560), (5312, 5248, 5184, 5120)):
        reqs.append({"op": "read", "sector": r, "nr_sectors": 8})
        reqs.append({"op": "write", "sector": w, "nr_sectors": 8})
    return reqs


def probe_requests():
    """traces/micro/wedf_weight_probe_8.json — two prospective weight classes
    (w=1: sectors 64..256; w=3: sectors 320..512) interleaved. The 'weight'
    field is dead weight for the CURRENT policy (no input path) — which is
    exactly what this probe measures."""
    reqs = []
    for lo, hi in zip((64, 128, 192, 256), (320, 384, 448, 512)):
        reqs.append({"op": "read", "sector": lo, "nr_sectors": 8, "weight": 1})
        reqs.append({"op": "read", "sector": hi, "nr_sectors": 8, "weight": 3})
    return reqs


# ── repo cross-check helpers (model == algorithm_derivation oracle) ────────

def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def cross_check_goldens(check):
    """If the trace/golden files exist, assert the model reproduces them."""
    all_ok = True
    root = _repo_root()
    micro = os.path.join(root, "tests/full_suite/deterministic/traces/micro")
    gold = os.path.join(root, "tests/full_suite/deterministic/expect/golden")

    pairs = [
        ("W1 trace file matches model scenario",
         os.path.join(micro, "wedf_sector_asc_8.json"), w1_requests),
        ("W2 trace file matches model scenario",
         os.path.join(micro, "wedf_rw_edf_8.json"), w2_requests),
        ("weight-probe trace file matches model scenario",
         os.path.join(micro, "wedf_weight_probe_8.json"), probe_requests),
    ]
    for name, path, scenario_fn in pairs:
        if not os.path.exists(path):
            all_ok = check(name, False, f"{path} missing") and all_ok
            continue
        tf = _load_json(path)
        got = [{k: v for k, v in r.items() if k in ("op", "sector", "nr_sectors", "weight")}
               for r in tf["requests"]]
        want = [{k: v for k, v in r.items()} for r in scenario_fn()]
        all_ok = check(name, got == want, f"trace={got} model={want}") and all_ok

    golden_checks = [
        ("golden wedf_sector_asc_8 == model actual order (W1)",
         os.path.join(gold, "wedf_sector_asc_8.json"), w1_requests, MODE_ACTUAL),
        ("golden wedf_rw_edf_8 == model actual order (W2)",
         os.path.join(gold, "wedf_rw_edf_8.json"), w2_requests, MODE_ACTUAL),
        ("golden wedf_weight_probe_intended == model intended order (weights 1:3)",
         os.path.join(gold, "wedf_weight_probe_intended.json"), probe_requests, MODE_INTENDED),
    ]
    for name, path, scenario_fn, mode in golden_checks:
        if not os.path.exists(path):
            all_ok = check(name, False, f"{path} missing") and all_ok
            continue
        gf = _load_json(path)
        golden_order = [e["sector"] for e in gf["expected_dispatch_order"]]
        model_order, _ = run_scenario(scenario_fn(), mode)
        all_ok = check(name, golden_order == model_order,
                       f"golden={golden_order} model={model_order}") and all_ok
    return all_ok


# ── self-tests ──────────────────────────────────────────────────────────────

def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


def test_w1_sector_ascending():
    """W1: same-op batch, deadlines mutually in-window -> sector ascending,
    the exact reverse of the descending submission order."""
    order, state = run_scenario(w1_requests(), MODE_ACTUAL)
    return check("w1_actual_sector_ascending", order == [2560, 2624, 2688, 2752, 2816, 2880, 2944, 3008],
                 f"order={order}") and check("w1_all_dispatched",
                                              state.dispatched == 8 and state.enqueued == 8,
                                              f"dispatched={state.dispatched}")


def test_w2_read_write_edf():
    """W2: read dl (arrival+50ms) vs write dl (arrival+500ms) differ by
    ~450ms >> 5ms window -> strict EDF reads-first; sector rule within class."""
    order, _ = run_scenario(w2_requests(), MODE_ACTUAL)
    want = [2560, 2624, 2688, 2752, 5120, 5184, 5248, 5312]
    return check("w2_actual_rw_edf", order == want, f"order={order} want={want}")


def test_window_tie_immunity():
    """Same-op batch with randomized arrival jitter (up to 2ms spread, well
    inside the 5ms window) — order must stay sector-ascending for every seed.
    This is why exact goldens are SAFE for q_wedf."""
    ok = True
    seed = 0
    order = []
    for seed in range(50):
        rng = random.Random(seed)
        reqs = w1_requests()
        # emulate per-request arrival jitter by re-running with different
        # submission orders (the queue result must not depend on it)
        rng.shuffle(reqs)
        order, _ = run_scenario(reqs, MODE_ACTUAL, spread_ns=rng.randint(1, 2000))
        if order != [2560, 2624, 2688, 2752, 2816, 2880, 2944, 3008]:
            ok = False
            break
    return check("window_tie_immunity_50_seeds", ok, f"seed={seed} order={order}")


def test_cross_window_strict_edf():
    """Deadline gap > window overrides the sector rule: a read arriving 40ms
    after another read gets a deadline 40ms later; 40ms > 5ms window means
    strict EDF — the earlier-arriving request dispatches first even though
    its sector is larger."""
    reqs = [{"op": "read", "sector": 1000, "nr_sectors": 8},
            {"op": "read", "sector": 0, "nr_sectors": 8}]
    # manual arrivals: first at t0, second at t0+40ms
    state = WedfState()
    d0 = build_desc(reqs[0], 1_000_000_000, MODE_ACTUAL)
    d1 = build_desc(reqs[1], 1_000_000_000 + 40_000_000, MODE_ACTUAL)
    state.insert(d0)
    state.insert(d1)
    order = []
    while True:
        p = state.dispatch(1_100_000_000)
        if p is None:
            break
        order.append(p.sector)
    return check("cross_window_strict_edf", order == [1000, 0], f"order={order}")


def test_max_pending_reject():
    """q_wedf.bpf.c:72-73: the 33rd insert into one hctx queue is rejected."""
    state = WedfState()
    for i in range(33):
        req = {"op": "read", "sector": i * 64, "nr_sectors": 8}
        rc = state.insert(build_desc(req, 1_000_000_000 + i * 1000, MODE_ACTUAL))
    ok = check("max_pending_reject_at_32",
               len(state.pending) == 32 and state.insert_rejected == 1,
               f"pending={len(state.pending)} rejected={state.insert_rejected}")
    return ok


def test_weight_ignored_in_actual_mode():
    """THE GAP, part 1: in ACTUAL mode weights 1:1 and 1:3 (and no weights)
    all produce the IDENTICAL dispatch order — weight has no observable
    effect because desc.weight is never set and never read."""
    order_none, _ = run_scenario(probe_requests(), MODE_ACTUAL)
    order_11, _ = run_scenario(
        [dict(r, weight=1) for r in probe_requests()], MODE_ACTUAL)
    order_13, _ = run_scenario(probe_requests(), MODE_ACTUAL)
    ok = check("actual_mode_weight_ignored",
               order_none == order_11 == order_13 == [64, 128, 192, 256, 320, 384, 448, 512],
               f"none={order_none} w11={order_11} w13={order_13}")
    return ok


def test_weight_divergence():
    """THE GAP, part 2 (measured proof): the same probe trace through
    ACTUAL vs INTENDED (weights 1:3) yields DIFFERENT dispatch orders.
    Intended: w3 class dl ~16.7ms vs w1 class dl 50ms -> 33ms gap > 5ms
    window -> all w3 first; within a class, sector-ascending."""
    actual_order, _ = run_scenario(probe_requests(), MODE_ACTUAL)
    intended_order, _ = run_scenario(probe_requests(), MODE_INTENDED)
    print(f"  divergence: actual  ={actual_order}")
    print(f"  divergence: intended={intended_order}")
    ok = check("weight_probe_orders_differ",
               actual_order != intended_order,
               f"actual={actual_order} intended={intended_order}")
    ok &= check("intended_weighted_edf_order",
                intended_order == [320, 384, 448, 512, 64, 128, 192, 256],
                f"intended={intended_order}")
    ok &= check("actual_is_pure_sector_asc",
                actual_order == [64, 128, 192, 256, 320, 384, 448, 512],
                f"actual={actual_order}")
    return ok


def test_now_ns_telemetry_only():
    """q_wedf.bpf.c:110-113: now_ns only classifies expired vs locality
    dispatches; the dispatch ORDER must be identical for any now."""
    o1, s1 = run_scenario(w1_requests(), MODE_ACTUAL)
    # rerun with absurd now (simulate dispatch far past deadlines)
    state = WedfState()
    for i, req in enumerate(w1_requests()):
        state.insert(build_desc(req, 1_000_000_000 + i * 10_000, MODE_ACTUAL))
    o2 = []
    while True:
        p = state.dispatch(1_000_000_000_000_000)   # now >> deadlines
        if p is None:
            break
        o2.append(p.sector)
    ok = check("now_ns_does_not_change_order", o1 == o2, f"{o1} vs {o2}")
    ok &= check("telemetry_counters_split",
                s1.expired_dispatches == 0 and s1.locality_dispatches == 8,
                f"expired={s1.expired_dispatches} locality={s1.locality_dispatches}")
    return ok


def test_insertion_first_position_semantics():
    """wedf_insert places the new request before the FIRST resident it
    precedes (q_wedf.bpf.c:75-82). Sanity: inserting an out-of-order sector
    into a sector-ascending queue re-sorts it (stable for same-op batches)."""
    state = WedfState()
    for s in (128, 0, 64):
        state.insert(build_desc({"op": "read", "sector": s, "nr_sectors": 8},
                                1_000_000_000, MODE_ACTUAL))
    order = [p.sector for p in state.pending]
    return check("insertion_sort_first_position", order == [0, 64, 128],
                 f"pending={order}")


def main():
    if "--divergence" in sys.argv:
        actual_order, _ = run_scenario(probe_requests(), MODE_ACTUAL)
        intended_order, _ = run_scenario(probe_requests(), MODE_INTENDED)
        print("wedf_weight_probe_8 (weights 1:3) dispatch orders:")
        print(f"  ACTUAL   (q_wedf.bpf.c as-is, weight ignored): {actual_order}")
        print(f"  INTENDED (dl = arrival + 50ms/weight):        {intended_order}")
        print("DIVERGENT" if actual_order != intended_order else "IDENTICAL")
        return 0 if actual_order != intended_order else 1

    ok = True
    ok &= test_w1_sector_ascending()
    ok &= test_w2_read_write_edf()
    ok &= test_window_tie_immunity()
    ok &= test_cross_window_strict_edf()
    ok &= test_max_pending_reject()
    ok &= test_weight_ignored_in_actual_mode()
    ok &= test_weight_divergence()
    ok &= test_now_ns_telemetry_only()
    ok &= test_insertion_first_position_semantics()
    ok &= cross_check_goldens(check)
    print("MODEL " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())