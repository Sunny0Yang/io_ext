# homo-v2 on bare-metal bm

Status: implementation and local validation in progress. No bm experiment from this runbook is valid until the loader bundle, read-only preflight, netconsole gate, and staged smoke gates below pass.

## Safety contract

- `nvme0n1` is the system root disk. fio targets only these regular files:
  - `/opt/ioext/homo_v2/data/elevator.bin`
  - `/opt/ioext/homo_v2/data/controller_lc.bin`
  - `/opt/ioext/homo_v2/data/controller_be.bin`
- `target_device: nvme0n1` is used only for scheduler and cgroup accounting/control.
- `scripts/run_homo_v2_bm.sh` must never forcibly terminate fio, a loader, or `vm.executor`.
- A busy process, held experiment lock, active root `io.cost`, missing knob, loader hash mismatch, or device-phase drift is a fail-stop condition.
- SysRq policy: capture the live state and report first. Do not trigger SysRq automatically. Wait for explicit user instruction; no automatic reboot is permitted.

## Runtime-derived observation limits

| Family | fio definition | Normal wall time | Warning | Incident |
|---|---|---:|---:|---:|
| elevator | runtime 60 s | 63-75 s | 90 s | 120 s |
| controller/frontier | runtime 60 s plus 5 s ramp/start delay | 68-85 s | 105 s | 150 s |

At the incident threshold the control runner writes `STOP_DISPATCH`, captures process/thread state, `/proc` stacks, cgroup state, BPF maps, dmesg, and journal directly to the control host, then exits 86. It does not signal the live process tree. The remote machine lock remains held by the detached campaign process.

## Local checks

```bash
python3 scripts/gen_homo_v2_yamls.py --check
python3 scripts/gen_homo_v2_yamls.py \
  --profile bm \
  --calibration-json experiments/homo_v2/calibration_bm.json \
  --check
bash -n scripts/run_homo_v2_bm.sh
pytest -q tests/test_homo_v2_generator.py tests/test_run_homo_v2_bm.py
bash scripts/run_homo_v2_bm.sh --dry-run --reps 2
```

## Loader gate

The versioned directory `experiments/homo_v2/build/loaders/` must contain these ten executables plus `MANIFEST.sha256` and `BUILD_PROVENANCE.txt`:

```text
loader_q_none loader_q_fifo loader_q_mq_deadline loader_q_kyber
loader_q_iomax loader_q_linnos loader_arb_wdrr loader_arb_iocost
loader_arb_iolatency loader_arb_d2fq
```

Provenance records the userspace and kernel commits, dirty patch hash, `KERNELRELEASE`, BTF or `vmlinux.h` hash, and every binary hash. The runner requires the provenance to contain the current remote `uname -r`.

## Staged execution

1. Read-only preflight:

   ```bash
   bash scripts/run_homo_v2_bm.sh --preflight-only
   ```

2. Arm the approved temporary netconsole path and 30 s hung-task diagnostics. Verify both the local UDP sink and remote module before any IOX case. Record original sysctl values for normal restoration.

3. Deploy and prepare three 32 GiB regular files. This is a write phase and therefore requires explicit `--execute`:

   ```bash
   CAMPAIGN_ID=homo_v2_bm_prepare \
     bash scripts/run_homo_v2_bm.sh --execute --prepare-data \
       --seq HOMO_elev_n_none
   ```

4. Native natural-exit smoke:

   ```bash
   CAMPAIGN_ID=homo_v2_bm_smoke_native \
     bash scripts/run_homo_v2_bm.sh --execute --skip-push \
       --seq HOMO_elev_n_none
   ```

5. IOX staged smoke. Run one sequence at a time and inspect sidecar/manifest after each: q-only elevator, no-arbiter q_iomax, one arbiter, native io.latency, and native io.cost. Do not automatically retry a failed case.

6. Full rep 1 pilot, then reps 1-3. Native and IOX counterparts are adjacent; even reps reverse AB to BA. Each pair is bracketed by a 10 s QD1 native sentinel. A before/after drift above 20% invalidates the pair and stops the campaign.

