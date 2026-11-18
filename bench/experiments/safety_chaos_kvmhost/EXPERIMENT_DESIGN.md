# EXPERIMENT: S-CHAOS — scx_io 策略热切换 + 故障注入下的内核 I/O 安全性验证（kvmhost）

> 目标读者：质疑本工作内核安全性的审稿人 / 答辩委员。
> 一句话结论预期：**在高并发 fio（QD=64×4 jobs）持续负载下，以 50–100 ms 周期热切换局部/全局策略并注入
> "空指针返回"与"拒绝排空（有界死锁）"两类故障，全部在途 I/O 100% 正常完成（0 丢失 / 0 损坏 / 0 卡死 / 0 hung
> task / 0 panic），单次策略静默+排空切换延迟（内核侧）处于数十微秒~数毫秒量级，故障注入仅在声明的时间窗内
> 使延迟受限膨胀、绝不丢请求。**
>
> 状态：DESIGN v1（2026-11-19）。前置机制核对已完成（见 §10 preflight 记录）。
> 本实验为**新增补充实验**（rebuttal 用），不修改任何已冻结的历史实验（freeze-4/5 等）。

---

## 1. 背景：要回应的质疑与对应的命题

| 质疑 | 需要的数据证据 | 对应实验臂 |
|---|---|---|
| "热切换期间在途 I/O 会丢/损坏/卡死吗？" | 数百万次 I/O 跨越切换窗口，0 丢失 0 损坏 0 卡死 | S1/S3/S5 |
| "排空逻辑被恶意/故障策略拒绝时会怎样？"（拒绝排空的死锁策略） | 有界拒绝：内核 20ms 轮询排空循环持续工作，窗口后全部请求归零式排空；超出优雅窗口后强制 teardown 不 panic | S4 / D0 |
| "策略异常返回（空指针/错误码）会怎样？" | 内核 fallback/bypass 路径 100% 兜底完成 | S3 |
| "切换延迟到底多大？" | 每次切换的静默+排空延迟分布（p50/p90/p99/max），内核侧与端到端分离 | 全部臂 |
| "全局策略（仲裁器）热替换安全吗？" | arbiter RCU 替换下 0 丢失 | S2 |

**命题 C1（完整性/活性）**：混沌运行期间，fio 报告的已提交 I/O 与已完成 I/O 完全相等，全量 verify 通过；
内核日志无 BUG/WARN/oops/panic/lockdep/hung-task；块层 in-flight 永不越过 nr_requests 上界、切换后归零。
**命题 C2（切换延迟）**：非故障切换的内核侧排空时长 p99 < 5 ms、端到端 p99 < 10 ms（初值，跑完 S0/S1 后
以实测收紧为正式门槛）；stall 故障臂延迟 ≈ 声明的拒绝窗长 + 排空时长，且全程无 hung task。

## 2. 被测系统与已核对的内核机制（证据基础）

- 平台：hypervisor 虚机 **kvmhost**（`target-host`，127.0.0.1，ssh `user`，sudo 可用），内核
  **7.3.1-ioext3+**，fio **3.41**，实验盘 **/dev/sdb 96G（裸盘，未挂载）**。
- 调度器：`echo scx_io > /sys/block/sdb/queue/scheduler`（三种盘中 sdb 当前为 `none`，scx_io 在支持列表中）。
- 内核侧策略树（`block/iosched_ext.c`）：
  - **全局策略** = arbiter（`current_arbiter`，RCU 替换，用户态 `loader_arb_*`）；
  - **局部策略** = per-cgroup q policy（`blkcg->iosched_policy`，用户态 `loader_q_*` attach 到 cgroup）。
  - **切换 = 杀旧 loader → 起新 loader**（eBPF struct_ops link 自动替换；`ioext_loader_common.h`
    的 detach 序列：mark draining → `bpf_link_detach` → wait quiesced（20ms 轮询，上限
    `IOSCHED_LOADER_GRACEFUL_DRAIN_MS=1000ms`）→ close）。
