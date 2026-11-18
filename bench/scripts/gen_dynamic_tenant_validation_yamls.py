#!/usr/bin/env python3
"""Generate the validation-phase task YAML for the dynamic-tenant experiment.

Emits src/vm/configs/task/isol_bench/rig_dynamic_tenant/dynamic_tenant_validation.yml
with 7 sequences:

  DYN_valid_smoke_ab     phase-0 infra smoke: A+B, L=15s, one transition of
                         each class (switch_native / attach_io_ext / detach)
                         plus BOTH swap_q directions under the resident
                         arbiter (q_fifo->q_none AND q_none->q_fifo, the slow
                         direction that measured 5.19-5.42s owner_token flip
                         wall) at owner_token_timeout_sec=10.
  DYN_valid_s{j}_dynamic   (j=1..3) the frozen feasibility schedule executed
                         for real on the timeline scene with
                         allow_transition_exec=true:
                           T1=kyber, T2/T3=none, T4/T5=f1_arbfb, T6=kyber
                         boundary 1: switch_native kyber->none
                         boundary 3: attach_io_ext (arb_feedback + per-tenant
                                     q_fifo, frozen prio/weight mapping)
                         boundary 5: detach_io_ext -> kyber (drain + restore)
                         tenant fio never restarted; no swap_q in this
                         schedule (pre-declared, no re-tuning allowed).
  DYN_valid_s{j}_static    (j=1..3) best-static arm f1_arbfb for the whole
                         trajectory (resident global loader scene, same
                         topology as the tuning campaign's DYN_arm_f1_arbfb).

Paired design: both arms of seed j share the SAME workload seed base
(randseed_base = V_j); per-tenant seeds follow the contract 7.11 derivation
rule  base + tenant index  (a=+1 b=+2 c=+3 d=+4, randrepeat=1).

Validation seed derivation (recorded; the contract froze per-tenant seeds
only for the tuning base 20261103, no validation seeds):
    V_j = 20261103 + 10*j   ->  V1=20261113, V2=20261123, V3=20261133
so no validation seed collides with any tuning seed (20261104..20261107) or
the transition-smoke ledger seed (20261104).

Frozen contract parameters are unchanged from the tuning campaign: L=90s,
B/C rates 1037/976 MiB/s, D unlimited, guardrails 7.9, loss 7.8.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "src/vm/configs/task/isol_bench/rig_dynamic_tenant"
SCHEDULE_JSON = REPO_ROOT / "results/dynamic_tenant_static_arms/schedule_feasible.json"

TARGET_DEVICE = "nvme0n1"
PHASE_LENGTH_SEC = 90.0
NUM_PHASES = 6
SMOKE_PHASE_LENGTH_SEC = 15
SMOKE_NUM_PHASES = 4
DURATION_FULL = 600
DURATION_SMOKE = 120

IOEXT_LOADER_DIR = "/opt/ioext-io_ext/userspace/io/build"
VALIDATION_RESULTS_REL = "results/dynamic_tenant_validation"

# Frozen validation seed bases (see module docstring).
VALIDATION_SEED_BASES = {1: 20261113, 2: 20261123, 3: 20261133}

# Frozen arbiter mapping (static arms campaign; lower prio = higher priority).
STATIC_ARM_PRIO = {"a": 0, "c": 2, "b": 5, "d": 5}
STATIC_ARM_WEIGHT = {"a": 400, "c": 200, "b": 100, "d": 100}

RATE_B_MIB_S = 1037
RATE_C_MIB_S = 976


def frozen_contract_block(seed_base: int) -> dict:
    """Freeze record carried on every scene (machine-readable)."""
    return {
        "formal_phase_length_sec": PHASE_LENGTH_SEC,
        "rate_b_mib_s": RATE_B_MIB_S,
        "rate_c_mib_s": RATE_C_MIB_S,
        "d_rate": "unlimited",
        "seed_base": seed_base,
        "seed_derivation": "contract 7.11 rule: per-tenant randseed = base + tenant index (a=+1..d=+4), randrepeat=1",
        "validation_seed_bases": VALIDATION_SEED_BASES,
        "schedule_source": str(SCHEDULE_JSON.relative_to(REPO_ROOT)),
        "schedule_phases": {"1": "n4_native_kyber", "2": "n1_native_none",
                            "3": "n1_native_none", "4": "f1_arbfb",
                            "5": "f1_arbfb", "6": "n4_native_kyber"},
        "owner_token_timeout_sec": 10,
        "loss_weights": {"a": 0.6, "c": 0.3, "low_group_bd": 0.1},
        "guardrail_thresholds": {"min_completion_fraction_of_solo": 0.05,
                                 "max_no_progress_sec": 5.0,
                                 "max_boundary_drift_sec": 1.0},
    }


def tenant_overrides() -> dict:
    return {"a": {}, "b": {"rate": f"{RATE_B_MIB_S}M"},
            "c": {"rate": f"{RATE_C_MIB_S}M"}, "d": {}}


def base_parameters(token: str, seed_base: int, scheduler: str,
                    duration: int, phase_length_sec: float = PHASE_LENGTH_SEC,
                    num_phases: int = NUM_PHASES) -> dict:
    return {
        "target_device": TARGET_DEVICE,
        "scheduler": scheduler,
        "timeline_id": token,
        "phase_length_sec": phase_length_sec,
        "num_phases": num_phases,
        "randseed_base": seed_base,
        # Absolute output dir inside the rw-mounted results tree so the
        # per-transition loader logs (io_ext_transitions/, tenant swap logs)
        # survive the VM session (the relative ./test_results default is
        # ephemeral under virtme-ng).
        "output_dir": f"/opt/ioext/{VALIDATION_RESULTS_REL}/run_dirs/{token}",
        "io_ext": {
            "cleanup_stale": False,
            "cleanup_bpffs_pins": False,
            "cleanup_stale_when_no_specs": False,
        },
        "duration": duration,
        "frozen_contract": frozen_contract_block(seed_base),
        "tenants": tenant_overrides(),
    }


def arbfb_cgroup_policies(token: str, tenants=("a", "b", "c", "d")) -> list:
    out = []
    for tenant in sorted(tenants):
        out.append({
            "name": f"t{tenant}_q_fifo",
            "path": f"{IOEXT_LOADER_DIR}/loader_q_fifo",
            "policy_cgroup_path": f"/sys/fs/cgroup/rdyn_{token}_t{tenant}",
            "args": [
                "-c", "{policy_cgroup_path}",
                "-p", str(STATIC_ARM_PRIO[tenant]),
                "-W", str(STATIC_ARM_WEIGHT[tenant]),
            ],
        })
    return out


def build_dynamic_scene(token: str, seed_base: int) -> dict:
    """Timeline scene executing the frozen schedule for real."""
    params = base_parameters(token, seed_base, scheduler="kyber", duration=DURATION_FULL)
    params["allow_transition_exec"] = True
    params["loader_dir"] = IOEXT_LOADER_DIR
    params["phase_actions"] = {
        1: [{"type": "switch_native", "scheduler": "none"}],
        3: [{
            "type": "attach_io_ext",
            "scheduler": "scx_io",
            "io_ext": {
                "cleanup_stale": False,
                "cleanup_bpffs_pins": False,
                "cleanup_stale_when_no_specs": False,
                "loader_start_gap_sec": 0.5,
                "arbiters": [{
                    "name": f"{token}_feedback_arb",
                    "path": f"{IOEXT_LOADER_DIR}/loader_arb_feedback",
                }],
                "cgroup_policies": arbfb_cgroup_policies(token),
            },
        }],
        5: [{"type": "detach_io_ext", "scheduler": "kyber",
             "drain_timeout_sec": 30}],
    }
    return {
        f"ib_rdyn_{token}": {
            "base_scene": "scene/io_ext/timeline.yml",
            "parameters": params,
        }
    }


def build_static_scene(token: str, seed_base: int) -> dict:
    params = base_parameters(token, seed_base, scheduler="scx_io", duration=DURATION_FULL)
    return {
        f"ib_rdyn_{token}": {
            "base_scene": "scene/io_ext/timeline.yml",
            "parameters": params,
        }
    }


def build_static_profile(policy_id: str, token: str, seed_base: int) -> dict:
    """Resident global-loader scene for the best-static f1_arbfb arm."""
    return {
        "name": f"dynamic tenant validation best-static arm {policy_id}",
        "target_device": TARGET_DEVICE,
        "scheduler": "scx_io",
        "output_dir": (
            f"/opt/ioext/{VALIDATION_RESULTS_REL}/"
            f"loader_scene_logs/{token}"
        ),
        "io_ext": {
            # Global loader scene runs BEFORE any tenant exists: its own
            # hygiene is allowed here (same pattern as the static-arms
            # campaign).  The timeline scene keeps cleanup keys false.
            "cleanup_stale": True,
            "cleanup_bpffs_pins": True,
            "stale_loader_patterns": ["loader_arb_", "loader_q_", "loader_minimal"],
            "root_cgroup": {"path": "/sys/fs/cgroup", "controllers": ["io"]},
            "arbiters": [{
                "name": f"{policy_id}_feedback_arb",
                "path": f"{IOEXT_LOADER_DIR}/loader_arb_feedback",
            }],
            "cgroup_policies": arbfb_cgroup_policies(token),
        },
    }


def build_smoke_scene() -> dict:
    """Phase-0 A+B smoke: one transition per class + BOTH swap_q directions.

    Derived from dynamic_tenant_transitions_smoke.yml (precedent), extended
    with the reverse swap (q_none->q_fifo, the slow owner_token-flip
    direction) and owner_token_timeout_sec=10 on both swaps.
    """
    token = "valid_smoke_ab"
    params = {
        "target_device": TARGET_DEVICE,
        "scheduler": "none",
        "timeline_id": token,
        "phase_length_sec": SMOKE_PHASE_LENGTH_SEC,
        "num_phases": SMOKE_NUM_PHASES,
        "randseed_base": VALIDATION_SEED_BASES[1],
        "output_dir": f"/opt/ioext/{VALIDATION_RESULTS_REL}/run_dirs/{token}",
        "io_ext": {
            "cleanup_stale": False,
            "cleanup_bpffs_pins": False,
            "cleanup_stale_when_no_specs": False,
        },
        "duration": DURATION_SMOKE,
        "tenants": ["a", "b"],
        "tenant_windows": {"a": [1, 4], "b": [2, 3]},
        "allow_transition_exec": True,
        "loader_dir": IOEXT_LOADER_DIR,
        "phase_actions": {
            1: [{"type": "switch_native", "scheduler": "mq-deadline"}],
            2: [{
                "type": "attach_io_ext",
                "scheduler": "scx_io",
                "io_ext": {
                    "cleanup_stale": False,
                    "cleanup_bpffs_pins": False,
                    "cleanup_stale_when_no_specs": False,
                    "loader_start_gap_sec": 0.5,
                    "arbiters": [{
                        "name": f"{token}_wdrr_arb",
                        "path": f"{IOEXT_LOADER_DIR}/loader_arb_wdrr",
                    }],
                    "cgroup_policies": [
                        {"name": "ta_q_fifo",
                         "path": f"{IOEXT_LOADER_DIR}/loader_q_fifo",
                         "policy_cgroup_path": f"/sys/fs/cgroup/rdyn_{token}_ta",
                         "args": ["-c", "{policy_cgroup_path}", "-p", "0", "-W", "400"]},
                        {"name": "tb_q_fifo",
                         "path": f"{IOEXT_LOADER_DIR}/loader_q_fifo",
                         "policy_cgroup_path": f"/sys/fs/cgroup/rdyn_{token}_tb",
                         "args": ["-c", "{policy_cgroup_path}", "-p", "5", "-W", "100"]},
                    ],
                },
            }],
            3: [
                {"type": "swap_q", "tenants": ["a"], "q": "q_none",
                 "arb_prio": 0, "arb_weight": 400,
                 "owner_token_timeout_sec": 10},
                {"type": "swap_q", "tenants": ["a"], "q": "q_fifo",
                 "arb_prio": 0, "arb_weight": 400,
                 "owner_token_timeout_sec": 10},
            ],
            4: [{"type": "detach_io_ext", "scheduler": "none",
                 "drain_timeout_sec": 30}],
        },
    }
    return {f"ib_rdyn_{token}": {"base_scene": "scene/io_ext/timeline.yml",
                                 "parameters": params}}


def build_config() -> dict:
    scenes: dict = {}
    profiles: dict = {}
    sequences: list = []

    # --- phase-0 smoke -------------------------------------------------------
    scenes.update(build_smoke_scene())
    sequences.append({
        "sequence_id": "DYN_valid_smoke_ab",
        "name": ("validation phase-0 smoke: A+B L=15s, switch_native/attach/"
                 "detach + swap_q both directions at owner_token_timeout=10s"),
        "io_ext_policy": "native",
        "steps": [{
            "name": "DYN_valid_smoke_ab four phases",
            "scenes": ["ib_rdyn_valid_smoke_ab"],
            "duration": DURATION_SMOKE,
        }],
    })

    # --- 3 seeds x 2 paired arms ---------------------------------------------
    for j, seed_base in sorted(VALIDATION_SEED_BASES.items()):
        dyn_token = f"valid_s{j}_dynamic"
        sta_token = f"valid_s{j}_static"
        scenes.update(build_dynamic_scene(dyn_token, seed_base))
        scenes.update(build_static_scene(sta_token, seed_base))
        policy_id = f"F_valid_s{j}_arbfb"
        profiles[policy_id] = build_static_profile(policy_id, sta_token, seed_base)
        sequences.append({
            "sequence_id": f"DYN_valid_s{j}_dynamic",
            "name": (f"validation seed {j} (base {seed_base}): frozen schedule "
                     "kyber->none->f1_arbfb->kyber executed for real "
                     "(allow_transition_exec, 3 transitions, tenant fio never "
                     "restarted)"),
            "io_ext_policy": "native",
            "steps": [{
                "name": f"DYN_valid_s{j}_dynamic six phases with transitions",
                "scenes": [f"ib_rdyn_{dyn_token}"],
                "duration": DURATION_FULL,
            }],
        })
        sequences.append({
            "sequence_id": f"DYN_valid_s{j}_static",
            "name": (f"validation seed {j} (base {seed_base}): best-static "
                     "f1_arbfb for the whole trajectory (paired comparison arm)"),
            "io_ext_policy": policy_id,
            "steps": [{
                "name": f"DYN_valid_s{j}_static six phases",
                "scenes": [f"ib_rdyn_{sta_token}"],
                "duration": DURATION_FULL,
            }],
        })

    schedule_note = ""
    if SCHEDULE_JSON.exists():
        sched = json.loads(SCHEDULE_JSON.read_text())
        schedule_note = (
            f" Frozen schedule under validation: "
            f"{sched['schedule']['phases']} "
            f"(dynamic-vs-static hypothetical delta "
            f"{sched['schedule_vs_static_delta'] * 100:.2f}%)."
        )

    return {
        "benchmark_name": "isol_bench rig dynamic tenant validation (frozen schedule vs best-static, virtme)",
        "description": (
            "Validation phase (pre-declared gate, NO schedule re-tuning): the "
            "feasibility schedule from schedule_feasible.json is executed for "
            "real on the timeline scene and paired against the best-static "
            "arm f1_arbfb on 3 independent validation seeds. Seed derivation: "
            f"V_j = 20261103 + 10*j = {VALIDATION_SEED_BASES}; per-tenant "
            "seeds = base + tenant index (contract 7.11 rule); no tuning seed "
            "reused. owner_token wait widened to 10s (scene action parameter; "
            "the loader has no ownership-wait timeout of its own - see "
            f"{VALIDATION_RESULTS_REL}/loader_patch/)." + schedule_note
        ),
        "fail_fast_on_scene_error": True,
        "scenes": scenes,
        "io_ext_policy_profiles": profiles,
        "test_sequences": sequences,
        "default_sequence": "DYN_valid_smoke_ab",
    }


def main() -> None:
    out_dir = DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dynamic_tenant_validation.yml"
    config = build_config()
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False, width=100)
    print(f"wrote {path} ({len(config['test_sequences'])} sequences, "
          f"default={config['default_sequence']})")


if __name__ == "__main__":
    main()