7. Only after all 16 arms pass the fio, parser, sidecar, setup, manifest, and phase gates at n=3 may the campaign extend to n=10 using `--resume`.

## Analysis and tuning

```bash
python3 scripts/build_homo_v2_results.py \
  --results-root results/homo_v2_bm/<CAMPAIGN_ID> \
  --out-dir results/homo_v2_bm/<CAMPAIGN_ID>/analysis
```

A data-quality failure is not an algorithm-tuning signal. Device-phase failures are discarded pairs. Host-specific tuning is allowed only when a valid n=3 comparison proves that a frozen native knob is not binding or an IOX/native pair is materially inconsistent. Change one parameter at a time, create a new frozen config/result root, run adjacent A/B once, and require n=3 confirmation before restarting formal collection.

## Run ledger

Append every remote action before the next phase: timestamp, campaign ID, kernel, loader manifest hash, selected sequences, data-file hashes/sizes, netconsole state, normal/incident duration, gate result, and any restoration performed. Incident records must state whether a D-state thread was directly observed; an unresponsive fio without `STAT=D` is recorded as a drain stall or dispatch wedge, not assumed to be D state.

### 2026-11-08: prepare wrapper false incident

- Campaign: `homo_v2_bm_native_smoke_20261108`.
- Preflight: kernel `7.3.1-ioext3`, device `259:0`, all knobs present, no lock/process conflict, root io.cost neutral, 569 GiB free.
- Bundle/config deployment and remote SHA/config verification passed.
- The prepare wrapper failed before starting fio: `/tmp/ioxhost_work.lock` was root-owned and opening it as `user` returned `Permission denied`.
- The original wrapper did not create `FAILED` on an early shell error, so the control supervisor waited until its then-600 s preparation threshold and captured an incident.
- Evidence: `results/homo_v2_bm/homo_v2_bm_native_smoke_20261108/incidents/prepare_data_20261108T064110Z.txt`.
- Classification: wrapper failure, not an IO hang. No fio/loader/executor process existed, no task had `STAT=D`, and kernel logs contained no hung-task, NVMe timeout/reset/error, lockup, oops, or panic. No SysRq action was warranted or taken.
- No data file was prepared and no benchmark sequence started.
- Local fixes before any retry: preserve the shared lock inode but set it writable by the SSH user before launch; write `FAILED` from prepare/campaign ERR traps; restore the one-time three-file preparation observation limit to 1800 s.

### 2026-11-08: netconsole gate stopped before IOX

- Native smoke completed before this gate: `HOMO_elev_n_none`, fio rc=0, 31 valid sidecar samples, phase drift 0.23%, read p50=626.7 us, read p99=1941.5 us, IOPS=113113.
- Original hung-task values were saved as `timeout=120`, `all_cpu_backtrace=0`. A temporary netconsole attempt set `30/1`, then restored the originals and unloaded netconsole.
- The existing UDP/6666 listener and a fresh Python UDP/6667 listener both bound successfully, but a userspace UDP probe from bm to either port did not arrive. Therefore netconsole delivery was not accepted as an evidence gate.
- No IOX smoke or formal campaign was started. No SysRq, process signal, teardown, reboot, or destructive recovery was used.
- The netconsole gate was later relaxed by explicit user instruction: proceed with SSH-only incident evidence while preserving the no-signal/no-SysRq boundary. Netconsole remains unavailable and must be called out in the campaign metadata.

### 2026-11-08: n=3 pilot completed, formal expansion paused

