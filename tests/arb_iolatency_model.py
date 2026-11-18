#!/usr/bin/env python3
"""arb_iolatency reference model — executable specification of the
io.latency-style LC/BE latency-target arbiter (gate 0 of its CI loop).

Same-semantics reduction of cgroup v2 io.latency (blk-iolatency) onto the
iosched_ext arbiter, mirroring userspace/arb_iolatency.bpf.c +
ioext_arb_common.h:
  - roles: prio 0..2 = LC (has the latency target), prio 3..7 = BE
    (no target; boost stays 1 — the BE floor), per the queue loaders'
    -p/--arb-prio convention; shares per -W/--arb-weight
  - target: ONE global --lat-target-ns (the framework has no per-cgroup
    channel into the arbiter); configurable, 0 disables the loop
  - feedback: every LC completion feeds the shared window FSM
    (arb_slo_update_window mirror: 100ms windows, >=20 samples,
    violation when over*100 > total, NORMAL->PRESSURE->RECOVERY->NORMAL
    ladder, 3 good windows to exit, boost clamped to cfg.boost_max)
  - backpressure: sustained LC violations raise the LC boost
    (effective_weight = weight * boost at REFILL time only).  Under DRR
    this is RATIO-EQUIVALENT to compressing BE by the same factor (the
    core's only weight-injection point is an integer >=1 multiplier;
    arb_drr_pick must not be copied — guide §7), so the observable
    io.latency behavior (LC misses -> BE share shrinks; LC recovers ->
    BE share returns) is preserved.
  - BE floor: boost cap (default 4) bounds the compression; BE-vs-BE
    service stays strictly weight-proportional (BE never boosts)
  - hold gate: per-hctx inflight >= cfg.protect_inflight while any LC is
    in PRESSURE -> hold dispatch (hard LC protection, arb_slo pattern);
    each hold bumps iolat_hold_cnt (observability)

The DRR core here is the CURRENT full spec (F2 serve-no-progress
rotation + S10 strict round hold), cloned from tests/arb_wdrr_model.py
(canonical mirror of the shared C core).  Charge unit = BYTES.

Self-test convention: cloned from tests/arb_slo_model.py /
arb_wdrr_model.py (check() printer, MODEL PASS/FAIL + exit code).
Exit 0 = all PASS.
"""
import sys

# ---------------------------------------------------------------------------
# Constants (encode ioext_arb_common.h + arb_iolatency.bpf.c verbatim)
# ---------------------------------------------------------------------------

SECTOR = 512                  # charge = nr_sectors * 512 (bytes, like wdrr/slo)
QUANTUM = 512                 # ARB_WDRR_QUANTUM_BYTES (F1 sweep 20261021)
CAP_MULT = 2                  # ARB_WDRR_IDLE_CAP_MULT
MIN_BUDGET = 4096             # ARB_WDRR_MIN_BUDGET_BYTES (one 4K page)
MAX_TENANTS = 16              # ARB_MAX_TENANTS
SERVE_NO_PROGRESS_MAX = 3     # ARB_SERVE_NO_PROGRESS_MAX (F2 liveness)
DISPATCH_FAIL_GHOST_RESET = 4  # ARB_DISPATCH_FAIL_GHOST_RESET (F2 liveness)
ROUND_IDLE_RELEASE_US = 20000  # ARB_ROUND_IDLE_RELEASE_NS / 1000 (S10)

WINDOW_NS = 100 * 1000 * 1000     # ARB_SLO_WINDOW_NS (100ms)
RECOVERY_WINDOWS = 3               # ARB_SLO_RECOVERY_WINDOWS
MIN_SAMPLES = 20                   # ARB_SLO_MIN_SAMPLES

BOOST_NORMAL = 1              # ARB_SLO_BOOST_NORMAL
BOOST_MILD = 2                # ARB_SLO_BOOST_MILD
BOOST_HIGH = 4                # ARB_SLO_BOOST_HIGH
BOOST_SEVERE = 8               # ARB_SLO_BOOST_SEVERE

SLO_NORMAL = 0                # enum arb_slo_state
SLO_PRESSURE = 1
SLO_RECOVERY = 2

# arb_iolatency.bpf.c defaults (also loader_arb_iolatency.c's)
DEFAULT_LAT_TARGET_NS = 100 * 1000    # 100 us
DEFAULT_PROTECT_INFLIGHT = 32
DEFAULT_BOOST_MAX = 4
PRIO_LC_MAX = 2                        # prio 0..2 are LC

# Fixture latencies (default target = 100us): GOOD under, BAD 2x over.
GOOD_LAT_NS = 50 * 1000
BAD_LAT_NS = 200 * 1000

# arb_slo_update_window treats window_start_ns == 0 as "not started";
# ktime never returns 0, so the model clock starts non-zero.
BASE_NS = 1