- 排空机制（已逐行核对）：
  - detach 触发 `scx_io_policy_prepare_teardown`（`noinline`，kprobe 可达）→ `teardown_workfn`（delayed
    work）以 **20ms 一轮**的循环调用 `ops->evacuate` 归还请求，全部走 `SCX_IO_BYPASS_QUIESCED` 旁路直通
    blk-mq 完成，并同步 arbiter 台账；
  - `ops->evacuate` 持续返回"无请求"时 `stall_rounds++`，**`SCX_IO_MAX_EVAC_STALL_ROUNDS=500`（≈10s）
    后强制 teardown**（防死锁兜底）；
  - **attach 时校验** `!ops->insert_request || !ops->evacuate` 直接拒绝（空指针 ops 在入口即被拦截）；
    `dispatch/evacuate` 全部调用点均有判空守卫（iosched_ext.c:1109/1551/1850/3164/3447）。
- 观测面（无需改内核，**均已在部署内核上实测可用**）：
  - **kprobe 双探针**（部署内核无 always-on 的 per-event 日志，见 §10；kprobe 是主观测通道）：
    `p:ioext_spre scx_io_policy_prepare_teardown`（= 静默开始）与
    `p:ioext_sfree scx_io_policy_free`（= 排空完成、策略释放）。逐事件给出
    `T_drain_kernel = t(sfree) − t(spre)`、`T_switch_e2e = t(sfree) − t(SIGTERM)`；
  - **meta map 轮询**：`bpftool map dump pinned /sys/fs/bpf/ioext_policy_meta` 中本 cgroup 条目消失
    = 排空完成（与 loader 内 `wait_policy_quiesced` 同语义，实测 detach 后条目清空）；
  - **stall loader stderr**：自打印 `stall_ms=… detach_to_kill_ms=… quiesce_wait_ms=… rc=…`
    （实测 500ms/9000ms 两档行为正确）；
  - `IOEXT_DEBUG=0` 门控的 dbg 日志（含 `teardown complete`、evacuate stall 警告）在部署内核不可用
    → 可选的 ftrace function_graph（`scx_io_policy_teardown_workfn`，符号在 kallsyms 可见）补充
    排空轮次细节；BUG/WARN/oops/panic 等系统级日志恒开（G2 证据不受影响）；
  - 块层 `/sys/block/sdb/inflight`、`/sys/block/sdb/stat`、`nr_requests`。

## 3. 实验拓扑与负载

- 单机（kvmhost）裸盘负载，**不走嵌套 VM tenant rig**：fio 直接写 /dev/sdb，进程放进专用 cgroup
  `/sys/fs/cgroup/scx_safety`，q 策略 attach 到该 cgroup（= 局部策略），arbiter attach 为全局。
  这样 QD 与注入节奏完全可控，且与 dynamic_tenant campaign 共享同一条内核切换路径。
- fio 形态（**size 定长、非 time_based**，理由见 §7.1）：
  - 4 jobs × `iodepth=64`（总 QD=256）、`bs=4k`、`direct=1`、`rw=randwrite`、每 job 2G（合计 **8G**）；
  - `--verify=crc32c --verify_backlog=16384 --verify_async=2 --verify_fatal=1 --randrepeat=0`：
    写阶段内联抽样校验 + 写完后 fio 自动进入 verify 阶段**全量重读校验**（见 §7.1 语义论证）；
  - **不预写全盘**（盘速实测见 §10：~40MB/s 顺序 / ~9k IOPS，全盘预写要 40 分钟且不必要）——丢失写
    由 fio verify header 机制检出（`randrepeat=0` 保证跨 run 种子不同，旧内容不可能携带有效 header）。
- **盘速标定（2026-11-19 实测）**：4k randwrite QD256×4 jobs ≈ **9k IOPS（36MB/s）**；1M 顺序写 QD32 ≈
  **40MB/s**。据此单 run 写阶段 ≈ 8G/9k IOPS ≈ 4 分钟 + verify 阶段 ≈ 2–4 分钟，单 run 全程 6–9 分钟。
- 量级核算：单 run 8G/4k = **2.1M 次 I/O**；整个 campaign（6 门控臂 × 3 rep + D0 诊断）合计 **≈ 40M I/O、
  ≥ 3 万次热切换**——满足"数百万次在途 I/O 100% 完成"的量级要求且 campaign 总时长可控（≈2.5–3h）。

## 4. 故障注入设计（两类"恶意"策略 + 全局热替换）

策略热切换的实现即"杀 loader → 起新 loader"，注入器按计划串行驱动（同一 cgroup 同一时刻至多一个策略
在切换），事件全程记录时间戳。新增两个 eBPF 故障策略（`scripts/bpf/`，基于 q_fifo 模板）：

