# EXPERIMENT_MATRIX — io_ext 同态复现 8 组固定实验配置

- 版本：v1.0（设计冻结稿，由子智能体 C 产出）
- 日期：2026-11-14
- 范围：本地设计产物（只读源码、只写本目录；未执行任何实验、未 ssh 任何机器）
- 产物：
  1. 本文档 `EXPERIMENT_MATRIX.md`
  2. `configs/<algo>/`（8 组实验的 fio job、臂激活/断言脚本、诊断脚本）
  3. `RUN_INDEX.csv`（run 模板，362 行：110 个正式 arm-run × phase 展开 + 2 诊断行）

来源标注约定（全文适用）：

| 标签 | 含义 |
|---|---|
| [PLAN] | 实验计划固定项，不可改 |
| [MSC] | 主会话补定口径 |
| [FIX-C] | 子智能体 C 据源码核对后的修正（附 file:line 证据） |
| [GAP-C] | 计划缺口由 C 补定（低风险默认值，待主会话确认） |
| 【占位引用】 | 依赖尚不存在的文档/特性，引用留位 |

---

## 1. 全局口径

### 1.1 guest 环境（[MSC] 主会话补定）

- bm 上 VM：6 vCPU / 6 GiB，virtio-blk `cache=none`，数据盘 `/dev/vdb` ext4 挂 `/data`。
- guest 内核：`7.3.1-ioext3`（preflight 断言 `uname -r` 匹配 `7.3.1-ioext*`）。
- 数据文件：`/data/lc.bin`、`/data/be.bin`，各 32 GiB 预分配。
  单租户实验（mq-deadline、io.max）用 `lc.bin`；coloc 实验 LC 用 `lc.bin`、BE 用 `be.bin`。
- fio 一律 `direct=1`；`ioengine=libaio`；`numjobs=1`；`group_reporting=1`；
  `percentile_list=50:90:95:99:99.9`；`log_avg_msec=500`；`time_based=1`。
- 随机序列冻结 [GAP-C]：`randrepeat=0` + `norandommap=1` + 每 phase 固定 `randseed`
  （`configs/common/env_preflight.sh` 常量），保证三臂 workload 逐位一致。
- 结果目录：`/dev/shm/ioext_repro/<experiment>/<arm>/r<rep>/`（RAM 盘，避免污染被测盘）。

### 1.2 臂定义（[PLAN] 三臂同条件；LinnOS/d2fq 只有 none/IOX）

| 臂 | 内容 |
|---|---|
| `none` | 与 native/iox **完全相同 workload**；native block 层 `scheduler=none`；无 loader、无 knob、无内部状态；图上只用外部指标（p99/带宽/份额），**不得填零** [PLAN] |
| `native` | 内核原生机制（scheduler echo / cgroup fs 写行），无 loader |
| `iox` | `scx_io` 基座 + ioext loader（arbiter + per-cgroup queue policy） |

- IOX 基座激活：`echo scx_io > /sys/block/vdb/queue/scheduler` + 读回 `[scx_io]`
  （[FIX-C] 字段验证模式冻结自 `ioext/userspace/io/tools/hotsplit/guest_hotswitch.sh:115-120`；
  内核含 scx_io 的前提断言 `grep scx_io /proc/kallsyms`，同脚本 ：58）。
- IOX 启动顺序（冻结自 `guest_hotswitch.sh:97-123`）：**arbiter loader 先 attach → 切 scx_io → 挂 q policies**；
  无 arbiter 的臂：切 scx_io → 挂 q policies。收尾统一 SIGTERM loader → drain 5s → 清 bpffs pin → `scheduler=none`。
- loader 输出重定向 [MSC]：arbiter 与 q loader 的 stdout+stderr 全量追加到
  `<RUN_DIR>/<run_id>.stats.log`（`--stats-interval-ms` 周期状态行打 stdout；支持范围与字段源见附录 D）。
- `nr_requests`：每臂激活后 best-effort `echo 1023`（`guest_hotswitch.sh:121` 模式），快照记录实际值。
- loader bundle：**loaders_stats1**（本仓 `experiments/reproduction/loaders_stats1/`，2026-11-14，
  `--stats-interval-ms` 已含且仅 5 loader 支持——附录 D）；部署到 guest 后由
  `IOEXT_LOADER_DIR` 指向（脚本默认 `/opt/io-ext/userspace`，主会话部署时以实际路径覆盖）。

### 1.3 重复与交错（[PLAN]）

- 每实验 **5 rep**；mq-deadline/io.max 每臂 60s；kyber/io.cost/io.latency/weight/d2fq 每臂 90s（step 协议）；linnos 120s。
- 每 rep 内臂顺序轮换（3 臂实验）：rep1 `none→native→iox`，rep2 `iox→none→native`，rep3 `native→iox→none`，rep4/rep5 循环（`none→native→iox` / `iox→none→native`）。
- 2 臂实验（d2fq/linnos）轮换：rep1 `none→iox`，rep2 `iox→none`，交替。
- 两两臂之间插 **3×10s QD1 fio 哨兵**（`configs/common/sentinel.fio`，4k randread QD1 direct=1）。
  口径：臂收尾后连发 3 发；本臂 `sentinel_after` = 第 1 发漂移，下一臂 `sentinel_before` = 第 3 发漂移。
  漂移 >20% **记 phase 异常但不淘汰**，只记录并报告 [MSC]——本轮判读主要靠分 rep 时序图。

### 1.4 状态快照与断言（[PLAN] preflight/postflight）

- 每臂运行前后各记录 scheduler/cgroup 状态快照：`<run_id>.preflight.txt` / `.postflight.txt`
  （`snapshot_state`：scheduler 读回、nr_requests、loader/fio 进程、根 io.cost.qos/model、根 io.stat vrate、
  各 cgroup 的 io.max/io.weight/io.bfq.weight/io.latency/io.stat、bpffs pins、dmesg tail）。
- 实验互斥锁：`flock /tmp/ioext_repro.lock`，无残留锁方可开跑（断言清单见各 `arms_*.sh` 头注释与 §2-§9）。

### 1.5 统计口径（[PLAN] 报告要求）

- 每实验 5 rep，报告值用 **中位数**（median of 5 reps），并在报告中注明口径：
  "n=5 独立重复，取中位数；时序图为单 rep 代表 + 全 rep 分位带；哨兵异常 rep 标注不淘汰"。
- 内部状态时序（loader stats）只存在于 iox 臂；native 臂的外部替代观测（vrate/io.stat 采样）与
  none 臂（纯 fio 指标）在图上分开绘制，不互相填零。

### 1.6 实验矩阵总表

| # | 实验（algo 目录） | 臂集 | workload | 时长 | 核心图 [PLAN] | stats 依赖 |
|---|---|---|---|---|---|---|
| 1 | `mq_deadline` | none / native mq-deadline / iox `q_mq_deadline` | 4k randrw 70r/30w **QD32**，单租户 lc.bin | 60s | Read P99 + Write P99（三组） | 不依赖（fio 指标） |
| 2 | `kyber` | none / native kyber / iox `q_kyber` | LC 4k randread + BE 128k seqwrite；BE 饱和；LC 中段 step up | 90s step | LC P99 时序 + **depth limit 时序**（内部） | iox 硬依赖 |
| 3 | `io_max` | none / native io.max / iox `q_iomax` | 单 BE cgroup 4k randwrite，限速 32 MiB/s | 60s | 写带宽时序 + 32MiB/s 限速线 | 不依赖 |
| 4 | `io_weight` | none / native bfq+io.weight / iox `arb_wdrr` | LC 4k randread + BE 128k seqwrite，权重 100:400 | 90s step | 两 cgroup 字节份额 | 不依赖（fio bytes） |
| 5 | `io_cost` | none / native io.cost / iox `arb_iocost` | LC 4k randread + BE 128k seqwrite；LC 中段 step up | 90s step | **vrate 时序**（native 归一化） | iox 硬依赖 + model 诊断步 |
| 6 | `io_latency` | none / native io.latency / iox `arb_iolatency` | LC 4k randread + BE 128k seqwrite；LC 中段 step up；LC target 2ms | 90s step | LC P99 + BE 带宽时序（方向分歧问题） | 不依赖（boost/FSM 为补充） |
| 7 | `d2fq` | none / iox `arb_d2fq`（**Native VM 内 N/A** [MSC]） | 两 cgroup 权重 100:400，**两流均 4k randread QD32** [MSC]，饱和 | 90s step | 累计字节份额随时间 | 不依赖（vtime/band 为补充） |
| 8 | `linnos` | none / iox `q_linnos`（**Native N/A** [PLAN]） | LC 4k randread 稳定 + BE 4k randwrite 阶梯 QD 8→16→32→64 | 120s（4×30s） | BE 目标拦截率 vs 实际拦截率（per-band 计数） | iox 硬依赖 + busy_ref 诊断步 |

