#!/usr/bin/env python3
"""Generate the dynamic multi-tenant timeline task YAMLs (main-experiment skeleton).

Emits three task configs under src/vm/configs/task/isol_bench/rig_dynamic_tenant/:

  dynamic_tenant_smoke.yml
      Skeleton verification sequences, phase length L=15s, native
      scheduler=none only:
        DYN_smoke    full contract trajectory T1..T6 at L=15s
        DYN_smoke_ab shortened trajectory (tenants A+B only, T1..T2, L=15s)
                     used by scripts/run_dynamic_tenant_virtme.sh for the
                     fast vng smoke.
  dynamic_tenant_calibration.yml
      Independent calibration sequences (static, scheduler=none only):
        DYN_calib_a/b/c/d  one tenant solo, single phase, 120s
        DYN_calib_t4       static worst coexistence T4={A,C,D}, 2 phases at
                           --t4-phase-length (default 60s).  NOTE: C runs
                           UNLIMITED here (the timeline scene cannot apply
                           fio --rate yet, see the scene-gap report), which
                           makes this run a conservative upper bound on
                           contention for the guardrail checks.
  dynamic_tenant_virtme.yml
      Formal skeleton, native scheduler=none only:
        DYN_virtme   full contract trajectory T1..T6 at L=<frozen>
      L and the frozen rates are taken from
      results/dynamic_tenant_virtme/contract/calibration.json when it exists
      (written by scripts/analyze_dynamic_tenant_calibration.py); before
      calibration the generator falls back to the L=60s contract default.
      io_ext q-switch arms will be added on top of this skeleton later; they
      are intentionally NOT generated here (switch_q gate FAILED, see
      results/iox_policy_exploration/hotswitch_gate/).

Timeline contract (see docs/plans/dynamic_tenant_reconfiguration_experiment.md
and src/vm/scene/io_ext/timeline.py):

    T1={A}  T2={A,B}  T3={A,B,C}  T4={A,C,D}  T5={A,D}  T6={A}

Tenant workloads (frozen precedents; recorded in the scene config comments):
  A: 4K randread QD1 single job   -- rig_iox d4 foreground precedent
  B: 64K randread QD8 single job  -- M2 fig2 precedent (gen_m2_fig2_yamls.py)
  C: 64K randread QD8 single job  -- same shape as B
  D: 4K randwrite QD64 single job -- explore_scene_combo 08 co-location precedent
All tenants direct=1, io_uring, per-tenant cgroup
/sys/fs/cgroup/rdyn_<seq>_t{a,b,c,d}, disjoint device quarters.

Service preference A > C > B = D is recorded in comments for later tuning;
this skeleton implements NO scheduling preference and no io_ext policy.

Usage:
  python3 scripts/gen_dynamic_tenant_yamls.py [--output-dir DIR]
                                             [--t4-phase-length SEC]
"""

from __future__ import annotations

import argparse
import hashlib
import copy
import json
import os as _os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "src/vm/configs/task/isol_bench/rig_dynamic_tenant"
CONTRACT_CALIBRATION_JSON = Path(
    _os.environ.get(
        "DYN_TENANT_CALIB_JSON",
        str(REPO_ROOT / "results/dynamic_tenant_virtme/contract/calibration.json"),
    )
)

# Target device and calibration source are parameterizable for other
# platforms (kvmhost /dev/sdb): DYN_TENANT_TARGET_DEVICE / DYN_TENANT_CALIB_JSON
# override; the local-virtme default and the frozen-contract outputs are
# unchanged when unset.
TARGET_DEVICE = _os.environ.get("DYN_TENANT_TARGET_DEVICE", "nvme0n1")

# Root-disk region-file mode (bare-metal port, 20261106).  kvmhost owned an exclusive
# 96G disk (/dev/sdb) and ran tenant fio directly against the raw device with
# per-tenant quarter regions (DISK_LAYOUT_DEFAULT in the timeline scene).  bm
# has NO exclusive disk: the campaign runs on the ROOT device nvme0n1 and the
# hard safety rule is "never write one byte to the raw block device", so every
# tenant targets a PREALLOCATED REGION FILE instead:
#   parameters.tenants.<t>.filename     = <REGION_DIR>/region_<t>.bin
#   parameters.tenants.<t>.disk_layout = {offset: "0", size: REGION_SIZE}
# "filename" is a RESERVED tenant-spec key consumed NATIVELY by the timeline
# scene (single --filename=<file>; the scene REJECTS /dev* spec targets).
# It must NOT ride the generic spec passthrough: fio treats multiple
# --filename occurrences as a multi-file STRIPE (verified fio-3.36: an absent
# first --filename is created at --size/nfiles and the I/O range is split
# across both; the "job options" JSON echo keeps only the last string),
# so a passthrough copy after the built-in --filename=/dev/<device> would
# stripe tenant D's randwrite over the RAW ROOT DEVICE.  The scene change is
# fail-visible at config time; the post-run audit additionally checks each
# tenant's spawn argv (exactly one --filename, the region file).
# Region geometry mirrors the kvmhost quarter semantics (one private region
# per tenant, equal share): four equal private files of REGION_SIZE each
# (default 30G => ~120G total), filled once by the campaign driver (the
# media-preconditioning analogue of kvmhost's whole-disk fill).
REGION_DIR = (_os.environ.get("DYN_TENANT_REGION_DIR", "") or "").strip() or None
REGION_SIZE = (_os.environ.get("DYN_TENANT_REGION_SIZE", "") or "").strip() or "30G"
if REGION_DIR and not REGION_DIR.startswith("/"):
    raise SystemExit("DYN_TENANT_REGION_DIR must be an absolute path (bare-metal region files)")
FORMAL_PHASE_LENGTH_SEC = 60
SMOKE_PHASE_LENGTH_SEC = 15
DURATION_MARGIN_SEC = 120  # L=90×6 needs >600s wall (boundaries + loader stop)
# Extra wall clock for tenant spawn, drop_caches and teardown on top of the
# six-phase timeline itself.
STEP_OVERHEAD_SEC = 30

# Independent calibration (see docs/plans/... section "calibration protocol"):
# solo runs are one continuous phase long enough for stable p50/p99; the T4
# worst-coexistence run is two phases at the L candidate.
CALIB_SOLO_RUNTIME_SEC = 120
CALIB_T4_NUM_PHASES = 2
CALIB_DEFAULT_T4_L_SEC = 60

NATIVE_ARMS = {"n1_native_none", "n2_native_mqddl", "n3_native_bfq", "n4_native_kyber"}
VALIDATION_SEED_BASE = 20261103      # frozen contract seed_base (7.x)
# Predeclared BEFORE any validation run: independent validation offsets.
VALIDATION_SEED_OFFSETS = (101, 102, 103)
# Held-out stage (predeclared in the frozen manifest BEFORE any held-out
# run): independent offsets disjoint from tuning bases (+1..+7) and
# validation offsets (+101..+103).  Offsets 202-205 are frozen only after
# the validation stage completed (never used for tuning/scheduling input).
HELDOUT_SEED_OFFSETS = (201, 202, 203, 204, 205)
HELDOUT_MAIN_FIGURE_OFFSET = 201
HELDOUT_NINE_ARMS = ("n1_native_none", "n2_native_mqddl", "n3_native_bfq",
                     "n4_native_kyber", "x1_qnone", "x2_qfifo_wdrr",
                     "x3_qddl_wdrr", "x4_spwedf_wdrr", "f1_arbfb")
# 20261106 correction (bare-metal port): the bm main figure runs N1-N4 + C1-C4 +
# f1_arbfb (x1-x4 are NOT run on bm -- no unnecessary iox policies).  The
# held-out arm list is switchable via DYN_TENANT_HELDOUT_ARMS (comma-separated
# arm ids); UNSET keeps HELDOUT_NINE_ARMS above (the kvmhost x1-x4 set) so the
# kvmhost heldout yml stays byte-reproducible.  The bm driver exports the
# comma-joined HELDOUT_NINE_ARMS_bm.
C_ARM_IDS = ("c1_io_max", "c2_bfq_weight", "c3_io_latency", "c4_io_cost")
HELDOUT_NINE_ARMS_bm = ("n1_native_none", "n2_native_mqddl", "n3_native_bfq",
                        "n4_native_kyber", "c1_io_max", "c2_bfq_weight",
                        "c3_io_latency", "c4_io_cost", "f1_arbfb")


def _heldout_arms() -> List[str]:
    """Effective held-out arm list (env override or the kvmhost default)."""
    raw = (_os.environ.get("DYN_TENANT_HELDOUT_ARMS", "") or "").strip()
    if not raw:
        return list(HELDOUT_NINE_ARMS)
    arms = [a.strip() for a in raw.split(",") if a.strip()]
    valid = set(NATIVE_ARMS) | IOEXT_ARM_KEYS | set(C_ARM_IDS)
    unknown = [a for a in arms if a not in valid]
    if unknown:
        raise SystemExit(
            f"DYN_TENANT_HELDOUT_ARMS has unknown arms {unknown}; valid arm ids: {sorted(valid)}")
    if len(set(arms)) != len(arms):
        raise SystemExit("DYN_TENANT_HELDOUT_ARMS contains duplicate arm ids")
    return arms
SCHEDULE_DEFAULT = REPO_ROOT / "results/dyn_tenant_kvmhost/schedule_feasible.json"
# io_ext-only amendment (20261104): validation's dynamic trajectory must stay
# under an iox policy the whole run; the schedule source is the constrained
# re-proposal, NOT the native-mixed v3 file.
SCHEDULE_IOX_ONLY_DEFAULT = REPO_ROOT / (
    "results/dyn_tenant_kvmhost/schedule_feasible_iox_only.json")
IOEXT_SCX_SCHEDULER = "scx_io"
# Arm ids whose loader topology is a static io_ext arm (profile prefix mapping
# arm -> X_/F_ profile id); mirrors build_static_arms_config() and the frozen
# ARM_TOPOLOGY in build_schedule_proposal.py.
IOEXT_ARM_KEYS = {"x1_qnone", "x2_qfifo_wdrr", "x3_qddl_wdrr",
                  "x4_spwedf_wdrr", "f1_arbfb"}


def _arm_static_profile_id(arm: str) -> str:
    """Static-arm profile id of an io_ext arm (X_<arm> / F_f1_arbfb)."""
    if arm == "f1_arbfb":
        return "F_f1_arbfb"
    if arm not in IOEXT_ARM_KEYS:
        raise SystemExit(f"unknown io_ext arm {arm!r}")
    return f"X_{arm}"