### 4.1 F-NULL：`q_inject_null` —— 模拟"返回空指针/异常返回值"

控制字（BPF ARRAY map，loader CLI `--mode N`）：

| mode | 行为 | 命中的内核路径 |
|---|---|---|
| 0 | `insert_request` 一律返回 `IOSCHED_INSERT_DEFER_TO_PARENT` | parent/root 兜底路径 |
| 1 | `insert_request` 一律返回 `-1`（拒绝） | `fallback_enqueue` + `bypassed_policy_rejected` 计数 |
| 2 | `cgroup_dispatch` 一律返回 0（NULL 派发）；insert 全拒绝 | dispatch 调用点判空守卫（:3164） |
| 3 | `evacuate` 一律返回 0（NULL 归还）；insert 全拒绝 | teardown evacuate 判空守卫（:1551/:3447） |

**安全不变式（在 BPF 内强制）**：mode 2/3 下 insert 永不 ACCEPT，队列永不持有请求 → 任何空指针返回
都不可能搁浅请求。注入器注入前先读内核 `:1850` 的 attach 校验行为（空 evacuate ops 直接 attach 失败）
作为**静态安全性质证据**记入报告。

### 4.2 F-STALL：`q_inject_stall` —— 模拟"拒绝排空的死锁策略"（有界）

- 正常期行为 = q_fifo（accept → queue map → dispatch）。
- loader 收到 SIGTERM 后：**先置"拒绝窗"** `[now, now+stall_ms)`（`cgroup_dispatch` 与 `evacuate`
  全部返回 0 = 拒绝派发/拒绝归还），**睡 0.6×stall_ms 让在途请求堆积**（QD 背压自然限流，队列上限
  4096，溢出插入走内核 fallback，不可能搁浅），再 mark draining → `bpf_link_detach` → wait quiesced。
- 内核侧体验：detach 后 `teardown_workfn` 反复 evacuate 被拒 → 每 20ms 一轮重试（死锁被周期性探测）
  → 窗口到期 → 一次性排空全部积压 → kprobe 观测到 `scx_io_policy_free`（排空完成）。
- 分级（对应"优雅排空等待上限 1000ms"这一 loader 侧常量）：
  - `stall_ms=200 / 900`：处于优雅窗口内（**门控臂**，要求 0 丢失 0 卡死）；
  - `stall_ms=3000`：越过优雅窗口 → loader 打印 "forcing detach"（**诊断臂 D0**：要求不 panic 不卡死
    内核，剩余请求结局如实记录——若此处暴露搁浅/丢失，即为需要修复的内核缺陷，进 bugs 清单）；
  - `stall_ms=12000`：逼近内核 `500×20ms=10s` 强制 teardown 线（**诊断臂 D0**，hypervisor snapshot 保护）。
- 硬上限：loader 拒绝 `--stall-ms > 15000`。

### 4.3 全局热替换（S2）

`loader_arb_noop ↔ loader_arb_sp` 每秒交替 kill/spawn；替换窗口内 `current_arbiter` 为空时内核
fallback（`bypassed_no_policy`）兜底。q 侧常驻 `q_fifo` 使数据面持续有流量。

## 5. 实验矩阵（每臂 size=8G，每臂 3 rep，种子不同；串行执行，臂间 fstrim 无需——裸盘）

| 臂 | 局部切换 | 全局切换 | 故障注入 | 预期切换次数/run | 验证目标 |
|---|---|---|---|---|---|
| S0 基线 | 无（常驻 q_fifo） | 无 | 无 | 0 | 完整性基线 + IOPS 标定 |
| S1 局部热切换 | 目标周期 100ms，q_fifo→q_none→q_wedf 循环 | 无 | 无 | ≈40–70（受排空时长约束，见 §6.2） | C1+C2 主臂 |
| S2 全局热替换 | 常驻 q_fifo | 目标周期 1s，arb_noop↔arb_sp | 无 | ≈15–30 | 全局路径 C1 |
| S3 空指针注入 | 目标周期 100ms，q_fifo→q_none；**每 3 次**切 q_inject_null（mode 0↔1↔2↔3 轮转） | 无 | ≈12–20 | fallback 路径 C1 |
| S4 拒绝排空注入 | 目标周期 100ms，q_fifo→q_none；**每 5 次**切 q_inject_stall（stall 200ms↔900ms 交替） | 无 | ≈8–14（stall 事件） | 排空死锁 C1+C2 |
| S5 混合混沌 | 目标周期 50ms；每 3 次切 null(mode 轮转)、每 5 次切 stall(200ms) | 周期 2s | 全部 | ≈40–70 | 极限混合 C1+C2 |
| D0 诊断（可选） | 单事件短 run（size=2G）：stall 3000ms、stall 12000ms 各一次 | 无 | 2 | 强制 teardown 行为记录（须 hypervisor snapshot 保护） |

