# Kernel-side artifact

`kernel/io_ext-linux-7.3.1.patch` is a single, self-contained patch that
turns **vanilla Linux 7.3.1** into the paper's kernel (flavor
`-ioextd2fq`). It contains:

| Piece | Files | What it adds |
|---|---|---|
| BPF struct_ops lifecycle hooks | `kernel/bpf/bpf_struct_ops.c` | per-link reg/unreg ctx callbacks needed for safe policy detach |
| blk-mq bypass & scheduler hooks | `block/blk-mq.c`, `block/blk-mq.h`, `block/blk-sysfs.c`, `block/blk-merge.c`, `block/elevator.c/h`, `block/blk-cgroup.c/h`, `block/blk.h` | request bypass path for external I/O schedulers, `allow_merge` ctx, arbiter lifecycle hooks |
| **iosched_ext framework** | `block/iosched_ext.c`, `block/iosched_ext.h` | the eBPF-programmable I/O scheduler (`scx_io`): N:1 model, request custody state machine, per-hctx native fallback FIFO, four-phase RCU quiescence protocol, telemetry kfuncs |
| KUnit test | `block/blk-merge-test.c` | plug-merge/bypass interaction test |
| D2FQ baseline port (FAST'21) | `block/d2fq.c/h`, `block/gholder.c/h`, `include/linux/d2fq.h`, `drivers/nvme/host/*` | native D2FQ kernel port (device-direct virtual-time fair queueing) used as a baseline |
| misc plumbing | `include/linux/{blkdev,blk-mq,bpf,sched,nvme,d2fq,fcntl}.h`, `fs/{aio,fcntl,read_write,sync}.c`, `kernel/{exit,fork}.c` | small hooks enabling the above |
| selftests | `tools/testing/selftests/iosched_ext/` | bio-merge, bypass, teardown/evacuate, TOCTOU capture scripts |
| localversion | `localversion` | `-ioextd2fq` kernel flavor suffix |

## Build

```bash
# vanilla Linux 7.3.1
git checkout v7.3.1            # or unpack linux-7.3.1.tar.xz
patch -p1 < /path/to/kernel/io_ext-linux-7.3.1.patch
cp /path/to/kernel/config-7.3.1-ioext .config
make olddefconfig
make -j"$(nproc)" bzImage modules
# optional Debian package (as used on the test machines)
make bindeb-pkg -j"$(nproc)"
```

The config in `config-7.3.1-ioext` enables the io controller (cgroup v2),
NVMe, BTF (`CONFIG_DEBUG_INFO_BTF=y` — required by the userspace loaders),
`IOSCHED_EXT`, and kunit pieces. `CONFIG_LOCALVERSION="-ioext1"` in the
shipped config is a build stamp; the `localversion` file in the patch makes
the final release string `7.3.1-ioextd2fq`.

## Smoke test

After booting the kernel:

```bash
cat /sys/block/nvme0n1/queue/scheduler     # shows ... [scx_io] once a loader is attached
# run the in-tree selftests
cd tools/testing/selftests/iosched_ext && make run_tests
# or run the userspace policy test suite (virtme-based)
../tests/run_on_virtme.sh                  # from the repository root
```