def _retarget_cgroup_policies(cgroup_policies: List[Dict[str, Any]], timeline_id: str) -> List[Dict[str, Any]]:
    """Copy a static arm's cgroup_policies onto the validation timeline's
    cgroup names: the timeline scene creates rdyn_<timeline_id>_t{tenant}
    cgroups while the static arm profile pinned rdyn_<arm_timeline>_t{tenant}.
    Tenant = the suffix after the last '_t'."""
    out: List[Dict[str, Any]] = []
    for pol in cgroup_policies:
        pol = copy.deepcopy(pol)
        path = str(pol.get("policy_cgroup_path") or "")
        tenant = path.rstrip("/").rsplit("_t", 1)[-1]
        pol["policy_cgroup_path"] = f"/sys/fs/cgroup/rdyn_{timeline_id}_t{tenant}"
        out.append(pol)
    return out


def _io_ext_map_for(profile: Dict[str, Any], timeline_id: str) -> Dict[str, Any]:
    """The attach_io_ext action's io_ext map: the f1 profile with cleanup
    helpers disabled and cgroup paths retargeted to the validation timeline."""
    io_ext = copy.deepcopy(profile["io_ext"])
    io_ext["cleanup_stale"] = False
    io_ext["cleanup_bpffs_pins"] = False
    io_ext["cleanup_stale_when_no_specs"] = False
    io_ext["loader_start_gap_sec"] = 0.5
    io_ext["cgroup_policies"] = _retarget_cgroup_policies(io_ext["cgroup_policies"], timeline_id)
    return io_ext


def _native_scheduler_of(arm: str) -> str:
    """Native elevator for the phase an arm owns (detach restore / native arms)."""
    return {"n1_native_none": "none", "n2_native_mqddl": "mq-deadline",
            "n3_native_bfq": "bfq", "n4_native_kyber": "kyber"}.get(arm, "none")


def _arm_profile_id(arm: str) -> str:
    return {"f1_arbfb": "F_f1_arbfb"}.get(arm, f"F_{arm}")