R4K = 8          # nr_sectors fixtures
R256K = 512
MAX_REQ_BYTES = R256K * SECTOR   # 256KB: largest fixture charge (overshoot)


class IolatCfg:
    """Mirror of struct arb_iolat_cfg (arb_iolatency.bpf.c) as pushed by
    loader_arb_iolatency.c (--lat-target-ns / --protect-inflight /
    --boost-max).  Zero fields mean the BPF-side compiled-in defaults."""

    def __init__(self, lat_target_ns=DEFAULT_LAT_TARGET_NS,
                 protect_inflight=DEFAULT_PROTECT_INFLIGHT,
                 boost_max=DEFAULT_BOOST_MAX):
        self.lat_target_ns = lat_target_ns
        self.protect_inflight = protect_inflight
        self.boost_max = boost_max


def iolat_is_lc(prio):
    """Mirror arb_iolatency.bpf.c iolat_is_lc()."""
    return prio <= PRIO_LC_MAX


def slo_update_window(ts, now_ns, latency_target_ns, max_boost,
                      total_latency_ns):
    """Mirror ioext_arb_common.h arb_slo_update_window() verbatim (shared
    FSM, identical to arb_slo_model.slo_update_window)."""
    if ts.window_start_ns == 0:
        ts.window_start_ns = now_ns
        ts.window_total = 0
        ts.window_over_target = 0

    if now_ns - ts.window_start_ns >= WINDOW_NS:
        violation = False

        if ts.window_total >= MIN_SAMPLES and latency_target_ns > 0:
            if ts.window_over_target * 100 > ts.window_total:
                violation = True

        if ts.slo_state == SLO_NORMAL:
            if violation:
                ts.slo_state = SLO_PRESSURE
                ts.boost = BOOST_HIGH
                ts.good_windows = 0
        elif ts.slo_state == SLO_PRESSURE:
            if not violation:
                ts.slo_state = SLO_RECOVERY
                ts.good_windows = 1
                ts.boost = BOOST_MILD
            elif ts.window_total > 0 and \
                    ts.window_over_target * 10 > ts.window_total:
                ts.boost = BOOST_SEVERE
            else:
                ts.boost = BOOST_HIGH
        elif ts.slo_state == SLO_RECOVERY:
            if violation:
                ts.slo_state = SLO_PRESSURE
                ts.boost = BOOST_HIGH
                ts.good_windows = 0
            else:
                ts.good_windows += 1
                if ts.good_windows >= RECOVERY_WINDOWS:
                    ts.slo_state = SLO_NORMAL
                    ts.boost = BOOST_NORMAL

        if ts.boost > max_boost:
            ts.boost = max_boost
        if ts.boost < BOOST_NORMAL:
            ts.boost = BOOST_NORMAL

        ts.window_start_ns = now_ns
        ts.window_total = 0
        ts.window_over_target = 0

    ts.window_total += 1
    if latency_target_ns > 0 and total_latency_ns > latency_target_ns:
        ts.window_over_target += 1

    if ts.ewma_latency_ns == 0:
        ts.ewma_latency_ns = total_latency_ns
    else:
        ts.ewma_latency_ns = (ts.ewma_latency_ns * 7 + total_latency_ns) // 8
    ts.last_latency_ns = total_latency_ns


# ---------------------------------------------------------------------------
# Tenant / Arbiter (mirror arb_tenant_state + arb_drr_pick + arb_note_*)
# ---------------------------------------------------------------------------

class Tenant:
    def __init__(self, cgid, weight, prio):
        self.cgid = cgid
        self.weight = weight
        self.prio = prio
        self.deficit = 0
        self.refilled_round = -1
        self.pending = 0
        self.inflight = 0
        self.dispatches = 0
        self.completed_bytes = 0
        self.requeues = 0
        self.requeued_bytes = 0
        self.dispatch_log = []
        self.serve_selects = 0
        self.refill_arms = 0
        self.skip_pending_zero = 0
        self.last_serve_dispatches = 0
        self.serve_no_progress = 0
        self.dispatch_fails = 0
        self.last_active_us = 0
        # FSM fields (arb_tenant_state tail; boost enters effective_weight
        # = weight * boost at REFILL time only)
        self.boost = BOOST_NORMAL
        self.slo_state = SLO_NORMAL
        self.window_start_ns = 0
        self.window_total = 0
        self.window_over_target = 0
        self.good_windows = 0
        self.last_latency_ns = 0
        self.ewma_latency_ns = 0


