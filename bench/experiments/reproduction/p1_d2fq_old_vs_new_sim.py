#!/usr/bin/env python3
"""arb_d2fq old-vs-new semantics simulation (P1 evidence, 2026-11-14).

Reproduces the d2fq campaign failure mechanism (weight 100:400 -> byte
share 0.500, per-hctx dispatch skews that sum to 1:1) and demonstrates
that the cgroup-level state fix restores the weight semantics (~1:4).

Faithful dynamics for BOTH simulators (same request stream):
  - per-tenant outstanding cap QD=16 (fio QD16): queued + inflight <= 16;
    a new request is inserted only when a completion frees a slot;
  - request home hctx = submission CPU's queue mapping, modelled as
    uniform over 6 hctx (single fio job migrating across vCPUs);
  - q_fifo holds ONE per-cgroup GLOBAL FIFO queue; any hctx's dispatch
    turn may pop it (scx_io_validate_dispatch_rq requires only the same
    QUEUE, not the same hctx);
  - dispatch turns arrive per hctx (insert re-kick + completion re-kick,
    modelled as uniform turns);
  - charge vtime = cost*VT_NUM*100/(100*weight) on the DECISION key
    (dispatching hctx for OLD, cgroup lane for NEW); pending-- on the
    HOME key (OLD) or the cgroup (NEW);
  - deceptive idleness: insert after the key's lanes were empty for
    > 200 ms resets its vtimes to the cached gvt (per-hctx cache for
    OLD, single global cache for NEW).

  OLD (pre-2026-11-14 arb_d2fq.bpf.c): WFQ state keyed per-(cgroup,hctx);
      pick on hctx H eligible iff (cg,H).pending > 0.
  NEW (post-fix): one cgroup-level key; eligible iff cgroup pending > 0.

Failure mechanism reproduced here (all three legs measured in the
campaign ledger, results/campaign/d2fq/rep1/iox/loader_arb_d2fq.stdout):
  (1) per-lane eligibility fragmentation: the per-cgroup queue is nearly
      empty at QD16 dispatch rates and global-FIFO pops drain any home,
      so (cg,h).pending is 0 for most (hctx,tenant) pairs at any instant
      -> single-tenant hctx episodes (per-window data: each hctx served
      exactly one tenant);
  (2) DI-reset credit wipe: a starved tenant's frozen lane re-arms only
      via an insert homed on that hctx, and the >200ms idle triggers
      vtime := hctx gvt -- the frozen lane's huge deficit signal (which
      would drive multi-second catch-up service) is erased
      (measured: (t400,h0) lane 2.4G frozen -> reset to 607.8G on re-arm);
  (3) net effect: per-hctx episodes are volume-balanced, aggregate
      share ~1:1 regardless of weights.
"""

import random
from collections import deque

VT_NUM = 840 << 10
COST_128K_WRITE = 8192          # write cost table bucket 3 (128K)
N_HCTX = 6
WEIGHTS = {100: 100, 400: 400}  # cgid -> arb_weight
QD = 16                         # per-tenant outstanding cap (fio iodepth)
COMPLETE_DELAY = 12             # steps from dispatch to completion
TOTAL_STEPS = 300_000
STEP_NS = 500_000               # 0.5 ms per dispatch turn
DI_PERIOD_STEPS = int(200e6 / STEP_NS)   # di_period 200ms in steps
POOL_SIZE = 2                   # sticky insert-home pool per tenant
POOL_SWITCH = 40_000            # fio CPU migration granularity (~20 s)


def draw_pool():
    return random.sample(range(N_HCTX), POOL_SIZE)


def advance(weight):
    return COST_128K_WRITE * VT_NUM * 100 // (100 * WEIGHTS[weight])


class BaseSim:
    def __init__(self):
        self.queued = {cg: deque() for cg in WEIGHTS}   # homes (FIFO)
        self.inflight = {cg: [] for cg in WEIGHTS}      # completion steps
        self.vt = {}
        self.pend = {}
        self.ds = {}
        self.last_active = {}
        self.di_resets = {}
        # sticky insert-home pools: the single fio job per tenant runs on
        # a migrating CPU whose blk-mq mapping stays on 1-2 hctx for long
        # stretches (campaign per-window data: t100 only h0/h1, t400 only
        # h2/h4 within single 1 s windows)
        self.pool = {cg: draw_pool() for cg in WEIGHTS}
        for cg in WEIGHTS:
            for h in range(N_HCTX):
                key = (cg, h)
                self.vt[key] = 0
                self.pend[key] = 0
                self.ds[key] = 0
                self.last_active[key] = 0
                self.di_resets[key] = 0
        self.hctx_gvt = [0] * N_HCTX   # cached gvt (DI reset target)

    def outstanding(self, cg):
        return len(self.queued[cg]) + len(self.inflight[cg])

    def completions(self, step):
        for cg in WEIGHTS:
            keep = []
            for done_at in self.inflight[cg]:
                if done_at <= step:
                    if self.outstanding(cg) < QD:
                        self.insert(cg, step)
                else:
                    keep.append(done_at)
            self.inflight[cg] = keep

    def _gvt_update(self, h, vt):
        self.hctx_gvt[h] = vt if self.hctx_gvt[h] == 0 \
            else min(self.hctx_gvt[h], vt)


