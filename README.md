# io_ext: Composable and Programmable Block-Layer Scheduling for Multi-Tenant NVMe Storage

Artifact repository for the paper **"io\_ext: Composable and Programmable
Block-Layer Scheduling for Multi-Tenant NVMe Storage"** (double-blind
submission, anonymous authors).

`io_ext` is an eBPF-based block-layer scheduling framework built on Linux
7.3.1. It introduces a decoupled *N:1* programming model — `N` per-`cgroup`
local queueing policies (`q_*`) composed with a single global dispatch
arbiter (`arb_*`) — and guarantees race-free dynamic policy updates via a
kernel-side request custody state machine and a four-phase RCU quiescence
protocol.

## Repository layout

```
├── kernel/                 Kernel-side artifact
│   ├── io_ext-linux-7.3.1.patch   Single patch: vanilla Linux 7.3.1 -> io_ext (+ D2FQ port)
│   ├── config-7.3.1-ioext         Kernel .config used for the paper builds
│   └── README.md                  Build instructions
├── userspace/              eBPF scheduling plane (userspace side)
│   ├── q_*.bpf.c           Local per-cgroup queueing policies
│   │                       (fifo, ddl, mq_deadline, kyber, bfq, wedf, sp_edf,
│   │                        ratelimit, iomax, linnos, inject_null/stall fault policies, ...)
│   ├── arb_*.bpf.c         Global arbiters (wdrr, cost, d2fq, bfq, feedback, iocost, ...)
│   ├── loader_*.c          Per-policy userspace loaders (struct_ops attach/detach)
│   ├── ioext_*.h           Shared BPF/kernel boundary headers
│   ├── ARB_*.md            Arbiter development guides & contracts
│   └── tools/              Guest-side smoke / hot-switch tooling
├── tests/                  Policy correctness tests (model/oracle/CI, virtme runner)
└── bench/                  Benchmarking framework + experiment campaigns + results
    ├── src/vm/             VM benchmark framework (scene-based; io_ext / block_bpf / system scenes)
    ├── src/lib/            Framework libraries (isol_bench, iox_safety, benchmark)
    ├── scripts/            Campaign drivers, analysis and plotting scripts
    ├── experiments/        Per-campaign configs, runbooks and scripts
    │   ├── m1m2_bm/            M1/M2 motivation (YCSB / db_bench / Shore profiles)
    │   ├── dyn_tenant_bm/      T1-T6 multi-tenant lifecycle + D1 overhead ladder
    │   ├── d2_rerun_kvmhost/   D2 proportional-fairness rerun
    │   ├── d3_kvmhost/         D3/D4 isolation reports
    │   ├── homo_v2/            Homomorphic reproduction campaign runbooks
    │   ├── reproduction/       Policy-fidelity campaign (q_none + 7 policies + D2FQ)
    │   └── safety_chaos_kvmhost/ Dynamic-update & fault-injection safety campaign
    └── results/            Figure-rendering scripts + per-figure documentation
                            (no raw data, summary tables, or figure files)
```

## Naming conventions

- `bm` — the bare-metal experiment host.
- `kvmhost` — the KVM guest used for the D2/D3/D4, reproduction and safety
  campaigns (a dedicated KVM instance with an Intel Core i5-9400 (6 cores),
  15 GiB DRAM and a dedicated 96 GiB NVMe block device, as described in the
  paper's experimental setup).
- SSH targets are intentionally **not** included: the campaign runners expect
  an environment file (e.g. `ioext-ssh/ssh_env.sh`) providing `SSH_HOST`,
  `SSH_USER`, `SSH_PORT` and remote-sudo helpers. Point them at your own test
  machine.

## Kernel build

```bash
# 1) obtain vanilla Linux 7.3.1
git checkout v7.3.1            # or unpack linux-7.3.1.tar.xz
# 2) apply the io_ext patch (adds block/iosched_ext, D2FQ, blk-mq bypass path)
patch -p1 < kernel/io_ext-linux-7.3.1.patch
# 3) configure & build
cp kernel/config-7.3.1-ioext .config
make olddefconfig
make -j"$(nproc)" bzImage modules
```

See `kernel/README.md` for details, and `tests/run_on_virtme.sh` for a
VM-based quick check of the policies.

## Userspace policies

```bash
cd userspace
make                    # build every BPF policy + loader against ../kernel/linux-7.3.1
make q_fifo arb_wdrr    # or a subset
```

Loaders attach per-`cgroup` local policies (`q_*`) and one global arbiter
(`arb_*`) through the kernel's `iosched_ext` struct_ops registration
(`echo scx_io > /sys/block/<dev>/queue/scheduler`). See `userspace/README.md`
and `userspace/PROTOCOL.md`.

## Benchmark campaigns

Each paper experiment has a self-contained directory under `bench/experiments/`
(configs + runbook + scripts). `bench/results/` holds the figure-rendering
scripts and per-figure documentation; per this artifact's scope, raw per-run
data, aggregated summary tables, and rendered figure files are intentionally
not included — regenerate the data by running the campaigns, then render
(e.g. `bash bench/results/d1/scripts/refresh_d1_figures.sh`).

The bench framework requires on the target machine: `fio` (3.41), an optional
RocksDB `db_bench`, YCSB, and TailBench/Shore binaries (referenced by config
paths, not bundled). Kernel-side requirements: the io_ext kernel built above,
cgroup v2 with the `io` controller delegated, and `bpftool` from the kernel
tree.

## License

Kernel-side code is GPL-2.0 (as Linux). Userspace components are GPL-2.0 as
well. See `LICENSE`.
