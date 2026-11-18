# RUNBOOK — S-CHAOS on kvmhost（内核安全性混沌验证）

> 前置阅读：`EXPERIMENT_DESIGN.md`。本手册假设在工作站（ioext-bench 仓库）操作；
> kvmhost = hypervisor 虚机 `target-host`（127.0.0.1，user/ssh，sudo 密码见 provision 归档 ssh_env.sh）。

## 0. 状态检查（每次 campaign 前）

```bash
# hypervisor 侧：确认 kvmhost 分配与虚机健康（snapshot 见第 5 步）
hypervisor alloc list --output json | jq '.[] | select(.name|test("110"))'
hypervisor vm health --output json
# 机器侧：空闲、无残留策略、sdb 裸盘
ssh user@127.0.0.1 'uptime; pgrep -af "^loader_|fio"; lsblk -no MOUNTPOINT /dev/sdb'
```

## 1. 构建故障策略（工作站）

```bash
experiments/safety_chaos_kvmhost/scripts/sc_build_bm.sh
```
脚本做的事：把 `scripts/bpf/{q_inject_null,q_inject_stall}.{bpf.c,loader}` 拷入
`ioext/userspace/io/`、在 Makefile 的 `POLICIES` 追加两个策略（自动备份原 Makefile）、
`make q_inject_null q_inject_stall`、rsync loader 与 run 脚本到 `/opt/safety_chaos/`。

## 2. BTF 对齐冒烟（一次性，5 分钟）

部署内核是 7.3.1-ioext3+；注入策略为用户态 eBPF，不改内核，但必须与部署内核 BTF 兼容：

```bash
# 机器上 test-load 一次 q_fifo（若无 loader 二进制，先用工作站构建产物 rsync 过去）
rsync -az /opt/ioext-io_ext/userspace/io/build/loader_q_fifo \
      user@127.0.0.1:/tmp/
ssh user@127.0.0.1 "echo <sudo_pass> | sudo -S bash -c '
  echo scx_io > /sys/block/sdb/queue/scheduler; cat /sys/block/sdb/queue/scheduler;
  mkdir -p /sys/fs/cgroup/scx_probe; /tmp/loader_q_fifo -c /sys/fs/cgroup/scx_probe &
  sleep 2; kill %1; sleep 1;
  dmesg | grep \"teardown complete\" | tail -2;
  echo none > /sys/block/sdb/queue/scheduler; rmdir /sys/fs/cgroup/scx_probe'"
```
- 出现 `teardown complete policy='q_fifo' ...` ⇒ 对齐 OK，进入第 3 步。
- load 失败（BTF/kfunc 缺失）⇒ 切到内核源码的 7.3.1 io_ext 集成分支重建策略：
  `git -C /opt/ioext-io_ext/kernel/linux-7.3.1 checkout <7.3.1-integration-branch>` 后重跑第 1 步。

## 3. 冒烟 run（约 3 分钟）

```bash
experiments/safety_chaos_kvmhost/scripts/sc_launch_bm.sh --smoke
# 30s runtime、4G 盘、200ms 周期 q_fifo↔q_none，约 150 次切换
experiments/safety_chaos_kvmhost/scripts/sc_analyze_bm.py experiments/safety_chaos_kvmhost/results/safety_chaos_S1_r1_*
```
冒烟通过标准：SAFETY_REPORT 全 PASS（G5 provisional 门可暂不达标）、`teardown complete`
条数≈切换条数、fio rc=0。

## 4. 正式 campaign（按矩阵串行）

> 盘速标定（2026-11-19）：4k randwrite QD256 ≈ 9k IOPS、1M 顺序 ≈ 40MB/s。每臂 8G（写 ≈4min +
> verify ≈2–4min，单 run ≈10–12min，18 run ≈ 3–4h）。切换为串行语义，满积压单次排空 ~12s，
> 故 `--runtime-s 600`（≈40–70 次切换/run）。切换注入只覆盖写阶段。