step 协议（[MSC]，适用于 kyber/io.cost/io.latency/weight/d2fq）：0-30s 基线（LC QD32）→ 30-60s LC step up
（**LC QD 32→64**，LC 单独换第二个 fio job 顺序跑，phase 间不中断调度器状态——实现为单 job 文件内
`stonewall` 串行段，段间零间隔）→ 60-90s 恢复 QD32。BE 全程 90s 不间断。

### 1.7 文件布局

```
experiments/reproduction/
├── EXPERIMENT_MATRIX.md          # 本文档
├── RUN_INDEX.csv                 # run 模板（列定义见 §11）
└── configs/
    ├── common/
    │   ├── env_preflight.sh      # 环境常量 + 共享断言 + arm 公共库（loader/fio/快照/哨兵）
    │   └── sentinel.fio          # 3×10s QD1 哨兵
    ├── mq_deadline/  fio_{none,native,iox}.fio + arms_{none,native,iox}.sh
    ├── kyber/        fio_<arm>_{lc,be}.fio + arms_{none,native,iox}.sh
    ├── io_max/       fio_{none,native,iox}.fio + arms_{none,native,iox}.sh
    ├── io_weight/    fio_<arm>_{lc,be}.fio + arms_{none,native,iox}.sh
    ├── io_cost/      fio_<arm>_{lc,be}.fio + arms_{none,native,iox}.sh + diag_iocost_model.sh
    ├── io_latency/   fio_<arm>_{lc,be}.fio + arms_{none,native,iox}.sh
    ├── d2fq/         fio_{none,iox}_{lc,be}.fio + arms_{none,iox}.sh
    └── linnos/       fio_{none,iox}_{lc,be}.fio + arms_{none,iox}.sh
```

---

## 2. 实验 1：mq-deadline（`configs/mq_deadline/`）

**固定项** [PLAN]：4k randrw 70r/30w **QD32**，持续 60s；图 = Read P99 + Write P99
（none / native mq-deadline / iox `q_mq_deadline` 三组）。

**workload**（`fio_<arm>.fio`，三臂逐位一致）：`rw=randrw rwmixread=70 bs=4k iodepth=32 runtime=60
ramp_time=5`，文件 `/data/lc.bin`（[MSC] 单租户用 lc.bin）。日志前缀 `mq_deadline_<arm>_main`。

**各臂激活与命令行**：

- none：`scheduler=none`（起步态即目标态），无 knob。
- native：`echo mq-deadline > /sys/block/vdb/queue/scheduler` + 读回 `[mq-deadline]`。
- iox：`sched_set scx_io` → `loader_q_mq_deadline -c /sys/fs/cgroup`（root 注册，同 homo_v2
  `elev_x_mq_deadline_q` topology=root，gen_homo_v2_yamls.py:322-327）。

**loader CLI 核对** [FIX-C]：`-c` 来自公共 policy 参数（ioext_cli_args.h:119
`{ "cgroup", required_argument, NULL, 'c' }`；getopt 串 `":c:p:W:hv"` :167）。`--merge-mode` 存在
（loader_q_mq_deadline.c:42）默认 0（:25），本实验省略。**[GAP-C] 附注**：loader 只有 `-c/-p/-W`，
没有"运行时长"参数——它阻塞运行直到 SIGINT/SIGTERM（ioext_loader_common.h:177-193
`iosched_loader_sig_handler`），臂脚本在 fio 结束后统一 SIGTERM，60s 窗口由 fio `runtime=60` 控制。

**preflight 断言**：环境总断言（env_preflight，见 §1.4）+ 可用 scheduler 列表含 `mq-deadline`
（native 臂）+ 起步态 `scheduler=none`。
**postflight**：native 臂断言运行后仍 `[mq-deadline]`；iox 臂断言 loader 收尾退出、pin 清理。

**stats 采样**：不需要（图数据全来自 fio json/lat log）。iox 臂如已落 `--stats-interval-ms` 则顺带记录。

---

## 3. 实验 2：kyber（`configs/kyber/`）

**固定项** [PLAN]：LC 4k randread + BE 128k seqwrite；BE 持续饱和；实验中段提高 LC 读压力再恢复，
观察深度反馈；图 = LC P99 时序 + **depth limit 时序**（内部状态）。

**workload**：LC `rw=randread bs=4k` QD32→64→32（3×30s stonewall 段，`fio_<arm>_lc.fio`）；
BE `rw=write bs=128k iodepth=16 runtime=90`（`fio_<arm>_be.fio`）。日志前缀 `kyber_<arm>_lc_p{1,2,3}` / `kyber_<arm>_be`。

**各臂激活**：

- none：`scheduler=none`，LC/BE cgroup 建 但无 knob。
- native：`echo kyber > scheduler`；显式写 `/sys/block/vdb/queue/iosched/kyber/read_lat_nsec=2000000`
  与 `write_lat_nsec=10000000`（[FIX-C] kyber-iosched.c:877-883 `KYBER_LAT_ATTR`，单位 ns；
  编译内默认即 2ms/10ms（kyber-iosched.c:69-74），与冻结 IOX 目标同值——写入+读回断言使 knob
  落点可审计）；native 臂无深度 limit 可观测，核心图深度时序仅 iox 臂，
  native 柱用外部 LC P99 时序对照（偏差表记为"native 内部状态不可观测"）。
- iox：`sched_set scx_io` → `loader_q_kyber -c /sys/fs/cgroup --read-target-ns 2000000 --write-target-ns 10000000`。

**loader CLI 核对** [FIX-C]：`--read-target-ns` loader_q_kyber.c:70（option 表）/ :32-34（解析）；
`--write-target-ns` :71/:35-37；默认 2000000/10000000（help 文案 :84-85）。值冻结自 homo_v2
（gen_homo_v2_yamls.py:111-112 `KYBER_READ_TARGET_NS=2_000_000` / `KYBER_WRITE_TARGET_NS=10_000_000`）[PLAN]。

**preflight 断言**：环境总断言 + （native）列表含 `kyber` + 起步 `none`；iox 臂另断言
`--stats-interval-ms` 已落地（未落地 fail-stop，`IOEXT_ALLOW_NO_STATS=1` 才放行降级）。
**postflight**：native 断言读回仍 `[kyber]`；iox 断言 stats.log 非空且含 depth_limit 类字段（宽松 grep）。

**stats 采样** [MSC]：iox 臂 `--stats-interval-ms 1000`（已落地，loaders_stats1 bundle 的
loader_q_kyber 支持）；字段：per-hctx depth_limit[4]/inflight/cur_domain/batching/domain_p99
（ioext_stats_json.h，见附录 D），复核以子智能体A **ALGORITHM_AUDIT.md** 为准【占位引用】。

---

## 4. 实验 3：io.max（`configs/io_max/`）

**固定项** [PLAN]：单 BE cgroup 4k randwrite，**限速 32 MiB/s**（wbps=33554432），持续 60s；
图 = 写带宽时序 + 32MiB/s 限速线。

**workload**：`rw=randwrite bs=4k iodepth=16 runtime=60 ramp_time=5`，`/data/lc.bin`。
QD16 为 [GAP-C] 补定（plan 未定 io.max 的 QD；对齐 BE QD16 口径；4k@QD16 供给远超 8k IOPS 上限，
保证 cap 绑定）。三臂 fio 进同名 cgroup `iox_main`。

**各臂激活**：

- none：无 knob（裸带宽对照柱）。
- native：BE cgroup `io.max` 写行 `"<MM> rbps=max wbps=33554432 riops=max wiops=max"`。
  [FIX-C] 内核核对：blk-throttle.c:1588 接受 `%llu` 或字面 `"max"`；**值 0 会被 -ERANGE 拒绝**
  （blk-throttle.c:1591-1593 `if (!val) goto unprep`），故用 `max` 不能用 0。读回断言四字段齐全。