（每臂 size=8G、每臂 3 rep、串行执行；**切换为串行语义**——上一次排空观测完成后才发起下一次，
实际周期 = max(目标周期, T_drain)，事件表如实记录。在 kvmhost 慢盘满积压下单次排空 ~12s，故
S 臂 runtime 取 600s 以积累 40+ 次切换；每次切换 evacuate 的在途请求可至队列上限 4096。）
切换总当量（3 rep，runtime 600s）：≈ **600–900 次热切换 / ≈100 次 null 注入 / ≈25 次 stall 注入**；
经切换窗口的在途请求当量 ≈ 700 × 数百~4096 ≈ **数十万~数百万次**（逐事件计入 kprobe 观测），
叠加 campaign 总 I/O ≈ **3700 万次**。若需更大切换量，可加长 `--runtime-s` 或在 bm 裸机 NVMe
（高速盘，排空为毫秒级）上复跑同矩阵。

## 6. 指标定义与采集方案

统一时间基准：所有采集端同时记录 `CLOCK_MONOTONIC` 与 `/proc/uptime`（boot 时钟）；dmesg 的
`[ss.us]` 亦为 boot 时钟，三者可精确对齐（无 suspend 场景下 sched_clock ≡ CLOCK_MONOTONIC ≡
CLOCK_BOOTTIME，偏差仅常数项，报告以首事件互校）。

### 6.1 完整性/活性指标（命题 C1）

| 指标 | 定义 | 来源 | 采集 |
|---|---|---|---|
| I/O 提交/完成/错误 | 每 job 的 submitted vs completed、error 计数 | fio JSON（`jobs[].total_ios`、`error`、`short_io*`） | 每 run |
| 数据损坏 | verify 错误数（crc32c 全量 + 内联 backlog） | fio（`verify_fatal=1` 使任何损坏即时致命） | 每 run |
| 丢失检测 | 丢失的写 = verify 重读到无本次 run 有效 header 的旧内容（`randrepeat=0` 保证跨 run 种子不同） | fio verify 阶段 | 每 run |
| in-flight 轨迹 | `read+write in_flight`，20Hz | `/sys/block/sdb/inflight` | `sc_sampler_bm.sh` |
| in-flight 越界 | max > nr_requests 或切换后不归零（>5s 平台，声明的 stall 窗除外） | 采样 + 事件表 join | 分析器 |
| hung I/O / hung task | `hung_task_timeout_secs=30`（run 内临时调低，run 后还原）下的内核报告 | dmesg tap | 全程 |
| 内核崩溃/异常 | BUG|WARNING|Oops|panic|lockdep|list corruption|slab corruption|use-after-free|KASAN|rcu stall | dmesg tap（+ netconsole 兜底） | 全程 |
| 排空完成事件计数 | 每切换事件的 kprobe `scx_io_policy_free` 到达数（应 1:1 对应切换数）；stall loader 的 quiesce_wait 作旁证 | kprobe tap + 事件表 | 全程 |
| fallback 统计 | `fallback bypass summary total/no_policy/unavailable/rejected/quiesced` | dmesg tap | detach 时 |
| 机器活性 | uptime 连续、无 reboot、盘热插拔事件 | `uptime -s` 前后对比 | 每 run |

### 6.2 切换延迟指标（命题 C2）