```bash
S=experiments/safety_chaos_kvmhost/scripts
# S0 基线（不注入；本地策略常驻）
$L/sc_launch_bm.sh --arm S0 --rep r1 --size-g-total 8 --injector-args "--period-ms 100000000 --local-cycle q_fifo"
# S1 局部热切换（r3 加 --with-ftrace 拿排空轮次细节）
$L/sc_launch_bm.sh --arm S1 --rep r1 --size-g-total 8 --runtime-s 600
$L/sc_launch_bm.sh --arm S1 --rep r2 --size-g-total 8 --runtime-s 600
$L/sc_launch_bm.sh --arm S1 --rep r3 --size-g-total 8 --runtime-s 600 --with-ftrace
# S2 全局热替换
$L/sc_launch_bm.sh --arm S2 --rep r1 --size-g-total 8 --runtime-s 600 \
  --injector-args "--period-ms 100000000 --local-cycle q_fifo --arbiter arb_noop --arbiter-cycle arb_sp --arbiter-period-ms 1000"
# S3 空指针注入（每 3 次切换注入一次，mode 0..3 轮转）
$L/sc_launch_bm.sh --arm S3 --rep r1 --size-g-total 8 --runtime-s 600 \
  --injector-args "--period-ms 100 --local-cycle q_fifo,q_none --fault-null-every 3 --null-modes 0,1,2,3"
# S4 拒绝排空注入（每 5 次切换注入一次，200/900ms 交替）
$L/sc_launch_bm.sh --arm S4 --rep r1 --size-g-total 8 --runtime-s 600 --with-ftrace \
  --injector-args "--period-ms 100 --local-cycle q_fifo,q_none --fault-stall-every 5 --stall-ms-list 200,900"
# S5 混合混沌
$L/sc_launch_bm.sh --arm S5 --rep r1 --size-g-total 8 --runtime-s 600 \
  --injector-args "--period-ms 50 --local-cycle q_fifo,q_none --fault-null-every 3 --null-modes 0,1 --fault-stall-every 5 --stall-ms-list 200 --arbiter arb_noop --arbiter-cycle arb_sp --arbiter-period-ms 2000"
# … 其余 rep 同理；跑完统一分析：
experiments/safety_chaos_kvmhost/scripts/sc_analyze_bm.py experiments/safety_chaos_kvmhost/results/safety_chaos_*
```
每个 run 结束自动回拉产物到 `experiments/safety_chaos_kvmhost/results/<seq>/`
（events.csv / sampler.csv / dmesg_tap.log / kprobe_tap.log / fgraph.log / chaos.json / run_meta.json）。

## 5. D0 诊断臂（越界拒绝排空；**必须先打 snapshot**）

```bash
hypervisor snapshot create --output json   # 按 --help 传入 kvmhost 的 alloc/vm id
$L/sc_launch_bm.sh --arm D0 --rep r1 --size-g-total 2 --runtime-s 90 \
  --injector-args "--period-ms 100000000 --local-cycle q_fifo --fault-stall-every 1 --stall-ms-list 3000"
$L/sc_launch_bm.sh --arm D0 --rep r2 --size-g-total 2 --runtime-s 90 \
  --injector-args "--period-ms 100000000 --local-cycle q_fifo --fault-stall-every 1 --stall-ms-list 12000"
# 若机器 wedge：netconsole 日志已在工作站 results/netconsole_sink.log；然后
hypervisor snapshot rollback <snapshot_id> && hypervisor vm power on <vm>   # 按 CLI 实际语法
```
D0 预期（如实记录，不算 FAIL）：loader 打印 `graceful drain timeout, forcing detach`；
内核可能在 10s 处触发 `SCX_IO_MAX_EVAC_STALL_ROUNDS` 强制 teardown。若出现请求搁浅 →
这是**内核缺陷证据**（进 ioext/bugs 清单），恰好回应"拒绝排空死锁"的质疑边界。

## 6. 数据核对与出报告

```bash
experiments/safety_chaos_kvmhost/scripts/sc_analyze_bm.py experiments/safety_chaos_kvmhost/results/safety_chaos_*
```
- 人工抽查 3 件事：
  1. `dmesg_tap.log` 里抽 5 条 `teardown complete` 与 events.csv 同名事件逐一对应（seq、drained）；
  2. sampler.csv 的 inflight 峰值与切换事件时间对齐（stall 事件应见平台期，长度≈stall_ms）；
  3. fio chaos.json 的 verify 阶段确实执行（write 后有 read phase 记录）。
- 汇总行 `CAMPAIGN: n/n runs PASS, total_io=…, switches=…` 直接进 rebuttal 文本。

## 7. 故障排查

| 症状 | 处置 |
|---|---|
| loader 起不来（BTF/kfunc） | 见第 2 步切到 7.3.1 集成分支重编 |
| scx_io 写入失败 EBUSY | 有 I/O 未静：`cat /sys/block/sdb/inflight`，等待归零再写 |
| 注入器 `no teardown/free 事件` | 确认 kprobe tap 进程活着（kprobe_tap.log 增长）；检查 kprobe_events 里 ioext_spre/ioext_sfree 存在 |
| verify 报 `bad header` 且偏移远超写入区间 | 历史 run 遗留问题：确认 `--size` 未因整除变 0（run 脚本已加下限 1G；fio size=0 = 全盘） |
| stall 事件 e2e ≫ stall_ms | 先看 loader stderr（quiesce_wait_ms/rc），再对照 fgraph.log 的多轮 workfn 调用（20ms 节奏） |
| fio watchdog 触发 | 这就是 hung I/O 证据：保留现场（勿清理），netconsole + dmesg_tail 已落盘，按第 5 步 snapshot rollback |
| 与其它 campaign 撞车 | run 脚本 flock `/tmp/safety_chaos.lock`（后台持有进程模式，无 fd 继承）+ guard 会直接 fail-stop |
| 清理机器时误杀自己的 ssh | 远程 pkill 用防自匹配写法（如 `pkill -f 'sc_[r]un_chaos'`） |
| launcher 无声退出（exit 141） | `set -o pipefail` + 命令替换里的 `\| head -1` 会令上游收 SIGPIPE；命令替换内一律用 `awk 'NR==1'` 读全输入（脚本已修） |