class IolatArbiter:
    """Per-hctx LC/BE latency arbiter: full F2/S10 DRR core (cloned from
    tests/arb_wdrr_model.py — the canonical spec of the shared
    arb_drr_pick), bytes charge, boost injected at refill, hold gate with
    per-hctx inflight, window FSM fed by LC completions only."""

    def __init__(self, cfg=None):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.cursor = 0
        self.current_round = 0
        self.round_max_charge = 0
        self.last_round_max_charge = 0
        self.clock_us = 0
        self.round_holds = 0
        self.hctx_inflight = 0     # inflight_count[hctx] map
        self.hold_cnt = 0          # iolat_hold_cnt[hctx] map
        self.cfg = cfg if cfg is not None else IolatCfg()

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    def _budget(self, ts, floor_max):
        """Round budget = QUANTUM * (weight * boost), floored at
        max(MIN_BUDGET, floor_max) — the get_boost_fn injection point
        (iolat_get_boost reads ts->boost at REFILL time only)."""
        ew = ts.weight * ts.boost
        if ew == 0:
            ew = 1
        floor = max(MIN_BUDGET, floor_max)
        if QUANTUM * ew < floor:
            ew = (floor + QUANTUM - 1) // QUANTUM
        return QUANTUM * ew

    def pick(self):
        self.clock_us += 1
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
                # Turn in progress (F2: detect empty-pop wedges ourselves)
                if ts.dispatches == ts.last_serve_dispatches:
                    ts.serve_no_progress += 1
                else:
                    ts.serve_no_progress = 0
                ts.last_serve_dispatches = ts.dispatches
                if ts.serve_no_progress > SERVE_NO_PROGRESS_MAX:
                    ts.serve_no_progress = 0
                    ts.refilled_round = self.current_round
                    ts.deficit = 0
                    self.cursor = (idx + 1) % MAX_TENANTS
                    continue
                self.cursor = idx
                ts.last_active_us = self.clock_us
                ts.serve_selects += 1
                return cgid
            if ts.refilled_round != self.current_round:
                budget = self._budget(ts, self.last_round_max_charge)
                ts.deficit = min(ts.deficit + budget, budget * CAP_MULT)
                ts.refilled_round = self.current_round
                ts.refill_arms += 1
                ts.last_active_us = self.clock_us
                self.cursor = idx
                return cgid
            self.cursor = (idx + 1) % MAX_TENANTS
        # scan complete: S10 round hold while any recently-active key keeps
        # unconsumed credit; stale credit forfeits at idle release.
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.deficit > 0:
                if self.clock_us - ts.last_active_us < ROUND_IDLE_RELEASE_US:
                    self.round_holds += 1
                    return 0
                ts.deficit = 0
        self.last_round_max_charge = self.round_max_charge
        self.round_max_charge = 0
        self.current_round += 1
        return 0

    def hold_dispatch(self):
        """Mirror arb_iolatency.bpf.c hold_dispatch(): true iff any active
        LC tenant is in SLO_PRESSURE and the per-hctx inflight aggregate
        has reached cfg.protect_inflight.  Each true decision bumps
        hold_cnt (iolat_hold_cnt, the acceptance-assertable counter)."""
        protect = self.cfg.protect_inflight or DEFAULT_PROTECT_INFLIGHT
        for idx in range(MAX_TENANTS):
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if not iolat_is_lc(ts.prio):
                continue
            if ts.slo_state == SLO_PRESSURE and self.hctx_inflight >= protect:
                self.hold_cnt += 1
                return True
        return False

    def advance_cursor(self):
        self.cursor = (self.cursor + 1) % MAX_TENANTS

    # framework hook handlers (mirror arb_note_*; charge unit = BYTES)

    def note_inserted(self, cgid):
        self.tenants[cgid].pending += 1

    def note_dispatched(self, cgid, nbytes):
        ts = self.tenants[cgid]
        ts.deficit -= nbytes
        if ts.pending > 0:
            ts.pending -= 1
        ts.inflight += 1
        self.hctx_inflight += 1
        ts.completed_bytes += nbytes
        ts.dispatches += 1
        ts.dispatch_fails = 0
        ts.serve_no_progress = 0
        ts.dispatch_log.append(nbytes)
        if nbytes > self.round_max_charge:
            self.round_max_charge = nbytes

    def note_requeue(self, cgid, refund):
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1
        if self.hctx_inflight > 0:
            self.hctx_inflight -= 1
        if refund:
            ts.deficit += refund
            ts.requeued_bytes += refund
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1
        if self.hctx_inflight > 0:
            self.hctx_inflight -= 1

    def note_dispatch_failed(self, cgid):
        ts = self.tenants[cgid]
        ts.dispatch_fails += 1
        if ts.dispatch_fails >= DISPATCH_FAIL_GHOST_RESET:
            ts.pending = 0
            ts.deficit = 0
            ts.dispatch_fails = 0
            ts.serve_no_progress = 0
            return
        if ts.pending > 0:
            ts.pending -= 1

    # BPF hook wiring (arb_iolatency.bpf.c)

    def dispatched_request(self, cgid, nr_sectors):
        self.note_dispatched(cgid, nr_sectors * SECTOR)

    def requeue_refund(self, cgid, nr_sectors):
        self.note_requeue(cgid, nr_sectors * SECTOR)

    def completed_request(self, cgid, now_ns, total_latency_ns):
        """Mirror arb_iolatency.bpf.c completed_request: inflight-- then
        the window FSM for LC tenants only (BE has no target: its boost
        can never move — that is the BE floor).  cfg.lat_target_ns == 0
        disables the loop (pure WDRR + hold gate)."""
        ts = self.tenants[cgid]
        self.note_completed(cgid)
        if not iolat_is_lc(ts.prio):
            return
        if self.cfg.lat_target_ns == 0:
            return
        slo_update_window(ts, now_ns, self.cfg.lat_target_ns,
                          self.cfg.boost_max, total_latency_ns)

    def dispatch_failed(self, cgid):
        self.note_dispatch_failed(cgid)
        self.advance_cursor()