| 量 | 定义 | 来源 | 粒度 |
|---|---|---|---|
| `T_to_detach` | SIGTERM → loader 退出（detach 发起） | 注入器事件表（boot 时钟） | 每事件 |
| `T_drain_kernel` | kprobe `scx_io_policy_prepare_teardown`（静默开始）→ `scx_io_policy_free`（排空完成）；**同钟配对，免受 VM suspend 时钟偏差影响** | kprobe tap | 每事件 |
| `T_switch_e2e` | `T_to_detach + T_drain_kernel`（分解求和，准确到 loader detach 的 ~2ms 内） | 注入器计算 | 每事件 |
| `T_detach_meta` | SIGTERM → meta map 中本 cgroup 条目消失（旁证，应 ≈ T_switch_e2e） | bpftool 轮询 | 每事件 |
| `T_loader_quiesce` | 自定义 stall loader 打印的 wait_policy_quiesced 实际耗时 | loader stderr | stall 事件 |

**实测校准（2026-11-19 smoke，kvmhost 慢盘 ~9k IOPS，QD256 满载）**：
- 空载/小积压策略 teardown：prepare→free ≈ **2–5ms**（q_none 直通策略满载下也仅 ~2ms）；
- **持有满积压的策略（q_fifo，BPF 队列 4096 满）满载 teardown ≈ 12s**（≈ 积压数 × 每请求
  evacuate+派发的设备时延）——切换延迟与积压强相关，且被严格有界、串行化；
- 满载下 4 次真实切换 + 4×1G 写 + 全量 verify：**fio rc=0、0 丢失 0 损坏 0 hung**。

对"数十微秒~几毫秒"主张的口径：该量级对应**小积压/快设备**（bm 裸机 NVMe、空载路径）；本实验
如实报告 `T_drain = f(积压, 设备)` 的关系曲线与各场景实测值，并以"0 丢失 0 卡死 + 延迟有界可解释"
作为安全性结论——不虚报常数延迟。

（部署内核 7.3.1-ioext3+ #12 将 per-event 排空日志门控在 IOEXT_DEBUG 下，kprobe 为主观测通道；
若未来换装 d2fq-native 分支内核（已含 always-on `teardown complete ... drained=N` 日志），可加用
dmesg join 通道交叉验证。）

统计口径：按臂 × 事件类别（normal/null/stall）输出 N、p50/p90/p99/max、直方图与 CDF 图
（`sc_analyze_bm.py` 产出 PNG）。**注意**：`T_switch_e2e` 含用户态 loader 的优雅排空等待与 BPF
detach 开销（上界），`T_drain_kernel` 为纯内核排空（下界）——报告两者并明确标注，避免口径混淆。

### 6.3 采集器一览（全部落盘到 `/opt/safety_chaos/results/<seq>/`，run 后 rsync 回工作站）

| 采集器 | 内容 | 频率/方式 |
|---|---|---|
| `sc_injector_bm.py` | 切换事件表 CSV（kind/from/to/fault/t_issue/t_exit/t_pre/t_free/t_meta_clean/e2e_ms/drain_kernel_ms/...） | 事件驱动 |
| `sc_sampler_bm.sh` | inflight、/sys/block/sdb/stat、nr_requests、memfree、loadavg | 20Hz |
| kprobe tap | `ioext_spre`/`ioext_sfree` 原始事件流（trace_pipe 捕获） | 全程 |
| dmesg tap | `dmesg --follow` 全量内核日志（G2 证据 + panic 现场） | 全程 |
| fgraph tap（可选） | function_graph `scx_io_policy_teardown_workfn` 排空轮次细节 | `--with-ftrace` |
| fio | JSON 输出 + bw/lat log | 每 run |
| netconsole | 内核日志 UDP 兜底（SSH 卡死时仍可取回 panic 现场） | 全程 arm，run 后 disarm |

## 7. 方法论要点（预答辩 Q&A）

### 7.1 为什么 fio verify 能证明"0 丢失、0 损坏"
- `--verify=crc32c` + size 定长 randwrite + fio 默认 randommap：每 job 对其 2G 区间**每块恰写一次**，
  写阶段结束后 fio 自动进入 verify 阶段**重读全部已写块**：每块带 fio 写入时嵌的 verify header
  （种子 + CRC）。
- "损坏"：内容被改 → CRC 失配 → fio 报错退出（`verify_fatal=1`）。
- "丢失"：写请求丢失/被吞 → verify 重读到旧内容 → **旧内容不含本次 run 的有效 verify header
  （`--randrepeat=0` 保证每次 run 种子不同，历史内容不可能通过 header 校验）** → 判 FAIL。