def _strip_profile_abs_paths(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Profile output_dir in the static-arms config is an absolute host path
    (defect noted 20261104: /home/... leaked into arm profiles); validation
    profiles must not depend on it."""
    prof = copy.deepcopy(profile)
    out_dir = prof.get("output_dir")
    if isinstance(out_dir, str) and out_dir.startswith("/"):
        prof.pop("output_dir", None)
    return prof


def build_validation_config(schedule_path: Path | None = None) -> Dict[str, Any]:
    """Validation-stage YAML: the feasible schedule EXECUTED for real at each
    predeclared validation seed, against static f1_arbfb (best-static) and
    static native bfq (native reference) at the same seeds.

    20261104 amendment: the dynamic trajectory MUST stay under an iox policy
    the whole run (no native-elevator phase) -- the schedule source is the
    io_ext-only re-proposal (schedule_feasible_iox_only.json).  Under the
    same-arbiter-family constraint the matrix3 per-phase losses give a
    whole-run resident arm as the DP optimum (num_transitions == 0): no
    dynamic phase_actions sequences are generated, the whole-run resident
    (f1 via per-seed retargeted profile) IS the dynamic arm, exactly like a
    matrix3 static arm.
    """
    import os as _env_os

    _default_schedule = (SCHEDULE_IOX_ONLY_DEFAULT
                         if SCHEDULE_IOX_ONLY_DEFAULT.exists() else SCHEDULE_DEFAULT)
    schedule_path = schedule_path or Path(
        _env_os.environ.get("DYN_TENANT_SCHEDULE_JSON", str(_default_schedule))
    )
    doc = json.loads(Path(schedule_path).read_text())
    # Normalize the two schedule doc shapes (frozen v3 feasible vs iox-only).
    if "candidate_A_dp_schedule" in doc:
        dp = doc["candidate_A_dp_schedule"]
        arm_sequence = list(dp["arm_sequence"])
        num_transitions = int(dp["num_transitions"])
        inventory = {int(tr["boundary"]): tr for tr in dp["transition_inventory"]}
        constraint = doc.get("constraint") or {}
        decision = doc.get("decision") or {}
    else:
        sched = doc["schedule"]
        phases_raw = sched["phases"]
        arm_sequence = [phases_raw[str(p)] for p in range(1, len(phases_raw) + 1)]
        num_transitions = int(sched["num_transitions"])
        inventory = {int(tr["boundary"]): tr for tr in sched["transition_inventory"]}
        constraint = {}
        decision = {}
    if constraint.get("iox_only") and not set(arm_sequence) <= IOEXT_ARM_KEYS:
        raise SystemExit("iox-only schedule reached a non-io_ext arm")
    initial_arm = arm_sequence[0]

    static_cfg = yaml.safe_load(Path(_env_os.environ.get(
        "DYN_TENANT_STATIC_YML",
        str(DEFAULT_OUT_DIR / "dynamic_tenant_static_arms.yml"),
    )).read_text())
    profiles = copy.deepcopy(static_cfg.get("io_ext_policy_profiles") or {})
    if "F_f1_arbfb" not in profiles:
        raise SystemExit("F_f1_arbfb profile missing; regenerate the static config first")
    f1_profile = _strip_profile_abs_paths(profiles["F_f1_arbfb"])
    static_tenants = None
    static_l = None
    for seq in static_cfg.get("test_sequences", []):
        if str(seq.get("sequence_id", "")).startswith("DYN_arm_f1_arbfb"):
            st_params = static_cfg["scenes"][seq["steps"][0]["scenes"][0]]["parameters"]
            static_tenants = st_params.get("tenants")
            static_l = float(st_params["phase_length_sec"])
            break
    if static_tenants is None or static_l is None:
        raise SystemExit("static f1 scene parameters not found in the static config")

    def _abs_loader(io_ext: Dict[str, Any]) -> None:
        for pol in io_ext.get("arbiters", []) + io_ext.get("cgroup_policies", []):
            pth = str(pol.get("path") or "")
            if not pth.startswith("/"):
                pol["path"] = f"{IOEXT_LOADER_DIR}/{pth}"

    def _initial_attach_map(timeline_id_str: str) -> Dict[str, Any]:
        profile = profiles.get(_arm_static_profile_id(initial_arm))
        if profile is None:
            raise SystemExit(f"no static profile for iox-only initial arm {initial_arm}")
        io_map = _io_ext_map_for(_strip_profile_abs_paths(profile), timeline_id_str)
        _abs_loader(io_map)
        return io_map

    # sanity: the static profile's loader paths (already /opt on the kvmhost
    # arm config) and the dynamic map share the same binaries
    scenes: Dict[str, Any] = {}
    per_seed_profiles: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []
    sched_note = (
        f"schedule={Path(schedule_path).name} arm_sequence={arm_sequence} "
        f"num_transitions={num_transitions}")
    if num_transitions == 0:
        # Whole-run resident: the dynamic arm IS a matrix3-style static arm
        # (resident policy the entire trajectory).  Per the amendment the
        # dynamic sequence is NOT duplicated: only the two comparison
        # families run per seed.
        pass
    for off in VALIDATION_SEED_OFFSETS:
        seed_val = VALIDATION_SEED_BASE + off
        if num_transitions > 0:
            # -- dynamic: initial resident arm attaches at b0 (before the
            # first fio spawn; the timeline scene runs boundary-0 actions
            # before the first tenant spawn), later boundaries act per the
            # schedule inventory (swap_q inside one arbiter family).
            timeline_id = f"valid_dyn_s{off}"
            scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                      frozen_contract=None))
            params = scenes[f"ib_rdyn_{timeline_id}"]["parameters"]
            params["tenants"] = copy.deepcopy(static_tenants)
            params["randseed_base"] = seed_val
            params["allow_transition_exec"] = True
            # Attach/detach/swap account evidence is snapshotted with bpftool;
            # the guest install is /usr/sbin/bpftool (the scene default points
            # at the build host's kernel tree, which does not exist on kvmhost).
            params["bpftool_path"] = _env_os.environ.get("DYN_TENANT_BPFTOOL",
                                                         "/usr/sbin/bpftool")

            actions: Dict[str, List[Dict[str, Any]]] = {}
            # Boundary 0 attach: a global policy scene would race its loader
            # chain against the timeline's cgroup creation and must NOT be
            # used for dynamic sequences (kvmhost DYN_valid_dyn_s101
            # 20261104 failure).
            actions["0"] = [{"type": "attach_io_ext",
                             "scheduler": IOEXT_SCX_SCHEDULER,
                             "io_ext": _initial_attach_map(timeline_id)}]
            for boundary in sorted(inventory):
                tr = inventory[boundary]
                if tr["type"] == "swap_q":
                    qc = tr.get("q_changes") or {}
                    tenants = sorted(qc)
                    if not tenants:
                        raise SystemExit(f"swap_q at b{boundary} has no q_changes")
                    tos = {qc[t]["to"] for t in tenants}
                    froms = {qc[t]["from"] for t in tenants}
                    if len(tos) != 1 or len(froms) != 1:
                        raise SystemExit(
                            f"swap_q at b{boundary}: per-tenant q diverges "
                            f"({froms}->{tos}) -- not supported")
                    actions[str(boundary)] = [{
                        "type": "swap_q",
                        "tenants": tenants,
                        "q": tos.pop(),
                        "from_q": froms.pop(),
                        "arb_prio_by_tenant": dict(STATIC_ARM_PRIO),
                        "arb_weight_by_tenant": dict(STATIC_ARM_WEIGHT),
                    }]
                elif tr["type"] == "detach_io_ext":
                    # only reachable when the schedule is NOT iox-only
                    actions[str(boundary)] = [{"type": "detach_io_ext",
                                               "scheduler": _native_scheduler_of(tr["to_arm"])}]
                elif tr["type"] == "attach_io_ext":
                    io_map = _io_ext_map_for(f1_profile, timeline_id)
                    _abs_loader(io_map)
                    actions[str(boundary)] = [{"type": "attach_io_ext",
                                               "scheduler": IOEXT_SCX_SCHEDULER,
                                               "io_ext": io_map}]
                else:
                    raise SystemExit(f"unsupported transition type {tr['type']}")
            params["phase_actions"] = actions
            sequences.append({
                "sequence_id": f"DYN_valid_dyn_s{off}",
                "name": f"validation dynamic schedule seed {seed_val} "
                        f"({sched_note}; initial={initial_arm})",
                "io_ext_policy": "native",
                "steps": [{
                    "name": "validation dynamic schedule",
                    "scenes": [f"ib_rdyn_{timeline_id}"],
                    "duration": step_duration(static_l, 6),
                    "tenant_count": 4,
                }],
            })
        # -- comparison arms at the same seed --------------------------------
        # f1 whole-run resident (the dynamic arm itself when
        # num_transitions==0; the best-static comparison when it is not).
        # matrix3-mirror: the f1 timeline scene requests scx_io itself
        # (idempotent with the global scene's pre-switch; a "none" request
        # here would switch the elevator AWAY from the resident policy at
        # step start).
        timeline_id = f"valid_f1_arbfb_s{off}"
        scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                  frozen_contract=None, scheduler=IOEXT_SCX_SCHEDULER))
        st_scene = scenes[f"ib_rdyn_{timeline_id}"]["parameters"]
        st_scene["tenants"] = copy.deepcopy(static_tenants)
        st_scene["randseed_base"] = seed_val
        # f1 static: per-seed retargeted profile -- a SEQUENCE-scoped copy of
        # F_f1_arbfb whose cgroup paths point at THIS arm's timeline cgroups.
        # The raw static-arm profile pins rdyn_<arm>_tX paths that do not
        # exist in the validation run (loader "open cgroup dir" failure,
        # kvmhost DYN_valid_f1_arbfb_s101 20261104).
        prof_id = f"F_f1_arbfb_s{off}"
        seed_f1_profile = copy.deepcopy(f1_profile)
        seed_f1_name = seed_f1_profile.get("name") or "F_f1_arbfb"
        seed_f1_profile["name"] = f"{seed_f1_name} (validation seed {seed_val}, retargeted)"
        io_ext_prof = seed_f1_profile.get("io_ext") or {}
        io_ext_prof["cleanup_stale"] = False
        io_ext_prof["cleanup_bpffs_pins"] = False
        io_ext_prof["cleanup_stale_when_no_specs"] = False
        io_ext_prof["loader_start_gap_sec"] = 0.5
        io_ext_prof["cgroup_policies"] = _retarget_cgroup_policies(
            io_ext_prof.get("cgroup_policies") or [], timeline_id)
        if initial_arm != "f1_arbfb" and num_transitions == 0:
            raise SystemExit(
                "whole-run schedule is not f1_arbfb: the comparison wiring "
                "below must be extended for this arm")
        per_seed_profiles[prof_id] = seed_f1_profile
        if num_transitions == 0:
            f1_name = (f"validation dynamic arm (iox-only schedule: whole-run "
                       f"resident f1_arbfb, num_transitions=0) seed {seed_val}")
        else:
            f1_name = f"validation static f1_arbfb (best-static) seed {seed_val}"
        sequences.append({
            "sequence_id": f"DYN_valid_f1_arbfb_s{off}",
            "name": f1_name,
            "io_ext_policy": prof_id,
            "steps": [{
                "name": "validation dynamic schedule (whole-run resident)"
                        if num_transitions == 0 else
                        f"validation static f1_arbfb",
                "scenes": [f"ib_rdyn_{timeline_id}"],
                "duration": step_duration(static_l, 6),
                "tenant_count": 4,
            }],
        })
        # bfq native reference (the phase-5 winner of the NATIVE-mixed v3
        # schedule; kept as the honest static reference that quantifies what
        # an unconstrained schedule would have gained at phase 5)
        timeline_id = f"valid_n3_native_bfq_s{off}"
        scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                  frozen_contract=None,
                                  scheduler=_native_scheduler_of("n3_native_bfq")))
        st_scene = scenes[f"ib_rdyn_{timeline_id}"]["parameters"]
        st_scene["tenants"] = copy.deepcopy(static_tenants)
        st_scene["randseed_base"] = seed_val
        sequences.append({
            "sequence_id": f"DYN_valid_n3_native_bfq_s{off}",
            "name": f"validation native reference (n3 bfq whole-run) seed {seed_val}",
            "io_ext_policy": "native",
            "steps": [{
                "name": "validation native reference bfq",
                "scenes": [f"ib_rdyn_{timeline_id}"],
                "duration": step_duration(static_l, 6),
                "tenant_count": 4,
            }],
        })

    if num_transitions == 0:
        description = (
            "Contract 7.x validation AMENDED 20261104: the dynamic trajectory "
            "must stay under an iox policy the whole run (no native phase). "
            "Under the same-arbiter-family constraint the schedule DP over "
            "iox arms gives a whole-run resident (f1_arbfb, "
            "num_transitions=0) as the optimum, so the dynamic arm runs as a "
            "whole-run resident exactly like a matrix3 static arm; the iox-only "
            "loss equals the best static loss by identity. Native-mixed v3 "
            "(bfq at T5) would have been "
            "-13.6% vs the same static -- reported for the ledger, NOT "
            "executed. Native bfq whole-run remains as the reference arm. "
            "No post-hoc schedule changes; all failures stay in the ledger.")
    else:
        description = (
            "Contract 7.x validation: three PREDECLARED independent seeds; "
            f"dynamic executes the schedule ({sched_note}) via timeline "
            "phase_actions (b0 attach initial arm, later swap_q/re-attach "
            "boundaries); comparisons: static f1_arbfb (per-seed retargeted "
            "global policy profile) and native bfq reference. No post-hoc "
            "schedule changes; all failures stay in the ledger.")

    return {
        "benchmark_name": "dynamic tenant validation (feasible schedule vs best-static)",
        "description": description,
        "validation_schedule": {
            "source": str(schedule_path),
            "arm_sequence": arm_sequence,
            "num_transitions": num_transitions,
            "initial_arm": initial_arm,
            "iox_only": bool(constraint.get("iox_only")),
            "decision": decision.get("winner"),
            "note": "contract 20261104 amendment: iox policy resident the "
                    "whole run; see schedule_feasible_iox_only.json",
        },
        "fail_fast_on_scene_error": True,
        "io_ext_policy_profiles": {
            **{k: _strip_profile_abs_paths(v) for k, v in profiles.items()},
            **{k: _strip_profile_abs_paths(v) for k, v in per_seed_profiles.items()},
        },
        "scenes": scenes,
        "test_sequences": sequences,
        "default_sequence": sequences[0]["sequence_id"],
    }
def build_heldout_config() -> Dict[str, Any]:
    """Held-out stage YAML (predeclared in the frozen manifest 20261104).

    Two run groups, all under the SAME frozen contract as matrix3/validation
    (L=90s, B/C frozen rates, guardrails 7.9, loss 7.8, tenants never
    restarted):

      1) MAIN-FIGURE seed (offset 201): ONE predeclared held-out seed at
         which every one of the 9 arms runs once (native n1-n4, iox static
         x1-x4, f1_arbfb).  The schedule terminal state is the whole-run
         resident f1_arbfb (num_transitions=0, COINCIDE_DP_IS_WHOLE_RUN), so
         the f1 run provides BOTH the "dynamic (= resident)" identity and
         the "best-static" identity of the main figure from the same real
         run -- no duplicated trajectory, both identities labeled in the
         figure as ident=f1_arbfb resident.

      2) PAIRED seeds (offsets 202-205): the two comparison families at each
         seed, f1_arbfb (resident) vs native bfq (whole-run reference),
         extending the validation matrix to 5 held-out seeds; offset 201's
         pair is shared with the main-figure group (the s201 f1 run and the
         s201 bfq run ARE pair members).

    Wiring reuses build_validation_config()'s vetted mechanisms: per-seed
    randseed_base override via the scene's randseed_base parameter
    (per-tenant randseed = base + tenant index, contract 7.11) and
    per-(arm, seed) retargeted io_ext profiles (the cgroup-path retargeting
    fix -- raw static-arm profile cgroup paths do not exist outside the
    static-arms naming, loader "open cgroup dir" failure 20261104).
    """
    import os as _env_os

    static_cfg = yaml.safe_load(Path(_env_os.environ.get(
        "DYN_TENANT_STATIC_YML",
        str(DEFAULT_OUT_DIR / "dynamic_tenant_static_arms.yml"),
    )).read_text())
    profiles = copy.deepcopy(static_cfg.get("io_ext_policy_profiles") or {})
    if "F_f1_arbfb" not in profiles:
        raise SystemExit("F_f1_arbfb profile missing; regenerate the static config first")

    # The static arms config carries the frozen contract (L + B/C rates) on
    # every arm scene; take it from the f1 scene.
    frozen_block = None
    static_l = None
    arm_tenants = None
    for seq in static_cfg.get("test_sequences", []):
        if str(seq.get("sequence_id", "")) == "DYN_arm_f1_arbfb":
            st_params = static_cfg["scenes"][seq["steps"][0]["scenes"][0]]["parameters"]
            frozen_block = st_params.get("frozen_contract")
            static_l = float(st_params["phase_length_sec"])
            arm_tenants = copy.deepcopy(st_params["tenants"])
            break
    if frozen_block is None or static_l is None or arm_tenants is None:
        raise SystemExit("static f1 scene parameters not found in the static config")

    # Effective held-out arm set (DYN_TENANT_HELDOUT_ARMS env override; unset
    # = the kvmhost x1-x4 nine-arm set, for byte-reproducibility).  The bm
    # campaign uses N1-N4 + C1-C4 + f1_arbfb (HELDOUT_NINE_ARMS_bm).
    arms = _heldout_arms()
    arms_overridden = bool((_os.environ.get("DYN_TENANT_HELDOUT_ARMS", "") or "").strip())
    print(f"[heldout arms] {len(arms)} arms: {','.join(arms)}"
          + (" (DYN_TENANT_HELDOUT_ARMS override)" if arms_overridden else " (default kvmhost set)"))

    def _c_arm_static_params(arm: str) -> Dict[str, Any]:
        scene = (static_cfg.get("scenes") or {}).get(f"ib_rdyn_arm_{arm}")
        if not scene:
            raise SystemExit(
                f"static C-arm scene ib_rdyn_arm_{arm} missing from the static "
                f"config (DYN_TENANT_STATIC_YML); regenerate the static config first")
        return scene.get("parameters") or {}

    scenes: Dict[str, Any] = {}
    heldout_profiles: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []

    def _arm_sequence(off: int, arm: str) -> None:
        seed_val = VALIDATION_SEED_BASE + off
        timeline_id = f"held_s{off}_{arm}"
        if arm in NATIVE_ARMS:
            scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                      frozen_contract=frozen_block,
                                      scheduler=_native_scheduler_of(arm)))
            scenes[f"ib_rdyn_{timeline_id}"]["parameters"]["randseed_base"] = seed_val
            policy = "native"
        elif arm in C_ARM_IDS:
            # C arm (bare-metal main-figure set): native policy; scheduler + the
            # per-tenant cgroup io.* extras copied VERBATIM from the static
            # config's C-arm scene so held-out C runs use literally the same
            # derived constants (the static config is the constants' carrier).
            static_params = _c_arm_static_params(arm)
            scheduler = str(static_params.get("scheduler") or "none")
            scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                      frozen_contract=frozen_block, scheduler=scheduler))
            st_scene = scenes[f"ib_rdyn_{timeline_id}"]["parameters"]
            static_io_ext = static_params.get("io_ext") or {}
            extras: Dict[str, Any] = {}
            if static_io_ext.get("cgroup_tenant_controls"):
                extras["cgroup_tenant_controls"] = copy.deepcopy(
                    static_io_ext["cgroup_tenant_controls"])
            if static_io_ext.get("io_cost"):
                extras["io_cost"] = copy.deepcopy(static_io_ext["io_cost"])
            if not extras:
                raise SystemExit(
                    f"static C-arm scene ib_rdyn_arm_{arm} carries no io_ext "
                    "cgroup_tenant_controls/io_cost; regenerate the static config")
            st_scene["io_ext"].update(extras)
            st_scene["randseed_base"] = seed_val
            policy = "native"
        else:
            if arm not in IOEXT_ARM_KEYS:
                raise SystemExit(f"unknown held-out arm {arm!r}")
            scenes.update(scene_entry(timeline_id, static_l, 6, ["a", "b", "c", "d"],
                                      frozen_contract=frozen_block,
                                      scheduler=IOEXT_SCX_SCHEDULER))
            st_scene = scenes[f"ib_rdyn_{timeline_id}"]["parameters"]
            st_scene["tenants"] = copy.deepcopy(arm_tenants)
            st_scene["randseed_base"] = seed_val
            prof_id = f"{'F' if arm == 'f1_arbfb' else 'X'}_held_s{off}_{arm}"
            prof = copy.deepcopy(_strip_profile_abs_paths(
                profiles[_arm_static_profile_id(arm)]))
            io_ext = prof.get("io_ext") or {}
            io_ext["cleanup_stale"] = False
            io_ext["cleanup_bpffs_pins"] = False
            io_ext["cleanup_stale_when_no_specs"] = False
            io_ext["loader_start_gap_sec"] = 0.5
            io_ext["cgroup_policies"] = _retarget_cgroup_policies(
                io_ext.get("cgroup_policies") or [], timeline_id)
            heldout_profiles[prof_id] = prof
            policy = prof_id
        sequences.append({
            "sequence_id": f"DYN_held_s{off}_{arm}",
            "name": (f"held-out seed {seed_val} (base {VALIDATION_SEED_BASE}+{off}) "
                     f"static arm {arm}"),
            "io_ext_policy": policy,
            "steps": [{
                "name": f"DYN_held_s{off}_{arm} six phases",
                "scenes": [f"ib_rdyn_{timeline_id}"],
                "duration": step_duration(static_l, 6),
                "tenant_count": 4,
            }],
        })

    main_off = HELDOUT_MAIN_FIGURE_OFFSET
    for arm in arms:
        _arm_sequence(main_off, arm)
    for off in HELDOUT_SEED_OFFSETS[1:]:
        _arm_sequence(off, "f1_arbfb")
        _arm_sequence(off, "n3_native_bfq")

    bases = [VALIDATION_SEED_BASE + off for off in HELDOUT_SEED_OFFSETS]
    heldout_stage: Dict[str, Any] = {
        "seed_offsets": list(HELDOUT_SEED_OFFSETS),
        "seed_bases": bases,
        "main_figure_seed_off": main_off,
        "nine_arms": list(arms),
        "schedule_terminal_state": "whole-run resident f1_arbfb, num_transitions=0",
    }
    if arms_overridden:
        heldout_stage["arms_source"] = (
            "DYN_TENANT_HELDOUT_ARMS env override (bare-metal main-figure set: "
            "n1-n4 native + c1-c4 cgroup io controllers + f1_arbfb resident)")
    description = (
        "Held-out stage (PREDECLARED in the frozen manifest before any "
        "held-out run): seeds = 20261103 + offset, offsets "
        f"{list(HELDOUT_SEED_OFFSETS)} => bases {bases}; per-tenant seed "
        "= base + tenant index (contract 7.11). Group 1 (main figure): "
        f"seed {VALIDATION_SEED_BASE + main_off} with ALL 9 static arms "
        "(one real full six-phase run per arm); the schedule terminal "
        "state is a whole-run resident f1_arbfb (num_transitions=0), so "
        "the f1 run doubles as the 'dynamic (= resident)' and "
        "'best-static' identities for the figure. Group 2 (paired "
        "f1-vs-bfq): offsets 202-205 add the two comparison families; "
        "the 201 pair is shared with group 1. No schedule re-tuning "
        "(num_transitions=0); all failures stay in the ledger.")
    if arms_overridden:
        description += (
            " ARM SET (20261106, bm): DYN_TENANT_HELDOUT_ARMS override -- the "
            "main-figure arms are n1-n4 native + c1-c4 cgroup io.* controllers "
            "+ f1_arbfb (x1-x4 are not run on bm); C-arm constants are the "
            "static-arms config's contract-derived values, copied verbatim.")
    return {
        "benchmark_name": "dynamic tenant held-out (main-figure run + 5-seed paired f1-vs-bfq)",
        "description": description,
        "heldout_stage": heldout_stage,
        "fail_fast_on_scene_error": True,
        "io_ext_policy_profiles": heldout_profiles,
        "scenes": scenes,
        "test_sequences": sequences,
        "default_sequence": sequences[0]["sequence_id"],
    }


def step_duration(phase_length_sec: float, num_phases: int) -> int:
    """Wall clock with margin: timeline + spawn/teardown overhead."""
    return int(phase_length_sec * num_phases + DURATION_MARGIN_SEC + STEP_OVERHEAD_SEC)


def load_frozen_contract() -> Optional[Dict[str, Any]]:
    """Load the frozen calibration contract, if calibration has been run.

    DYN_TENANT_NO_CALIB=1 skips it: numbers frozen for one device (the local
    virtme QEMU disk) are NOT valid for another platform (kvmhost /dev/sdb);
    the remote pipeline re-freezes its own contract from its own calibration.
    """
    if _os.environ.get("DYN_TENANT_NO_CALIB") == "1":
        return None
    if not CONTRACT_CALIBRATION_JSON.exists():
        return None
    with open(CONTRACT_CALIBRATION_JSON) as f:
        data = json.load(f)
    frozen = data.get("frozen") or {}
    if not frozen.get("formal_phase_length_sec"):
        return None
    data["calibration_json_sha256"] = hashlib.sha256(
        CONTRACT_CALIBRATION_JSON.read_bytes()).hexdigest()
    return data


def _region_tenant_spec(tenant: str) -> Dict[str, Dict[str, Any]]:
    """Per-tenant region-file spec (bare-metal root-disk mode); {} when disabled.

    filename is a RESERVED tenant-spec key: the timeline scene consumes it
    natively as the single fio --filename target (it never reaches the
    generic passthrough -- duplicate --filename is a multi-file stripe in
    fio, not an override); disk_layout overrides the raw-device quarter
    defaults with the whole preallocated file [0, REGION_SIZE).
    """
    if not REGION_DIR:
        return {}
    return {
        "filename": f"{REGION_DIR}/region_{tenant}.bin",
        "disk_layout": {"offset": "0", "size": REGION_SIZE},
    }


def scene_entry(seq: str, phase_length_sec: float, num_phases: int, tenants: List[str],
                tenant_windows: Dict[str, List[int]] | None = None,
                frozen_contract: Dict[str, Any] | None = None,
                scheduler: str = "none") -> Dict[str, Any]:
    """One timeline scene instance for a sequence; native scheduler only."""
    scene_id = f"ib_rdyn_{seq}"
    parameters: Dict[str, Any] = {
        "target_device": TARGET_DEVICE,
        "scheduler": scheduler,
        "timeline_id": seq,
        "phase_length_sec": phase_length_sec,
        "num_phases": num_phases,
        "output_dir": f"./test_results/isol_bench/rig_dynamic_tenant/{seq}",
        # Explicitly false (not defaulted): cleanup helpers are forbidden while
        # tenants are active (gen_rq5_coloc / policy_transition lesson).
        "io_ext": {
            "cleanup_stale": False,
            "cleanup_bpffs_pins": False,
            "cleanup_stale_when_no_specs": False,
        },
        "duration": step_duration(phase_length_sec, num_phases),
    }
    if frozen_contract is not None:
        # Frozen contract parameters (L, rates, D pressure, seeds, guardrails).
        # The IoExtTimelineScene forwards unknown scalar tenant-spec keys to
        # fio (_TENANT_SPEC_RESERVED_KEYS gate), so the frozen B/C rates are
        # applied as per-tenant overrides below; frozen_contract stays as the
        # machine-readable freeze record.
        parameters["frozen_contract"] = frozen_contract
        rate_b = frozen_contract.get("rate_b_mib_s")
        rate_c = frozen_contract.get("rate_c_mib_s")
        overrides: Dict[str, Dict[str, Any]] = {t: {} for t in tenants}
        if rate_b:
            overrides["b"] = {"rate": f"{int(rate_b)}M"}
        if rate_c:
            overrides["c"] = {"rate": f"{int(rate_c)}M"}
        if REGION_DIR:
            # bm root-disk mode: every tenant MUST carry its region file
            # (merged after the rate override; a tenant without it would
            # silently fall back to the raw block device).
            for tenant in tenants:
                overrides[tenant].update(_region_tenant_spec(tenant))
        # NOTE: parameters.tenants is a Mapping that BOTH selects tenants and
        # carries per-tenant spec overrides, so it must list every tenant.
        parameters["tenants"] = overrides
    elif REGION_DIR:
        # bm root-disk mode without a frozen contract (smoke / calibration
        # stages): the region-file mapping is the only safe tenant selection
        # -- the raw-device default is forbidden on 86.
        parameters["tenants"] = {t: dict(_region_tenant_spec(t)) for t in tenants}
    elif set(tenants) != {"a", "b", "c", "d"}:
        parameters["tenants"] = tenants
    if tenant_windows is not None:
        parameters["tenant_windows"] = tenant_windows
    return {
        scene_id: {
            "base_scene": "scene/io_ext/timeline.yml",
            "parameters": parameters,
        }
    }


def build_smoke_config() -> Dict[str, Any]:
    seq_full = "DYN_smoke"
    seq_ab = "DYN_smoke_ab"
    scenes: Dict[str, Any] = {}
    scenes.update(scene_entry("smoke", SMOKE_PHASE_LENGTH_SEC, 6, ["a", "b", "c", "d"]))
    # Shortened A+B trajectory: T1={A}, T2={A,B}; both exit at the T2 boundary.
    scenes.update(scene_entry(
        "smokeab", SMOKE_PHASE_LENGTH_SEC, 2, ["a", "b"],
        tenant_windows={"a": [1, 2], "b": [2, 2]},
    ))
    return {
        "benchmark_name": "isol_bench rig dynamic tenant timeline (smoke, virtme)",
        "description": (
            "Dynamic multi-tenant timeline skeleton smoke: six-phase continuous "
            "trajectory T1={A} T2={A,B} T3={A,B,C} T4={A,C,D} T5={A,D} T6={A} on one "
            "absolute monotonic clock, L=15s, native scheduler=none only. "
            "DYN_smoke_ab is the shortened A+B/T1-T2 variant for the vng smoke. "
            "No io_ext policy, no scheduling preference (skeleton)."
        ),
        "fail_fast_on_scene_error": True,
        "scenes": scenes,
        "test_sequences": [
            {
                "sequence_id": seq_full,
                "name": "dynamic tenant timeline smoke (full contract, L=15s)",
                "io_ext_policy": "native",
                "steps": [{
                    "name": f"{seq_full} six phases",
                    "scenes": ["ib_rdyn_smoke"],
                    "duration": step_duration(SMOKE_PHASE_LENGTH_SEC, 6),
                }],
            },
            {
                "sequence_id": seq_ab,
                "name": "dynamic tenant timeline smoke (A+B, T1-T2, L=15s)",
                "io_ext_policy": "native",
                "steps": [{
                    "name": f"{seq_ab} two phases",
                    "scenes": ["ib_rdyn_smokeab"],
                    "duration": step_duration(SMOKE_PHASE_LENGTH_SEC, 2),
                }],
            },
        ],
        "default_sequence": seq_ab,
    }


def build_calibration_config(t4_phase_length_sec: float) -> Dict[str, Any]:
    solo = CALIB_SOLO_RUNTIME_SEC
    scenes: Dict[str, Any] = {}
    for tenant in ["a", "b", "c", "d"]:
        seq = f"calib_{tenant}"
        scenes.update(scene_entry(seq, solo, 1, [tenant], tenant_windows={tenant: [1, 1]}))
    # Static worst coexistence T4={A,C,D}: every tenant runs both phases
    # continuously (no joins/exits), C unlimited (conservative upper bound).
    scenes.update(scene_entry(
        "calib_t4", t4_phase_length_sec, CALIB_T4_NUM_PHASES, ["a", "c", "d"],
        tenant_windows={"a": [1, CALIB_T4_NUM_PHASES],
                        "c": [1, CALIB_T4_NUM_PHASES],
                        "d": [1, CALIB_T4_NUM_PHASES]},
    ))
    return {
        "benchmark_name": "isol_bench rig dynamic tenant timeline (independent calibration, virtme)",
        "description": (
            "Independent solo calibration for the dynamic multi-tenant contract: "
            f"each tenant solo for {solo}s (throughput + p50/p99 from the same "
            "hist method as M2), plus a static worst coexistence T4={A,C,D} run "
            f"({CALIB_T4_NUM_PHASES} phases x {t4_phase_length_sec:g}s) for the L "
            "decision, D pressure validation and guardrail feasibility. C runs "
            "UNLIMITED (scene cannot apply fio --rate yet): conservative upper "
            "bound on contention. Native scheduler=none only."
        ),
        "fail_fast_on_scene_error": True,
        "scenes": scenes,
        "test_sequences": [
            {
                "sequence_id": f"DYN_calib_{tenant}",
                "name": f"tenant {tenant} solo calibration ({solo}s)",
                "io_ext_policy": "native",
                "steps": [{
                    "name": f"DYN_calib_{tenant} solo phase",
                    "scenes": [f"ib_rdyn_calib_{tenant}"],
                    "duration": step_duration(solo, 1),
                }],
            } for tenant in ["a", "b", "c", "d"]
        ] + [
            {
                "sequence_id": "DYN_calib_t4",
                "name": (f"static T4={{A,C,D}} worst coexistence "
                         f"({CALIB_T4_NUM_PHASES}x{t4_phase_length_sec:g}s, C unlimited)"),
                "io_ext_policy": "native",
                "steps": [{
                    "name": "DYN_calib_t4 two-phase static coexistence",
                    "scenes": ["ib_rdyn_calib_t4"],
                    "duration": step_duration(t4_phase_length_sec, CALIB_T4_NUM_PHASES),
                }],
            },
        ],
        "default_sequence": "DYN_calib_t4",
    }


def build_virtme_config() -> Dict[str, Any]:
    seq = "DYN_virtme"
    contract = load_frozen_contract()
    if contract is not None:
        frozen = contract["frozen"]
        formal_l = float(frozen["formal_phase_length_sec"])
        frozen_contract_block = dict(frozen)
        frozen_contract_block["calibration_json_sha256"] = contract.get("calibration_json_sha256")
        l_note = f"frozen L={formal_l:g}s from {CONTRACT_CALIBRATION_JSON}"
    else:
        formal_l = FORMAL_PHASE_LENGTH_SEC
        frozen_contract_block = None
        l_note = f"contract default L={formal_l:g}s (not yet frozen)"
    scenes: Dict[str, Any] = {}
    scenes.update(scene_entry("virtme", formal_l, 6, ["a", "b", "c", "d"],
                              frozen_contract=frozen_contract_block))
    return {
        "benchmark_name": "isol_bench rig dynamic tenant timeline (formal skeleton, virtme)",
        "description": (
            "Dynamic multi-tenant timeline main-experiment skeleton: six-phase "
            "continuous trajectory T1={A} T2={A,B} T3={A,B,C} T4={A,C,D} T5={A,D} "
            f"T6={{A}} on one absolute monotonic clock, {l_note}, native "
            "scheduler=none only. Service preference A > C > B = D is recorded for "
            "later tuning; no io_ext policy in this skeleton. Frozen B/C rates "
            "are applied as per-tenant fio --rate via the scene's tenant-spec "
            "passthrough; frozen_contract is carried as the freeze record."
        ),
        "fail_fast_on_scene_error": True,
        "scenes": scenes,
        "test_sequences": [
            {
                "sequence_id": seq,
                "name": f"dynamic tenant timeline formal skeleton (L={formal_l:g}s)",
                "io_ext_policy": "native",
                "steps": [{
                    "name": f"{seq} six phases",
                    "scenes": ["ib_rdyn_virtme"],
                    "duration": step_duration(formal_l, 6),
                }],
            },
        ],
        "default_sequence": seq,
    }


# ---------------------------------------------------------------------------
# Static io_ext arms (main campaign; contract 7.10 + campaign ambiguity
# resolutions recorded in docs/plans/dynamic_tenant_reconfiguration_experiment.md
# and results/dynamic_tenant_static_arms/campaign_report.md).
#
# Contract 7.10 freezes: N1-N4 native schedulers, C1-C4 cgroup io.* arms,
# X1 "best-static io_ext" and F1 "arb_feedback static", one pre-declared run
# per arm.  Two ambiguities are resolved here and reported (NOT silently):
#   1. RESOLVED 20261105: the timeline scene now applies per-tenant cgroup
#      v2 io.* controls at spawn (io_ext.cgroup_tenant_controls) and root
#      io.cost model+qos once in setup() (io_ext.io_cost, tenant_weights ->
#      per-tenant io.weight device lines), each with a strict read-back
#      verification (a missing io.* file or a mismatching line fails the
#      run).  The C1-C3 constants below are literals frozen from the kvmhost
#      campaign contract (results/dyn_tenant_kvmhost/contract/calibration.json,
#      QEMU HARDDISK 96G sdb; io.weight/io.max/io.latency/io.bfq.weight all
#      verified write+read-back on kvmhost); C4's iocost coefficients are
#      injected via DYN_TENANT_IOCOST_COEF_JSON (iocost_coef_gen measured on
#      the campaign disk) and the generator refuses to invent them.
#   2. X1's identity is undefined (no prior static io_ext comparison) and the
#      numeric arb_prio/arb_weight mapping for "A > C > B = D" is not frozen.
#      The four static io_ext q arms below run at the frozen budget (1 run
#      each) and contract-X1 is assigned post-hoc to the lowest weighted tail
#      loss (7.8, tie-break 7.11).  Numeric mapping proposed from loader
#      semantics (lower arb_prio = higher priority; arb_prio <= 2 is the
#      latency-critical band for arb_feedback/arb_slo):
#        A prio 0 / weight 400, C prio 2 / weight 200,
#        B prio 5 / weight 100, D prio 5 / weight 100   (4 : 2 : 1 : 1)
# ---------------------------------------------------------------------------

# Loader binaries: /opt/io-ext/userspace/ on the campaign machines (kvmhost
# convention, populated by the rsync in run_dyn_tenant_kvmhost.sh); local
# virtme overlays /opt/io-ext as well... but the local smoke runners install
# to /opt/io-ext/userspace inside the VM, so use the /opt path everywhere.
# DYN_TENANT_LOADER_DIR overrides for local-tree debugging.
IOEXT_LOADER_DIR = _os.environ.get("DYN_TENANT_LOADER_DIR", "/opt/io-ext/userspace")
# Loader-scene log dir (profile output_dir): the historical default is the
# CONTROL HOST's results tree (a 20261104-noted defect that leaks an absolute
# host path into pushed configs); DYN_TENANT_LOADER_LOG_DIR retargets it for
# bm to a path inside the bm deploy root so loader logs land under the
# campaign results and are pulled back with them.
IOEXT_LOADER_LOG_DIR = _os.environ.get(
    "DYN_TENANT_LOADER_LOG_DIR",
    "/opt/ioext/results/dynamic_tenant_static_arms/loader_scene_logs")

STATIC_ARM_PRIO: Dict[str, int] = {"a": 0, "c": 2, "b": 5, "d": 5}
STATIC_ARM_WEIGHT: Dict[str, int] = {"a": 400, "c": 200, "b": 100, "d": 100}

_MIB = 1024 * 1024

# C1 io.max caps (scheduler=none; values are kvmhost frozen-contract literals,
# results/dyn_tenant_kvmhost/contract/calibration.json): B/C at the frozen
# rates rate_b_mib_s=299 / rate_c_mib_s=198 (the same source as every arm's
# fio --rate override), D at floor(0.8 * solo_d 207.617449760437) = 166 MiB/s
# (the predeclared throttling formula; the frozen contract leaves D's fio
# --rate unlimited).  A stays uncapped: the latency-critical foreground
# tenant (contract service preference A > C > B = D).
C1_IO_MAX: Dict[str, Dict[str, Any]] = {
    "b": {"rbps": 299 * _MIB, "wbps": "max", "riops": "max", "wiops": "max"},
    "c": {"rbps": 198 * _MIB, "wbps": "max", "riops": "max", "wiops": "max"},
    "d": {"rbps": "max", "wbps": 166 * _MIB, "riops": "max", "wiops": "max"},
}

# C2 io.bfq.weight (scheduler=bfq): the frozen service-preference weight
# mapping A:C:B:D = 4:2:1:1 (STATIC_ARM_WEIGHT); io.bfq.weight carries it on
# the "default <w>" line (kernel 7.0 cgroup v2 has no per-device bfq line).
C2_IO_BFQ_WEIGHT: Dict[str, int] = {"a": 400, "c": 200, "b": 100, "d": 100}

# C3 io.latency targets (scheduler=none): ceil(2 * solo p99_steady) up to the
# next 1000us.  Solo p99_steady (us, kvmhost frozen contract): a=419.84,
# b=9240.576, c=11862.016, d=5079.04 =>
#   a: 2*419.84    = 839.68     -> 1000
#   b: 2*9240.576  = 18481.152  -> 19000
#   c: 2*11862.016 = 23724.032  -> 24000
#   d: 2*5079.04   = 10158.08   -> 11000
C3_IO_LATENCY_TARGET_US: Dict[str, int] = {"a": 1000, "b": 19000, "c": 24000, "d": 11000}

# C4 io.cost: per-tenant io.weight device lines use the same frozen weight
# mapping (iocost has no per-tenant knob other than io.weight on kernel 7.0;
# the root model/qos come from load_iocost_coefficients()).
C4_TENANT_WEIGHTS: Dict[str, int] = {"a": 400, "c": 200, "b": 100, "d": 100}

# NOTE (20261106, bm port): C1_IO_MAX / C3_IO_LATENCY_TARGET_US above are now
# the FALLBACK literals (kvmhost frozen-contract numbers).  The effective
# constants are DERIVED per platform by derive_c_arm_constants() from the
# frozen contract loaded via DYN_TENANT_CALIB_JSON (bare-metal: the bm calibration
# contract; with the kvmhost contract the derivation reproduces the literals
# exactly -- same formulas, platform numbers).  The literals are kept because
# scripts/gen_dyn_tenant_c_smoke.py imports them and because the pre-freeze
# dry-run (DYN_TENANT_NO_CALIB=1) still needs a parseable static-arms yml;
# the bm driver only pushes static arms generated WITH the bm contract.


def derive_c_arm_constants(
    frozen_contract: Optional[Dict[str, Any]],
) -> tuple:
    """C1 io.max + C3 io.latency constants derived from the frozen contract.

    Same formulas as the kvmhost literals, platform-correct numbers (20261106
    correction: the bm campaign must not reuse kvmhost-measured constants):
      C1: rbps(b) = frozen.rate_b_mib_s, rbps(c) = frozen.rate_c_mib_s,
          wbps(d) = floor(0.8 * solo_d.steady_mib_s); A uncapped.
      C3: target(t) = ceil(2 * solo_t.p99_us_steady up to the next 1000us).

    ``frozen_contract`` is load_frozen_contract()'s full calibration document
    (rates in .frozen, solo stats in .solo).  Returns (c1, c3, source); when
    the contract is absent/incomplete the kvmhost fallback literals are
    returned with an explicit fallback source string (and a loud WARNING --
    fallback constants are kvmhost-measured and wrong for the bm disk).
    """
    src = (frozen_contract or {}).get("frozen") or {}
    if not src and frozen_contract:
        src = frozen_contract  # accept a bare frozen block too
    solo = (frozen_contract or {}).get("solo") or {}
    rate_b = src.get("rate_b_mib_s")
    rate_c = src.get("rate_c_mib_s")
    solo_d = (solo.get("d") or {}).get("steady_mib_s")
    p99s = {t: (solo.get(t) or {}).get("p99_us_steady") for t in ("a", "b", "c", "d")}
    derivable = (rate_b is not None and rate_c is not None and solo_d is not None
                 and all(p99s[t] is not None for t in p99s))
    if derivable:
        c1: Dict[str, Any] = {
            "b": {"rbps": int(rate_b) * _MIB, "wbps": "max", "riops": "max", "wiops": "max"},
            "c": {"rbps": int(rate_c) * _MIB, "wbps": "max", "riops": "max", "wiops": "max"},
            "d": {"rbps": "max", "wbps": int(0.8 * float(solo_d)) * _MIB,
                  "riops": "max", "wiops": "max"},
        }
        # ceil(2*p99/1000)*1000 == int((2*p99 + 999)//1000)*1000 for p99 > 0
        c3: Dict[str, int] = {t: int((2 * float(p99s[t]) + 999) // 1000) * 1000
                              for t in ("a", "b", "c", "d")}
        source = ("derived from the frozen contract calibration "
                  f"(rate_b_mib_s={rate_b}, rate_c_mib_s={rate_c}, "
                  f"solo_d_mib_s={solo_d}, solo p99_us_steady="
                  + "/".join(str(p99s[t]) for t in ("a", "b", "c", "d")) + ")")
        return c1, c3, source, float(solo_d)
    print("WARNING: C1/C3 constants are NOT derivable from a frozen contract "
          "(no DYN_TENANT_CALIB_JSON / DYN_TENANT_NO_CALIB=1): falling back to "
          "the kvmhost frozen-contract LITERALS.  These are kvmhost-measured "
          "numbers and MUST NOT be pushed for the bm campaign (the bm driver "
          "pushes static arms only when generated with the bm contract).")
    return dict(C1_IO_MAX), dict(C3_IO_LATENCY_TARGET_US), \
        "kvmhost frozen-contract literal fallback (no contract loaded)", None


def _c_constants_doc(c1: Dict[str, Any], c3: Dict[str, int], source: str,
                     solo_d_mib: Optional[float]) -> str:
    """The C1-C4 constants paragraph for the static-arms description."""
    b_mib = int(c1["b"]["rbps"]) // _MIB
    c_mib = int(c1["c"]["rbps"]) // _MIB
    d_mib = int(c1["d"]["wbps"]) // _MIB
    solo_d_txt = f"{solo_d_mib:.4f}" if solo_d_mib is not None else f"{d_mib / 0.8:.4f}"
    return (
        f"Constants {source}: C1 caps B/C rbps at the frozen rates "
        f"{b_mib}/{c_mib} MiB/s and D wbps at floor(0.8*solo_d {solo_d_txt})="
        f"{d_mib} MiB/s (A uncapped, latency-critical foreground); C2 "
        "io.bfq.weight A:C:B:D=400:200:100:100 (STATIC_ARM_WEIGHT, "
        "'default <w>' lines); C3 io.latency targets ceil(2*solo p99_steady "
        f"up to the next 1000us) = a {c3['a']} / b {c3['b']} / c {c3['c']} / "
        f"d {c3['d']} us; C4 root model+qos from DYN_TENANT_IOCOST_COEF_JSON "
        "(iocost_coef_gen measured on the campaign disk) with the same "
        "400:200:100:100 per-tenant io.weight weights."
    )

STATIC_ARMS_DOC = (
    "Static-arm tuning matrix (contract 7.10/7.11, frozen seed base 20261103, "
    "L=90s, 1 pre-declared run per arm). Native arms N1-N4 set the block "
    "scheduler in the timeline scene. io_ext arms run device scheduler=scx_io "
    "with a resident global loader scene (arbiters first, then per-tenant q "
    "policies attached to the tenant cgroups /sys/fs/cgroup/rdyn_<seq>_t{a..d} "
    "pre-created by the runner). cleanup_stale/cleanup_bpffs_pins stay false "
    "in the timeline scene (forbidden while tenants are active); the GLOBAL "
    "loader scene may clean stale loaders/pins because it runs before any "
    "tenant exists. C1-C4 (cgroup io.max / io.bfq.weight / io.latency / "
    "io.cost) are SUPPORTED since 20261105 (the old contract deviation -- no "
    "scene support, no calibrated constants -- is resolved): the timeline "
    "scene applies io_ext.cgroup_tenant_controls per tenant at spawn and "
    "io_ext.io_cost model/qos once at setup (tenant_weights become per-tenant "
    "io.weight device lines), each write strictly read-back verified (a "
    "missing io.* file or a mismatching line fails the run)."
)


def load_iocost_coefficients() -> Dict[str, Any]:
    """Root io.cost model+qos coefficients from DYN_TENANT_IOCOST_COEF_JSON.

    The JSON file must hold {"model": {...}, "qos": {...}} exactly in the
    setup_iocost_at_root input formats:
      model: ctrl/model plus the six linear-coefficient rate fields
             (rbps, rseqiops, rrandiops, wbps, wseqiops, wrandiops)
      qos:   enable/ctrl/rpct/rlat/wpct/wlat/min/max
    The coefficients MUST be measured on the campaign disk (iocost_coef_gen);
    another disk's numbers would silently mis-size iocost, so the generator
    refuses to run the C4 arm without this file.
    """
    raw_path = _os.environ.get("DYN_TENANT_IOCOST_COEF_JSON", "").strip()
    if not raw_path:
        raise SystemExit(
            "C4 arm (DYN_arm_c4_io_cost) needs measured io.cost coefficients: "
            "set DYN_TENANT_IOCOST_COEF_JSON=<json> holding "
            '{"model": {...}, "qos": {...}} (setup_iocost_at_root formats, '
            "produced by iocost_coef_gen on the campaign disk)")
    path = Path(raw_path)
    if not path.is_file():
        raise SystemExit(
            f"DYN_TENANT_IOCOST_COEF_JSON={raw_path!r} does not exist or is not "
            f"a file (C4 arm root io.cost coefficients)")
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"DYN_TENANT_IOCOST_COEF_JSON={raw_path!r} is not readable JSON "
            f"({exc}); C4 needs iocost_coef_gen output") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("model"), dict) \
            or not isinstance(doc.get("qos"), dict):
        raise SystemExit(
            f"DYN_TENANT_IOCOST_COEF_JSON={raw_path!r} must be a JSON object "
            'with "model" and "qos" mappings (setup_iocost_at_root formats)')
    return doc


def _arm_seq(arm_id: str) -> str:
    return f"DYN_arm_{arm_id}"


def _arm_token(arm_id: str) -> str:
    return f"arm_{arm_id}"


def _arm_tenant_cgroups(seq_token: str) -> Dict[str, str]:
    return {t: f"/sys/fs/cgroup/rdyn_{seq_token}_t{t}" for t in ("a", "b", "c", "d")}


# q_ratelimit_wdrr legacy per-tenant caps (MiB/s); x6 uses C1-aligned rates.
QRATELIMIT_RATE_MIB: Dict[str, int] = {"a": 64, "b": 299, "c": 198, "d": 208}
KYBER_Q_EXTRA_ARGS = ["--read-target-ns", "2000000", "--write-target-ns", "10000000"]


def _ioext_arm_profile(policy_id: str, seq_token: str, arm: str,
                       tenants: List[str],
                       qratelimit_iomax_mib: Optional[Dict[str, int]] = None,
                       ) -> Dict[str, Any]:
    """io_ext_policy profile -> global loader scene for one static io_ext arm.

    arm: qnone | qfifo_wdrr | qddl_wdrr | spwedf_wdrr | arbfb |
         qkyber | qkyber_wdrr | qratelimit | qratelimit_wdrr | fifo_arbiocost
    """
    q_of = {
        "qnone": {t: "q_none" for t in tenants},
        "qfifo_wdrr": {t: "q_fifo" for t in tenants},
        "qddl_wdrr": {t: "q_ddl" for t in tenants},
        "spwedf_wdrr": {t: ("q_sp_edf" if t == "a" else "q_wedf") for t in tenants},
        "arbfb": {t: "q_fifo" for t in tenants},
        "qkyber": {t: "q_kyber" for t in tenants},
        "qkyber_wdrr": {t: "q_kyber" for t in tenants},
        "qratelimit": {t: "q_ratelimit" for t in tenants},
        "qratelimit_wdrr": {t: "q_ratelimit" for t in tenants},
        "fifo_arbiocost": {t: "q_fifo" for t in tenants},
    }[arm]
    io_ext: Dict[str, Any] = {
        # Loader-scene-local hygiene: safe here (this scene starts BEFORE any
        # tenant exists and stops after they exit).  The timeline scene keeps
        # its own cleanup keys explicitly false (contract guardrail).
        "cleanup_stale": True,
        "cleanup_bpffs_pins": True,
        "stale_loader_patterns": ["loader_arb_", "loader_q_", "loader_minimal"],
        "root_cgroup": {"path": "/sys/fs/cgroup", "controllers": ["io"]},
        "arbiters": [],
        "cgroup_policies": [],
    }
    if arm == "qkyber":
        io_ext["root_policies"] = [{
            "name": f"{policy_id}_kyber_root",
            "path": f"{IOEXT_LOADER_DIR}/loader_q_kyber",
            "scope": "root_policy",
            "args": ["-c", "/sys/fs/cgroup", *KYBER_Q_EXTRA_ARGS],
        }]
        return {
            "name": f"dynamic tenant static io_ext arm {policy_id}",
            "target_device": TARGET_DEVICE,
            "scheduler": "scx_io",
            "output_dir": f"{IOEXT_LOADER_LOG_DIR}/{seq_token}",
            "io_ext": io_ext,
        }
    wdrr_family = ("qfifo_wdrr", "qddl_wdrr", "spwedf_wdrr", "qkyber_wdrr", "qratelimit_wdrr")
    if arm in wdrr_family:
        io_ext["arbiters"].append({
            "name": f"{policy_id}_wdrr_arb",
            "path": f"{IOEXT_LOADER_DIR}/loader_arb_wdrr",
        })
    elif arm == "arbfb":
        io_ext["arbiters"].append({
            "name": f"{policy_id}_feedback_arb",
            "path": f"{IOEXT_LOADER_DIR}/loader_arb_feedback",
        })
    elif arm == "fifo_arbiocost":
        # kvmhost: loader_arb_iocost struct_ops attach hangs (ready gate);
        # arb_cost is the same DRR+cost core without qos/vrate cfg maps.
        io_ext["arbiters"].append({
            "name": f"{policy_id}_cost_arb",
            "path": f"{IOEXT_LOADER_DIR}/loader_arb_cost",
        })
    cgroups = _arm_tenant_cgroups(seq_token)
    for tenant in sorted(tenants):
        q = q_of[tenant]
        if arm == "qratelimit":
            # Homomorph to C1 io.max: A uncapped (no q_ratelimit loader).
            if tenant == "a":
                continue
            if not qratelimit_iomax_mib:
                raise ValueError(
                    "qratelimit arm requires qratelimit_iomax_mib (C1 caps b/c/d)"
                )
        args: List[str] = [
            "-c", "{policy_cgroup_path}",
            "-p", str(STATIC_ARM_PRIO[tenant]),
            "-W", str(STATIC_ARM_WEIGHT[tenant]),
        ]
        if arm == "qkyber_wdrr":
            args.extend(KYBER_Q_EXTRA_ARGS)
        elif arm == "qratelimit_wdrr":
            args.extend(["--rate-mib-s", str(QRATELIMIT_RATE_MIB[tenant])])
        elif arm == "qratelimit":
            args.extend(["--rate-mib-s", str(qratelimit_iomax_mib[tenant])])
        io_ext["cgroup_policies"].append({
            "name": f"t{tenant}_{q}",
            "path": f"{IOEXT_LOADER_DIR}/loader_{q}",
            "policy_cgroup_path": cgroups[tenant],
            "args": args,
        })
    return {
        "name": f"dynamic tenant static io_ext arm {policy_id}",
        "target_device": TARGET_DEVICE,
        "scheduler": "scx_io",
        # Absolute loader-scene output dir (inside the shared workspace) so the
        # loader stdout/stderr logs land under the sequence result tree and are
        # archived by the runner (ioext_map_dump/).  IOEXT_LOADER_LOG_DIR
        # (DYN_TENANT_LOADER_LOG_DIR) retargets it per platform.
        "output_dir": f"{IOEXT_LOADER_LOG_DIR}/{seq_token}",
        "io_ext": io_ext,
    }


def build_static_arms_config(frozen_contract: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Static-arm tuning matrix: N1-N4 native + 4 static io_ext q arms + F1 +
    C1-C4 cgroup io controller arms.

    L and the frozen B/C rates come from the frozen contract exactly as for
    DYN_virtme.  Sequence IDs map to contract arm ids:
      N1..N4  native none / mq-deadline / bfq / kyber
      X1..X4  static io_ext (qnone / qfifo+arb_wdrr / qddl+arb_wdrr /
              sp_edf(A)+wedf(others)+arb_wdrr); contract-X1 = post-hoc best
      F1      arb_feedback (static) + per-tenant q_fifo
      C1..C4  cgroup v2 io controllers applied by the timeline scene
              (io.max / io.bfq.weight / io.latency / root io.cost +
              per-tenant io.weight); constants are kvmhost frozen-contract
              literals (see C1_IO_MAX / C2_IO_BFQ_WEIGHT /
              C3_IO_LATENCY_TARGET_US), C4 model/qos from
              DYN_TENANT_IOCOST_COEF_JSON (load_iocost_coefficients).
    Plus DYN_arm_smoke_ab: the A+B L=15s io_ext smoke (X2 loader topology,
    own scene token arm_smokeab) used before the tuning matrix.
    """
    if frozen_contract is not None:
        # Accept either the full calibration.json dict or its "frozen" block;
        # scene_entry needs the frozen block itself for the rate overrides.
        src = frozen_contract.get("frozen", frozen_contract)
        formal_l = float(src["formal_phase_length_sec"])
        frozen_block = dict(src)
        frozen_block["calibration_json_sha256"] = frozen_contract.get("calibration_json_sha256")
        l_note = f"frozen L={formal_l:g}s"
    else:
        formal_l = FORMAL_PHASE_LENGTH_SEC
        frozen_block = None
        l_note = f"contract default L={formal_l:g}s (not yet frozen)"

    # C1/C3 constants: derived from the loaded contract when present (bare-metal: the
    # bm calibration contract; kvmhost contract: reproduces the historical
    # literals exactly -- same formulas); fallback literals + loud WARNING
    # when no contract is loaded.
    c1_io_max, c3_latency, c_const_source, c_solo_d = derive_c_arm_constants(frozen_contract)
    print(f"[C-arm constants] source: {c_const_source}")
    print(f"[C-arm constants] C1 io.max bytes: b.rbps={c1_io_max['b']['rbps']} "
          f"c.rbps={c1_io_max['c']['rbps']} d.wbps={c1_io_max['d']['wbps']}")
    print(f"[C-arm constants] C3 io.latency targets (us): {c3_latency}")

    scenes: Dict[str, Any] = {}
    profiles: Dict[str, Any] = {}
    sequences: List[Dict[str, Any]] = []

    def add_native(arm_id: str, scheduler: str) -> None:
        seq = _arm_seq(arm_id)
        token = _arm_token(arm_id)
        scenes.update(scene_entry(token, formal_l, 6, ["a", "b", "c", "d"],
                                  frozen_contract=frozen_block, scheduler=scheduler))
        sequences.append({
            "sequence_id": seq,
            "name": f"static arm {arm_id} native scheduler={scheduler}",
            "io_ext_policy": "native",
            "steps": [{
                "name": f"{seq} six phases",
                "scenes": [f"ib_rdyn_{token}"],
                "duration": step_duration(formal_l, 6),
            }],
        })

    def add_ioext(arm_id: str, arm: str,
                  qratelimit_iomax_mib: Optional[Dict[str, int]] = None) -> None:
        seq = _arm_seq(arm_id)
        token = _arm_token(arm_id)
        policy_id = f"X_{arm_id}" if arm_id.startswith("x") else f"F_{arm_id}"
        profiles[policy_id] = _ioext_arm_profile(
            policy_id, token, arm, ["a", "b", "c", "d"],
            qratelimit_iomax_mib=qratelimit_iomax_mib,
        )
        scenes.update(scene_entry(token, formal_l, 6, ["a", "b", "c", "d"],
                                  frozen_contract=frozen_block, scheduler="scx_io"))
        sequences.append({
            "sequence_id": seq,
            "name": f"static arm {arm_id} io_ext {arm} (scx_io + resident loader)",
            "io_ext_policy": policy_id,
            "steps": [{
                "name": f"{seq} six phases",
                "scenes": [f"ib_rdyn_{token}"],
                "duration": step_duration(formal_l, 6),
            }],
        })

    def add_cgroup_controller(arm_id: str, scheduler: str,
                              io_ext_extras: Dict[str, Any], name: str) -> None:
        seq = _arm_seq(arm_id)
        token = _arm_token(arm_id)
        scenes.update(scene_entry(token, formal_l, 6, ["a", "b", "c", "d"],
                                  frozen_contract=frozen_block, scheduler=scheduler))
        # The C-arm knobs ride the same io_ext mapping as the cleanup keys
        # (the timeline scene reads io_ext.cgroup_tenant_controls / io_ext.io_cost).
        scenes[f"ib_rdyn_{token}"]["parameters"]["io_ext"].update(io_ext_extras)
        sequences.append({
            "sequence_id": seq,
            "name": name,
            "io_ext_policy": "native",
            "steps": [{
                "name": f"{seq} six phases",
                "scenes": [f"ib_rdyn_{token}"],
                "duration": step_duration(formal_l, 6),
            }],
        })

    add_native("n1_native_none", "none")
    add_native("n2_native_mqddl", "mq-deadline")
    add_native("n3_native_bfq", "bfq")
    add_native("n4_native_kyber", "kyber")
    add_ioext("x1_qnone", "qnone")
    add_ioext("x2_qfifo_wdrr", "qfifo_wdrr")
    add_ioext("x3_qddl_wdrr", "qddl_wdrr")
    add_ioext("x4_spwedf_wdrr", "spwedf_wdrr")
    add_ioext("f1_arbfb", "arbfb")
    # Supplementary io_ext arms (compact figure row TBD); same six-phase contract.
    add_ioext("x5_qkyber", "qkyber")
    qratelimit_iomax_mib = {
        "b": int(c1_io_max["b"]["rbps"]) // _MIB,
        "c": int(c1_io_max["c"]["rbps"]) // _MIB,
        "d": int(c1_io_max["d"]["wbps"]) // _MIB,
    }
    add_ioext("x6_qratelimit_wdrr", "qratelimit",
              qratelimit_iomax_mib=qratelimit_iomax_mib)
    add_ioext("x7_fifo_arbiocost", "fifo_arbiocost")

    # C1 io.max (scheduler=none): B/C at the frozen rates, D at floor(0.8*solo)
    # -- A deliberately NOT in the mapping (uncapped foreground tenant).
    add_cgroup_controller(
        "c1_io_max", "none",
        {"cgroup_tenant_controls": {
            tenant: {"io_max": dict(c1_io_max[tenant])} for tenant in c1_io_max
        }},
        "static arm c1_io_max cgroup io.max (scheduler=none; b/c rbps="
        f"{int(c1_io_max['b']['rbps']) // _MIB}/{int(c1_io_max['c']['rbps']) // _MIB} "
        "MiB/s frozen-contract rates, d wbps="
        f"{int(c1_io_max['d']['wbps']) // _MIB} MiB/s floor(0.8*solo_d), a uncapped)")
    # C2 io.bfq.weight (scheduler=bfq): per-tenant "default <w>" lines with the
    # frozen STATIC_ARM_WEIGHT mapping.
    add_cgroup_controller(
        "c2_bfq_weight", "bfq",
        {"cgroup_tenant_controls": {
            tenant: {"io_bfq_weight": weight} for tenant, weight in C2_IO_BFQ_WEIGHT.items()
        }},
        "static arm c2_bfq_weight cgroup io.bfq.weight (scheduler=bfq; "
        "A:C:B:D=400:200:100:100 static-arm weights)")
    # C3 io.latency (scheduler=none): target = ceil(2 * solo p99_steady).
    add_cgroup_controller(
        "c3_io_latency", "none",
        {"cgroup_tenant_controls": {
            tenant: {"io_latency": {"target": target}}
            for tenant, target in c3_latency.items()
        }},
        "static arm c3_io_latency cgroup io.latency (scheduler=none; "
        "target=ceil(2*solo p99_steady): "
        f"a={c3_latency['a']} b={c3_latency['b']} "
        f"c={c3_latency['c']} d={c3_latency['d']} us)")
    # C4 io.cost (scheduler=none): root model+qos once in setup() + per-tenant
    # io.weight device lines; coefficients from DYN_TENANT_IOCOST_COEF_JSON.
    iocost_coef = load_iocost_coefficients()
    add_cgroup_controller(
        "c4_io_cost", "none",
        {"io_cost": {
            "model": copy.deepcopy(iocost_coef["model"]),
            "qos": copy.deepcopy(iocost_coef["qos"]),
            "tenant_weights": dict(C4_TENANT_WEIGHTS),
        }},
        "static arm c4_io_cost root io.cost + per-tenant io.weight "
        "(scheduler=none; model/qos from DYN_TENANT_IOCOST_COEF_JSON, "
        "A:C:B:D=400:200:100:100 io.weight device lines)")

    # io_ext smoke: A+B over T1-T2 at L=15s with the X2 loader topology so the
    # resident-loader + timeline coexistence is verified before the matrix.
    smoke_token = "arm_smokeab"
    profiles["X_arm_smoke_qfifo_wdrr"] = _ioext_arm_profile(
        "X_arm_smoke_qfifo_wdrr", smoke_token, "qfifo_wdrr", ["a", "b"])
    scenes.update(scene_entry(smoke_token, SMOKE_PHASE_LENGTH_SEC, 2, ["a", "b"],
                              tenant_windows={"a": [1, 2], "b": [2, 2]},
                              scheduler="scx_io"))
    sequences.append({
        "sequence_id": "DYN_arm_smoke_ab",
        "name": "static io_ext arm smoke (A+B, T1-T2, L=15s, arb_wdrr + per-tenant q_fifo)",
        "io_ext_policy": "X_arm_smoke_qfifo_wdrr",
        "steps": [{
            "name": "DYN_arm_smoke_ab two phases",
            "scenes": [f"ib_rdyn_{smoke_token}"],
            "duration": step_duration(SMOKE_PHASE_LENGTH_SEC, 2),
        }],
    })

    return {
        "benchmark_name": "isol_bench rig dynamic tenant static arms (main campaign, virtme)",
        "description": (STATIC_ARMS_DOC + " "
                        + _c_constants_doc(c1_io_max, c3_latency, c_const_source, c_solo_d)
                        + f" {l_note}."),
        "fail_fast_on_scene_error": True,
        "scenes": scenes,
        "io_ext_policy_profiles": profiles,
        "test_sequences": sequences,
        "default_sequence": "DYN_arm_smoke_ab",
    }