# ---------------------------------------------------------------------------
# Saturated drive (mirror arb_slo_model.drive_backlogged, per-request)
# ---------------------------------------------------------------------------

def drive_backlogged(arb, requests, rounds=None, total=None,
                     lat=None, step_ns=None):
    """Keep every listed tenant backlogged at exactly one pending request
    (re-insert after each dispatch).  requests maps cgid -> nr_sectors.
    lat: optional per-cgid completion latency (constant or callable
    f(now_ns)); when given, each dispatch completes through
    arb.completed_request(now, lat) — the FSM runs live and a virtual
    clock advances step_ns per completed request (default 1ms).  When lat
    is None completions bypass the FSM (arb_note_completed only)."""
    if rounds is None and total is None:
        raise ValueError("drive_backlogged needs a rounds or total stop condition")
    for cg in requests:
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    served = {cg: 0 for cg in requests}
    now = BASE_NS
    if step_ns is None:
        step_ns = 1000 * 1000            # 1ms: ~100 samples per 100ms window
    done = 0
    while (rounds is None or arb.current_round < rounds) and \
            (total is None or done < total):
        cg = arb.pick()
        if cg == 0:
            continue            # round boundary; retry like blk-mq's next kick
        arb.dispatched_request(cg, requests[cg])
        if lat is not None:
            lv = lat[cg]
            lval = lv(now) if callable(lv) else lv
            arb.completed_request(cg, now, lval)
            now += step_ns
        else:
            arb.note_completed(cg)
        served[cg] += 1
        done += 1
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    return served


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Window-FSM helper: feed a whole window through the real slo_update_window
# (identical to arb_slo_model.feed_window, retargeted at the cfg)
# ---------------------------------------------------------------------------

def feed_window(ts, start_ns, total, over, cfg):
    """Feed `total` in-window completions (`over` of them violating) at 1us
    spacing, then one completion exactly WINDOW_NS after the window opened:
    the window rolls and the FSM consumes it.  Returns the new start."""
    now = start_ns
    for i in range(total):
        lat = BAD_LAT_NS if i < over else GOOD_LAT_NS
        slo_update_window(ts, now, cfg.lat_target_ns, cfg.boost_max, lat)
        now += 1000
    slo_update_window(ts, start_ns + WINDOW_NS, cfg.lat_target_ns,
                      cfg.boost_max, GOOD_LAT_NS)
    return start_ns + WINDOW_NS