- "卡死"：请求永不完成 → fio 卡死在写阶段（watchdog 超时判 FAIL）或 in-flight 采样平台期 → 直接暴露。
- 因此 fio 退出码 0 + JSON error==0 + in-flight 轨迹归零 ⇒ **三者互证的 0 丢失/0 损坏/0 卡死**。
  （原设计的全盘 1M 预写因盘速标定后不必要且过于昂贵——40 分钟/run——已移除，默认 `--prefill-s 0`，
  需要时可选配限时长预写。）

### 7.2 为什么故障注入不会"自己制造"丢失（实验效度）
- stall 队列上限 4096、溢出插入返回拒绝 → 内核 fallback 兜底；拒绝窗被 loader 与内核双重有界
  （优雅 1s / 强制 10s）；所有"空指针"模式禁止持有请求（§4.1 不变式）。
- D0 诊断臂明确标注"可能暴露真缺陷"，其判定门槛是**不 panic、机器可恢复（hypervisor snapshot 回滚）**，
  与门控臂的"0 丢失"门槛区分——混淆两者才是无效实验。

### 7.3 与"100ms 高频"的关系
切换为**串行**语义：上一次排空观测完成后才发起下一次（实际周期 ≥100ms，事件表记录真实周期）。
这保证每事件的延迟/移交计数可无歧义归属。高频压力由 S5（50ms）体现；若 loader 冷启动（BPF load
≈10–40ms）使 50ms 周期不可达，注入器自动以"排空完成即切"的最大频率运行并在报告中如实标注。

## 8. 判定门槛（每 run 自动判定，汇总进 SAFETY_REPORT.md）

| # | 门槛 | 判定数据 |
|---|---|---|
| G1 | fio 退出码 0，JSON `error==0`，verify 阶段无失配 | fio JSON |
| G2 | dmesg 无 forbidden 模式（§6.1 列表）；机器未 reboot | dmesg tap |
| G3 | in-flight ≤ nr_requests；每个切换事件后 5s 内归零（stall 窗 ≤ stall_ms+15s 除外）；run 末 =0 | 采样+事件表 |
| G4 | 每个切换事件在 kprobe 通道观测到排空完成（`scx_io_policy_free`）或 meta 条目清除；无孤儿/丢失 | 注入器+kprobe join |
| G5 | 延迟可解释且有界：非故障事件在小积压/空载时 `T_drain_kernel` p99 < 5ms（初值）；满积压场景如实报告 `T_drain = f(积压)` 并要求每次切换后 in-flight 归零（G3）；stall 事件 e2e ≤ stall_ms + 排空预算 + 15s | 事件表（kprobe） |
| G6 | 单 run 完成 I/O ≥ 80%×size 换算块数（8G run ≈ ≥1.7M） | fio JSON |
| G7 | S0 基线（无切换）同为全绿 → 排除负载本身干扰 | S0 |

**campaign 级门槛**：全部门控臂（S1–S5）× 3 rep 全部 G1–G7 通过，且各臂统计量在 rep 间稳定
（p99 相对波动 < 2×）。

## 9. 产物与复现

```
experiments/safety_chaos_kvmhost/
├── EXPERIMENT_DESIGN.md        # 本文
├── RUNBOOK_kvmhost.md           # 逐条操作手册（含 hypervisor snapshot/rollback、netconsole）
├── scripts/
│   ├── bpf/                    # q_inject_null / q_inject_stall 的 BPF 与 loader 源码
│   ├── sc_build_bm.sh          # 工作站：装源码进 ioext → make → rsync 到 kvmhost
│   ├── sc_launch_bm.sh         # 工作站：同步脚本 → netconsole sink → 远程执行 → 取回产物
│   ├── sc_run_chaos_bm.sh      # kvmhost(root)：单 run 编排（guards→scx_io→cgroup→fio→注入→verify→清理）
│   ├── sc_injector_bm.py       # kvmhost(root)：切换事件驱动器 + 事件表
│   ├── sc_sampler_bm.sh        # kvmhost：20Hz in-flight 采样
│   └── sc_analyze_bm.py        # 工作站：G1–G7 自动判定 + 统计 + CDF 图 + SAFETY_REPORT.md
└── results/<seq>/              # 每 run 原始产物 + SAFETY_REPORT.md + summary.json
```
序列命名：`safety_chaos_<arm>_<rep>_<yyyymmdd_HHMMSS>`（如 `safety_chaos_S4_r2_20261120_103000`）。