- Campaign: `homo_v2_bm_pilot_r1_retry_20261108`; 16/16 manifests for each of reps 1-3, 48/48 cases returned naturally, no incident and no D-state evidence.
- All parser/setup gates pass after adapting IOX setup evidence to the bm loader+`scx_io` sidecar semantics. Legacy `ioext_*` bpffs pin names are absent but are retained as an audit fact.
- The paired phase sentinel is unstable during the campaign: several before/after QD1 windows moved between roughly 0.9k and 9.1k IOPS. Recorded drift pairs include `ctrl_cost`, `front_d2fq`, `front_linnos`, and additional rep2/rep3 pairs.
- Three independent read-only sentinels after campaign cleanup were stable at 9179, 9139, and 9464 IOPS, so the device was healthy at the final check but not phase-stable throughout the campaign.
- Interpretation: n=3 is a valid execution/evidence pilot, but absolute cross-arm medians are not yet a trustworthy formal comparison. Do not extend to n=10 or tune algorithm knobs until the phase protocol isolates the transient root io.cost/scheduler/device-state interaction.

### 2026-11-09: single-variable tune round T1 (q_iomax dispatch serialization)

- Diagnosis first, from the merged repair tables (`results/homo_v2_bm/repair_tables_20261108/`): phase covariates recorded in each case's `phase_gate.json` show native and IOX arms of the same campaign/rep share one phase window (50 same-bucket vs 7 diff-bucket combos, the 7 all frontier), so the repair-table gaps are not phase artifacts. The only gap with a direction stable across all three phase regimes is `ctrl_iomax` `be_track_err_pct`: native `n_io_max` 0.013-0.020% vs IOX `x_iomax_q` 3.56-7.51%, BE overshooting the 64 MiB/s cap to 66.3-68.8 MiB/s in 6/6 samples.
- Root cause (code read of `q_iomax.bpf.c` at the frozen HEAD): `cgroup_dispatch` runs concurrently across blk-mq hardware queues; admission checked the pre-peek token snapshot and the floor-at-0 deduct let concurrently-admitted requests through uncharged. Sustained overshoot (+288 MiB over 60 s) exceeds any burst-cap explanation (32 MiB cap). Loader-side evidence after the fix confirms the concurrency is real (`busy_bounces` 6087/11102/14452 per rep).
- Fix (single variable): per-cgroup dispatch serialization (`dispatch_busy` gate at dispatch entry, cleared on every return path, 3 ms BLK_MQ_BUDGET_DELAY self-kick for bounced callers) plus a `busy_bounces` evidence counter; mirrors blk-throttle per-tg dispatch serialization. Applied on top of the clean frozen tree (`/tmp/homo_v2_loader_tune1_qiomax_20261109/src`); only `loader_q_iomax` was rebuilt (binary hash elided), all other bundle binaries unchanged; `BUILD_PROVENANCE.txt` carries the tune1 section and `MANIFEST.sha256` was regenerated.
- Campaign: `homo_v2_bm_tune_iomax1_20261109`, `ctrl_iomax` pair only, 3 reps, AB/BA order flip on rep2. Exit 0, no incident, no D-state evidence, all six cases exited naturally; `lost=0` in every q_iomax stats dump (accounting conservation intact).
- Result: the overshoot is gone. BE bandwidth now tracks the cap from below: 63.93/63.95/62.91 MiB/s vs 64 (was 66.3-68.8). `be_track_err_pct` IOX 0.117%/0.079%/1.711% vs native 0.016%/0.609%/0.016% (rep1 gate-invalid: a genuine 90% mid-rep phase drift 9422->893 IOPS, both arms equally affected). Rep2 reaches full parity (jain 0.999, share_ratio_dev 1.001, lc_read_p99 0.987, track err better than native); rep3 keeps a small ~1.7% undershoot, direction reversed and magnitude ~4x smaller than the pre-fix overshoot.
- Merged tables refreshed to `results/homo_v2_bm/repair_tables_20261109/` (tune campaign takes precedence for `n_io_max`/`x_iomax_q` reps 2-3).
- Not tuned in this round (discipline: one variable per round): the rep3 1.7% undershoot (candidate: bounce-induced 3 ms waits under slow-phase dispatch cadence) and the phase-conditional `elev_kyber` slow-phase p99 gap (0.90 ratio fast vs 1.56-1.60 slow; needs phase-conditional data before any `--read-target-ns` decision).