def flush_window(ts, cfg):
    """Roll the in-flight window NOW on its own accumulated samples (one
    seeded GOOD sample lands in the fresh window, mirroring the C), so a
    subsequent feed_window judges a pure window instead of a mixture of
    drive leftovers and fed samples."""
    if ts.window_start_ns == 0:
        ts.window_start_ns = BASE_NS
        return
    slo_update_window(ts, ts.window_start_ns + WINDOW_NS,
                      cfg.lat_target_ns, cfg.boost_max, GOOD_LAT_NS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_class_and_config():
    """Roles and the --lat-target-ns knob: LC = prio 0..2; the SAME
    latency stream violates or satisfies the target depending on the
    configured value — proof the loader knob reaches the FSM."""
    ok = check("lc_roles",
               all(iolat_is_lc(p) for p in (0, 1, 2)) and
               not any(iolat_is_lc(p) for p in (3, 4, 5, 6, 7)),
               "LC must be exactly prio 0-2")

    stream = 100 * 1000   # 100us completions
    for target, want_state in ((50 * 1000, SLO_PRESSURE),
                               (200 * 1000, SLO_NORMAL)):
        ts = Tenant(100, 100, prio=1)
        cfg = IolatCfg(lat_target_ns=target)
        t = BASE_NS
        for _ in range(30):
            slo_update_window(ts, t, cfg.lat_target_ns, cfg.boost_max, stream)
            t += 1000
        slo_update_window(ts, BASE_NS + WINDOW_NS, cfg.lat_target_ns,
                          cfg.boost_max, stream)
        ok &= check(f"target_{target // 1000}us_{'pressure' if want_state else 'normal'}",
                    ts.slo_state == want_state,
                    f"stream=100us target={target}us state={ts.slo_state}")

    # target 0 disables the loop entirely (completed_request early-out)
    arb = IolatArbiter(IolatCfg(lat_target_ns=0))
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    drive_backlogged(arb, {100: R4K}, total=50,
                     lat={100: BAD_LAT_NS})
    ok &= check("target_zero_disables_loop",
                a.slo_state == SLO_NORMAL and a.boost == BOOST_NORMAL
                and a.window_total == 0,
                f"state={a.slo_state} boost={a.boost} window={a.window_total}")
    return ok


def _violating_feeder(period=20):
    """Every `period`-th LC completion misses the target (~5% violations:
    above the 1% line every window, never above the 10% severe line)."""
    n = [0]

    def feeder(_now):
        n[0] += 1
        return BAD_LAT_NS if n[0] % period == 0 else GOOD_LAT_NS
    return feeder


def test_case1_lc_pressure_compresses_be():
    """THE io.latency backpressure case: A = LC (prio 1, weight 100)
    missing its target ~5% of the time, B = BE (prio 5, weight 100).
    Sustained LC violations must compress BE service: A.boost climbs to
    the cap (4), B.boost stays 1 (per-tenant, BE floor), and the long-run
    service ratio A:B ~= boost ratio 4 — while B keeps a bounded nonzero
    share (BE floor)."""
    arb = IolatArbiter(IolatCfg())
    a = Tenant(100, 100, prio=1)     # LC
    b = Tenant(200, 100, prio=5)    # BE
    arb.register(a, 0)
    arb.register(b, 1)

    # warm-up: ~4 rounds per 100ms window at boost 1 (13 dispatches each);
    # rounds=10 covers the first window roll (PRESSURE/boost 4) plus
    # several boost-4 refills (round budget 512*100*4 = 204800B = 50 4K
    # dispatches per round vs B's 12.5).
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=10,
                     lat={100: _violating_feeder(), 200: GOOD_LAT_NS})

    ok = check("case1_lc_pressure_boost_capped",
               a.boost == BOOST_HIGH and a.slo_state == SLO_PRESSURE,
               f"A boost={a.boost} state={a.slo_state} "
               f"(expected 4/PRESSURE; severe 8 clamped by boost_max=4)")
    ok &= check("case1_be_boost_untouched",
                b.boost == BOOST_NORMAL and b.slo_state == SLO_NORMAL
                and b.window_total == 0,
                f"B boost={b.boost} state={b.slo_state} "
                f"(BE never updates — the floor)")

    # measurement phase: fresh rounds at boost 4
    bytes_a0, bytes_b0 = a.completed_bytes, b.completed_bytes
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=14,
                     lat={100: _violating_feeder(), 200: GOOD_LAT_NS})
    dfa = a.completed_bytes - bytes_a0
    dfb = b.completed_bytes - bytes_b0
    ratio = dfa / dfb if dfb else 0
    ok &= check("case1_be_compressed_ratio_4", 3.3 <= ratio <= 4.7,
                f"bytes A/B={ratio:.3f} (A={dfa} B={dfb}, expected ~4: "
                f"boost 4 vs 1 at equal weights)")
    ok &= check("case1_be_floor_nonzero", dfb > 0,
                f"B served={dfb} bytes (BE floor: cap 4 keeps B >= 1/5 "
                f"of contended service at equal weights)")
    return ok


def test_case2_be_floor_and_be_fairness():
    """BE floor semantics: under sustained LC pressure the BE CLASS is
    compressed but BE-vs-BE service stays strictly weight-proportional
    (every BE keeps boost 1), and the LC:BE class ratio is bounded by
    the boost cap."""
    arb = IolatArbiter(IolatCfg())
    a = Tenant(100, 100, prio=1)     # LC
    b1 = Tenant(200, 100, prio=5)    # BE
    b2 = Tenant(300, 300, prio=6)    # BE
    arb.register(a, 0)
    arb.register(b1, 1)
    arb.register(b2, 2)

    drive_backlogged(arb, {100: R4K, 200: R4K, 300: R4K}, rounds=10,
                     lat={100: _violating_feeder(),
                          200: GOOD_LAT_NS, 300: GOOD_LAT_NS})
    ok = check("case2_lc_in_pressure_setup",
               a.boost == BOOST_HIGH and a.slo_state == SLO_PRESSURE,
               f"A boost={a.boost} state={a.slo_state}")

    s0 = {cg: t.completed_bytes for cg, t in arb.tenants.items()}
    drive_backlogged(arb, {100: R4K, 200: R4K, 300: R4K}, rounds=16,
                     lat={100: _violating_feeder(),
                          200: GOOD_LAT_NS, 300: GOOD_LAT_NS})
    da = a.completed_bytes - s0[100]
    db1 = b1.completed_bytes - s0[200]
    db2 = b2.completed_bytes - s0[300]
    be_ratio = db1 / db2 if db2 else 0
    ok &= check("case2_be_vs_be_weight_proportional",
                0.28 <= be_ratio <= 0.4,
                f"B1/B2={be_ratio:.3f} (weights 100:300, expected ~0.333 "
                f"— BE stays weight-fair under LC pressure)")
    ok &= check("case2_be_boosts_stay_1",
                b1.boost == 1 and b2.boost == 1,
                f"B1 boost={b1.boost} B2 boost={b2.boost}")
    class_ratio = da / (db1 + db2)
    ok &= check("case2_class_ratio_bounded_by_cap",
                class_ratio <= 1.3,
                f"LC:BE class ratio={class_ratio:.3f} (boost cap 4 at "
                f"equal weights bounds LC:BE_class ~ 4*100/(100+300)=1)")
    return ok


