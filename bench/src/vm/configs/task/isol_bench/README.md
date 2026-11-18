# isol_bench — IISWC 2025 storage isolation benchmark task configs

isol-bench (IISWC 2025 — *Does Linux Provide Performance Isolation for NVMe SSDs?*)
re-implementation on top of ioext-bench `IoExtFioScene`. These task YAMLs reproduce
the D1–D4 desiderata on a real NVMe device under cgroup v2 block-layer controls.

- Library code: [`src/lib/isol_bench/`](../../../../src/lib/isol_bench)
  (`desiderata.py`, `metrics.py`, `workloads.py`, `knobs.py`).
- **实验矩阵（baseline / 场景 / sequence / 结果目录）**：
  [`docs/plans/isol-bench-integration/EXPERIMENT_MATRIX.md`](../../../../docs/plans/isol-bench-integration/EXPERIMENT_MATRIX.md)
- Upstream reference: [atlarge-research/isol-bench](https://github.com/atlarge-research/isol-bench)
  (local clone: `/home/anon/isol-bench`); **not** a submodule.

---

## Directories

| Path | Purpose |
|------|---------|
| `paper/` | Full paper reproduction matrix (D1–D4, all knobs, n up to 128/256) |
| `rig_iox/` | **kvmhost Step3 rig** — per-policy io-ext evaluation (D1–D4) |
| `iox/` | Generated iox track configs (legacy / generator output) |

### `rig_iox/` task files (Step3 主实验)

| File | Desideratum | Sequences |
|------|-------------|-----------|
| `d1_q1_rig_iox.yml` | D1-Q1 LC latency, n=32, 10 queue policies | `D1Q1_iox_*_n32` |
| `d2_fairness_rig_iox.yml` | D2 weighted fairness, cg=8 | `D2_saturated_weighted_iox_*_cg8_iter1` |
| `d3_prio_util_rig_iox.yml` | D3 LC+BE, homo + hetero arb | `D3LC_rread_4k_iox_*` |
| `d4_burst_rig_iox.yml` | D4 LC burst | `D4LC_iox_*` |

Run: `scripts/run_rig_iox_full_remote.sh` or `scripts/run_rig_iox_gated_remote.sh`

### `paper/` task files

| File | Desideratum |
|------|-------------|
| `d1_q1_latency.yml` | D1-Q1 LC latency overhead vs tenant count |
| `d1_q2_bandwidth.yml` | D1-Q2 batch bandwidth scalability |
| `d2_fairness.yml` | D2 Jain fairness (mixed tenant profiles) |
| `d3_prio_util_lc.yml` | D3 LC Pareto (LC QD=1 + BE tenants) |
| `d3_prio_util_batch.yml` | D3 batch-variant Pareto scans |
| `d4_burst_lc.yml` | D4 LC burst latency (`startdelay`) |
| `d4_burst_batch.yml` | D4 batch burst bandwidth |

---

## Environment

```bash
export PERF_ARENA_WORKSPACE=/opt/ioext
export TARGET_DEVICE=sdb
export PYTHONPATH=$PERF_ARENA_WORKSPACE/src
export BLOCK_LAYER_NR_REQUESTS=32
```

Rig on kvmhost uses **`/dev/sdb`**. Verify `lsblk` before running.

---

## Run commands

```bash
cd $PERF_ARENA_WORKSPACE

# rig_iox full batch (control host)
./scripts/run_rig_iox_gated_remote.sh --skip-known-broken

# Single sequence on guest
.venv/bin/python -m vm.executor \
  --config src/vm/configs/task/isol_bench/rig_iox/d1_q1_rig_iox.yml \
  --sequence D1Q1_iox_q_fifo_n32 \
  --output /tmp/rig_iox_D1Q1_iox_q_fifo_n32
```

---

## Output

Per-sequence results land under `--output` with `metadata.json`, per-scene fio
JSON, and optional monitor samples. Post-run analysis scripts are kept locally
under `scripts/` (not committed).

---

## Known pitfalls (20260916 campaign — full retrospective in the archive)

**Design**
- iox (strategy) arm must carry **no** cgroup io controller values — controller
  values belong only to the `rig_native/` baseline arm. Base-scene deep-merge
  re-injects `io_weight`/`io_max` defaults, so template removal requires explicit
  `null` overrides.
- `io.cost.qos/model` are written at the cgroup **root** and are sticky device
  state with no teardown — every non-io_cost sequence must scrub with
  `io_cost: {qos: {enable: false}}`.
- Device-dependent constants must come from THIS device's calibration: sdb
  saturates at ~320 MiB/s (not the 2300 MiB/s NVMe-era constant); io.latency
  targets below the measured saturation p50 degenerate to permanent-violation
  clamping. bfq arms need `low_latency: 0, slice_idle: 1`.
- All `loader_q_*` must use the `ioext_cli_args.h` flag parser. Positional-argv
  loaders (q_noop, q_accept_noop were the stragglers) fail with
  `open cgroup dir: No such file or directory` when the framework passes `-c`.
  Revisit "known-broken" lists before trusting them (q_mq_deadline was fine).

**Operations**
- Guest `/tmp` is a ~7.8G tmpfs and `run_one` only cleans the CURRENT sequence's
  dir — clean `/tmp/rig_iox_*` between phases or the campaign dies at ~60
  sequences (Errno 28).
- Batch failure leaves completed sequences' tarballs stranded on the guest —
  **pull regardless of exit code** before any cleanup (order matters).
- `hypervisor alloc release` rolls the VM back to the base snapshot (loses kernel,
  loaders, repo); snapshot before releasing; a fresh alloc starts from base.
- Post-incident bookends are contaminated (stale teardown state) — validate
  environment recovery before reading drift verdicts from them.

**Monitoring**
- `system_metric` needs `disk_devices: [sdb]` to sample disk busy%.
- Saturation gate is device-calibrated (93% for sdb, measured ceiling
  93.8–95.5%) — 95% false-flags every saturated run.
- `fio_json_metrics.bw_mbps` is actually **MiB/s** (mislabel); bpftrace averages
  over <10 events are meaningless — check sample counts.

See `$ARCHIVE/20260916_exp_RIG_BASELINE_RETROSPECTIVE.md` for the full incident
log, timeline, and follow-ups.

---

## Related

- `explore_scene_combo/` — real-workload multi-tenant experiments; **do not**
  co-schedule with isol_bench on the same VM session (loader pin conflicts).
- `src/vm/scene/io_ext/fio.py` — `IoExtFioScene`
- `src/vm/scene/io_ext/common.py` — cgroup v2 io controls
