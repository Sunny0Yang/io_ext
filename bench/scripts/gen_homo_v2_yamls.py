#!/usr/bin/env python3
"""Generate the homo-v2 (homomorphic-algorithm reproduction) task YAML.

Emits ONE task config holding THREE sequence families (run each with
`python -m vm.executor --config <cfg> --sequence <SEQ>`):

  src/vm/configs/task/homo_v2/homo_v2.yml

  seq_elevator    elevator-family homo arms, single tenant, fio 4k
                  randrw 70/30 QD64 direct=1, ~60 s (DEEP queue: elevator
                  value lives under deep QD read/write interference, not
                  QD8 shallow streams — validation rep1 2026-11-07):
                    HOMO_elev_n_none              native none
                    HOMO_elev_n_mq_deadline       native mq-deadline
                    HOMO_elev_x_mq_deadline_q     scx_io + q_mq_deadline (root)
                    HOMO_elev_n_kyber             native kyber
                    HOMO_elev_x_kyber_q           scx_io + q_kyber (root)
  seq_controller  controller-family homo arms, LC+BE coloc fio
                  (LC = 4k randread QD32 high pressure (~4x the QD8
                  validation-round point: LC+BE offered demand ~2x device
                  capacity so weight/cost/latency arbitration can bind),
                  BE = 64k seqwrite QD16 unchanged):
                    HOMO_ctrl_n_none              native none baseline
                    HOMO_ctrl_n_io_max            native BE io.max rbps/wbps cap
                    HOMO_ctrl_x_iomax_q           scx_io + loader_q_iomax (BE)
                    HOMO_ctrl_n_io_weight         native io.weight 100:200
                    HOMO_ctrl_x_weight_arb        arb_wdrr + q_fifo -p/-W
                    HOMO_ctrl_n_io_cost           native io.cost model+qos
                    HOMO_ctrl_x_cost_arb          arb_iocost (provisional CLI)
                    HOMO_ctrl_n_io_latency        native io.latency target
                    HOMO_ctrl_x_latency_arb       arb_iolatency --lat-target-ns
  seq_frontier    frontier arms on the SAME LC+BE coloc workload:
                    HOMO_front_x_d2fq_arb         arb_d2fq (r/w cost default)
                    HOMO_front_x_linnos_q         q_linnos (LC never intercepted,
                                                  BE-only --ir table)
                  The frontier native reference column does NOT re-run:
                  it cross-references HOMO_ctrl_n_none (identical workload,
                  kernel, and device) — no HOMO_front_n_none sequence is
                  generated, and the run script never replays it.

Sequence ids embed the arm id after the `HOMO_<family>_` prefix so
aggregators can parse the arm from the sequence id (same semantics as
build_rq1_results.py ARM_FROM_SEQ):
    arm_id = sequence_id_without_prefix("HOMO_<family>_")

Conventions follow: config/task/homo_similarity/paper_homo_mq_deadline.yml
(profile -> io_ext_policy global loader scene), scripts/gen_isol_bench_paper_yamls.py
(_root_policy/_arbiter/_cg_policy/cgroup_policies_from_groups), and
scripts/gen_dynamic_tenant_yamls.py (loader CLI "-c {policy_cgroup_path}",
scx hygiene, /opt/io-ext/userspace loader dir).  All tunable constants live
at the top of this file.

Usage:
  python3 scripts/gen_homo_v2_yamls.py [--check] [--output-dir DIR]
      [--profile kvmhost|bm] [--calibration-json PATH]
Env:
  HOMO_V2_TARGET_DEVICE   block device inside the guest (default: sdb;
                          run_homo_v2_kvmhost.sh exports the discovered dev)
  HOMO_V2_LOADER_DIR      guest loader dir (default: /opt/io-ext/userspace)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "src/vm/configs/task/homo_v2"
OUT_FILENAME = "homo_v2.yml"

TARGET_DEVICE = os.environ.get("HOMO_V2_TARGET_DEVICE", "sdb")
LOADER_DIR = os.environ.get("HOMO_V2_LOADER_DIR", "/opt/io-ext/userspace")
PROFILE = "kvmhost"
FIO_FILES: Dict[str, str] = {
    "elevator": f"/dev/{TARGET_DEVICE}",
    "lc": f"/dev/{TARGET_DEVICE}",
    "be": f"/dev/{TARGET_DEVICE}",
}
STEP_TIMEOUTS_ENABLED = True
LOADER_STALE = ["loader_arb_", "loader_q_", "loader_minimal"]
IOEXT_SCX_SCHEDULER = "scx_io"

_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024

# ---------------------------------------------------------------------------
# Tunable workload constants (single source of truth; TODO-calibration points
# are annotated per constant).
# ---------------------------------------------------------------------------

# -- seq_elevator (single-tenant fio, 4k 70r/30w randmix, QD64, direct=1) -----
# DEEP-queue caliber (2026-11-07 iteration#1): elevator schedulers have no
# room to help under shallow QD8 streams (validation rep1: native none was
# fastest at 10818 IOPS); their value is protecting READ tail latency under
# deep-queue r/w interference, so ELEV_QD goes 8 -> 64 and the head metric is
# elev_read_p99_us (IOPS kept secondary).
ELEV_RUNTIME_SEC = 60          # ~60 s per arm per rep (task spec)
ELEV_STEP_DURATION = 90        # fio runtime + spawn/teardown margin
ELEV_BS = "4k"
ELEV_IODEPTH = 64
ELEV_RWMIXREAD = 70
ELEV_PERCENTILES = "50:90:95:99:99.9"
# q_kyber proxy targets (loader defaults = kernel kyber defaults; TODO
# calibration: consider matching native kyber's /sys/block/*/queue/iosched
# read/write lat targets measured on the campaign disk).
KYBER_READ_TARGET_NS = 2_000_000
KYBER_WRITE_TARGET_NS = 10_000_000
# q_mq_deadline proxy merge mode D0..D3 (loader default 0; TODO calibration).
MQDD_MERGE_MODE: Optional[int] = None   # None = omit (loader default 0)

# -- seq_controller / seq_frontier (LC+BE coloc fio) --------------------------
# LC QD32 (2026-11-07 iteration#1): LC QD8 demanded only ~200 MiB/s, so
# LC+BE offered ~1.1x device capacity and the weight/cost/latency arms could
# not bind (share_ratio_dev 3.05 uninformative at saturation boundary).
# QD32 -> ~4x -> total offered ~2x under-subscription, enough headroom for
# arbitration metrics to differentiate.  BE unchanged (64k seqwrite QD16).
CTRL_RUNTIME_SEC = 60
CTRL_STEP_DURATION = 120       # fio runtime + two tenants + monitoring margin
LC_BS = "4k"
LC_RW = "randread"
LC_IODEPTH = 32                # high-pressure LC probe (deep enough to saturate)
LC_RAMP_SEC = 5
BE_BS = "64k"
BE_RW = "write"                # BE = seqwrite
BE_IODEPTH = 16
BE_PERCENTILES = "50:90:95:99:99.9"
# BE offered-rate override (MiB/s); None = UNLIMITED seqwrite.  The io.max /
# q_iomax caps below are the knob under test, so the workload stays uncapped
# by default (the cap is what must bind).  NOTE (rq5 contract 2026-11-05): the
# kvmhost sdb sustained write band is 83-145 MiB/s with deep-trough excursions
# -- BE write results at sustained band will sit well below the nominal cap.
# Set e.g. 64 to anchor BE at the sustained-band floor instead (rq5 ladder
# top A2) -- then interpret the io.max cap as a safety ceiling, and tracking
# error as deviation of the OFFERED rate, not of the cap.
BE_RATE_MIB: Optional[int] = None

# -- arm constants ------------------------------------------------------------
# The io.cost linear-model input keeps the D3/RECAL 320 MiB/s saturation
# anchor (model-device mismatch is the honest virtualized-device data point
# per the rq5 contract; TODO iocost_coef_gen re-measure before publishing
# graded comparisons).
#
# The io.max cap tier is re-anchored: the pilot (2026-11-06, no per-run disk
# reset) showed the 320 MiB/s cap does NOT bind under LC colocation (BE
# seqwrite sustained ~50-275 MiB/s -> be_track_err_pct 80-90% by
# construction).  With the per-run blkdiscard reset restored (run script
# rerun#2), 64 MiB/s = the rq5 BE write anchor binds in fresh and decayed
# regimes, making |BE throughput - cap|/cap a faithful cap-tracking metric.
IO_MAX_BYTES = 64 * _MIB
IOCOST_MODEL = {                # gen_isol_bench_paper_yamls.py IOCOST_MODEL_SDB
    "ctrl": "user", "model": "linear",
    "rbps": 320 * _MIB, "rseqiops": 82000, "rrandiops": 82000,
    "wbps": 320 * _MIB, "wseqiops": 82000, "wrandiops": 82000,
}
# io.cost qos: rpct 99 stays on the paper sdb grid vertex; rlat is
# RE-ANCHORED 20000 -> 2000 us (2026-11-07 iteration#3): the former 20 ms
# rlat NEVER triggered (coloc LC p99 under the none baseline is ~4.9 ms,
# far below 20 ms), so vrate never adjusted and native io.cost read as a
# no-op.  rlat=2 ms < 4.9 ms makes the qos binding: whenever the LC 99th
# percentile (qos rpct 99) exceeds 2 ms the controller must move vrate and
# throttle the BE stream back down -- a healthy native io.cost actively
# arbitrates instead of idling.  TODO iocost_coef_gen re-measure stays.
IOCOST_QOS = {
    "enable": True, "ctrl": "user",
    "rpct": 99.0, "rlat": 2_000, "wpct": 95.0, "wlat": 1_000_000,
    "min": 50.0, "max": 150.0,
}


def _load_bm_calibration(path: Path) -> None:
    """Load the measured bm io.cost model without changing frozen QoS targets."""
    import json

    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load bm calibration {path}: {exc}") from exc
    model = doc.get("iocost_model")
    required = ("rbps", "rseqiops", "rrandiops", "wbps", "wseqiops", "wrandiops")
    if not isinstance(model, dict) or any(not isinstance(model.get(key), int) for key in required):
        raise SystemExit(f"invalid iocost_model in {path}: expected integer fields {required}")
    IOCOST_MODEL.update({key: model[key] for key in required})


# io.weight arm: LC:BE = 1:2 (task spec "weights 1:2, native io.weight=100/200";
# LC first => LC=100, BE=200).  The SAME ratio travels to arb_wdrr via loader
# -W so native and homo arms are comparable.  TODO: revisit direction (BE
# heavier is the literal task reading) before freezing the figure.
WEIGHT_LC = 100
WEIGHT_BE = 200
# arb -p priorities (lower = higher priority; LC stays in the latency band,
# BE in the background band; mirrors STATIC_ARM_PRIO from the dynamic-tenant
# campaign).  Weights are the -W values.
ARB_PRIO_LC = 2
ARB_PRIO_BE = 5
# io.latency arm: LC target 2 ms (2026-11-07 iteration#3 recalibration:
# the iteration#2 1 ms target is BELOW the reachable latency floor and the
# knob's self-throttling semantics turned it into self-harm -- blk-iolatency
# has no downstream tenant to compress when the LC stream's own submitters
# are the ones over target, so the LC stream itself got throttled (validation
# rep1: LC iops 84k -> 72k) while LC p99 stayed at ~5.7 ms, far off the
# 1 ms target).  target=2 ms sits BETWEEN the ~4.9 ms none-baseline LC p99
# (QD32 coloc) and the hardware floor: the self-throttle shrinks the LC
# queue depth as p99 approaches target, so LC latency lands close to 2 ms
# at modest throughput cost, and native io.latency wins over none on
# PREDICTABILITY (p99/p99.9 near-target) rather than mean latency -- the D3
# precedent measured target 4 ms -> achieved ~4.7 ms.  (History: the
# original 20 ms target never bound either -- the controller idled while
# the arbiter machinery still taxed every IO.)  Kernel io.latency files are
# guarded in common.py (`if [ -f io.latency ]`): on a guest whose kernel
# lacks blk-iolatency the write is SKIPPED SILENTLY -- the rq5 doc recorded
# io.latency missing on the stock guest kernel; 7.3.1-ioext kernels must be
# verified to ship it before grading this arm (see RUNBOOK).
IO_LATENCY_TARGET_US = 2_000
# arb_iolatency LC latency target (ns; == IO_LATENCY_TARGET_US * 1000).
# protect-inflight / boost-max ride the loader defaults (32 / 4) -- see ARMS.
ARB_IOLATENCY_LAT_TARGET_NS = 2_000_000
# q_linnos device-busy reference (validation fix 2026-11-07): under LC QD32 +
# BE the inflight count sits routinely above 24, so --busy-ref 24 arms the
# interception whenever occupancy is >= a quarter of the deep-queue inflight
# band ("band >= mid" activates); the calibration round's loader default 64
# never activated (device busy never crossed 50% in any rep1 sample).
LINNOS_BUSY_REF = 24
# q_linnos interception table (iteration#2 2026-11-07, explicit on BOTH
# tenant instances; the loader default table ir-band0/1/2 = LC:BE 0:0 / 0:20
# / 5:60 is NOT usable as-is): iteration#1 validation rep1 measured the
# default band2 5:60 intercepting the LC stream too (LC is prio 2 < 3, so it
# is an eligible victim) and the per-IO hold drove LC p99 from the 1.86 ms
# none baseline to 8.2 ms -- interception became a LC tail-latency tax.
# Fix: LC is pinned at 0% intercept in EVERY busy band ("latency-sensitive
# traffic is never intercepted"), and only the BE stream is compressed per
# busy band (0:40 mid, 0:60 high), i.e. LinnOS semantics here = sacrifice
# BE throughput to buy LC predictability/protectability.
LINNOS_IR_LOW = "0:0"
LINNOS_IR_MID = "0:40"
LINNOS_IR_HIGH = "0:60"
# q_linnos per-IO hold (2026-11-07 iteration#3): the loader default
# --max-hold-us 2000 releases interception slots in batches, and after a
# deep-queue hold the batch lands as a release burst that taxes the LC
# stream's tail (held-batch release bursts hurt LC); both tenant instances
# now carry an EXPLICIT --max-hold-us 500 (4x smaller batch window) so the
# held batch drains before it can pile up as a burst against LC.
LINNOS_MAX_HOLD_US = 500

# Loader -p/-W role args for the coloc arms (finalized CLIs, frozen loaders): prio band 0-2 = LC, 3-7 = BE (lower = higher
# priority; IOSCHED_DEFAULT_ARB_PRIO is 7, so the LC side MUST carry an
# explicit -p).  Weights mirror the n_io_weight arm's 100:200 io.weight
# structure so native and homo arms are comparable.
LC_W_ARGS = ["-p", str(ARB_PRIO_LC), "-W", str(WEIGHT_LC)]
BE_W_ARGS = ["-p", str(ARB_PRIO_BE), "-W", str(WEIGHT_BE)]
# q_linnos tenant args: role args + the calibrated busy reference + the
# explicit LC-never/BE-only interception table (see LINNOS_BUSY_REF and the
# LINNOS_IR_* block above; identical on both tenant instances so the LC
# loader itself never re-arms LC interception).
LINNOS_IR_ARGS = ["--ir-low", LINNOS_IR_LOW, "--ir-mid", LINNOS_IR_MID,
                  "--ir-high", LINNOS_IR_HIGH]
LINNOS_HOLD_ARGS = ["--max-hold-us", str(LINNOS_MAX_HOLD_US)]
LINNOS_LC_ARGS = LC_W_ARGS + ["--busy-ref", str(LINNOS_BUSY_REF)] \
    + LINNOS_IR_ARGS + LINNOS_HOLD_ARGS
LINNOS_BE_ARGS = BE_W_ARGS + ["--busy-ref", str(LINNOS_BUSY_REF)] \
    + LINNOS_IR_ARGS + LINNOS_HOLD_ARGS

# ---------------------------------------------------------------------------
# ARMS dict: EVERY loader binary + argument set in one place.
# CLI finalized against the landed loaders (build/: loader_q_iomax,
# loader_arb_iocost / loader_arb_iolatency / loader_arb_d2fq /
# loader_q_linnos family); the remaining calibration TODOs
# are the two listed in CALIB_NOTES below (iocost coefficients re-measure,
# io.latency sidecar verification) — no provisional CLIs remain.
# ---------------------------------------------------------------------------

CALIB_NOTES = (
    "    - IOCOST_MODEL/QOS: re-measure on the campaign 96G disk with "
    "iocost_coef_gen before publishing graded comparisons (currently the rig "
    "sdb calibration literals, 320 MiB/s RECAL anchor; the iteration#3 "
    "qos rlat=2000 us re-anchor is the binding change, keep it when "
    "re-measuring);\n"
    "    - n_io_latency arm: blk-iolatency availability on 7.3.1-ioext3 must "
    "be verified AFTER the first run via the sidecar 'cgroup io.* arm state' "
    "block (common.py skips a missing io.latency file silently); also verify "
    "the target=2000 us write landed AND that the self-throttle actually "
    "shrinks the LC queue instead of pinning LC throughput (2 ms target sits "
    "between the ~4.9 ms none-baseline LC p99 and the hardware floor; the "
    "iteration#2 1 ms target was below the reachable floor and self-"
    "throttled LC itself: iops 84k -> 72k with p99 still ~5.7 ms -- if the "
    "2 ms arm repeats that pattern, the arm goes to the ledger for review)."
)
# Default-table comments for arms that intentionally ride loader defaults:
#   arb_iocost: --cost-table R4K,R16K,R64K,R128K,RMAX,W4K,W16K,W64K,W128K,WMAX
#     defaults 64,256,1024,2048,4096,256,1024,4096,8192,16384;
#     --cost-quantum 1; --cost-min-budget 64 (weights travel on the cgroup
#     q loaders via -W, LC:BE 100:200, same structure as the n_io_weight arm).
#   arb_iolatency: --lat-target-ns 2000000 (2 ms, == IO_LATENCY_TARGET_US;
#     iteration#3 recalibration -- the 1 ms target was below the reachable
#     latency floor and the self-throttle turned it into LC self-harm) is
#     the ONLY explicit arg;
#     --protect-inflight 32 / --boost-max 4 ride defaults; LC/BE roles via
#     q-side -p (0-2 LC, 3-7 BE) and -W.
#   q_iomax: per-channel burst cap = rate * 0.5s (io.max burst convention);
#     an all-unlimited config is rejected by the loader.
#   q_linnos: interception table EXPLICIT on both tenant instances
#     (iteration#2: --ir-low/--ir-mid/--ir-high = LC:BE 0:0 / 0:40 / 0:60,
#     the LC side of every band is 0 -- LC (prio 2 < 3) is never intercepted),
#     --busy-ref 24 (LINNOS_BUSY_REF above, 2026-11-07 calibration;
#     loader built-in default 64), --max-hold-us 500 (EXPLICIT, iteration#3;
#     loader default 2000 releases held batches as LC-taxing bursts),
#     --lc-prio-max 3
#     (LC = prio < 3, hence the explicit -p 2 on the LC side).
#   arb_d2fq: per-type cost table defaults (R0..R4:W0..W4) — no override.

ARMS: Dict[str, Dict[str, Any]] = {
    # --- elevator family ---------------------------------------------------
    "elev_n_none": {"family": "elevator", "kind": "native", "scheduler": "none"},
    "elev_n_mq_deadline": {"family": "elevator", "kind": "native", "scheduler": "mq-deadline"},
    "elev_n_kyber": {"family": "elevator", "kind": "native", "scheduler": "kyber"},
    "elev_x_mq_deadline_q": {
        "family": "elevator", "kind": "iox", "topology": "root",
        "q": "q_mq_deadline",
        "args": ["-c", "{policy_cgroup_path}"]
        + (["--merge-mode", str(MQDD_MERGE_MODE)] if MQDD_MERGE_MODE is not None else []),
    },
    "elev_x_kyber_q": {
        "family": "elevator", "kind": "iox", "topology": "root",
        "q": "q_kyber",
        "args": ["-c", "{policy_cgroup_path}",
                 "--read-target-ns", str(KYBER_READ_TARGET_NS),
                 "--write-target-ns", str(KYBER_WRITE_TARGET_NS)],
    },
    # --- controller family ---------------------------------------------------
    "ctrl_n_none": {"family": "controller", "kind": "native", "scheduler": "none"},
    "ctrl_n_io_max": {"family": "controller", "kind": "native", "scheduler": "none",
                      "lc_knob": {}, "be_knob": {"io_max": {"rbps": IO_MAX_BYTES, "wbps": IO_MAX_BYTES, "riops": "max", "wiops": "max"}}},
    "ctrl_x_iomax_q": {
        "family": "controller", "kind": "iox", "topology": "coloc",
        "arbiters": [],
        "lc_policy": ("q_none", LC_W_ARGS),
        # D3 caliber: BE read AND write capped at the same 320 MiB/s tier
        # (BE is a seqwrite workload, so --wbps is the binding channel).
        "be_policy": ("q_iomax", ["--rbps", str(IO_MAX_BYTES), "--wbps", str(IO_MAX_BYTES)]),
    },
    # Native io.weight's ONLY effective native carrier is BFQ (none / mq /
    # kyber ignore io.weight entirely: 2026-11-07 validation rep1 showed the
    # knob is a no-op under scheduler=none, share_ratio_dev 3.05 noise).
    # Therefore this arm pins scheduler=bfq and keeps the original 100:200
    # io.weight knob (the bfq weight carrier); the RUNBOOK states the carrier
    # pairing explicitly.
    "ctrl_n_io_weight": {"family": "controller", "kind": "native", "scheduler": "bfq",
                         "lc_knob": {"io_weight": WEIGHT_LC},
                         "be_knob": {"io_weight": WEIGHT_BE}},
    "ctrl_x_weight_arb": {
        "family": "controller", "kind": "iox", "topology": "coloc",
        "arbiters": [("arb_wdrr", [])],
        "lc_policy": ("q_fifo", LC_W_ARGS),
        "be_policy": ("q_fifo", BE_W_ARGS),
    },
    "ctrl_n_io_cost": {"family": "controller", "kind": "native", "scheduler": "none",
                       "lc_knob": {"io_weight": WEIGHT_LC},
                       "be_knob": {"io_weight": WEIGHT_BE},
                       # Root io.cost model+qos is written exactly once (by
                       # the LC tenant scene, the single writer) at setup().
                       "lc_root_iocost": True},
    "ctrl_x_cost_arb": {
        "family": "controller", "kind": "iox", "topology": "coloc",
        # arb_iocost rides its default cost table/quantum/min-budget (see the
        # default-table comment block above); weights via q-side -W 100:200.
        "arbiters": [("arb_iocost", [])],
        "lc_policy": ("q_fifo", LC_W_ARGS),
        "be_policy": ("q_fifo", BE_W_ARGS),
    },
    "ctrl_n_io_latency": {"family": "controller", "kind": "native", "scheduler": "none",
                          "lc_knob": {"io_latency": {"target": IO_LATENCY_TARGET_US}},
                          "be_knob": {}},
    "ctrl_x_latency_arb": {
        "family": "controller", "kind": "iox", "topology": "coloc",
        # LC latency probe rides q_none with an explicit LC-band prio (the
        # default arb prio is 7 = BE band); BE q_fifo neutral-band.
        "arbiters": [("arb_iolatency", ["--lat-target-ns", str(ARB_IOLATENCY_LAT_TARGET_NS)])],
        "lc_policy": ("q_none", LC_W_ARGS),
        "be_policy": ("q_fifo", BE_W_ARGS),
    },
    # --- frontier family -----------------------------------------------------
    # Native reference for the two frontier IOX arms. It uses the identical
    # LC+BE workload but keeps the block scheduler at none.
    "front_n_none": {"family": "frontier", "kind": "native", "scheduler": "none"},
    # front_n_d2fq: native D2FQ (FAST'21) via the ioextd2fq kernel port
    # (block/d2fq.c + NVMe WRRU arbitration).  Requires the 7.3.1-ioextd2fq
    # kernel on the host; the queue/d2fq_en sysfs write fails closed
    # (RuntimeError) on kernels without the port.  cls_base is left to the
    # reference d2fq_en=1 convention (=6, == x_d2fq_arb frozen
    # --fast-share 6); declared deviation d2fq_force_all=1 (rig
    # drivability, include/linux/d2fq.h deviation 4).
    "front_n_d2fq": {"family": "frontier", "kind": "native", "scheduler": "none",
                     "d2fq": {
                         "enable": 1,
                         "sysctls": {
                             "d2fq_thrsh": 8192,
                             "d2fq_md_ratio": 70,
                             "d2fq_di_period": 50,
                             "d2fq_dw_en": 0,
                             "d2fq_force_all": 1,
                         },
                     }},
    "front_x_d2fq_arb": {
        "family": "frontier", "kind": "iox", "topology": "coloc",
        # arb_d2fq per-type cost table defaults (see comment block above).
        "arbiters": [("arb_d2fq", [])],
        "lc_policy": ("q_none", LC_W_ARGS),
        "be_policy": ("q_fifo", BE_W_ARGS),
    },
    "front_x_linnos_q": {
        "family": "frontier", "kind": "iox", "topology": "coloc",
        # q_linnos with --busy-ref 24 + the explicit LC-never/BE-only
        # interception table + the explicit --max-hold-us 500 (iteration#3;
        # both tenant instances; see LINNOS_BUSY_REF, LINNOS_IR_* and
        # LINNOS_MAX_HOLD_US); LC/BE classified by q-side -p (--lc-prio-max 3
        # => prio < 3 is LC).
        "arbiters": [],
        "lc_policy": ("q_linnos", LINNOS_LC_ARGS),
        "be_policy": ("q_linnos", LINNOS_BE_ARGS),
    },
}

MONITORING_COMMON = ["paper_system_metric", "paper_io_stack_cpu_cost", "paper_block_mq_latency"]
MONITORING_SCX = MONITORING_COMMON + [
    "scx_io_bypass_fallback", "scx_io_bpf_hook_cost", "scx_io_lock_contention",
]


def _monitoring_scenes() -> Dict[str, Any]:
    """Monitoring scene block (homo_similarity paper_* conventions)."""
    return {
        "paper_block_mq_latency": {
            "base_scene": "scene/block_bpf/block_mq_latency.yml",
            "parameters": {
                "output_dir": "/dev/shm/ioext_bench_paper_monitoring/block_mq_latency",
                "output_file": "/dev/shm/ioext_bench_paper_monitoring/block_mq_latency/block_mq_latency.bpftrace.log",
                "summary_file": "/dev/shm/ioext_bench_paper_monitoring/block_mq_latency/block_mq_latency_summary.json",
                "require_events": True,
                "privilege": {"method": "sudo_nopasswd"},
            },
        },
        "paper_io_stack_cpu_cost": {
            "base_scene": "scene/block_bpf/io_stack_cpu_cost.yml",
            "parameters": {
                "output_dir": "/dev/shm/ioext_bench_paper_monitoring/io_stack_cpu_cost",
                "output_file": "/dev/shm/ioext_bench_paper_monitoring/io_stack_cpu_cost/io_stack_cpu_cost.bpftrace.log",
                "summary_file": "/dev/shm/ioext_bench_paper_monitoring/io_stack_cpu_cost/io_stack_cpu_cost_summary.json",
                "require_events": True,
                "privilege": {"method": "sudo_nopasswd"},
            },
        },
        "paper_system_metric": {
            "base_scene": "scene/system/system_metric.yml",
            "parameters": {
                "collection_mode": "proc",
                "output_dir": "/dev/shm/ioext_bench_paper_monitoring/system_metric",
                "output_file": "/dev/shm/ioext_bench_paper_monitoring/system_metric/system_metric_summary.json",
                "sample_interval": 1.0,
                "require_samples": True,
                "disk_devices": [TARGET_DEVICE],
            },
        },
        "scx_io_bpf_hook_cost": {
            "base_scene": "scene/block_bpf/scx_io_bpf_hook_cost.yml",
            "parameters": {
                "output_dir": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_bpf_hook_cost",
                "output_file": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_bpf_hook_cost/scx_io_bpf_hook_cost.bpftrace.log",
                "summary_file": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_bpf_hook_cost/scx_io_bpf_hook_cost_summary.json",
                "startup_grace_sec": 1.0,
                "require_events": False,
                "privilege": {"method": "sudo_nopasswd", "askpass_script": "", "env_var": "SUDO_PASSWORD"},
            },
        },
        "scx_io_lock_contention": {
            "base_scene": "scene/block_bpf/scx_io_lock_contention.yml",
            "parameters": {
                "output_dir": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_lock_contention",
                "output_file": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_lock_contention/scx_io_lock_contention.bpftrace.log",
                "summary_file": "/dev/shm/ioext_bench_frame_cost/micro/scx_io_lock_contention/scx_io_lock_contention_summary.json",
                "startup_grace_sec": 1.0,
                "require_events": False,
                "privilege": {"method": "sudo_nopasswd", "askpass_script": "", "env_var": "SUDO_PASSWORD"},
            },
        },
        "scx_io_bypass_fallback": {
            "base_scene": "scene/block_bpf/scx_io_bypass_fallback.yml",
            "parameters": {
                "output_dir": "/dev/shm/ioext_bench_frame_cost/macro/scx_io_bypass_fallback",
                "output_file": "/dev/shm/ioext_bench_frame_cost/macro/scx_io_bypass_fallback/scx_io_bypass_fallback.bpftrace.log",
                "summary_file": "/dev/shm/ioext_bench_frame_cost/macro/scx_io_bypass_fallback/scx_io_bypass_fallback_summary.json",
                "startup_grace_sec": 1.0,
                "require_events": False,
                "privilege": {"method": "sudo_nopasswd", "env_var": "SUDO_PASSWORD"},
            },
        },
    }


# ---------------------------------------------------------------------------
# fio shapes
# ---------------------------------------------------------------------------

def _elev_fio() -> Dict[str, Any]:
    job = {
        "ioengine": "libaio",
        "direct": 1,
        "bs": ELEV_BS,
        "rw": "randrw",
        "rwmixread": ELEV_RWMIXREAD,
        "iodepth": ELEV_IODEPTH,
        "numjobs": 1,
        "runtime_sec": ELEV_RUNTIME_SEC,
        "time_based": True,
        "group_reporting": True,
        "percentile_list": ELEV_PERCENTILES,
        "write_bw_log": "homo_v2_elev_fio",
        "write_iops_log": "homo_v2_elev_fio",
        "write_lat_log": "homo_v2_elev_fio",
        "log_avg_msec": 500,
    }
    return job


def _lc_fio() -> Dict[str, Any]:
    return {
        "ioengine": "libaio",
        "direct": 1,
        "bs": LC_BS,
        "rw": LC_RW,
        "iodepth": LC_IODEPTH,
        "numjobs": 1,
        "runtime_sec": CTRL_RUNTIME_SEC,
        "time_based": True,
        "group_reporting": True,
        "ramp_time": LC_RAMP_SEC,
        "percentile_list": BE_PERCENTILES,
        "write_bw_log": "homo_v2_ctrl_lc_fio",
        "write_iops_log": "homo_v2_ctrl_lc_fio",
        "write_lat_log": "homo_v2_ctrl_lc_fio",
        "log_avg_msec": 500,
    }


def _be_fio() -> Dict[str, Any]:
    job = {
        "ioengine": "libaio",
        "direct": 1,
        "bs": BE_BS,
        "rw": BE_RW,
        "iodepth": BE_IODEPTH,
        "numjobs": 1,
        "runtime_sec": CTRL_RUNTIME_SEC,
        "time_based": True,
        "group_reporting": True,
        "percentile_list": BE_PERCENTILES,
        "startdelay": 5,
        "write_bw_log": "homo_v2_ctrl_be_fio",
        "write_iops_log": "homo_v2_ctrl_be_fio",
        "write_lat_log": "homo_v2_ctrl_be_fio",
        "log_avg_msec": 500,
    }
    if BE_RATE_MIB is not None:
        # fio --rate (offered-rate cap; the io.max/q_iomax knobs above are the
        # controller knobs under test — see the BE_RATE_MIB docstring).
        job["rate"] = f"{BE_RATE_MIB}M"
    return job


# ---------------------------------------------------------------------------
# cgroup builders
# ---------------------------------------------------------------------------

def _neutral_cgroup() -> Dict[str, Any]:
    """Hygiene baseline copied from gen_isol_bench_paper_yamls._base_cgroup:
    neutral io_weight and explicit io.max cleanup so leftovers from a previous
    sequence cannot contaminate the next arm; sticky root io.cost.qos is
    scrubbed on every non-io_cost arm."""
    return {
        "controllers": ["io"],
        "io_weight": 100,
        "io_max": {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
        "io_cost": {"qos": {"enable": False}},
    }


def _tenant_cgroup(family: str, arm: str, role: str, arm_spec: Dict[str, Any],
                   cgroup_path: str) -> Dict[str, Any]:
    """io_ext.cgroup block for one tenant of a NATIVE coloc arm."""
    cg = _neutral_cgroup()
    cg["path"] = cgroup_path
    role_knob = arm_spec.get(f"{role}_knob") or {}
    for key, value in role_knob.items():
        if key == "io_weight":
            cg["io_weight"] = value
        else:
            cg[key] = dict(value) if isinstance(value, dict) else value
    if arm_spec.get("lc_root_iocost") and role == "lc":
        # io.cost model+qos must be written at the cgroup ROOT
        # (fio-scene io_cost key + setup_iocost_at_root); the LC tenant is
        # the SINGLE writer so the two coloc scenes never race on io.cost.*.
        cg["io_cost"] = {"model": dict(IOCOST_MODEL), "qos": dict(IOCOST_QOS)}
    return cg


def _attach_only_cgroup(cgroup_path: str) -> Dict[str, Any]:
    """IOX tenant cgroup: attach-ONLY (gen_isol _strategy_cgroup semantics):
    io_weight/io_max explicitly None so ConfigParser._deep_merge cannot
    re-inject base-scene defaults; root io.cost scrubbed.  Weights travel ONLY
    through loader -W."""
    return {
        "path": cgroup_path,
        "controllers": ["io"],
        "io_weight": None,
        "io_max": {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
        "io_cost": {"qos": {"enable": False}},
    }


def _tenant_template(output_subdir: str, duration: int, fio_global: Dict[str, Any],
                     fio_file: str, cgroup: Dict[str, Any], scheduler: Optional[str],
                     root_iocost: bool = False,
                     d2fq: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    io_ext: Dict[str, Any] = {
        "cleanup_stale_when_no_specs": True,
        "cleanup_bpffs_pins": True,
        "cgroup": cgroup,
    }
    if d2fq:
        # D2FQ native-port activation (queue/d2fq_en + kernel.d2fq_* sysctls,
        # applied by the fio scene after the scheduler switch; idempotent
        # across the two coloc tenants, both carry it).
        io_ext["d2fq"] = d2fq
    if root_iocost:
        # io.cost model+qos must be written at the cgroup ROOT
        # (setup_iocost_at_root); root_cgroup tells the fio scene where.
        io_ext["root_cgroup"] = {"path": "/sys/fs/cgroup", "controllers": ["io"]}
    parameters: Dict[str, Any] = {
        "target_device": TARGET_DEVICE,
        "file_name": fio_file,
        "output_dir": f"/dev/shm/ioext_bench_homo_v2/{output_subdir}",
        "duration": duration,
        "drop_caches_before_run": False,
        "cooldown_sec": 1,
        "fio_global": fio_global,
        "io_ext": io_ext,
    }
    if scheduler is not None:
        parameters["scheduler"] = scheduler
    return {"base_scene": "scene/io_ext/fio.yml", "parameters": parameters}


# ---------------------------------------------------------------------------
# loader helpers (gen_isol_bench_paper_yamls conventions, modern "-c" CLI)
# ---------------------------------------------------------------------------

def _root_policy(name: str, q: str, extra_args: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "path": f"{LOADER_DIR}/loader_{q}",
        "scope": "root_policy",
        "args": extra_args,
    }


def _arbiter(name: str, extra_args: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "path": f"{LOADER_DIR}/loader_{name}",
        "scope": "arbiter",
        "args": list(extra_args),
    }


def _cg_policy(name_template: str, q: str, extra_args: List[str]) -> Dict[str, Any]:
    args = ["-c", "{policy_cgroup_path}"] + list(extra_args)
    return {
        "name_template": name_template,
        "path": f"{LOADER_DIR}/loader_{q}",
        "scope": "cgroup_policy",
        "args": args,
    }


def _scx_base() -> Dict[str, Any]:
    """Global loader-scene-local hygiene (safe: runs before any tenant)."""
    return {
        "cleanup_stale": True,
        "cleanup_bpffs_pins": True,
        "stale_loader_patterns": list(LOADER_STALE),
        "root_cgroup": {"path": "/sys/fs/cgroup", "controllers": ["io"]},
    }


# ---------------------------------------------------------------------------
# arm -> profile / groups / sequence builders
# ---------------------------------------------------------------------------

def _arm_short(arm: str) -> str:
    """ARMS keys carry the family prefix ('elev_x_kyber_q'); the id that must
    be embedded in sequence ids / group ids / cgroup names drops it."""
    return arm.split("_", 1)[1]


FAMILY_SEQ_TOKEN = {"elevator": "elev", "controller": "ctrl", "frontier": "front"}


def _seq_id(family: str, arm: str) -> str:
    # arm suffix parsing convention (see RUNBOOK): arm = sequence_id minus the
    # 'HOMO_<family-token>_' prefix, family token in {elev, ctrl, front}.
    return f"HOMO_{FAMILY_SEQ_TOKEN[family]}_{_arm_short(arm)}"


def _profile_id(arm: str) -> str:
    return f"HV_{arm}"


def build_elevator(arms: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """seq_elevator: single-tenant fio, one sequence per arm (paper_homo style:
    native arms switch the elevator via the sequence-io_ext_policy global
    scene; scx arms pin a root q_* policy on the same global loader scene).
    """
    profiles: Dict[str, Any] = {}
    tenant_groups: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []
    for arm, spec in arms.items():
        if spec["family"] != "elevator":
            continue
        gid = f"elev_{arm}"
        short = _arm_short(arm)
        native_scheduler = spec.get("scheduler")
        tenant_groups[gid] = {
            "count": 1,
            "scene_id_template": f"hv_elev_{short}_job",
            "template": _tenant_template(
                f"elev/{short}/mix70r30w", ELEV_STEP_DURATION, _elev_fio(),
                FIO_FILES["elevator"],
                {"path": f"/sys/fs/cgroup/homo_v2_elev_{short}",
                 "controllers": ["io"], "io_weight": 100},
                # v1 convention: only the none baseline pins scheduler in the
                # tenant scene; the other arms rely on the profile scene's
                # global scheduler switch.
                "none" if arm == "elev_n_none" else None,
            ),
        }
        profiles[_profile_id(arm)] = {
            "name": f"homo-v2 elevator {arm}",
            "target_device": TARGET_DEVICE,
            "scheduler": native_scheduler or IOEXT_SCX_SCHEDULER,
            "io_ext": {
                **_scx_base(),
                "root_policies": [_root_policy(f"{arm}_root", spec["q"], spec["args"])],
            } if spec["kind"] == "iox" else {},
        }
        step = {
            "name": f"HOMO elevator {short}",
            "tenant_groups": [gid],
            # Keep metric monitoring symmetric across native and IOX arms.
            "monitoring_scenes": MONITORING_COMMON,
            "monitoring_startup_time": 3,
        }
        if STEP_TIMEOUTS_ENABLED:
            step["duration"] = ELEV_STEP_DURATION
        sequences.append({
            "sequence_id": _seq_id("elevator", arm),
            "name": (f"homo-v2 elevator {arm} (4k randrw 70r/30w "
                     f"QD{ELEV_IODEPTH} direct=1, {ELEV_RUNTIME_SEC}s)"),
            "io_ext_policy": _profile_id(arm),
            "steps": [step],
        })
    return {"profiles": profiles, "tenant_groups": tenant_groups, "sequences": sequences}


def build_coloc(arms: Dict[str, Dict[str, Any]], family: str) -> Dict[str, Any]:
    """seq_controller / seq_frontier: LC+BE coloc fio, one sequence per arm.

    Native C-knob arms carry their constants on the tenant scenes' cgroup
    blocks (fio-scene io_weight/io_max/io_latency + LC-only root io.cost),
    mirroring gen_isol D3/D4 cgroup semantics.  IOX arms carry an arbiter
    (optional) and per-tenant q_* loaders via cgroup_policies_from_groups
    (loader -c/-p/-W, weights through -W only).
    """
    profiles: Dict[str, Any] = {}
    tenant_groups: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []
    for arm, spec in arms.items():
        if spec["family"] != family:
            continue
        lc_gid, be_gid = f"{family}_{arm}_lc", f"{family}_{arm}_be"
        short = _arm_short(arm)
        lc_cg_path = f"/sys/fs/cgroup/homo_v2_{family}_{short}_lc"
        be_cg_path = f"/sys/fs/cgroup/homo_v2_{family}_{short}_be"
        lc_cgroup = (_tenant_cgroup(family, arm, "lc", spec, lc_cg_path)
                     if spec["kind"] == "native" else _attach_only_cgroup(lc_cg_path))
        be_cgroup = (_tenant_cgroup(family, arm, "be", spec, be_cg_path)
                     if spec["kind"] == "native" else _attach_only_cgroup(be_cg_path))
        tenant_groups[lc_gid] = {
            "count": 1,
            "scene_id_template": f"hv_{family}_{short}_lc",
            "template": _tenant_template(
                f"{family}/{short}/lc", CTRL_STEP_DURATION, _lc_fio(),
                FIO_FILES["lc"], lc_cgroup,
                # pin the arm's own native scheduler (n_io_weight carries its
                # io.weight knob through bfq; the other native arms are "none")
                spec["scheduler"] if spec["kind"] == "native" else None,
                root_iocost=bool(spec.get("lc_root_iocost")),
                d2fq=spec.get("d2fq") if spec["kind"] == "native" else None,
            ),
        }
        tenant_groups[be_gid] = {
            "count": 1,
            "scene_id_template": f"hv_{family}_{short}_be",
            "template": _tenant_template(
                f"{family}/{short}/be", CTRL_STEP_DURATION, _be_fio(),
                FIO_FILES["be"], be_cgroup,
                spec["scheduler"] if spec["kind"] == "native" else None,
                d2fq=spec.get("d2fq") if spec["kind"] == "native" else None,
            ),
        }
        if spec["kind"] == "native":
            profiles[_profile_id(arm)] = {
                "name": f"homo-v2 {family} {arm}",
                "target_device": TARGET_DEVICE,
                "scheduler": spec["scheduler"],
            }
        else:
            lc_q, lc_args = spec["lc_policy"]
            be_q, be_args = spec["be_policy"]
            profile_io_ext: Dict[str, Any] = {
                **_scx_base(),
                "arbiters": [_arbiter(name, extra) for name, extra in spec["arbiters"]],
            }
            profiles[_profile_id(arm)] = {
                "name": f"homo-v2 {family} {arm}",
                "target_device": TARGET_DEVICE,
                "scheduler": IOEXT_SCX_SCHEDULER,
                "io_ext": profile_io_ext,
                "cgroup_policies_from_groups": [
                    {"tenant_group": lc_gid, **_cg_policy(f"{lc_q}_lc", lc_q, lc_args)},
                    {"tenant_group": be_gid, **_cg_policy(f"{be_q}_be", be_q, be_args)},
                ],
            }
        step = {
            "name": f"HOMO {family} {short}",
            "tenant_groups": [lc_gid, be_gid],
            # Keep metric monitoring symmetric across native and IOX arms.
            "monitoring_scenes": MONITORING_COMMON,
            "monitoring_startup_time": 3,
        }
        if STEP_TIMEOUTS_ENABLED:
            step["duration"] = CTRL_STEP_DURATION
        sequences.append({
            "sequence_id": _seq_id(family, arm),
            "name": (f"homo-v2 {family} {short} (LC 4k randread QD{LC_IODEPTH} + "
                     f"BE 64k seqwrite QD{BE_IODEPTH}, {CTRL_RUNTIME_SEC}s)"),
            "io_ext_policy": _profile_id(arm),
            "steps": [step],
        })
    return {"profiles": profiles, "tenant_groups": tenant_groups, "sequences": sequences}


def _build_config() -> Dict[str, Any]:
    built = build_elevator(ARMS)
    profiles = dict(built["profiles"])
    tenant_groups = dict(built["tenant_groups"])
    sequences = list(built["sequences"])
    for family in ("controller", "frontier"):
        built = build_coloc(ARMS, family)
        profiles.update(built["profiles"])
        tenant_groups.update(built["tenant_groups"])
        sequences.extend(built["sequences"])

    # sanity: unique sequence ids / profile ids / arm-derived group ids
    seq_ids = [s["sequence_id"] for s in sequences]
    if len(set(seq_ids)) != len(seq_ids):
        raise SystemExit(f"duplicate sequence ids: {seq_ids}")
    prof_ids = list(profiles)
    if len(set(prof_ids)) != len(prof_ids):
        raise SystemExit(f"duplicate profile ids: {prof_ids}")

    cli_lines = []
    for a, s in ARMS.items():
        if s["kind"] != "iox":
            continue
        if s.get("topology") == "root":
            cli_lines.append(f"    - {a}: loader_{s['q']} {' '.join(s['args'])}")
        else:
            arb_txt = (f"loader_{n}{' ' + ' '.join(x) if x else '(defaults)'}; "
                       for n, x in s["arbiters"])
            cli_lines.append(
                f"    - {a}: {'; '.join(arb_txt)} LC loader_{s['lc_policy'][0]} "
                f"{' '.join(s['lc_policy'][1])}; BE loader_{s['be_policy'][0]} "
                f"{' '.join(s['be_policy'][1])}")
    description = (
        "Homo-v2 homomorphic-algorithm reproduction (homo v2) on the kvmhost "
        f"exclusive 96G experiment disk (device: {TARGET_DEVICE}, generated by "
        "scripts/gen_homo_v2_yamls.py; ALL homo-v2 data must be re-run on the "
        "7.3.1-ioext3 kernel). Three sequence families: seq_elevator "
        f"(single-tenant 4k randrw 70/30 QD{ELEV_IODEPTH} direct=1 "
        "elevator-family homo arms; head metric elev_read_p99_us, IOPS "
        "secondary), seq_controller (LC+BE coloc 4k-randread-"
        f"QD{LC_IODEPTH} / 64k-seqwrite "
        "controller-family arms: native io.max/io.weight/io.cost/io.latency "
        "vs scx_io q_iomax/arb_wdrr/arb_iocost/arb_iolatency; NATIVE "
        "io.weight's effective carrier is BFQ, so the n_io_weight arm pins "
        "scheduler=bfq, io.max cap "
        f"{IO_MAX_BYTES // _MIB} MiB/s per the rq5 BE write anchor "
        "(rerun#2 recalibration; the io.cost RECAL linear model keeps the "
        "320 MiB/s saturation anchor), BE capped on "
        "both channels), and seq_frontier (arb_d2fq / q_linnos on the same "
        "coloc workload). The frontier native reference column does NOT "
        "re-run: it cross-references HOMO_ctrl_n_none (identical "
        "workload/device/kernel; no HOMO_front_n_none sequence exists). Arm "
        "ids parse from sequence ids as arm = <id> minus the "
        "'HOMO_<family-token>_' prefix. Tunable constants: ELEV runtime "
        f"{ELEV_RUNTIME_SEC}s QD{ELEV_IODEPTH}, CTRL runtime {CTRL_RUNTIME_SEC}s "
        f"LC QD{LC_IODEPTH} BE QD{BE_IODEPTH} "
        f"(BE {'rate ' + str(BE_RATE_MIB) + ' MiB/s' if BE_RATE_MIB else 'uncapped'}), "
        f"io.weight {WEIGHT_LC}:{WEIGHT_BE} (LC:BE; native carrier bfq), "
        f"q_linnos --busy-ref {LINNOS_BUSY_REF} --max-hold-us "
        f"{LINNOS_MAX_HOLD_US}, io.latency target "
        f"{IO_LATENCY_TARGET_US}us, arb prio band LC 0-2 / BE 3-7 "
        f"(LC {ARB_PRIO_LC} / BE {ARB_PRIO_BE}).\n"
        "FINAL loader CLIs (verified against the landed build/ loaders):\n" + "\n".join(cli_lines) + "\n"
        "REMAINING calibration TODOs:\n"
        + CALIB_NOTES
    )
    return {
        "benchmark_name": f"io-ext homo-v2 homomorphic reproduction ({PROFILE})",
        "description": description,
        "fail_fast_on_scene_error": True,
        "io_ext_policy_profiles": profiles,
        "scenes": _monitoring_scenes(),
        "tenant_groups": tenant_groups,
        "test_sequences": sequences,
        "default_sequence": sequences[0]["sequence_id"],
    }


def _validate(doc: Dict[str, Any]) -> None:
    yaml.safe_dump(doc)  # serializable
    # every io_ext_policy reference resolves
    profiles = doc["io_ext_policy_profiles"]
    for seq in doc["test_sequences"]:
        pol = seq.get("io_ext_policy")
        if pol and pol not in profiles:
            raise SystemExit(f"sequence {seq['sequence_id']}: unresolved io_ext_policy {pol}")
        for gid in seq["steps"][0].get("tenant_groups", []):
            if gid not in doc["tenant_groups"]:
                raise SystemExit(f"sequence {seq['sequence_id']}: unknown tenant group {gid}")
    if PROFILE == "bm":
        approved_root = Path("/opt/ioext/homo_v2/data")
        for name, file_name in FIO_FILES.items():
            path = Path(file_name)
            if not path.is_absolute() or str(path).startswith("/dev/"):
                raise SystemExit(
                    f"bm profile {name} fio target must be an absolute regular-file path: {path}"
                )
            try:
                path.relative_to(approved_root)
            except ValueError as exc:
                raise SystemExit(
                    f"bm profile {name} fio target escapes {approved_root}: {path}"
                ) from exc
    # coloc scx profiles reference existing groups only
    for prof in profiles.values():
        for spec in prof.get("cgroup_policies_from_groups") or []:
            gid = spec.get("tenant_group")
            if gid not in doc["tenant_groups"]:
                raise SystemExit(f"profile cgroup_policies_from_groups unknown tenant group {gid}")


def main() -> int:
    global PROFILE, TARGET_DEVICE, FIO_FILES, STEP_TIMEOUTS_ENABLED

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="build + validate in memory only, write nothing")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--profile", choices=("kvmhost", "bm"), default="kvmhost")
    ap.add_argument("--calibration-json", type=Path)
    args = ap.parse_args()

    PROFILE = args.profile
    if PROFILE == "bm":
        TARGET_DEVICE = os.environ.get("HOMO_V2_TARGET_DEVICE", "nvme0n1")
        data_root = Path(os.environ.get(
            "HOMO_V2_FIO_ROOT", "/opt/ioext/homo_v2/data"
        ))
        FIO_FILES = {
            "elevator": str(data_root / "elevator.bin"),
            "lc": str(data_root / "controller_lc.bin"),
            "be": str(data_root / "controller_be.bin"),
        }
        STEP_TIMEOUTS_ENABLED = False
        if args.calibration_json is None:
            raise SystemExit("--profile bm requires --calibration-json")
        _load_bm_calibration(args.calibration_json)
    elif args.calibration_json is not None:
        raise SystemExit("--calibration-json is only valid with --profile bm")

    doc = _build_config()
    _validate(doc)

    arms_in_order = [s["sequence_id"] for s in doc["test_sequences"]]
    print(f"[homo_v2] {len(arms_in_order)} sequences across 3 families:")
    for sid in arms_in_order:
        print(f"  {sid}")
    if args.check:
        print("[homo_v2] check-only: config built + validated in memory")
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / OUT_FILENAME
    if out_path.exists():
        existing = yaml.safe_load(out_path.read_text())
        old_ids = [s.get("sequence_id") for s in (existing.get("test_sequences") or [])]
        if set(old_ids) != set(arms_in_order):
            raise SystemExit(
                f"refusing to overwrite {out_path}: existing arm set differs; "
                "delete the stale config first")
    with open(out_path, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
        fh.write("\n# Generated by scripts/gen_homo_v2_yamls.py — do not hand-edit.\n")
    # post-write parse + duplicate-arm check (acceptance)
    check = yaml.safe_load(out_path.read_text())
    ids = [s["sequence_id"] for s in check["test_sequences"]]
    assert len(ids) == len(set(ids)), "duplicate sequence ids in written YAML"
    print(f"[homo_v2] wrote {out_path} (parsed OK, {len(ids)} unique sequences)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