def test_case3_recovery_smooth():
    """Backpressure release is SMOOTH (io.latency's proportional scaling
    analog): PRESSURE/4 -> RECOVERY/2 after one good window -> NORMAL/1
    after three consecutive good windows.  No 4->1 jump; the service
    ratio steps down ~4 -> ~2 -> ~1 with the ladder (measured with the
    FSM frozen — lat=None completions bypass the window update — so each
    phase measures the pure DRR share at that boost)."""
    cfg = IolatCfg()
    arb = IolatArbiter(cfg)
    a = Tenant(100, 100, prio=1)
    b = Tenant(200, 100, prio=5)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=10,
                     lat={100: _violating_feeder(), 200: GOOD_LAT_NS})
    ok = check("recovery_setup_pressure",
               a.slo_state == SLO_PRESSURE and a.boost == BOOST_HIGH,
               f"boost={a.boost} state={a.slo_state}")

    # one clean window -> RECOVERY, boost 2 (no direct 4 -> 1 jump).  The
    # in-flight mixed window must roll on its own samples first.
    flush_window(a, cfg)
    if a.slo_state == SLO_PRESSURE:
        feed_window(a, a.window_start_ns + 1, 100, 0, cfg)
    ok &= check("recovery_step_to_2",
                a.slo_state == SLO_RECOVERY and a.boost == BOOST_MILD,
                f"boost={a.boost} state={a.slo_state} (4 -> 2, no jump)")

    # service at boost 2 (FSM frozen): A round budget 512*100*2 = 102400B
    # = 25 dispatches per round vs B's 12.5
    s0 = (a.completed_bytes, b.completed_bytes)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=13)
    dfa = a.completed_bytes - s0[0]
    dfb = b.completed_bytes - s0[1]
    ratio = dfa / dfb if dfb else 0
    ok &= check("recovery_service_ratio_2", 1.7 <= ratio <= 2.3,
                f"bytes A/B={ratio:.3f} (expected ~2 at boost MILD)")

    # clean windows to good_windows == 3 -> NORMAL, boost 1 (each flush
    # rolls one window; the mixed windows are all-GOOD by now)
    for _ in range(4):
        if a.slo_state == SLO_NORMAL:
            break
        flush_window(a, cfg)
    ok &= check("recovery_to_normal",
                a.slo_state == SLO_NORMAL and a.boost == BOOST_NORMAL,
                f"boost={a.boost} state={a.slo_state} good={a.good_windows}")
    # service at boost 1: BE recovered its weight share (equal weights)
    s0 = (a.completed_bytes, b.completed_bytes)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=16)
    dfa = a.completed_bytes - s0[0]
    dfb = b.completed_bytes - s0[1]
    ratio = dfa / dfb if dfb else 0
    ok &= check("recovery_service_ratio_1", 0.75 <= ratio <= 1.35,
                f"bytes A/B={ratio:.3f} (BE recovered its weight share)")
    return ok


