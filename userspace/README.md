# io-ext 用户态策略与 CLI 参考

> **协议规范**：[`../PROTOCOL.md`](../PROTOCOL.md)（io + cache 统一 CLI 契约）  
> **Merge 专题**：[`MERGE.md`](MERGE.md)  
> **最后更新**：2026-07-14

---

## 1. 设计理念

io-ext 采用 **eBPF struct_ops** 可插拔 IO 调度框架。每个 loader 是一个**自包含的静态二进制**，仿 sched_ext 形态：

- **启动即加载**：进程启动 → open BPF skeleton → load → attach → 阻塞等待信号
- **退出即卸载**：SIGINT/SIGTERM → 优雅 drain → detach → destroy → exit
- **热切换**：kill 旧 loader → 起新 loader，内核 struct_ops link 自动替换
- **静态打包**：`-static` + `libbpf.a`，产物可在任意带 BTF 的同架构内核运行

## 2. 架构

```
                    ┌─────────────────────────────────────┐
                    │         kernel: scx_io              │
                    │  (echo scx_io > .../queue/scheduler) │
                    └──────────┬──────────────┬───────────┘
                               │              │
                    ┌──────────▼──┐  ┌────────▼────────┐
                    │  Arbiter    │  │  Queueing Policy │
                    │  (arb_*)    │  │  (q_*)           │
                    │  控制面      │  │  数据面           │
                    │  多 cgroup  │  │  单 cgroup 排队   │
                    │  pick_cg    │  │  insert/dispatch │
                    └─────────────┘  └─────────────────┘
```

**加载顺序**：先 Arbiter（控制面），后 Policy（数据面）。

## 3. 构建

```bash
# 1) prepare the kernel tree: vanilla Linux 7.3.1 + kernel/io_ext-linux-7.3.1.patch
#    (place it at ../kernel/linux-7.3.1, or point KERNEL_SRC elsewhere)
# 2) build the BPF policies + loaders against that tree
cd userspace
make                # all policies/arbiters/loaders
make q_fifo arb_wdrr   # a subset
```

产物：`userspace/io/build/loader_q_*`、`userspace/io/build/loader_arb_*`（静态 ELF）

```bash
file userspace/io/build/loader_q_fifo
# ELF 64-bit LSB executable, statically linked
```

## 4. 策略清单

### Queueing Policies（数据面）

| Loader | 额外参数 | 说明 |
|--------|---------|------|
| `loader_q_fifo` | — | FIFO 基线 |
| `loader_q_prio` | — | 优先级队列 |
| `loader_q_none` | — | bypass（直接 passthrough） |
| `loader_q_wedf` | — | 加权 EDF |
| `loader_q_sp_edf` | — | SP-EDF |
| `loader_q_bfq` | — | BFQ 风格 |
| `loader_q_mq_deadline` | `--merge-mode U32` | mq-deadline + merge hook (D0-D3) |
| `loader_q_kyber` | `--read-target-ns` 等 | Kyber + 延迟目标调优 |
| `loader_q_merge_probe` | `--mode U32` | merge 探针（测试专用） |

### Arbiters（控制面）

| Loader | 说明 |
|--------|------|
| `loader_arb_sp` | 静态优先级选 cgroup |
| `loader_arb_mix` | 混合策略 |
| `loader_arb_bfq` | BFQ 配对（需与 q_bfq 一起用） |

## 5. CLI 参数参考

### 5.1 共有参数（所有 `loader_q_*`）

| Long | Short | 类型 | 默认 | 说明 |
|------|-------|------|------|------|
| `--cgroup` | `-c` | PATH | `/sys/fs/cgroup` | 目标 cgroup |
| `--arb-prio` | `-p` | u32 | 7 | Arbiter 优先级 |
| `--arb-weight` | `-W` | u32 | 100 | Arbiter 权重 |
| `--help` | `-h` | — | — | 打印用法并退出 |
| `--verbose` | `-v` | flag | off | 启用 libbpf debug 输出 |

### 5.2 Arbiter 参数

