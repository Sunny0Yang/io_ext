# io_ext 用户态策略 CLI 协议规范

> **适用范围**：`userspace/io/loader_*` 与 `userspace/cache/build/*.out`  
> **设计原则**：仿照 sched_ext（scx_*）形态——**二进制即接口**，启动即加载，退出即卸载。  
> **目标**：每个 loader 是一个自包含、静态链接、可移植的二进制；共有参数名一致，策略特有参数以 `--long-flag` 暴露。  
> **最后更新**：2026-07-14

---

## 1. 生命周期语义（两种 ext 通用）

| 事件 | 行为 |
|------|------|
| **启动** | 解析参数 → open BPF skeleton → load → attach → 阻塞等待信号 |
| **SIGINT / SIGTERM** | 设置退出标志 → 优雅 drain（io: 等 queue_depth==0；cache: 直接 detach）→ detach link → destroy skeleton → exit 0 |
| **异常退出** | load/attach 失败时立即 destroy + exit 非 0 |
| **热切换** | 无显式命令。停旧 loader（SIGTERM）→ 起新 loader。内核 struct_ops link 自动替换。 |

退出码：`0` = 干净退出；`1` = 参数/加载/attach 错误。

**静态打包**：两个 Makefile 均以 `-static` + `libbpf.a` + 静态 libelf/zlib/libzstd 链接，产物可在任意带 BTF 的同架构内核上运行，无运行时依赖。

---

## 2. 共有参数

下列参数在 io 和 cache **所有** loader 中语义一致、拼写一致：

| Long | Short | 类型 | 默认值 | 说明 |
|------|-------|------|--------|------|
| `--cgroup` | `-c` | PATH | 见下 | 目标 cgroup 路径 |
| `--help` | `-h` | — | — | 打印用法并 exit 0 |
| `--verbose` | `-v` | flag | off | 启用 libbpf debug 输出（`bpf_set_print`） |

`--cgroup` 默认值：
- **io**：`/sys/fs/cgroup`（根 cgroup，单租户全局挂载）
- **cache**：**无默认，必填**（cache_ext 必须绑定到具体 memory cgroup）

---

## 3. io_ext 专属参数

### 3.1 所有 Queueing Policy（`loader_q_*`）共有

| Long | Short | 类型 | 默认 | 说明 |
|------|-------|------|------|------|
| `--arb-prio` | `-p` | u32 | 7 | Arbiter 优先级 |
| `--arb-weight` | `-W` | u32 | 100 | Arbiter 权重 |

### 3.2 Arbiter（`loader_arb_*`）

Arbiter **无必填参数**，仅接受 `--help` / `--verbose`。加载顺序约束：**先 Arbiter，后 Policy**。

### 3.3 策略特有参数

| Policy | 参数 | 类型 | 默认 | 说明 |
|--------|------|------|------|------|
| `q_mq_deadline` | `--merge-mode` | u32 (0-3) | 0 | D0..D3 merge 模式 |
| `q_kyber` | `--read-target-ns` | u64 | 2000000 | 读延迟目标 ns |
| `q_kyber` | `--write-target-ns` | u64 | 10000000 | 写延迟目标 ns |
| `q_kyber` | `--discard-target-ns` | u64 | 5000000000 | discard 延迟目标 ns |
| `q_kyber` | `--other-target-ns` | u64 | 5000000 | 其他 op 延迟目标 ns |
| `q_kyber` | `--merge-enable` | u32 (0/1) | 1 | 是否启用 merge |
| `q_merge_probe` | `--mode` | u32 (0-3) | 0 | DENY_ALL/ALLOW_ALL/ADJACENT_SAME_OP/COUNT_ONLY |

### 3.4 调用示例

```bash
# 单租户（无 arbiter），根 cgroup
./loader_q_fifo --cgroup /sys/fs/cgroup

# 带 arbiter 的多租户
./loader_arb_sp &
./loader_q_prio --cgroup /sys/fs/cgroup/workload_a --arb-prio 5 --arb-weight 200 &

# kyber 自定义延迟目标
./loader_q_kyber --cgroup /sys/fs/cgroup/db --read-target-ns 1000000 --merge-enable 0

# 热切换
kill -TERM $old_pid          # 旧 loader 优雅 drain + detach
./loader_q_bfq --cgroup /sys/fs/cgroup &   # 新策略自动替换
```

### 3.5 内核前置