def test_case4_jitter_smoothing():
    """Backpressure jitter is smoothed STRUCTURALLY (window aggregation +
    min samples): marginal violation rates below the 1% line and thin
    windows below MIN_SAMPLES cannot engage pressure; sustained >1% does.
    (Mirrors the shared FSM's no-truncation violation math.)"""
    cfg = IolatCfg()

    def rolled_state(total, over, prio=1):
        ts = Tenant(100, 100, prio=prio)
        feed_window(ts, BASE_NS, total, over, cfg)
        return ts

    ok = check("jitter_below_1pct_suppressed",
               rolled_state(150, 1).slo_state == SLO_NORMAL,
               "0.67% over: 100 <= 150 -> NORMAL")
    ok &= check("jitter_at_1pct_strict_no_violation",
                rolled_state(100, 1).slo_state == SLO_NORMAL,
                "exactly 1%: 100 > 100 is false -> NORMAL")
    ok &= check("jitter_1p33pct_violates",
                rolled_state(150, 2).slo_state == SLO_PRESSURE,
                "1.33% over: 200 > 150 -> PRESSURE")
    ok &= check("jitter_thin_window_suppressed",
                rolled_state(19, 19).slo_state == SLO_NORMAL,
                f"19 samples all violating < MIN_SAMPLES={MIN_SAMPLES}")
    ok &= check("jitter_sustained_5pct_violates",
                rolled_state(100, 5).slo_state == SLO_PRESSURE,
                "5% over: 500 > 100 -> PRESSURE (the case1 feeder regime)")
    # one isolated BAD completion inside a healthy window cannot trip it
    ok &= check("jitter_single_bad_isolated",
                rolled_state(100, 1).ewma_latency_ns >= 0
                and rolled_state(100, 0).slo_state == SLO_NORMAL,
                "a single bad completion is averaged away")
    return ok


def test_case5_hold_gate():
    """The hard LC protection brake (arb_slo's second-level gate): engages
    at the per-hctx inflight threshold while an LC tenant is in PRESSURE,
    releases as completions drain inflight, and NEVER fires without LC
    pressure — neither for a BE-only workload nor for LC in NORMAL."""
    cfg = IolatCfg(protect_inflight=2)
    arb = IolatArbiter(cfg)
    a = Tenant(100, 100, prio=1)
    b = Tenant(200, 100, prio=5)
    arb.register(a, 0)
    arb.register(b, 1)
    feed_window(a, BASE_NS, 25, 5, cfg)     # 25 samples, 5 over -> PRESSURE
    ok = check("hold_precondition_lc_pressure",
               a.slo_state == SLO_PRESSURE,
               f"state={a.slo_state}")

    def pick_until(arb_):
        cg = arb_.pick()
        while cg == 0:
            cg = arb_.pick()
        return cg

    for _ in range(8):
        arb.note_inserted(100)
    for _ in range(2):
        arb.dispatched_request(pick_until(arb), R4K)
    ok &= check("hold_engages_at_threshold",
                arb.hctx_inflight == 2 and arb.hold_dispatch() is True
                and arb.hold_cnt == 1,
                f"inflight={arb.hctx_inflight} hold_cnt={arb.hold_cnt} "
                f"(protect_inflight=2 from cfg)")
    arb.completed_request(100, BASE_NS + WINDOW_NS, GOOD_LAT_NS)
    ok &= check("hold_releases_after_completion",
                arb.hctx_inflight == 1 and arb.hold_dispatch() is False,
                f"inflight={arb.hctx_inflight}")
    arb.dispatched_request(pick_until(arb), R4K)
    ok &= check("hold_reengages_when_refilled",
                arb.hold_dispatch() is True and arb.hold_cnt == 2,
                f"hold_cnt={arb.hold_cnt}")

    # BE-only workload at the threshold: never fires
    arb2 = IolatArbiter(cfg)
    a2 = Tenant(100, 100, prio=1)
    b2 = Tenant(200, 100, prio=5)
    arb2.register(a2, 0)
    arb2.register(b2, 1)
    for _ in range(8):
        arb2.note_inserted(200)
        arb2.dispatched_request(pick_until(arb2), R4K)
    ok &= check("hold_never_without_pressure_be_only",
                arb2.hctx_inflight == 8 and arb2.hold_dispatch() is False
                and arb2.hold_cnt == 0,
                f"inflight={arb2.hctx_inflight} hold_cnt={arb2.hold_cnt}")
    # LC present but NORMAL at the threshold: never fires
    arb3 = IolatArbiter(cfg)
    a3 = Tenant(100, 100, prio=1)
    arb3.register(a3, 0)
    for _ in range(2):
        arb3.note_inserted(100)
        arb3.dispatched_request(pick_until(arb3), R4K)
    ok &= check("hold_never_with_lc_normal",
                arb3.hctx_inflight == 2 and arb3.hold_dispatch() is False,
                f"inflight={arb3.hctx_inflight} state={a3.slo_state}")
    return ok