Arbiter 仅接受 `--help/-h` 和 `--verbose/-v`，无其他参数。

### 5.3 策略特有参数

#### q_mq_deadline

```
--merge-mode U32    D0=0, D1=1, D2=2, D3=3（默认 0）
```

#### q_kyber

```
--read-target-ns U64       读延迟目标（默认 2000000）
--write-target-ns U64     写延迟目标（默认 10000000）
--discard-target-ns U64   discard 延迟目标（默认 5000000000）
--other-target-ns U64     其他 op 延迟目标（默认 5000000）
--merge-enable U32         是否启用 merge（默认 1）
```

#### q_merge_probe

```
--mode U32    0=DENY_ALL, 1=ALLOW_ALL, 2=ADJACENT_SAME_OP, 3=COUNT_ONLY（默认 0）
```

### 5.4 用法示例

```bash
# 查看帮助
./loader_q_kyber --help

# 单租户（无 arbiter），根 cgroup
./loader_q_fifo --cgroup /sys/fs/cgroup

# 带 arbiter 的多租户
./loader_arb_sp &
./loader_q_prio --cgroup /sys/fs/cgroup/workload_a --arb-prio 5 --arb-weight 200 &

# kyber 自定义延迟目标
./loader_q_kyber --cgroup /sys/fs/cgroup/db --read-target-ns 1000000 --merge-enable 0

# mq_deadline 指定 merge 模式
./loader_q_mq_deadline --cgroup /sys/fs/cgroup --merge-mode 2
```

## 6. 生命周期

```
启动
  │
  ├─ parse args (getopt_long)
  ├─ skeleton_open()
  ├─ setup_policy_maps()          # 确保/reuse BPF pinned maps
  ├─ [policy-specific map setup]  # kyber: rodata; mq_dd: merge_cfg_map; etc.
  ├─ bpf_map__set_map_flags(BPF_F_LINK)
  ├─ skeleton_load()
  ├─ register_policy()            # 写 policy_meta + active_cgroups
  ├─ attach_and_run()
  │    ├─ populate struct_ops value
  │    ├─ BPF_LINK_CREATE          # attach to cgroup
  │    └─ while(!stop) sleep(1)   # 阻塞等待信号
  │
  │ SIGINT/SIGTERM
  ├─ mark_draining()
  ├─ wait_quiesced()              # 优雅 drain（queue_depth==0）
  ├─ link_detach()
  ├─ unregister_policy()
  └─ skeleton_destroy()
```

## 7. 内核前置

```bash
# 启用 io-ext elevator
echo scx_io > /sys/block/nvme0n1/queue/scheduler

# cgroup v2 IO 委托
echo '+io' > /sys/fs/cgroup/cgroup.subtree_control

# 验证
cat /sys/block/nvme0n1/queue/scheduler
# [scx_io] none mq-deadline kyber
```

VM 内首次初始化用 `scripts/vm_init.sh`。

## 8. 热切换

io-ext 没有显式 "switch" 命令——struct_ops link 自动替换：

```bash
# 当前 q_fifo 运行中（pid=$P1）
kill -TERM $P1          # 旧 loader 优雅 drain + detach
# 等待退出（dmesg 可见 detach）
./loader_q_bfq --cgroup /sys/fs/cgroup &   # 新策略自动替换
```

测试脚本 `tests/io/run_all_policies_ycsb.sh --hot-switch-only` 在 YCSB 高负载下自动执行此流程。

## 9. BPF Map Pin Paths

| Pin 路径 | 类型 | 用途 |
|----------|------|------|
| `/sys/fs/bpf/ioext_policy_meta` | HASH | cgroup_id → policy_meta（queue_depth, arb_prio, owner_token） |
| `/sys/fs/bpf/ioext_active_cgroups` | ARRAY | 活跃 cgroup_id 列表（64 slot） |
| `/sys/fs/bpf/ioext_bfq_tenant_state` | HASH | BFQ 租户状态（q_bfq/arb_bfq 专用） |
| `/sys/fs/bpf/merge_stats_map` | — | mq_deadline merge 统计 |
| `/sys/fs/bpf/merge_cfg_map` | — | mq_deadline merge 配置 |
| `/sys/fs/bpf/kyber_merge_cfg` | — | kyber merge 配置 |
| `/sys/fs/bpf/merge_stats` | — | merge_probe 统计 |
| `/sys/fs/bpf/probe_cfg` | — | merge_probe 模式配置 |