- iox：`sched_set scx_io` → `loader_q_iomax -c $IOEXT_MAIN_CG --wbps 33554432 --rbps 0 --wiops 0`。
  **[FIX-C] 修正 plan 写法**：plan 写的 `rbps=max wiops=max` 是 native cgroup 语法；q_iomax 的
  `--rbps/--wiops/--rbps/--wbps` 是严格 U64 解析（`q_iomax_parse_u64_strict`，loader_q_iomax.c:84-97，
  strtoull 全串校验），字面 `max` 直接报错。0 = 不限速（loader_q_iomax.c:5-8 头注释；显式写 0 而非
  省略，语义可审计）。全 0 会被拒（:249-253），本臂 wbps>0 满足。
  [FIX-C] 声明偏差：q_iomax burst cap = rate×0.5s（loader_q_iomax.c:72-75），native io.max 无 burst
  语义——非同值点，报告注明。

**preflight 断言**：环境总断言 + `iox_main` cgroup 可用；native 臂 io.max 读回
`"wbps=33554432"`+`"rbps=max"`+`"riops=max"`+`"wiops=max"`；iox 臂 loader 存活（全 0/非法值即刻退出）。
**postflight**：native 断言运行后 io.max 仍含 `wbps=33554432`；iox 臂留档 loader 收尾 bucket 统计
（`q_iomax_dump_stats`，loader_q_iomax.c:155-222、293 调用，随 stats.log）。

**stats 采样**：不需要（带宽时序来自 fio `write_bw_log=io_max_<arm>_main`）。

---

## 5. 实验 4：io.weight / arb_wdrr（`configs/io_weight/`）

**固定项** [PLAN]：两 cgroup 权重 **100:400**；LC 4k randread + BE 128k seqwrite；跑至份额稳定
（90s step 全窗）；图 = 两 cgroup 字节份额（native = BFQ + io.weight）。

**workload**：同 kyber 形状（LC 3×30s step；BE 90s）。日志前缀 `io_weight_<arm>_*`。

**各臂激活**：

- none：无 knob。
- native：`echo bfq > scheduler`；`io.bfq.weight` 写 `default 100` / `default 400`（读回断言）；
  补充 best-effort 写 `io.weight "<MM> 100/400"`（homo_v2 冻结口径）。
  [FIX-C] **内核载体核对**：`io.weight` 是 blk-iocost 注册的 cftypes（blk-iocost.c:3574-3579
  `.name="weight"`）；其 per-device 写路径经 `blkg_conf_prep`（blk-iocost.c:3188），要求 iocost
  已在盘上激活（blk-cgroup.c:852-855 `blkcg_policy_enabled` 检查 → 未激活 -EOPNOTSUPP），
  而 iocost 只在写 io.cost.model/qos 时激活（blk-iocost.c:3311/3503）。BFQ 的权重载体是
  `io.bfq.weight`（bfq-cgroup.c:1281 `.name="bfq.weight"`，接受 `"default N"` 或 `"N"`，
  bfq_io_set_weight），bfq 挂盘即激活。因此主载体 = io.bfq.weight，io.weight 为 homo_v2 口径
  伴写（失败仅记录为声明偏差，不 fail-stop）。homo_v2 冻结注释同向（gen_homo_v2_yamls.py
  ctrl_n_io_weight 注释 "native io.weight's ONLY effective native carrier is BFQ"）。
- iox：`loader_arb_wdrr`（无参数，默认 STRICT）→ `sched_set scx_io` →
  `loader_q_fifo -c $LC_CG -p 2 -W 100` + `loader_q_fifo -c $BE_CG -p 5 -W 400`。

**loader CLI 核对** [FIX-C]：`arb_wdrr` 仅 `--allow-fallback 0|1`（loader_arb_wdrr.c:110）/ 
`--diag-interval-ms`（:111），本实验不用；**arbiter 不接受 -c/-p/-W**（ioext_cli_args.h:226-232
注释明确 "Arbiters are global singletons: they take NO -c/-p/-W"）。权重经 q 侧 `-W/--arb-weight`
（ioext_cli_args.h:121）进 policy_meta。[FIX-C] 角色分类：`arb_prio < 3 = LC`（arb_iolatency.c:5-6
注释；q_linnos/arb_d2fq 同机制），默认 prio=7=BE band（ioext_loader_common.h:39
`IOSCHED_DEFAULT_ARB_PRIO 7U`），**LC 侧必须显式 `-p 2`**；默认权重 100（ioext_loader_common.h:40）。

**preflight 断言**：native 臂列表含 `bfq` + 权重读回（`default 100`/`default 400`）；iox 臂三 loader 存活。
**postflight**：native 断言权重未漂移；份额数据源 = fio json bytes（逐 cgroup fio 进程）+ 
io.stat bfq 字节采样（external_sampler 1s）。

**stats 采样**：不依赖（份额核心图来自 fio bytes）。

---

## 6. 实验 5：io.cost / arb_iocost（`configs/io_cost/`）