def main() -> None:
    import os as _em
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--t4-phase-length", type=float, default=CALIB_DEFAULT_T4_L_SEC,
                        help="phase length L (s) for the DYN_calib_t4 worst "
                             "coexistence run (L escalation rounds re-run with "
                             "90 / 135)")
    parser.add_argument("--emit-validation", type=Path, default=None,
                        help="also emit the validation-stage YAML at this path")
    parser.add_argument("--emit-heldout", type=Path, default=None,
                        help="also emit the held-out stage YAML at this path")
    parser.add_argument("--skip-static-arms", action="store_true",
                        help="skip dynamic_tenant_static_arms.yml (the C4 arm "
                             "requires measured io.cost coefficients via "
                             "DYN_TENANT_IOCOST_COEF_JSON; the bm pre-freeze "
                             "stages smoke/calibration generate without them)")
    args = parser.parse_args()
    if args.emit_validation:
        cfg = build_validation_config()
        args.emit_validation.parent.mkdir(parents=True, exist_ok=True)
        with open(args.emit_validation, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, width=100)
        print(f"wrote validation config {args.emit_validation} "
              f"({len(cfg['test_sequences'])} sequences)")
        return
    if args.emit_heldout:
        cfg = build_heldout_config()
        args.emit_heldout.parent.mkdir(parents=True, exist_ok=True)
        with open(args.emit_heldout, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, width=100)
        print(f"wrote heldout config {args.emit_heldout} "
              f"({len(cfg['test_sequences'])} sequences)")
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if REGION_DIR:
        print(f"[region-file mode] dir={REGION_DIR} size={REGION_SIZE} per tenant "
              "(parameters.tenants.<t>.filename=region_<t>.bin; tenant fio never "
              "targets the raw block device)")
    outputs = [
        ("dynamic_tenant_smoke.yml", build_smoke_config()),
        ("dynamic_tenant_calibration.yml", build_calibration_config(args.t4_phase_length)),
        ("dynamic_tenant_virtme.yml", build_virtme_config()),
    ]
    if not args.skip_static_arms:
        # NOTE: built LAST and optionally skipped -- build_static_arms_config
        # aborts without measured iocost coefficients (C4, measured constants
        # only), and the list construction above runs before ANY file is
        # written; the bm pre-freeze stages pass --skip-static-arms.
        outputs.append(
            ("dynamic_tenant_static_arms.yml",
             build_static_arms_config(load_frozen_contract())))
    for filename, config in outputs:
        path = args.output_dir / filename
        with open(path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False, width=100)
        print(f"wrote {path} ({len(config['test_sequences'])} sequences, "
              f"default={config['default_sequence']})")


if __name__ == "__main__":
    main()