## 10. 清理

```bash
# 优雅：逐个 kill -TERM（触发 drain + detach）
# 强制：
pkill -9 -f 'loader_arb_|loader_q_'
rm -rf /sys/fs/bpf/ioext_{policy_meta,active_cgroups,bfq_tenant_state,prio_config}
rm -rf /sys/fs/bpf/{merge_stats,merge_stats_map,probe_cfg,merge_cfg_map,kyber_merge_cfg}
echo none > /sys/block/nvme0n1/queue/scheduler
```

一键脚本：`scripts/cleanup_policy.sh [nvme0n1]`

## 11. 测试

```bash
# 宿主机：构建 + 启动 VM + YCSB 全策略
./tests/io/run_on_virtme.sh
./tests/io/run_on_virtme.sh --stable-only
./tests/io/run_on_virtme.sh --policy q_fifo

# VM 内直接运行
./tests/io/run_all_policies_ycsb.sh --stable-only
./tests/io/run_all_policies_ycsb.sh --hot-switch-only
```

## 12. 故障排查

| 症状 | 检查 |
|------|------|
| `-EBUSY` 加载失败 | `cleanup_policy.sh` 清僵尸 + BPF map pin |
| 调度器切换后卡死 | `echo none > .../scheduler` 回退，抓 dmesg + vmcore |
| loader 启动即退出 | `--verbose` 查看详细错误；确认 scx_io 已激活 |
| BPF load 失败 | `/sys/kernel/btf/vmlinux` 存在？内核 `CONFIG_DEBUG_INFO_BTF=y`？ |
| 静态链接缺库 | `apt install libelf-dev zlib1g-dev libzstd-dev`（提供静态库） |
| dmesg `hung_task` | 见 [`io-ext-scheduler-dev` skill debugging 参考](../../.agents/skills/io-ext-scheduler-dev/references/debugging.md) |

## 13. 文件索引

| 文件 | 用途 |
|------|------|
| [`ARB_POLICY_DEV_GUIDE.md`](ARB_POLICY_DEV_GUIDE.md) | **arbiter 策略开发指导（dev skill）**：框架契约、DRR 核心、rig 要求、易错点、CI 闭环 |
| `ioext_cli_args.h` | 统一 CLI 参数解析（getopt_long） |
| `ioext_loader_common.h` | 共享生命周期：map setup, register, attach, drain, detach |
| `ioext_arb_common.h` | WDRR/SLO/COST 共享 DRR 核心（round 语义、charge/refund、pick 遥测） |
| `ioext_policy_shared.h` | BPF/userspace 共享结构：policy_meta, request_desc, kyber 配置 |
| `ioext_bpf_common.h` | BPF 侧宏：note_insert/dispatch/drain, SIMPLE_CGROUP_DISPATCH |
| `Makefile` | 构建：POLICIES, ARBITERS, vmlinux.h, skeleton, 静态链接 |
| `loader_q_*.c` | 策略 loader 入口（9 个策略 + heimdall_v2） |
| `loader_arb_*.c` | arbiter loader 入口（含 wdrr/slo/cost） |
| `q_*.bpf.c` | BPF 策略程序 |
| `arb_*.bpf.c` | BPF arbiter 程序 |
| `tests/io/arb_wdrr_model.py` | arb_wdrr 参考模型（算法 spec，host 可跑自测） |
| `tests/io/arb_wdrr_accept.sh` | arb_wdrr 验收测试（VM 内 12 项断言） |
| `tests/io/arb_wdrr_ci.sh` | host 一键 CI：以正确 rig 启动 VM 并跑验收，exit 0 = PASS |