**固定项** [PLAN]：LC 4k randread + BE 128k seqwrite；中段提高 LC 压力再恢复，观察 vrate 反馈；
图 = vrate 时序（native vrate 归一化处理）。io.weight LC=100/**BE=200**（注意与实验 4 的 100:400 不同
——本臂权重是 io.cost 的 cost-unit 权重）+ io.cost.model（**guest 内 virtio 盘重测**，诊断步，
冻结后写死进配置）+ `io.cost.qos enable=1 rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150`。
IOX：`arb_iocost --qos-rlat-ns 2000000`。

**workload**：同 kyber 形状。日志前缀 `io_cost_<arm>_*`。

**各臂激活**：

- none：无 knob。
- native：根 cgroup 顺序写 **model → weight → qos**：
  1. `io.cost.model "<MM> ctrl=user model=linear rbps=.. rseqiops=.. rrandiops=.. wbps=.. wseqiops=.. wrandiops=.."`（冻结值占位 MUST-FREEZE）
  2. LC/BE cgroup `io.weight "<MM> 100"/"<MM> 200"`（model 激活后 per-device 写才可用）
  3. `io.cost.qos "<MM> enable=1 ctrl=user rpct=99 rlat=2000 wpct=95 wlat=1000000 min=50 max=150"`
  [FIX-C] 顺序核对：io.cost.model/qos 是 `CFTYPE_ONLY_ON_ROOT`（blk-iocost.c:3583-3594），只能根写；
  本树 `ioc_qos_write` 在 ioc 未初始化时会自动 `blk_iocost_init`（blk-iocost.c:3310-3315），"model 必须
  先于 qos"在本树不是硬内核约束，但按 plan 固定顺序写（model 先激活、qos 后调参），且 per-device
  io.weight 写依赖 model 先激活（见 §5 [FIX-C]）。
  [FIX-C] 读回格式：`"<MM> enable=1 ctrl=user rpct=99.00 rlat=2000 wpct=95.00 wlat=1000000 min=50.00 max=150.00"`
  （blk-iocost.c:3232 seq_printf）。
  [FIX-C] **native vrate 可观测**：`io.stat` 打 `cost.vrate=X.YZ`（blk-iocost.c:3110 `ioc_pd_stat`，
  level-0 iocg、ioc enabled 时）——1s 采样进 `native_state.log`，即 native vrate 时序数据源；
  归一化口径：vrate/100 相对 1.0=满模型速率。
- iox：`loader_arb_iocost --qos-rlat-ns 2000000` → `sched_set scx_io` →
  `loader_q_fifo -c $LC_CG -p 2 -W 100` + `-c $BE_CG -p 5 -W 200`。

**loader CLI 核对** [FIX-C]：`--qos-rlat-ns` loader_arb_iocost.c:202（option 表）/ :136-139（解析），
默认即 2000000（:52），显式写出便于审计；其余默认（[PLAN] 未要求覆盖）：`--qos-rpct` 99（:53）、
`--vrate-min/max` 25%/400%（:54-55）、`--cost-table` byte-linear `64,256,1024,2048,4096,64,256,1024,
2048,4096`（:76-77）、`--cost-quantum` 8 / `--cost-min-budget` 64（:50-51）。
[PLAN] 声明偏差（不改）：native vrate clamp min=50/max=150 vs IOX 默认 [25,400]——两臂钳位区间不同，
报告注明为 plan 固定差异。

**诊断步（[MSC]）**：`configs/io_cost/diag_iocost_model.sh`。
[FIX-C] 位置修正：plan 猜测的 `scripts/iocost_coef_gen_bm.py` 不存在；实际可用
`/opt/ioext/experiments/dyn_tenant_bm/build/iocost_coef_gen_bm.py`
（bm host nvme0n1 校准所用版本；CLI 无 `--json`，model 行打 stdout）或内核树上游
`/opt/ioext-io_ext/kernel/linux-7.3.1/tools/cgroup/iocost_coef_gen.py`。
guest 内 file 模式跑（`--testfile-size-gb 16`，**勿用 --testdev 毁盘**）；bm host 的
`experiments/homo_v2/calibration_bm.json` 是 nvme0n1 的，guest virtio 不适用（[PLAN] 已定重测）。
冻结流程：诊断 → 人工核对 → 替换 `arms_native.sh` 的 `MODEL_LINE` → `IOCOST_MODEL_FROZEN=1` 放行。

**preflight 断言**：native 臂 qos 读回 `enable=1` + `rlat=2000`、LC/BE io.weight 读回
`"<MM> 100"/"<MM> 200"`、model 未冻结则 fail-stop（`IOEXT_ALLOW_UNFROZEN_MODEL=1` 才放行）；
iox 臂 stats 开关 fail-stop + 三 loader 存活。
**postflight**：断言运行后 qos 仍 `enable=1`；根 io.cost.qos 复位 `enable=0`（防残留）。

**stats 采样** [MSC]：iox 臂硬依赖 `--stats-interval-ms 1000`（arb_iocost 支持，已落地；
vrate_fp/vrate/eff_quantum/window FSM——字段见附录 D / ALGORITHM_AUDIT.md【占位引用】）；
native 侧来自 io.stat cost.vrate（1s 采样）。

---

## 7. 实验 6：io.latency / arb_iolatency（最高优先级，`configs/io_latency/`）

**固定项** [PLAN]：LC 4k randread + BE 128k seqwrite，LC target **2ms**；中段增加 LC 压力，
观察 LC P99 保护与 BE 带宽方向；图 = LC P99 + BE 带宽时序。**本实验要回答的问题**：native 自节流
（blk-iolatency 压 LC 自己的提交队列）vs IOX boost（压 BE 保 LC）的**方向分歧**。

**workload**：同 kyber 形状。日志前缀 `io_latency_<arm>_*`。

**各臂激活**：

- none：无 knob。
- native：LC cgroup `io.latency "<MM> target=2000"`（BE 无 knob，同 homo_v2 `ctrl_n_io_latency`
  be_knob={}）。[FIX-C] 格式核对：写解析按 `key=val` token（blk-iolatency.c:867-876），读回
  `"<dev> target=<us>"`（blk-iolatency.c:908），target 单位 us，2ms=2000。
  [MSC] 坑位预录（gen_homo_v2_yamls.py `IO_LATENCY_TARGET_US` 注释）：blk-iolatency 无下游可压时
  会自节流 LC——homo_v2 iteration#2 的 1ms 目标低于可达地板，LC iops 84k→72k 且 p99 仍 5.7ms；
  2ms 目标位于 ~4.9ms none 基线与硬件地板之间。若本实验复现"自伤"模式，按 plan 记入判读而非失败。
  [FIX-C] preflight 前置断言：LC cgroup 必须存在 `io.latency` 文件（homo_v2 记录过"缺失被静默跳过"
  的坑，本配置改为 fail-stop）。
- iox：`loader_arb_iolatency --lat-target-ns 2000000` → `sched_set scx_io` →
  `loader_q_none -c $LC_CG -p 2 -W 100` + `loader_q_fifo -c $BE_CG -p 5 -W 200`。
  [FIX-C] `--lat-target-ns` loader_arb_iolatency.c:76/:50-53，**默认 100us（:30）必须显式覆盖**；
  `--protect-inflight` 默认 32（:31）、`--boost-max` 默认 4 clamp [1,16]（:32,58-66），[PLAN] 其余默认
  省略。LC 用 `q_none` 冻结自 homo_v2 `ctrl_x_latency_arb`（gen_homo_v2_yamls.py:379-387）。
  BE 权重 200 为 [GAP-C]（plan 未定本臂权重，沿 homo_v2 LC_W/BE_W=100/200 冻结）。

**preflight/postflight**：见 `arms_native.sh`/`arms_iox.sh` 内联断言（target=2000 读回前后一致；
loader 存活）。

**stats 采样**：核心图不依赖（LC P99 + BE 带宽来自 fio）；iox 臂 `--stats-interval-ms 1000`
（arb_iolatency 支持，已落地）——
补 per-tenant boost/FSM state 时序（字段以 ALGORITHM_AUDIT.md 为准【占位引用】）。

---

## 8. 实验 7：D2FQ / arb_d2fq（`configs/d2fq/`）

**固定项** [PLAN]：两 cgroup 权重 100:400 持续饱和；图 = 累计字节份额随时间。
**已定决策（主会话）[MSC]：VM 内 Native N/A**——ioextd2fq 需要 NVMe WRRU，virtio-blk 上
`queue/d2fq_en` 写 1 被 -EINVAL 拒绝（[FIX-C] 证据：
`experiments/homo_v2/D2FQ_NATIVE_PORT_20261110.md:70` "非 NVMe 队列写 1 → 被 -EINVAL 安全拒绝"），
口径与 LinnOS 相同；bm 裸机 `front_n_d2fq` 三臂列为后续独立补充项。

**workload** [MSC]：**两流均 4k randread QD32**（同类型流使"字节份额==权重比"的检验干净，
read/write 类型成本不对称性排除在本实验外——矩阵注明）；LC 走 step 协议（QD 32→64→32），
BE 90s 不间断。日志前缀 `d2fq_<arm>_*`。

**iox 激活**：`loader_arb_d2fq`（全默认）→ `sched_set scx_io` →
`loader_q_fifo -c $LC_CG -p 5 -W 100` + `-c $BE_CG -p 5 -W 400`。
[FIX-C] 两租户都 `-p 5`（BE 档，[MSC]）→ D2FQ fast-share（LC 类加速）不介入，纯权重公平性检验；
`--fast-share` 默认 6（loader_arb_d2fq.c:61，== homo_v2 `x_d2fq_arb` 冻结值）、`--type-weights`
默认 100:100、cost 表默认 `read{64,256,1024,2048,4096}:write{256,1024,4096,8192,16384}`
（loader_arb_d2fq.c:57-69）；option 全集 :162-173（`--thrsh/--md-ratio/--lc-prio-max/--fast-share/
--type-weights/--cost-table/--di-period-ms`）。

**份额读数**：fio json bytes（两租户）+ `write_bw_log` 时序积分 → 累计字节份额曲线；
native/none 柱无份额机制时 none 臂按到达量份额展示（外部指标 [PLAN] 不填零，native 柱标 N/A）。

**stats 采样**：不依赖（vtime/band 内部时序为补充，字段以 ALGORITHM_AUDIT.md 为准【占位引用】）。

---

## 9. 实验 8：LinnOS / q_linnos（`configs/linnos/`）

**固定项** [PLAN]：LC 4k randread 稳定 + BE 4k randwrite 逐步固定压力（推进 busy band），
120s 分 4 段（每段 30s）BE randwrite QD 8→16→32→64 阶梯；图 = BE 目标拦截率 vs 实际拦截率
（loader stats 的 per-band 计数）。**Native N/A** [PLAN]（无可对照参考实现；论文在线 NN 语义
无法同态复现；`D2FQ_NATIVE_PORT_20261110.md` "LinnOS：native 不可行的原因"）。

**workload**：LC `randread 4k QD32 runtime=120 ramp_time=5`（`fio_<arm>_lc.fio`，单 job 连续）；
BE `randwrite 4k` 4×30s stonewall 段 QD 8/16/32/64（`fio_<arm>_be.fio`）。
日志前缀 `linnos_<arm>_lc` / `linnos_<arm>_be_s{1..4}`。

**iox 激活**（无 arbiter）：`sched_set scx_io` → LC/BE 两实例 q_linnos：
```
loader_q_linnos -c $LC_CG -p 2 -W 100 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --max-hold-us 500 --busy-ref <FROZEN>
loader_q_linnos -c $BE_CG -p 5 -W 100 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --max-hold-us 500 --busy-ref <FROZEN>
```
[FIX-C] 核对：`--ir-low/--ir-mid/--ir-high` loader_q_linnos.c:181-183，值格式 `"LC:BE"`（:74-91），
各 0..100；`--max-hold-us` :185（默认 2000）；`--busy-ref` :184（默认 64）；`--lc-prio-max` 默认 3
（:186,70）→ `arb_prio<3=LC`，LC 必须 `-p 2`。两实例参数完全一致（IR 表是 band×class 全局表，同值幂等；
homo_v2 iteration#2 教训：默认表 band2=5:60 会拦 LC → LC p99 1.86ms→8.2ms；
LINNOS_IR_* 冻结，gen_homo_v2_yamls.py:239-247,261-267；`--max-hold-us 500` 冻结自 iteration#3
:248-255）。两实例模式冻结自 homo_v2 `front_x_linnos_q`（gen_homo_v2_yamls.py:416-425）。

**busy_ref 诊断（[MSC]）**：`LINNOS_DIAG=1 bash arms_iox.sh <RUN_DIR> diag` 以 busy-ref=64 跑一次
完整 120s 协议；判据 = loader stats per-band inserts（band0/band1/band2）**每条 busy band 至少占一个
1s window**；诊断允许、结果不入正式。通过后：设 `LINNOS_BUSY_REF_FROZEN=<值>` 与
`LINNOS_BUSY_REF_FROZEN_OK=1` 再跑正式 5 rep（脚本 fail-closed：未冻结直接拒绝正式运行）。
参考起点：homo_v2 校准值 `--busy-ref 24`（gen_homo_v2_yamls.py:228 注释：默认 64 在其负载下
从未激活 band≥mid）——本实验负载不同，以诊断实测为准。

**拦截率读数**：目标 = IR 表 BE 列（band0/1/2 = 0/40/60%）；实际 = `intercepted[band][BE] /
inserts_by_band[band]`（per-band 计数来自 loader stats；字段名以 ALGORITHM_AUDIT.md 为准【占位引用】；
未落 stats 开关时的降级口径 = loader 收尾 dump 总数（loader_q_linnos.c:132-176,243）+ 外部 LC P99
时序，矩阵记偏差）。

---

## 10. 附录 A：loader CLI 逐条核对表（[FIX-C]，源码 = `/opt/ioext-io_ext/userspace/io/`）

公共机制：policy loader（q_*）公共参数 `-c/--cgroup`、`-p/--arb-prio`、`-W/--arb-weight`、`-h/-v`
（ioext_cli_args.h:118-124 option 表；:167 getopt 串 `":c:p:W:hv"`）；**arbiter loader（arb_*）无
-c/-p/-W**（ioext_cli_args.h:226-232）；默认 prio=7、weight=100（ioext_loader_common.h:39-40）。

| 实验 | loader | 用到的 flag | 核对位置 | 结论 |
|---|---|---|---|---|
| 1 | loader_q_mq_deadline | `-c` | loader_q_mq_deadline.c:53 + ioext_cli_args.h:119,167 | OK（默认根 cgroup，:17） |
| 1 | loader_q_mq_deadline | `--merge-mode` | :42,21-36 | 存在，默认 0，本实验省略 |
| 2 | loader_q_kyber | `--read-target-ns` | :70,32-34（默认 2000000 :84） | OK |
| 2 | loader_q_kyber | `--write-target-ns` | :71,35-37（默认 10000000 :85） | OK |
| 2 | loader_q_kyber | `--discard-target-ns`/`--other-target-ns`/`--merge-enable` | :72-74 | 存在，未用 |
| 3 | loader_q_iomax | `--wbps` | :230,118-119 | OK |
| 3 | loader_q_iomax | `--rbps`/`--wiops`/`--riops` | :228-229,227,109-117 | **值严格 U64，不接受 "max"**（:84-97）；0=不限（:5-8）；全 0 拒绝（:249-253）→ [FIX-C] 修正为 `--rbps 0 --wiops 0` |
| 4 | loader_arb_wdrr | （无参 STRICT 默认） | :118-132 | OK；`--allow-fallback` :110、`--diag-interval-ms` :111 未用 |
| 4/5/6/7 | loader_q_fifo | `-c/-p/-W` | loader_q_fifo.c:17 + ioext_cli_args.h:118-124 | OK |
| 5 | loader_arb_iocost | `--qos-rlat-ns` | :202,136-139（默认 2000000 :52） | OK |
| 5 | loader_arb_iocost | `--qos-rpct/--vrate-min/--vrate-max/--cost-table/--cost-quantum/--cost-min-budget` | :203-207 | 存在，默认 rpct 99、clamp 25/400%、byte-linear 表（:50-55,76-77），[PLAN] 未覆盖 |
| 6 | loader_arb_iolatency | `--lat-target-ns` | :76,50-53；**默认 100us 必须覆盖**（:30,85） | OK |
| 6 | loader_arb_iolatency | `--protect-inflight/--boost-max` | :77-78,54-66（默认 32/4，clamp [1,16]） | 存在，[PLAN] 默认省略 |
| 6 | loader_q_none | `-c/-p/-W` | loader_q_none.c:17 | OK |
| 7 | loader_arb_d2fq | （全默认） | option 全集 :162-173；默认值 :57-69 | OK |
| 8 | loader_q_linnos | `--ir-low/--ir-mid/--ir-high` | :181-183,74-91（"LC:BE" 格式） | OK |
| 8 | loader_q_linnos | `--busy-ref` | :184,102-104（默认 64） | OK；诊断后冻结 [MSC] |
| 8 | loader_q_linnos | `--max-hold-us` | :185,105-107（默认 2000） | OK |
| 8 | loader_q_linnos | `--lc-prio-max` | :186,108-110（默认 3） | 默认即所需，省略 |
| 2/5/6/7/8 | （新落地）`--stats-interval-ms` | q_kyber/q_linnos/arb_iocost/arb_iolatency/arb_d2fq | **已落地**：loaders_stats1 bundle（patch + BUILD_PROVENANCE.txt，2026-11-14）；仅上述 5 loader 支持，其余 5 个冻结副本二进制无此选项（strings 计数=0）[MSC][FIX-C] |

生命周期补充 [FIX-C]：loader 无时长参数，阻塞至 SIGINT/SIGTERM（ioext_loader_common.h:177-193），
收尾 graceful drain + 注销 policy（:1130-1133）；q_iomax/q_linnos 收尾额外 dump 统计到 stderr
（loader_q_iomax.c:293；loader_q_linnos.c:243）。

## 10.1 附录 A2：native 臂激活方式核对（[FIX-C]，内核 = `/opt/ioext-io_ext/kernel/linux-7.3.1/block/`）

| 机制 | 激活方式 | 内核证据 |
|---|---|---|
| mq-deadline / kyber / bfq / none | `echo <name> > /sys/block/vdb/queue/scheduler`，读回 `[name]` | 标准 elevator 接口；scx_io 同口（guest_hotswitch.sh:115-120） |
| native kyber lat target | `iosched/kyber/{read,write}_lat_nsec`，单位 ns；默认 read=2ms/write=10ms（== 冻结 IOX 值，仍显式写+读回断言） | kyber-iosched.c:877-883（KYBER_LAT_ATTR，kstrtoull ns）；默认值 :69-74 |
| io.max | cgroup v2 行 `"<MM> rbps=max wbps=33554432 riops=max wiops=max"` | blk-throttle.c:1588（%llu 或 "max"）；0 → -ERANGE（:1591-1593）；cftypes `.name="max"`（:1623-1627） |
| io.weight（iocost 的文件） | per-device `"<MM> <weight>"` 或 scalar | blk-iocost.c:3574-3579（`.name="weight"`）；写处理器 :3145-3219；per-device 写要求 iocost 已激活（blk-cgroup.c:852-855 → -EOPNOTSUPP） |
| io.bfq.weight（BFQ 载体） | `"default N"` 或 `"N"` | bfq-cgroup.c:1281（`.name="bfq.weight"`）；写处理器 bfq_io_set_weight；bfq 附盘即激活 |
| io.cost.model / io.cost.qos | **仅根 cgroup**，行首 `<MM>`，顺序 model→(weight)→qos | blk-iocost.c:3583-3594（CFTYPE_ONLY_ON_ROOT）；qos 自动初始化 :3310-3315；model 自动初始化 :3501-3506；读回格式 :3232 |
| io.latency | cgroup 行 `"<MM> target=<us>"`；读回 `"<dev> target=<us>"` | blk-iolatency.c:867-876（key=val 解析）、:908（seq_printf） |
| native vrate 读数 | `cat /sys/fs/cgroup/io.stat` → `cost.vrate=X.YZ` | blk-iocost.c:3097-3112（ioc_pd_stat） |

## 10.2 附录 A3：C 据源码修正/补定清单（[FIX-C]/[GAP-C] 汇总）

1. **q_iomax 不接受 "max"**：`--rbps=max` → `--rbps 0`（loader_q_iomax.c:84-97,5-8,249-253）。[FIX-C]
2. **io.weight 与 io.bfq.weight 是两个文件**：BFQ 载体 = io.bfq.weight（bfq-cgroup.c:1281）；
   io.weight 是 blk-iocost 的（blk-iocost.c:3574），per-device 写需 iocost 已激活。native weight 臂
   双写（bfq.weight 主 + io.weight 伴写 best-effort）。[FIX-C]
3. **iocost_coef_gen_bm.py 实际位置**：`experiments/dyn_tenant_bm/build/iocost_coef_gen_bm.py`
   （plan 猜测的 `scripts/` 无此文件）；另有内核树 `tools/cgroup/iocost_coef_gen.py`；CLI 无 `--json`。
   [FIX-C]
4. **io.cost model/qos 只能根写**（CFTYPE_ONLY_ON_ROOT，blk-iocost.c:3583-3594）；本树 qos 写可自动
   初始化 ioc（:3310-3315），"model 先于 qos"按 plan 顺序保留为脚本约定。[FIX-C]
5. **io.max 值 0 非法**（-ERANGE，blk-throttle.c:1591-1593），native 行用 `max`。[FIX-C]
6. **native kyber target 走 sysfs**：`iosched/kyber/{read,write}_lat_nsec`（单位 ns，
   kyber-iosched.c:877-883——宏拼接属性名，初次 grep `read_lat` 漏检，已复核）；编译内默认
   2ms/10ms == 冻结 IOX 值（kyber-iosched.c:69-74），native 臂显式写 + 读回断言。[FIX-C]
7. **native vrate 经 io.stat cost.vrate 可观测**（blk-iocost.c:3110），native io_cost 臂时序数据源
   落地。[FIX-C]
8. **`--stats-interval-ms` 已落地**（loaders_stats1 bundle，2026-11-14）但**仅 5 个 loader 支持**；
   q_fifo/q_none/q_mq_deadline/q_iomax/arb_wdrr 是冻结副本，误传即退出——各臂脚本已改为
   `loader_supports_stats` 二进制探测后传参（附录 D）。[FIX-C]
9. **LC 必须显式 `-p 2`**（默认 prio 7 = BE band；ioext_loader_common.h:39；角色边界
   `arb_prio<3`，arb_iolatency.c:5-6）。[FIX-C]
10. **io_latency 臂 BE 权重 200** [GAP-C]（plan 未定，沿 homo_v2 冻结 LC_W/BE_W=100/200）；
    **io.max 臂 QD16** [GAP-C]（plan 未定，对齐 BE QD16 口径）；**fio randseed 冻结** [GAP-C]。
    三项待主会话确认。

## 10.3 附录 D：--stats-interval-ms 落地状态、依赖表与字段来源

**落地状态（[FIX-C] 2026-11-14 复核）**：该开关**已落地**——`experiments/reproduction/loaders_stats1/`
bundle（loader_stats_interval.patch，基线冻结包 + 只读 map 遥测）。**仅 5 个 loader
支持**：`loader_q_kyber / loader_q_linnos / loader_arb_iocost / loader_arb_iolatency / loader_arb_d2fq`
（证据：loaders_stats1/BUILD_PROVENANCE.txt；patch 内 ioext_cli_args.h 公共选项 +
arb_* 各自 OPT_STATS_INTERVAL 表；二进制 `strings` 计数 5 支持/5 不支持）。其余 5 个
（q_fifo/q_none/q_mq_deadline/q_iomax/arb_wdrr）是冻结副本、**传该 flag 即 "unknown option" 退出**——
各臂脚本已按 `loader_supports_stats`（二进制探测）逐 loader 决定是否传参。

| 实验 | 支持该开关的 loader | 采样间隔 | 缺开关时的行为 |
|---|---|---|---|
| mq_deadline | 无（q_mq_deadline 冻结副本） | — | 不传参；核心图全来自 fio |
| kyber | **q_kyber（硬依赖）** | 1s | 脚本 fail-stop（`IOEXT_ALLOW_NO_STATS=1` 才降级；native 柱本就无内部观测） |
| io_max | 无（q_iomax 冻结副本） | — | 不传参；核心图来自 fio（收尾 bucket dump 仍在） |
| io_weight | 无（arb_wdrr/q_fifo 冻结副本） | — | 不传参；份额核心图来自 fio bytes |
| io_cost | **arb_iocost（硬依赖）** | 1s | 脚本 fail-stop（vrate 时序无法产出） |
| io_latency | arb_iolatency（可选） | 1s | 核心图（LC P99+BE 带宽）来自 fio；boost/FSM 时序缺失仅记偏差 |
| d2fq | arb_d2fq（可选） | 1s | 份额核心图来自 fio bytes |
| linnos | **q_linnos（硬依赖）** | 1s | 脚本 fail-stop（诊断判据与拦截率时序均不可得） |

stats JSON 字段命名来源（[FIX-C]）：主依据 = `loaders_stats1/loader_stats_interval.patch` 新增的
`ioext_stats_json.h`（q_kyber：per-hctx depth_limit[4]/inflight/cur_domain/batching/domain_p99；
arb_iocost：vrate_fp/vrate/eff_quantum/window FSM late/good；q_linnos：per (busy band,class)
inserts/intercepted + dispatch/inflight/lost，由 loader_q_linnos.c 自带 mirror dump）；
复核依据 = 子智能体A **ALGORITHM_AUDIT.md**【占位引用：该文件可能尚不存在；脚本内 stats 断言
均为宽松 grep，字段落定后收紧】。

---

## 11. RUN_INDEX.csv 列定义与填写约定

列（`experiment,algo,arm,rep,run_id,status,start_ts,end_ts,phase,fio_json_path,stats_log_path,
sentinel_before,sentinel_after,notes`，另附 `figure_data` 尾列以满足模板另一处口径）：

- `experiment`：mq_deadline / kyber / io_max / io_weight / io_cost / io_latency / d2fq / linnos。
- `algo`：臂的策略名 —— none 臂 `none`；native 臂 `mq-deadline`/`kyber`/`io.max`/`bfq+io.weight`/
  `io.cost`/`io.latency`；iox 臂 `q_mq_deadline`/`q_kyber`/`q_iomax`/`arb_wdrr`/`arb_iocost`/
  `arb_iolatency`/`arb_d2fq`/`q_linnos`；N/A 臂无行。
- `arm`：none / native / iox。
- `rep`：1-5；诊断行 `diag`。
- `run_id`：`<experiment>_<arm>_r<rep>`（同 run 的多个 phase 行共用）。
- `status`：planned（模板默认）/ running / done / failed / diagnostic / na。
- `phase`：60s 单租户实验 = `main`；90s coloc = `lc_p1`/`lc_p2`/`lc_p3`/`be`；linnos = `lc`/
  `be_s1`..`be_s4`。同 run 的 LC 三 phase 共享同一 fio json（一次 fio 调用，json 内 3 jobs），
  分 phase 时序来自 `write_*_log` 文件（`<algo>_<arm>_<phase>_lat/_bw/_iops.log`）。
- `fio_json_path`：`/dev/shm/ioext_repro/<experiment>/<arm>/r<rep>/<run_id>.{main|lc|be}.fio.json`
  （`main` = 单租户整跑；`lc` = LC 一次调用含 3 phase；`be` = BE 一次调用）。
- `stats_log_path`：iox 臂 = 同目录 `<run_id>.stats.log`（arbiter+q loader 追加混写）；
  native 臂 = `<run_id>.native_state.log`（1s 外部采样：scheduler/vrate/io.stat）；none 臂 = `-`。
- `sentinel_before/after`：漂移百分数（相对 campaign 基准 `_sentinel_ref.json`；`sentinel_before`
  = 该 run 第一 phase 行上 = 前一臂收尾哨兵第 3 发漂移；`sentinel_after` = 最后 phase 行上 =
  本臂收尾哨兵第 1 发漂移；其余行 `-`；无基准时 `NA`）。>20% 在 `notes` 记
  `PHASE_ANOMALY`（不淘汰 [MSC]）。
- `notes`：轮换序号、异常、偏差引用。
- `figure_data`：该 phase 服务的核心图代号（见 §1.6 图列；如 `kyber:lc_p99+depth_limit`）。

---

## 12. 主会话执行清单

### 12.1 推荐执行顺序（理由附后）

| 序 | 步骤 | 预计 | 前置 |
|---|---|---|---|
| 0 | smoke：env_preflight 全绿 + `sentinel_make_ref` 产出哨兵基准 `_sentinel_ref.json` + 任选 1 臂 10s fio 试跑 | 15 min | guest 就绪 |
| 1 | **io_latency**（[PLAN] 最高优先级，先跑留足返工预算） | ~45 min | smoke |
| 2 | kyber（同 step 驱动工作流，验证 step 协议与 stats 输出） | ~45 min | stats1 bundle 部署 |
| 3 | io_cost（先跑 `diag_iocost_model.sh` 冻结 model，再 5 rep） | ~20 min 诊断 + 45 min | 诊断 |
| 4 | mq_deadline（单租户最简，调节奏） | ~30 min | smoke |
| 5 | io_max（单租户） | ~30 min | smoke |
| 6 | io_weight | ~45 min | smoke |
| 7 | d2fq（2 臂，快） | ~30 min | smoke |
| 8 | linnos（先 busy_ref 诊断，再 5 rep） | ~10 min 诊断 + 45 min | stats1 bundle 部署 |

顺序理由：io.latency 是计划钦定的最高优先级且其核心图不依赖 stats 开关——可最先产出正式数据；
kyber/io_cost/io_latency 同用 step 协议驱动器，连跑保持工作流温热；单租户的 mq_deadline/io_max
插在中段做节奏调整；d2fq 2 臂快；linnos 依赖诊断且时长最长，压轴。stats 开关已落地（loaders_stats1），
kyber/io_cost/linnos 的硬依赖仅剩"bundle 部署到位 + 二进制探测通过"（附录 D）。

**总预计：纯运行 ≈ 5.5 h；含 rep 间冷却（每 rep 后 10-20s 静默 + 快照转录）与记录 ≈ 7.5-8 h，
单日可完成（不含返工与裸机 bm 环境准备）。**

### 12.2 执行时需要主会话注意的坑

1. **stats 开关已落地但只有 5 个 loader 支持**（kyber/io_cost/linnos 核心图依赖的
   q_kyber/arb_iocost/q_linnos 均在支持集内）：部署到 guest 的 loader bundle 必须是
   **loaders_stats1**（或等价含 --stats-interval-ms 的重编包），并把 `IOEXT_LOADER_DIR` 指向它；
   千万不要给 q_fifo/q_none/q_mq_deadline/q_iomax/arb_wdrr 传该 flag（冻结副本，直接退出）——
   臂脚本已内置二进制探测，若手工传参需自查。
2. **q_iomax 的 "max"**：IOX io.max 臂命令行必须是 `--wbps 33554432 --rbps 0 --wiops 0`
   （loader_q_iomax.c:84-97 严格 U64）——照抄 plan 原文会直接报错退出。
3. **io.weight 双载体**：native weight 臂 io.bfq.weight 是 BFQ 真载体；io.weight per-device 写在
   iocost 未激活时 -EOPNOTSUPP（blk-cgroup.c:852-855；脚本已 best-effort 容忍 + 记录）。若期望 io.weight 行生效，
   需先在该盘写一次 io.cost.model（副作用：给 native bfq 臂叠加 iocost，**不建议**，保持声明偏差）。
4. **io.cost 三件事**：model 必须 guest vdb 重测（bm host calibration_bm.json 是 nvme0n1，
   不适用）；model/qos 只能根写；本臂权重是 100:**200** 不是 100:400。
5. **io.latency 自伤模式**：2ms 目标若仍低于可达地板，native 臂会复现"自节流 LC"（homo_v2
   iteration#2 先例）——这是要回答的方向分歧问题的一部分，按数据判读，不算失败。
6. **LC 实例 prio**：所有 iox 臂 LC 侧 `-p 2`（默认 7 = BE band），漏写会让 LC 落进 BE 档
   （iolatency/linnos/d2fq 语义全变）。d2fq 是例外：两租户都 `-p 5`（[MSC]）。
7. **arbiter 不收 -c/-p/-W**：权重/角色只在 q 侧传（ioext_cli_args.h:226-232）。
8. **d2fq/linnos native N/A**：图上 native 柱标 N/A 不留空；bm 裸机 front_n_d2fq 三臂是后续独立
   补充项（本矩阵不含）。
9. **linnos busy_ref 冻结门**：诊断（busy-ref=64 起步）→ 判定三 band 均有 window 占据 → 冻结
   `LINNOS_BUSY_REF_FROZEN` → 才能跑正式 rep（脚本 fail-closed）。homo_v2 参考值 24，勿直接照抄。
10. **哨兵漂移 >20%**：记 `PHASE_ANOMALY` 不淘汰（[MSC]）；判读靠分 rep 时序图。
11. **每次臂间冷却**：teardown 已含 scheduler 回 none + cgroup 摘除 + pin 清理 + 根 io.cost
    enable=0 复位（io_cost 臂）；env_preflight 会拦任何残留。
12. **fio 文件**：三臂同一 job 内容（仅日志前缀不同），LC step 三段在同一 job 文件内 stonewall
    串行（段间零间隔，满足"phase 间不中断调度器状态"）；BE 是独立 90s/120s 调用，先 BE 后 LC 启动。
13. **guest 资源**：6 vCPU 上 fio 单 job + loader + 采样器的 CPU 开销很小，但 128k QD16 seqwrite
    长跑会加热盘——rep 间冷却别省。
14. **RUN_INDEX 填写**：执行驱动按 §11 约定逐行落 ts 与路径；362 行模板已按轮换序预生成
    （110 arm-run 展开 + 2 诊断行），status 全部 `planned`。

---

## 附录 E：实现对齐记录（2026-11-14，主会话定稿 · 正式 campaign 启动前）

执行权威 = `guest/ioext_repro_lib.sh` + `guest/run_experiment.sh` + `guest/run_campaign.sh`
（本文件 configs/ 保留为设计参考；冲突处以 runner 为准）。以下为相对计划正文
与本文前述口径的**全部已定案偏差**，每条带证据：

| # | 偏差 | 证据 |
|---|---|---|
| E1 | D2FQ Native N/A（VM 内），arb_d2fq vs none 两臂；bm 裸机 front_n_d2fq 三臂为后续独立项 | ioextd2fq 依赖 NVMe WRRU；virtio-blk 上 d2fq_en=-EINVAL（D2FQ_NATIVE_PORT_20261110.md + B 验收） |
| E2 | io.latency / io.cost 的 LC 从 4k randread 改为 4k randwrite | **读侧 blkcg 归账在 ioext 内核系统性损坏**（探针：guest root/子 cgroup 读均不计数；bm 裸机 ioext4v3 同样，6s×~50万IOPS 读仅 +313 rios）。读侧完成的反馈（native rq-qos 与 IOX FSM）永不触发；写侧记账/限速正常（io.max 8MiB/s cap 精确执行探针） |
| E3 | io.latency / io.cost 目标 2ms → **12ms**（rpct/其余不变） | LC=write 标定（diag_iolat_none_w）：QD32 p99≈7ms、QD64≈14.5ms → 12ms 只在阶跃段 binding，p1/p3 合规（kvmhost 迭代#2"target 不 binding"教训同源） |
| E4 | kyber 目标 2ms/10ms → **80µs/1ms**（native sysfs 与 IOX CLI 同值） | LC=randread 标定（diag_iolat_none）：QD32 p99≈57µs、QD64≈118µs → 80µs 只在阶跃段 binding 且远离地板 |
| E5 | io_weight / d2fq 租户负载改为 **128k seqwrite QD16 ×2**（同型饱和流） | ①读归账损坏 → 读租户对 arbiter 账本不可见（smoke_d2fq 空账本实证）；②4k randwrite 需求仅 ~130MiB/s 不饱和 → 权重不显现（work-conserving WFQ 份额=需求比）。128k seqwrite 单租户 ~3GiB/s → 两租户必然争用。份额==权重检验保持同型 |
| E6 | native io.weight 载体双写 **io.bfq.weight**（主）+ io.weight（快照一致） | io.weight 是 blk-iocost 的 cftype（blk-iocost.c:3574，A/C 审计一致）；BFQ v2 真旋钮 io.bfq.weight（bfq-cgroup.c） |
| E7 | kyber native sysfs 路径 = `/sys/block/vdb/queue/iosched/{read,write}_lat_nsec`（无 kyber 子目录） | guest 实测（本文附录 A 的 kyber-iosched.c:877 行号不变，sysfs 布局无子目录） |
| E8 | 数据文件 30G×2；fio size=30G | 64G data.img 只装得下 31G×2；32G fio size 会扩文件 → ENOSPC（smoke 实证）；30G 留 fs 元数据余量 |
| E9 | 日志：agg 模式（1s bw/iops）为默认；per-IO 延迟日志仅 kyber/io_latency 的 LC（时序图需要）；结果落 guest root 盘并逐 run gzip | data.img 无空间；per-IO 日志体积（~800k IOPS 时 850MB/相） |
| E10 | io.max / io.cost.model / io.cost.qos / io.latency 写入行必须带 `MAJ:MIN` 前缀 | 无前缀写入无效（格式要求）；已全链路加 `DEVMJMN` |
| E11 | io.cost 模型 = guest vdb 现场标定并冻结（`/opt/ioext_repro/iocost_model.env`） | rbps=1282428580 rseqiops=317715 rrandiops=286557 wbps=640363068 wseqiops=197100 wrandiops=64121（iocost_coef_gen_bm.py file mode /data，60s 档） |

### 冒烟期机制发现（待正式数据确认，均为如实记录）

| # | 发现 | 冒烟证据 |
|---|---|---|
| F1 | arb_wdrr 权重链条端到端正确：100:400 → 份额 0.211:0.789（≈1:3.8） | smoke_weight_iox fio JSON |
| F2 | **arb_d2fq 权重不分化**：同型饱和 128k 写 ×2、权重 100:400 → 份额 50.1:49.9；arbiter 账本 dispatch 归账高度偏斜 | smoke_d2fq_iox fio JSON + stats 账本；对照 F1 → arb_d2fq 特有问题，正式实验将如实产出，不做中途修改 |
| F3 | q_linnos 拦截机制精确：band2 实际拦截率 60.02% vs 目标 60%（确定性 1/100 序号抽签） | smoke_linnos_iox loader stats |
| F4 | q_linnos inflight 计数泄漏（policy 级 completed_request 钩子不触发，completions=0/3.9M）→ busy 恒 band2，band 覆盖退化 | smoke_linnos_iox stats；与 E2 的读归账损坏、F5 的完成钩子问题同族 |
| F5 | arb_iolatency 完成侧反馈在 LC=write 下完全生效：boost 达满档 4、slo 进入 PRESSURE、hold 门触发 14437 次；LC（q_fifo）租户入账正常 | smoke_iolat_iox stats（q_none 直通臂则完全不触发——LC 租户必须用非 bypass loader，已修正） |
| F6 | arb_iocost vrate FSM 全程摆动：vrate_fp 64↔1024（0.25×~4.0×）、370 次步进、DOWN/UP 双向 | smoke_iocost_iox stats |
| F7 | native io.max 精确限速：31.9 vs 32 MiB/s；native io.cost 机制生效（LC 写 p99 23→84ms 随权重预算） | smoke_iomax_native / smoke_iocost_native |
| F8 | q_kyber 深度反馈动态可见：读域 depth_limit 256↔64 摆动（153 行 1s stats） | smoke_kyber_iox stats |
| F9 | scx_io 激活顺序：先 `echo scx_io` 再起 loader 链（与框架 timeline.py 一致）；q_none（BYPASS_MODE_DIRECT）租户的请求不进完成钩子 | B 验收 + smoke_iolat 对比 |

### 统计口径

正式结果 = 5 rep × 臂（轮换顺序），报告 median（CI 说明见 §5.2 语义）；哨兵
3×10s QD1 前后夹持、漂移>20% 记录不淘汰；排除标准 = runner FAIL（RESULT.json）
与 manifest 缺失，不作任何"择优"。

### 数据质量补充（正式 campaign 后，2026-11-14）

| # | 事项 | 说明 |
|---|---|---|
| D1 | **fio 逐窗/per-IO 日志未入 run 目录**：`write_bw_log/write_lat_log` 落在 fio 进程 CWD（campaign 启动目录），同名逐 run 覆盖 → 每 run 的时序日志不可恢复；每实验**最后一轮**的日志幸存（37 个文件，已拉回 `results/stray_logs/`，通过 mtime 与 campaign.log 对齐可归属到具体 run） | 已确证（tarball 3354 项核验 + guest /home/anon 实查）。影响：时序面板改用（a）arbiter/loader 1s 统计账本（5 个 instrumented loader 的 stdout 在 run 目录内 ✓）+（b）fio JSON 分相（3×30s）百分位；（c）幸存单 run 序列作示例（标注）。缺陷根因已定位（runner gzip 只覆盖 RUN_DIR；fio 日志路径与 CWD 绑定），后续 campaign 修复（fio 工作目录设为 run 目录） |
| D2 | **native 内部状态伴随补采**：kyber native 深度（debugfs `sched/{read,write}_tokens` 的 depth）与 io_cost native vrate（io.stat `cost.vrate`）在正式 campaign 未采样；python 采样器伴随重采（`kyber_depth_native/`、`io_cost_vrate_native/` 独立目录，**不覆盖原始正式数据**） | 补采 10 run（各 5 rep），采样器为 python（1s，bash 管道版本在多层引号下失效的教训已记录） |
| D3 | 哨兵漂移判读注意：before 哨兵读的是全洞文件（blkdiscard 后首读 ~93 万 IOPS 假象）→ 写过 lc.bin 的实验 before/after 比值 0.01–0.15 属机制性文件状态效应而非设备不稳；未写 lc.bin 的实验（kyber/io_max/linnos）比值 0.977–1.011 | `RESULTS/sentinel_drift.csv`（110 run） |

### 审查发现采纳记录（E 报告 U1-U6，2026-11-14）

| # | E 的发现 | 主会话处置 |
|---|---|---|
| U1 | mq_deadline 实际 50/50 而非计划 70/30（mk_job_file 签名重写时丢失 rwmixread=70） | **实现缺陷，修复后重跑**：`mq_deadline_v2/`（15 run，70/30），原 50/50 数据保留为 `mq_deadline/`（不作删除）；fig.mq_deadline 以 v2 为正式口径 |
| U2 | io_max 实际 QD32 非矩阵 QD16 | 采纳为已记录偏差（主会话补定：32MiB/s=8k IOPS 的 cap 下 QD32 提供更深的桶压力；QD 不是正确性参数），矩阵在此补记 |
| U3 | step 协议为每相独立 fio 调用（非单 job stonewall） | 采纳为实现口径：相位间隔数秒（调度器/BPF 状态跨调用持续）；与 C 的 stonewall 方案等价性以相位边界竖线标注处理 |
| U4 | state_snapshot 对 io.cost.model/qos 的 grep 键错误（"vdb" vs "253:16"）→ 运行时落盘从未被快照证实 | **已修**（grep "^${DEVMJMN}"）；io_cost native 的实际生效由冒烟期直接 grep 证实（io_cost.qos enable=1 rlat=12ms 读回） |
| U5 | io.bfq.weight 与 kyber 目标无运行时读回断言 | 采纳：本轮以 native 行为差异（kyber 深度不收缩等）间接暴露；后续 campaign 补读回断言 |
| U6 | linnos BE -W 200（计划未定权重） | 采纳为主会话补定（拦截机制与 W 无关；-W 仅经 q_linnos 登记） |
| U7 | D3 哨兵上界舍入差（实为 1.0125） | 更正 DATA_SUMMARY/矩阵相应数字 |
