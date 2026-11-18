# bench — benchmark framework, campaigns, and results

`bench/` is the benchmarking half of the artifact: the scene-based VM
benchmark framework used for every experiment in the paper, the per-campaign
experiment directories, and the cleaned result layer (CSV/JSON summaries +
figures + the scripts that render them).

## Framework (`src/`)

- `src/vm/` — scene-based benchmark engine. A *scene* is one benchmark unit
  (fio, RocksDB db_bench, YCSB/RocksDB, TailBench/Shore, block-layer
  tracers...); a *task config* (YAML, `src/vm/configs/task/...`) composes
  scenes into a campaign. Key scenes for this paper:
  - `scene/io_ext/` — fio / dbbench / ycsb_rocksdb / tailbench_shore /
    `timeline.py` (the multi-tenant lifecycle engine used by the dynamic-
    tenant and D1/D2/D3/D4 campaigns; spawns tenant cgroups, drives
    policy/loader state transitions, applies cgroup v2 io.* controls).
  - `scene/block_bpf/` — bpftrace-based block-layer observers
    (`m1_pattern_sampler`, `scx_io_bypass_fallback`, latency/cost probes).
  - `scene/system/` — system-metric and perf sampling sidecars.
- `src/lib/` — `isol_bench` (result schema + validation), `iox_safety`
  (custody/teardown evidence helpers), `benchmark` (common launchers).

Run a task config on the target machine with:

```bash
python3 -m vm.executor --config src/vm/configs/task/isol_bench/<rig>/<case>.yml
```

(the guest-side runner is invoked by the campaign scripts in `scripts/`).

## Campaigns (`experiments/`)

| Directory | Paper part |
|---|---|
| `m1m2_bm/` | M1 intra-tenant I/O profiles (YCSB-A/C, db_bench, Shore) + M1.2/M2 native-scheduler motivation |
| `dyn_tenant_bm/` | T1–T6 multi-tenant lifecycle (main experiment) + D1 overhead ladder scripts + frozen contract JSONs |
| `d2_rerun_kvmhost/` | D2 proportional-fairness rerun (runbook, results, calibration) |
| `d3_kvmhost/` | D3/D4 isolation reports + the combined `fig_d2_d3_d4_rq13_row` figure |
| `homo_v2/` | homomorphic-reproduction campaign runbooks + D2FQ native-port notes |
| `reproduction/` | policy-fidelity campaign: 8 arms (q_none + 7 policies) incl. native D2FQ; `RESULTS/` holds per-policy cleaned CSVs + figures |
| `safety_chaos_kvmhost/` | dynamic-update & fault-injection safety campaign (`q_inject_null` / `q_inject_stall` fault policies, dmesg/netconsole capture via scripts) |

Machine aliases: `bm` = bare-metal host; `kvmhost` = KVM guest (see root
README). SSH credentials are **not** included — each campaign script reads an
ssh-env file (default `ioext-ssh/ssh_env.sh`) providing `SSH_HOST` /
`SSH_USER` / `SSH_PORT` / remote-sudo helpers; point it at your own machine.

## Results (`results/`)

Figure-rendering layer: the **plotting scripts** for every paper figure plus
per-figure documentation. Per this artifact's scope, neither raw per-run
data (timeseries, per-case fio/json outputs, bpftrace logs, host evidence)
nor aggregated summary tables nor rendered figure files are included —
regenerate the data by running the campaigns in `experiments/`, then render
with these scripts.

| Directory | Contents |
|---|---|
| `results/d1/` | D1 overhead + scalability rendering scripts |
| `results/d2/` | D2 cg-scale rendering scripts + final report (`report/`) |
| `results/d3/` | D3/D4 combined-figure rendering chain entry |
| `results/reproduction/` | eight-arm strip rendering scripts |
| `results/dynamic_tenant/` | design-ref rendering scripts |
| `results/m1m2_bm/` | M1/M2 motivation rendering scripts |
| `results/motivation_m1/tools/` | M1 sampler-log parsing helpers (`m1_metrics.py`) |

Render a figure after its campaign data exists locally, e.g.:

```bash
bash results/d1/scripts/refresh_d1_figures.sh
bash results/m1m2_bm/scripts/refresh_m1m2_bm_figures.sh
bash results/d3/scripts/refresh_d3_kvmhost_figures.sh
```