```bash
echo scx_io > /sys/block/<dev>/queue/scheduler   # 启用 io-ext elevator
echo '+io' > /sys/fs/cgroup/cgroup.subtree_control  # cgroup v2 IO 委托
```

---

## 4. cache_ext 专属参数

### 4.1 所有策略共有

| Long | Short | 类型 | 默认 | 说明 |
|------|-------|------|------|------|
| `--watch-dir` | `-d` | DIR | **必填** | 监控目录（realpath ≤ 128 字节） |
| `--cgroup-size` | `-s` | BYTES | 0 | cgroup 内存上限；s3fifo 用于计算 cache_size，其余策略忽略 |

### 4.2 调用示例

```bash
# 基础
./cache_ext_fifo.out --cgroup /sys/fs/cgroup/cache_ext_test --watch-dir /tmp/watch

# s3fifo 指定 cache 大小
./cache_ext_s3fifo.out --cgroup /sys/fs/cgroup/db --watch-dir /data --cgroup-size 1073741824

# 热切换
kill -TERM $old_pid
./cache_ext_mru.out --cgroup /sys/fs/cgroup/cache_ext_test --watch-dir /tmp/watch &
```

### 4.3 内核前置

```bash
# cgroup v2 memory 控制器委托
echo '+memory' > /sys/fs/cgroup/cgroup.subtree_control
# 创建 cgroup 并设限
mkdir -p /sys/fs/cgroup/<name>
echo <bytes> > /sys/fs/cgroup/<name>/memory.max
# 旧接口（可选，新 per-cgroup attach 不需要）：
# echo -n "/<name>" > /proc/cache_ext_enabled_cgroup
```

---

## 5. 参数冲突与向后兼容

- io loader **旧 positional 接口**（`loader_q_fifo /sys/fs/cgroup 7 100`）在新版中**移除**，统一为 `--long-flag`。
- cache loader **旧下划线名**（`--watch_dir` / `--cgroup_path`）改为连字符（`--watch-dir` / `--cgroup`）。旧名**不保留**（调用方均在仓库内，一并更新）。
- 调用方（`tests/io/common.sh`、`tests/cache/*.sh`、`scripts/run_functional.sh`）同步更新。

## 6. 构建产物

```
userspace/io/build/loader_q_<policy>      # 静态 ELF
userspace/io/build/loader_arb_<arbiter>   # 静态 ELF
userspace/cache/build/cache_ext_<policy>.out  # 静态 ELF
```

两个 Makefile 的 `USER_CFLAGS` / `USERSPACE_CFLAGS` 均含 `-static`；`USER_LDFLAGS` / `USERSPACE_LDFLAGS` 使用 `libbpf.a -lelf -lz -lzstd`（静态库优先）。

---

## 7. 清理协议

### io_ext
```bash
# 优雅：kill -TERM 各 loader（触发 drain + detach）
# 强制：pkill -9 -f 'loader_arb_|loader_q_'
rm -rf /sys/fs/bpf/ioext_{policy_meta,active_cgroups,bfq_tenant_state,prio_config}
rm -rf /sys/fs/bpf/{merge_stats,merge_stats_map,probe_cfg,merge_cfg_map}
echo none > /sys/block/<dev>/queue/scheduler
```

### cache_ext
```bash
# 优雅：kill -TERM loader（触发 bpf_link__destroy + skeleton destroy）
# 强制：pkill -9 -f 'cache_ext_.*\.out'
rm -f /sys/fs/bpf/cache_ext/scan_pids   # 仅 get_scan
# 删除 cgroup：先 kill cgroup.procs 内进程，再 rmdir
rmdir /sys/fs/cgroup/<name>
echo 1 > /proc/sys/vm/drop_caches        # 可选
```

---

## 8. 故障排查速查

| 症状 | 首先检查 |
|------|----------|
| io: `-EBUSY` 加载失败 | `cleanup_policy.sh` 清僵尸 + BPF map pin |
| io: 调度器切换后卡死 | `echo none > .../scheduler` 回退，抓 dmesg |
| cache: loader 报 "Failed to open cgroup path" | cgroup 未创建或路径错 |
| cache: "watch_dir path too long" | realpath > 128 字节，换短路径 |
| 两端: BPF load 失败 | `/sys/kernel/btf/vmlinux` 存在？内核 `CONFIG_DEBUG_INFO_BTF=y`？ |
| 两端: 静态链接报缺 libelf.a | `apt install libelf-dev zlib1g-dev libzstd-dev` 提供静态库 |