## 10. 前置 preflight 与机制核对记录（2026-11-19）

| 项 | 结果 |
|---|---|
| ssh/sudo | `user@127.0.0.1` 可达；sudo（密码）可用，可读写 sysfs/tracing |
| 内核 | `7.3.1-ioext3+`（#12，2026-11-06 构建，7.3.1 集成分支谱系），scx_io 在 sdb 支持列表 |
| 实验盘 | sdb 96G 裸盘未挂载（201326592 sectors 与 guard 一致）；**实测 ~9k IOPS（4k randwrite QD256）/ ~40MB/s（1M seq）** |
| fio | 3.41 在 PATH |
| loader 环境 | /opt/io-ext/userspace 构建树存在，/opt/ioext-bench 仓库在位，bpftool 可用 |
| dmesg | sudo 可读；hung_task_timeout_secs=120（run 内临时调 30） |
| **内核日志门控（重要发现）** | 部署内核的 `teardown complete` 在 `ioext_dbg_pr_info` 门控下（IOEXT_DEBUG=0 → no_printk），dmesg 无 per-event 排空日志；**本地 D2FQ-native 开发分支已改 always-on 但未部署** → 观测主通道改 kprobe（下方已验证） |
| **kprobe 观测（已实测）** | `p:ioext_spre scx_io_policy_prepare_teardown` / `p:ioext_sfree scx_io_policy_free` 均可创建并触发；空闲策略 prepare→free ≈ **4–5ms** |
| **meta map（已实测）** | attach 期间条目在、detach 后 `bpftool map dump pinned /sys/fs/bpf/ioext_policy_meta` 为 `[]`——排空完成的用户态旁证成立 |
| **故障策略冒烟（已实测）** | q_fifo BTF 对齐 OK；q_inject_null --mode 1 全生命周期 rc=0；q_inject_stall --stall-ms {500,9000} 行为正确（stall 窗、pile-up、quiesce_wait 均按设计打印）；空载下 stall 不产生任何内核异常 |
| **e2e 冒烟（sc_launch --smoke）** | `results/safety_chaos_S1_r1_20261118_221012`：4×1G 写+全量 verify rc=0（2.1M I/O），8 次满载切换（q_fifo↔q_none），**分析器判定 PASS**（G1/G2/G3/G4/G6 全绿；G5 两个 provisional 门按实测报告：满积压 drain ≈12.2s、直通策略 drain 2.1–4.6ms）——0 丢失 0 损坏 0 hung |
| 机器状态 | 空闲（load ~0.1）；无残留 loader/fio 进程；冒烟后机器态已核验恢复（scheduler=none、hung_task=120、无残留 cgroup/trace instance） |

## 11. 风险与安全网

1. **内核 wedge/panic**（主要来自 D0）：hypervisor snapshot 前置；netconsole 全程 arm；SSH 死亡时
   `hypervisor vm serial`/snapshot rollback 恢复；门控臂（S1–S5）设计上不越界（stall ≤900ms < 优雅窗）。
2. **实验盘写穿影响宿主**：kvmhost 本身就是 hypervisor 虚机（kvmhost 宿主），QD256 4k 写为其标称实验负载
   （历史 dynamic_tenant campaign 同量级）。
3. **fio verify 语义风险**：size 定长 + randommap 是 fio verify 的教科书用法；S0 臂先跑一次纯
   write+verify（无注入）验证该形态本身通过，再开混沌（G7）。
4. **时钟对齐偏差**：VM 曾发生 suspend 使 sched_clock 与 CLOCK_BOOTTIME 产生 ~0.88s 常数偏差——
   因此 T_drain 采用 kprobe 同钟配对、T_to_detach 采用 boot 时钟，二者相加构成 e2e，天然免疫；
   meta_clean 时刻作三方互证。
5. **与其它 campaign 冲突**：run 脚本带 flock（/tmp/safety_chaos.lock，后台持有进程模式、无 fd
   继承）+ 启动前 guard（无残留 loader_ 进程、无其它 fio、sdb 未挂载），fail-stop。
6. **trace_pipe 单读者**：顶级 trace_pipe 只允许一个读者，残留读者会让新 tap 静默丢事件——
   已改用专用 trace instance（`instances/sc_chaos`），与其它工具完全隔离。