def test_boost_consumed_at_refill():
    """Boost enters effective_weight at REFILL time only: mid-turn boost
    changes must not re-arm the current turn (the next round's refill
    picks up the new value)."""
    arb = IolatArbiter(IolatCfg())
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    served = drive_backlogged(arb, {100: R4K}, rounds=1)
    # boost 1: round budget 512*100 = 51200B -> 13 dispatches (12 full +
    # overshoot 2560)
    ok = check("boost_refill_round1_budget",
               served[100] * 4096 == 13 * 4096 and a.refill_arms == 1,
               f"served={served[100]} refills={a.refill_arms}")
    a.boost = 2                               # boost change between turns
    served = drive_backlogged(arb, {100: R4K}, rounds=2)
    # boost 2: round budget 102400 armed at 99840 (carry -2560) -> 25 dispatches
    ok &= check("boost_refill_uses_new_boost",
                served[100] * 4096 == 25 * 4096 and a.refill_arms == 2,
                f"served={served[100]} refills={a.refill_arms}")
    # mid-turn change: grant one request's worth, drop the boost, dispatch
    # once — no refill may fire mid-round and the charge comes out of the
    # armed budget.
    a.boost = 1
    a.deficit = 4096
    a.refilled_round = arb.current_round      # already granted this round
    served = drive_backlogged(arb, {100: R4K}, total=1)
    ok &= check("boost_midturn_no_rearm",
                served[100] == 1 and a.deficit == 0
                and a.refill_arms == 2,
                f"served={served[100]} deficit={a.deficit} "
                f"refills={a.refill_arms}")
    return ok


def test_invariants():
    """Lifecycle/invariants: requeue refund + no pending++, dispatch_failed
    retire, work conservation, refill once per round, overshoot bound."""
    # requeue lifecycle
    arb = IolatArbiter(IolatCfg())
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()
    armed = a.deficit
    arb.dispatched_request(cg, R4K)
    arb.requeue_refund(cg, R4K)              # refund, NO pending++
    pending_after_requeue = a.pending
    arb.note_inserted(cg)                    # kernel re-insert
    arb.dispatched_request(arb.pick(), R4K)
    arb.note_completed(cg)
    ok = check("requeue_no_pending_increment_and_refund",
               pending_after_requeue == 0 and a.pending == 0
               and a.inflight == 0 and arb.hctx_inflight == 0
               and a.deficit == armed - 4096 and a.requeued_bytes == 4096,
               f"pending={a.pending} inflight={a.inflight} "
               f"deficit={a.deficit} armed={armed} "
               f"(net -4096: two charges, one refund)")

    # dispatch_failed: retire pending only; emptied tenant not picked
    arb2 = IolatArbiter(IolatCfg())
    b = Tenant(100, 100, prio=1)
    arb2.register(b, 0)
    arb2.note_inserted(100)
    cg = arb2.pick()
    assert cg == 100
    armed = b.deficit
    arb2.dispatch_failed(100)
    ok &= check("dispatch_failed_retires_pending",
                b.pending == 0 and b.deficit == armed,
                f"pending={b.pending} deficit={b.deficit}")
    ok &= check("dispatch_failed_empty_not_picked", arb2.pick() == 0,
                "strict NULL for the emptied tenant")

    # work conservation + idle credit cap + refill once per round
    arb3 = IolatArbiter(IolatCfg())
    x = Tenant(100, 100, prio=1)
    y = Tenant(200, 100, prio=5)
    arb3.register(x, 0)
    arb3.register(y, 1)
    served = drive_backlogged(arb3, {100: R4K}, rounds=3)
    ok &= check("work_conservation_idle_no_credit",
                y.dispatches == 0 and y.deficit == 0 and y.refill_arms == 0,
                f"Y dispatches={y.dispatches} deficit={y.deficit}")
    ok &= check("idle_credit_cap",
                x.deficit <= arb3._budget(x, 0) * CAP_MULT,
                f"X deficit={x.deficit}")

    arb4 = IolatArbiter(IolatCfg())
    x = Tenant(100, 100, prio=1)
    y = Tenant(200, 100, prio=5)
    arb4.register(x, 0)
    arb4.register(y, 1)
    drive_backlogged(arb4, {100: R4K, 200: R4K}, rounds=3)
    ok &= check("refill_once_per_round",
                x.refill_arms == 3 and y.refill_arms == 3,
                f"X refills={x.refill_arms} Y refills={y.refill_arms}")

    # overshoot bounded by one max request
    arb5 = IolatArbiter(IolatCfg())
    z = Tenant(100, 100, prio=1)
    arb5.register(z, 0)
    worst = 0
    for _ in range(500):
        arb5.note_inserted(100)
        cg = arb5.pick()
        while cg == 0:
            cg = arb5.pick()
        arb5.dispatched_request(cg, R256K)
        arb5.note_completed(100)
        worst = min(worst, z.deficit)
    ok &= check("overshoot_bound", worst >= -MAX_REQ_BYTES,
                f"worst deficit={worst} bound=-{MAX_REQ_BYTES}")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_class_and_config()
    ok &= test_case1_lc_pressure_compresses_be()
    ok &= test_case2_be_floor_and_be_fairness()
    ok &= test_case3_recovery_smooth()
    ok &= test_case4_jitter_smoothing()
    ok &= test_case5_hold_gate()
    ok &= test_boost_consumed_at_refill()
    ok &= test_invariants()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