class OldSem(BaseSim):
    """Per-(cgroup,hctx) lanes + global per-cgroup queue (the bug)."""

    def insert(self, cg, step_now):
        if step_now // POOL_SWITCH != (step_now - 1) // POOL_SWITCH:
            self.pool[cg] = draw_pool()
        home = random.choice(self.pool[cg])
        key = (cg, home)
        # DI mirror (d2fq_note_inserted): THIS key's lanes (read+write;
        # this workload is write-only) empty for > di_period
        if self.pend[key] == 0 and self.last_active[key] and \
                step_now - self.last_active[key] > DI_PERIOD_STEPS:
            self.vt[key] = self.hctx_gvt[home]
            self.di_resets[key] += 1
        self.last_active[key] = step_now
        self.queued[cg].append(home)
        self.pend[key] += 1

    def step(self, h, step_now):
        best, best_vt = None, None
        for cg in WEIGHTS:
            if self.pend[(cg, h)] > 0:
                vt = self.vt[(cg, h)]
                if best is None or vt < best_vt:
                    best, best_vt = cg, vt
        if best is None:
            return None
        home = self.queued[best].popleft()      # GLOBAL queue: any home
        self.pend[(best, home)] -= 1            # pending-- on HOME key
        self.vt[(best, h)] += advance(WEIGHTS[best])  # charge DECISION key
        self.ds[(best, h)] += 1
        self._gvt_update(h, self.vt[(best, h)])
        self.inflight[best].append(step_now + COMPLETE_DELAY)
        return best


class NewSem(BaseSim):
    """Cgroup-level key (the fix): eligibility, charge, DI per cgroup."""

    def insert(self, cg, step_now):
        key = (cg, 0)
        if self.pend[key] == 0 and self.last_active[key] and \
                step_now - self.last_active[key] > DI_PERIOD_STEPS:
            self.vt[key] = self.hctx_gvt[0]     # global gvt cache
            self.di_resets[key] += 1
        self.last_active[key] = step_now
        self.queued[cg].append(random.randrange(N_HCTX))  # home unused
        self.pend[key] += 1

    def step(self, _h, step_now):
        best, best_vt = None, None
        for cg in WEIGHTS:
            if self.pend[(cg, 0)] > 0:
                vt = self.vt[(cg, 0)]
                if best is None or vt < best_vt:
                    best, best_vt = cg, vt
        if best is None:
            return None
        self.queued[best].popleft()
        self.pend[(best, 0)] -= 1
        self.vt[(best, 0)] += advance(WEIGHTS[best])
        self.ds[(best, 0)] += 1
        self._gvt_update(0, self.vt[(best, 0)])
        self.inflight[best].append(step_now + COMPLETE_DELAY)
        return best


def run(sim, label):
    random.seed(20261114)
    for cg in WEIGHTS:                 # fio ramp-up: fill to QD
        while sim.outstanding(cg) < QD:
            sim.insert(cg, 0)
    for step in range(TOTAL_STEPS):
        sim.completions(step)
        h = random.randrange(N_HCTX)   # dispatch turn on some hctx
        sim.step(h, step)
    if isinstance(sim, OldSem):
        d = {cg: sum(sim.ds[(cg, h)] for h in range(N_HCTX))
             for cg in WEIGHTS}
        di = sum(sim.di_resets.values())
    else:
        d = {cg: sim.ds[(cg, 0)] for cg in WEIGHTS}
        di = sum(sim.di_resets.values())
    share400 = d[400] / (d[100] + d[400])
    print(f"{label}: t100 ds={d[100]}  t400 ds={d[400]}  "
          f"t400 share={share400:.4f}  di_resets={di}")
    if isinstance(sim, OldSem):
        print("  per-hctx t100:t400 dispatch ratios:")
        for h in range(N_HCTX):
            a, b = sim.ds[(100, h)], sim.ds[(400, h)]
            if a or b:
                r = f"{a / b:.2f}:1" if b else "all t100"
                print(f"    h{h}: {a}:{b}  ({r})")
    return share400


if __name__ == "__main__":
    print(f"simulation: {N_HCTX} hctx, 128K writes, weight 100:400, "
          f"QD={QD}/tenant, completion delay {COMPLETE_DELAY} steps, "
          f"{TOTAL_STEPS} turns")
    s_old = run(OldSem(), "OLD per-(cg,hctx) lanes")
    s_new = run(NewSem(), "NEW cgroup-level lanes  ")
    print(f"\nverdict: OLD = {s_old:.3f} (campaign observed 0.500), "
          f"NEW = {s_new:.3f} (weight target 0.800)")
