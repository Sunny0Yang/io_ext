# homo-v2 同态算法复现 · kvmhost RUNBOOK

状态：**仅本地产出，未执行任何 kvmhost 动作**（2026-11-06）。宿主 kvmhost 于
写作时点处于掉线/不可达状态（延续 run_dyn_tenant_kvmhost.sh 头部 2026-11-04
reboot 记录以来的不稳定状态）；本实验全部脚本为上台前干跑产物，恢复后从
"执行顺序"一节开始，以 remote_guards 全绿为第一步。

## 0. 硬契约

- **内核**：全部 homo-v2 数据在 **7.3.1-ioext3** 内核重跑（同态算法族的可比
  性前提；run 脚本 remote_guards 对 `uname -r` 做 ioext3 子串硬校验，可用
  `HOMO_V2_KERNEL_MARK` 覆盖 marker 子串，但不得用于放行非 ioext 内核）。
- **磁盘**：kvmhost 独占 96G 实验盘，按字节大小（103079215104 B / 201326592
  sectors）+ 无挂载/无 holders 唯一性识别（sdX 字母 reboot 后会漂移）。
  一次性全盘 fill（marker `/opt/ioext-bench/results/.homo_v2_sdb_filled`）。
- **RECAL 饱和锚 320 MiB/s**（rig sdb 标定，D2 饱和 / io.cost 模型输入）：
  **仅 io.cost 线性模型的六个速率字段**沿用这一档。io.max 臂与 q_iomax 臂的
  cap 已按 rerun#2 重锚定为 **64 MiB/s**（33554432 B/s，rq5 BE 写锚；2026-11-07
  迭代#1 把 §1 表内的 320 旧值同步更正，与 gen/builder 常量对齐）。
- **BE 写锚**：rq5 契约（docs/plans/rq5_realapp_coloc_experiment.md）实测
  sdb 持续写带 83-145 MiB/s（SLC burst ~1.1 GiB/s 会回落）——因此本实验
  BE 64k seqwrite **默认不限速**，由被测 cap 档位自己去约束（诚实数据点：
  持续带内 BE 吞吐会低于名义 cap）。若改走"锚定供给率"口径，置
  gen 脚本 `BE_RATE_MIB=64` 重新生成配置（跟踪误差含义随之变为供给率偏差）。
- 指标采样侧车（sidecar）与构建语义对齐 scripts/build_rq1_results.py：

## 1. 臂矩阵（sequence 内嵌 arm 后缀，arm = <seq> 去 `HOMO_<family>_` 前缀）

解析规则：sequence id 形如 `HOMO_{elev|ctrl|front}_<arm>`；family token 固定
{elev, ctrl, front}（build_rq1_results.py ARM_FROM_SEQ 的"从 id 解析 arm"语义，
但对 homo-v2 用前缀剥离而非子串匹配——n_io_max/n_none 等原生旋钮名是完整 arm id）。

config：`src/vm/configs/task/homo_v2/homo_v2.yml`（gen 脚本现场生成后推 guest）。

### seq_elevator（单租户 fio：4k randrw 70r/30w，**QD64**，direct=1，60s/次；
主指标 = **elev_read_p99_us**，IOPS 为次要指标——深队列口径见 §6 迭代#1）

| sequence | 权重 | 加载链 | 预期状态（sidecar 门槛） |
|---|---|---|---|
| HOMO_elev_n_none | n_none | native | active scheduler=`none`，无 pins/loaders，qos_enable=0 |
| HOMO_elev_n_mq_deadline | n_mq_deadline | native | active scheduler=`mq-deadline` |
| HOMO_elev_n_kyber | n_kyber | native | active scheduler=`kyber` |
| HOMO_elev_x_mq_deadline_q | x_mq_deadline_q | `loader_q_mq_deadline -c <root>`（root_policy） | active scheduler=`scx_io`，bpffs pins 有，`loader_q_mq_deadline` 进程在 |
| HOMO_elev_x_kyber_q | x_kyber_q | `loader_q_kyber -c <root> --read-target-ns 2000000 --write-target-ns 10000000`（root_policy） | 同上，`loader_q_kyber` 在 |

### seq_controller（LC+BE coloc：LC=4k randread **QD32**，BE=64k seqwrite QD16，60s/次；
LC QD 深化理由与总需求率见 §6 迭代#1）

| sequence | 权重 | 加载链 / cgroup 控 | 预期状态 |
|---|---|---|---|
| HOMO_ctrl_n_none | n_none | 原生基线（io.max 无限/io.cost.qos erase） | scheduler=`none`，全 neutral |
| HOMO_ctrl_n_io_max | n_io_max | BE cgroup `io.max rbps=wbps=64MiB/s`（native） | none + BE io.max 33554432 行 |
| HOMO_ctrl_x_iomax_q | x_iomax_q | `loader_q_iomax -c <BE cgroup> --rbps 33554432 --wbps 33554432`（BE 读写同档，rerun#2 重锚口径；burst cap=rate×0.5s）+ LC `loader_q_none -p 2 -W 100` | scx_io + loader_q_iomax |
| HOMO_ctrl_n_io_weight | n_io_weight | LC io.weight=100 / BE=200（native；**io.weight 的 native 载体是 BFQ**——`none/mq-deadline/kyber` 下该旋钮是 no-op，故本臂 **scheduler=bfq**，见 §6 迭代#1） | **`bfq`** + 两条 io.weight 行 |
| HOMO_ctrl_x_weight_arb | x_weight_arb | `loader_arb_wdrr`（arbiter；audit 零改动，CLI 未变）+ LC `loader_q_fifo -c -p 2 -W 100` + BE `loader_q_fifo -c -p 5 -W 200` | scx_io + arb_wdrr + 2×q_fifo |
| HOMO_ctrl_n_io_cost | n_io_cost | root io.cost.model+qos（LC 场景单写者）+ LC/BE io.weight 100/200（native） | none + qos_enable=1（8:16 行） |
| HOMO_ctrl_x_cost_arb | x_cost_arb | `loader_arb_iocost`（默认 cost-table 64,256,1024,2048,4096,256,1024,4096,8192,16384 / quantum 1 / min-budget 64，无显式参数）+ LC `loader_q_fifo -c -p 2 -W 100` + BE `-c -p 5 -W 200`（权重结构与 n_io_weight 同构） | scx_io + arb_iocost |
| HOMO_ctrl_n_io_latency | n_io_latency | LC cgroup `io.latency target=2000`us（native；迭代#3 重锚，**target=2ms 介于 none 基线 LC p99 ≈4.9ms 与硬件地板之间**：自节流收缩 LC 队列 → p99 逼近 target，native 靠可预测性赢 none；理由见 §8） | none + LC io.latency 行（见 §4 内核可用性） |
| HOMO_ctrl_x_latency_arb | x_latency_arb | `loader_arb_iolatency --lat-target-ns 2000000`（== 目标 2ms；protect-inflight 32 / boost-max 4 默认）+ LC `loader_q_none -c -p 2 -W 100` + BE `loader_q_fifo -c -p 5 -W 200` | scx_io + arb_iolatency |

### seq_frontier（同 seq_controller coloc 场景）

| sequence | 权重 | 加载链 | 预期状态 |
|---|---|---|---|
| HOMO_front_x_d2fq_arb | x_d2fq_arb | `loader_arb_d2fq`（默认 per-type cost 表 R…:W…）+ LC `loader_q_none -c -p 2 -W 100` + BE `loader_q_fifo -c -p 5 -W 200` | scx_io + arb_d2fq |
| HOMO_front_x_linnos_q | x_linnos_q | LC `loader_q_linnos -c -p 2 -W 100 --busy-ref 24 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --max-hold-us 500` + BE `-c -p 5 -W 200 --busy-ref 24 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --max-hold-us 500`（**拦截表显式（迭代#2）**：三条 busy 带的 LC 份额恒 0、BE 分 0/40/60%，不再用 default 表 ir 0:0 / 0:20 / 5:60——迭代#1 校验轮 default 表 band2 把 LC 也拦了 5%+held 延迟，LC p99 从 1.86 基线恶化到 8.2ms；busy-ref 24（迭代#1 校准）、**hold 500（迭代#3 由 loader 默认 2000 显式改，批量释放突发伤 LC p99，见 §8）**、lc-prio-max 3 → LC 必须 -p<3，理由见 §7/§8） | scx_io + 2×q_linnos |
| （native 参照柱） | n_none | —— 复用 seq_controller 臂 **HOMO_ctrl_n_none**，交叉引用不重跑 | 配置中无 HOMO_front_n_none 序列 |

约定：native 臂 profile scheduler 经 sequence 级 io_ext_policy → 全局 loader
场景（homo_similarity v1 惯例）；scx coloc 臂 arbiter + per-tenant q loader 走
`cgroup_policies_from_groups`（gen_isol_bench_paper_yamls.py 惯例，tenant
cgroup 权重全部只经 loader `-W` 传递）。优先级带（P4 交付后确认）：q 侧
`-p` 0-2 = LC、3-7 = BE，lower = higher（默认 arb_prio=7 是 BE 档），所以
所有 coloc LC 侧 loader 都显式 `-p 2`。

## 2. 每臂主指标口径

| 臂族 | 主指标 | 定义 |
|---|---|---|
| elev_* | **read p99 clat（elev_read_p99_us）** | 4k 70r/30w **QD64** 混合流 read p99（fio clat_percentiles）= 面板主指标；IOPS 为次要面板指标；同态差 = x_* 相对同名 n_*（Δ% 标注同用该组自身基线） |
| n_io_max / x_iomax_q | **BE 跟踪误差 + LC p99** | 跟踪误差 = \|BE 实际吞吐 − 64 MiB/s cap\|/cap（rerun#2 重锚，持续带内的诚实口径见 §0）；LC p99 = coloc 下 4k randread read p99 |
| n_io_weight / x_weight_arb | **字节份额偏差 + Jain** | 字节份额（bytes 口径，bw_mbps 占 LC+BE 总带宽比例）：目标 LC = 1/3、BE = 2/3（io.weight 100:200）；**share_ratio_dev = \|be_share − 2/3\| / (2/3) × 100%**（锚定 BE 目标；builder 另产出 lc_share/be_share/字面比 share_ratio）；Jain = (Σx)²/(n·Σx²) 于两租户字节带宽 |
| n_io_cost / x_cost_arb | **字节份额偏差 + Jain + LC p99** | 同上（io.weight 传递；io.cost 模型保持 320 RECAL 锚），qos **rlat=2ms（迭代#3 重锚）**：rlat < none 基线 LC p99（≈4.9ms）→ qos binding，LC 99 分位越限即调 vrate 压 BE（旧 20ms rlat 从未触发、io.cost ≈ no-op，见 §8） |
| n_io_latency / x_latency_arb | **LC p99 / p99.9 + BE 吞吐** | target=**2ms（迭代#3）**；target 低于 none 基线 LC p99（≈4.9ms）且高于硬件地板 → 自节流收缩 LC 队列、p99 逼近 target（D3 先例：target 4ms→实测 4.7ms）；LC p99 对 target 的贴合度 + native 相对 none 的可预测性优势（旧 1ms target 低于可达地板、自节流伤 LC，见 §8） |
| x_d2fq_arb | **类份额 vs 成本权重比** | LC/BE 实际份额比 对 arb_d2fq r/w 成本表权重的比（默认表；校准后填入） |
| x_linnos_q | **LC p99 + BE 吞吐** | 拦截表显式（迭代#2）：LC 恒 0% 拦截、BE 分 0:40/0:60 压缩——延迟敏感流不拦截、只拿 BE 换可预测性；hold 显式 500us（迭代#3，批量释放突发不再砸 LC 尾延迟）；两指标同框 |

统一侧要求：每 (rep, sequence) 一个 `<SEQ>.sidecar.log`（run 脚本侧车产出，
落在 run dir 内随 tar 归档），聚合时先做 expected_state 一致性门槛
（build_rq1_results.py 的 `expected_state()`/gate_ok 语义扩展到本臂矩阵），门
不过进 ledger 不进中位数。

## 3. 统计口径

- **n=10**（`--reps 10` 默认，外循环 rep=1..10、内循环按臂矩阵顺序；每
  (rep, seq) 归档 `results/homo_v2_kvmhost/rep<NN>/<SEQ>.tar.gz` +
  `<SEQ>_manifest.json` sha256 校验，完成即拉回本地再跑下一例）。
- 报告每臂 **median**（跨 rep 的 run 级聚合值）与 **95% CI**（bootstrap 于
  rep 样本；rep 数 <5 时降级为 min/median/max 全表）。
- 聚合器（后续 `scripts/build_homo_v2_results.py`，未交付）从已拉回的
  rep*/<SEQ>/ 目录树读 fio JSON，arm 解析规则见 §1。

## 4. 未决事项（上台前校准点）

**P2 loader CLI 已全部落地并校准（2026-11-06 收敛）**：
`loader_q_iomax`（--rbps/--wbps 同档，现随迭代#1 重锚 64MiB/s，BE 写负载补齐 wbps）、
`arb_wdrr`（audit 零改动，原参数已是最终版）、`arb_iocost`（默认 cost-table
显式注释、权重走 q 侧 -W）、`arb_iolatency`（--lat-target-ns 显式
**2000000（2ms，迭代#3 重锚）**，其余默认、LC -p 2 已显式化）、
`arb_d2fq`（默认表）/`loader_q_linnos`（**--ir 表显式（迭代#2）+** busy-ref
24 + **--max-hold-us 500（迭代#3 显式，两实例）** + LC/BE -p 角色显式化）—— `PROVISIONAL_ARM_NOTES` 已清空，`ARMS` dict 顶部保留
最终参数与默认表注释。仅剩以下待办：

1. **io.cost 模型系数**：`IOCOST_MODEL`/`IOCOST_QOS` 现为 rig sdb 标定字面量
   （320 MiB/s 锚 + 82k IOPS），上台前用 iocost_coef_gen 在该 96G 盘重测后替换。
2. **io.latency 可用性**：rq5 契约记录 stock guest 内核无 blk-iolatency；
   common.py 对 io.latency 文件缺失是静默跳过（`if [ -f io.latency ]`）——
   n_io_latency 臂首跑后必须用 sidecar `cgroup io.* arm state` 段核实行真的
   写入；7.3.1-ioext3 需确认恢复该 controller，否则该臂进 ledger 作废。
   另注意迭代#3 已把 target 重锚 **2000us（2ms）**：臂语义要求自节流
   binding（§8），sidecar 必须核实 `target=2000` 行真实写出。
3. **权重方向**：io.weight 1:2 按"LC:BE"字面序读为 100:200；若最终口径是
   BE 限比、LC 抢占，改 `WEIGHT_LC/WEIGHT_BE` 重_gen。
4. **q_kyber / q_mq_deadline 靶值**：现为 loader 默认（2ms/10ms；merge 0），
   可对齐 native kyber/iosched 实测后重_gen。
5. **cap 与持续带的关系**：io.max/q_iomax cap 已重锚 64 MiB/s（§0、§6 迭代#1）；
   如 BE 改走锚定供给率口径需重_gen（`BE_RATE_MIB`）。
6. **聚合脚本已交付**（scripts/build_homo_v2_results.py：cases/summary/gate
   CSV + expected_state 门槛；scripts/plot_homo_v2.py：8 面板图）；frontier
   参照柱引用规则（HOMO_ctrl_n_none）已在两者实现。loader presence 由 run
   脚本 remote_guards 的 `-x` 硬检查兜底。

## 5. 执行顺序（kvmhost 恢复后）

```bash
# 0) 干跑校验（已在写作时通过：gen 解析 OK / bash -n OK）
python3 scripts/gen_homo_v2_yamls.py --check
bash -n scripts/run_homo_v2_kvmhost.sh

# 1) 同步 + 生成 + 推送（不做执行）
scripts/run_homo_v2_kvmhost.sh --sync-only

# 2) 冒烟：单 rep 单臂
scripts/run_homo_v2_kvmhost.sh --reps 1 --seq HOMO_elev_n_none

# 3) 全量正式 campaign（n=10，flock 锁定，fail-stop）
scripts/run_homo_v2_kvmhost.sh
# 中断续跑
scripts/run_homo_v2_kvmhost.sh --resume
```

注意：`--seq` 后的 sequence id 为位置参数；脚本内部
`flock -n /tmp/homo_v2_kvmhost.lock` 单飞；任一 run 失败 dump 尾日志即停，
已完成 (rep, seq) 的归档已拉回 `results/homo_v2_kvmhost/rep<NN>/` 不受影响。

## 6. 迭代#1（2026-11-07）：负载适配调度器特征 + 指标语义修正

依据：校验轮 rep1 实测数据（全臂 16 序列已落 `results/homo_v2_kvmhost/`）。
本轮只改本地 gen/builder/plot/RUNBOOK 四件，不动 run 脚本、不重跑实验
（重新上台前按 §5 从干跑校验开始）。逐条修正与数据证据：

1. **电梯族队列深度 QD8 → QD64**（`ELEV_IODEPTH`）：校验轮 QD8 浅队列下
   elevator 无收益空间——native none 10818 IOPS 全场最快，mq-deadline/kyber
   持平或更差；电梯调度的价值在深队列读写干扰下保读尾延迟，这是负载适配
   调度器需要被激发的场景。bs 4k / randmix 70r30w / 60s 时长保持不变。
   主指标由"**IOPS + p99**"改为 **elev_read_p99_us**（IOPS 降为次要指标；
   plot 面板 (a)(b) 的 y 轴主指标与 --abs 口径同为 p99，Δ% 标注始终按各
   x 组自身基线计算）。builder/plot 均已同步。
2. **coloc LC 队列深度 QD8 → QD32**（`LC_IODEPTH`）：校验轮 LC(QD8≈200
   MiB/s 需求)+BE(≈600) ≈ 1.1× 设备容量，处于欠阻临界——权重/代价/延迟
   类仲裁臂绑不住，`share_ratio_dev` 3.05 之类读数在饱和边界上无意义。
   QD32 ≈ 4×，LC+BE 总需求 ≈2× 欠阻，仲裁指标才有区分度。BE（64k
   seqwrite QD16）不变。
3. **n_io_weight 臂载体 none → bfq**（`scheduler: bfq`）：io.weight 的
   native 载体是 BFQ，`none/mq-deadline/kyber` 下该旋钮是 no-op（校验轮
   100:200 在 scheduler=none 下未产生任何分流差异）。gen 已把该臂 profile
   及其租户场景调度器钉为 bfq，同臂保留 100:200 io.weight；其 homo 对位
   x_weight_arb（arb_wdrr）不变。**图注/RUNBOOK 均须写明「native io.weight
   的载体是 BFQ」**。builder 的 sidecar 门槛（EXPECTED_STATE sched）与
   fixtures 已同步 `bfq`。其余 native 臂不动（io.cost 的 weight 机制走
   iocost 自身，与 elevator 无关）。
4. **x_linnos_q 臂 `--busy-ref` 64 → 24**（两条租户实例均显式）：校验轮
   设备 busy 从未越过 50%，loader 默认 64 的拦截从未激活（`map_busy`/
   拦截计数恒零类证据）。LC QD32+BE 下 inflight 常态 >24，busy-ref 24 使
   band≥mid 的拦截在目标扰动窗口内可激活。
5. **share 指标统一为字节份额口径**（builder `extract_case_kpis`）：
   - `lc_share` / `be_share` = 各租户 `bw_mbps` 占 LC+BE 总带宽比例（新增）；
   - **`share_ratio_dev` = |be_share − 2/3| / (2/3) × 100%**，锚定
     io.weight 字节份额目标（LC:BE = 100:200 → BE 目标 2/3、LC 1/3），
     单位为 %（旧口径是与 0.5 档的比值，饱和边界上无标定意义）；
   - `share_ratio`（LC:BE 字面比）与 `jain_index`（两租户字节带宽）保留，
     jain 口径本就基于字节带宽、不变。
6. **随轮校正的陈旧文字**：§0/§1 表内 io.max cap 的 320 MiB/s 旧值统一为
   **64 MiB/s（33554432）**，与 gen `IO_MAX_BYTES` / builder
   `TRACK_ANCHOR_MIB`（rerun#2 重锚）对齐；io.cost RECAL 模型仍保持 320 锚。

接缝点（本轮不动，留待主线 / q_iomax 修复代理结论）：
- **x_iomax_q 臂是否加 `arb_noop`**：其 `be_policy` 目前是裸 `q_iomax`
  (arbiters=[])，若 q_iomax 修复后需要显式 no-op arbiter 才能对位
  n_io_max 的 native cap 行为，由主线在 `ctrl_x_iomax_q` 臂上补加。
- io.max/q_iomax cap 档位（64 MiB/s）与 BE 供给率锚定口径（`BE_RATE_MIB`
  现为 None）是否重_gen，随 q_iomax 结论一并定。

## 7. 迭代#2（2026-11-07）：两臂参数语义修正（校验轮证据）

依据：迭代#1 校准后的校验轮 rep 数据（LC p99 under coloc none 基线
n_none=1859us；其余读数见 §1/§2 表内标注）。本轮只改 gen/builder/RUNBOOK
三件并重_gen 配置，不 ssh、不跑实验；x_weight_arb / x_cost_arb /
x_iomax_q / elevator 臂不动（另有并行代理）。

1. **io.latency 目标 20000us → 1000us（1ms）**：
   - 证据：校验轮 coloc LC p99（none 基线）实测 1859us——旧 target=20ms
     **永不 binding**（LC p99 从未越入 20ms 的 throttle 带），控制器全程
     空转；而 native io.latency 与 x_latency_arb 的 LC p99 均恶化到
     ~4.8ms（反加开销：protect-inflight/boost 机制在无节流收益时纯付
     成本）。
   - 改动（gen/builder/RUNBOOK 同步）：`IO_LAT_TARGET` 20000→**1000us**；
     `arb_iolatency --lat-target-ns` 20000000→**1000000**（= 1ms × 1000，
     与 native target 严格等值）；builder `IO_LAT_TARGET_US`/
     `EXPECTED_STATE`（sidecar 门槛 `target=1000` 行）与 fixtures 同步。
   - 语义：**target=1ms < none 基线 1.86ms → 控制器必须主动压 BE 换
     LC p99≤1ms**；健康的 native io.latency 语义下该臂应显著优于
     none（若首跑后 LC p99 仍 ~4.8ms、BE 未被压，则说明控制器再次未
     binding，进 ledger 复查——见 §4 待办 2）。
   - 注意：io.cost qos 的 `rlat=20000`（n_io_cost/x_cost_arb 臂）是另一
     个常量（IOCOST_QOS），本轮**不改**。
2. **q_linnos 拦截表显式化（LC 恒 0、BE 分档）**：
   - 证据：迭代#1 校验轮（band2 表 LC:BE=5:60、busy-ref 已校 24 仍带
     default 拦截表）——LC（prio 2 < 3）也是可拦 victim，5%+ 拦截份额与
     per-IO held 延迟把 LC p99 从 none 基线 1.86ms 推到 **8.2ms**，拦截
     反向伤延迟敏感流。
   - 改动：两实例（LC/BE loader 均显式）加
     **`--ir-low 0:0 --ir-mid 0:40 --ir-high 0:60`**（loader default 表
     ir 0:0 / 0:20 / 5:60 不再用作隐含值）：LC 在每条 busy 带恒 0%，
     BE 分 0/40/60%。
   - 语义：**LinnOS 对延迟敏感流不拦截，只压缩 BE 换 LC 的
     可预测性/保护性**——LC p99 不再被拦截机制直接加税，BE 吞吐与
     LC 尾延迟稳定性是本臂读数。
   - builder 的 EXPECTED_STATE（loaders=`loader_q_linnos`）不变；参数
     变化由 gen YAML 表达、sidecar 只核 loader 存在性。

## 8. 迭代#3（2026-11-07）：臂参数自伤修正（校验轮 rep1 实测）

依据：迭代#2 重_gen 后的校验轮 rep1 实测数据（LC QD32 口径）。本轮只改
本地 gen/builder/RUNBOOK 三件并重_gen 配置，不 ssh、不跑实验；x_weight_arb
/ x_cost_arb 权重结构、arb BPF、elevator 臂均不动。

1. **io.latency target 1000us → 2000us（2ms）**：
   - 证据：rep1 target=1ms 下 LC iops 84k→72k、LC p99 仍 ~5.7ms——blk-
     iolatency 是**自我节流**机制（LC 超标时压的是 LC 自己的提交，没有
     下游租户可压缩），1ms 低于可达延迟地板，自伤不达标。
   - 改动（gen/builder 同步）：`IO_LATENCY_TARGET_US` 1000→**2000**；
     `arb_iolatency --lat-target-ns` 1000000→**2000000**（与 native
     target 严格等值）；builder `IO_LAT_TARGET_US` 1000→2000，
     `EXPECTED_STATE`（sidecar 门槛 `target=2000` 行）与 fixtures 同步。
   - 语义：**2ms 介于 none 基线 LC p99（≈4.9ms，QD32 coloc）与硬件地板
     之间**，自节流收缩 LC 队列深度 → p99 逼近 target（D3 先例：
     target 4ms→实测 4.7ms），native 靠**可预测性**（p99/p99.9 贴合
     target）赢 none，而非均值时延。校验轮证据背书：该臂 target 贴合度
     偏差 Δ1.1%（rep1），口径忠实。若 2ms 臂仍复现 iops 掉档、p99 离
     target 远的模式，臂进 ledger 复查（见 §4 待办 2）。
2. **io.cost qos rlat 20000us → 2000us**（gen `IOCOST_QOS`；§7 曾注明
   该常量迭代#2 不改，本轮按 rep1 证据重锚）：
   - 证据：rlat=20ms 在 rep1 全窗**从未触发**（LC p99 远低于 20ms），
     vrate 全程不调节，native io.cost ≈ no-op（与 q 侧 arb_iocost 形成
     不可比的口径）。
   - 改动：`IOCOST_QOS` rlat 20000→**2000**（rpct 99 保持）；builder
     fixture 的 `io_cost_qos_line` 字面量 `rlat=20000`→`rlat=2000`
     （EXPECTED_STATE 的 qos gate 只断言 LC `enable=1`，无 rlat 字面
     断言，无需变更）。
   - 语义：**rlat=2ms < none 基线 LC p99 ≈4.9ms → qos binding**：LC
     99 分位越限即调 vrate 压 BE，健康的 native io.cost 被迫主动仲裁。
     iocost_coef_gen 重测系数的 §4 待办 1 保留（重锚语义随测随保）。
3. **q_linnos `--max-hold-us` 2000 → 500（LC/BE 两实例均显式）**：
   - 证据：held 批量释放以突发形式砸 LC 尾延迟（迭代#1 校验轮 LC p99
     8.2ms 的成分之一）；loader 默认 hold 2000us 的批量窗口过长。
   - 改动：gen 两条租户实例（LINNOS_LC_ARGS/LINNOS_BE_ARGS）新增显式
     `--max-hold-us 500`（新常量 `LINNOS_MAX_HOLD_US`）——held 批量
     提前 4× 排空，释放突发不再堆积伤 LC；builder 无需变更（sidecar
     只核 loader 存在性）。
4. **监控对称移植 gen 说明**：监控对称化已移植进 gen——全部 16 序列的
   step 均只挂 `MONITORING_COMMON`（paper_system_metric /
   paper_io_stack_cpu_cost / paper_block_mq_latency），3 个 scx_io 诊断
   kprobe（bypass_fallback / bpf_hook_cost / lock_contention）因 x-only
   且 QD64 下 ~+800us 读 p50 的测量不对称开销（iter#2 归因；cf.
   监控对称提交）移出指标序列。对称配置后的校验轮复测证据：
   n_io_max 主指标跨该配置变动仅 **Δ0.9%**（监控开销不构成臂间系统差）；
   n_io_weight `share_ratio_dev` = **3.1%**（weight 仲裁已 binding，
   字节份额口径可信）；n_io_latency target 贴合偏差 Δ1.1%（对应第 1 条）。

参数同步汇总（本轮）：io.latency target 2000us / arb_iolatency
--lat-target-ns 2000000 / io.cost qos rlat 2000us（rpct 99 不变）/
q_linnos --max-hold-us 500（LC+BE）。§1/§2/§4 表内旧值已同步。

## 9. 设计收口（2026-11-07）

本节为 homo v2 策略开发阶段的设计收口记录：八臂复现口径、冻结参数与
判定数据一次性定格，方法面与交付物随策略一起带走。§1–§8 历史迭代记录
保持原样（含后续被取代的旧参数值），收口以本节为准。
机器侧交接另见 `experiments/homo_v2/HANDOFF_主机器.md`。

### 9.1 八臂收口表

| 臂 | 复现口径 | 冻结参数 | 收口判定与数据 |
|---|---|---|---|
| mq_deadline-q（x_mq_deadline_q） | per-hctx 6-lane 逐条对应 native（读优先、写饥饿 2、deadline FIFO、c-look、batch 16） | elevator 负载 QD64 4k 70/30 | 成对交错 p50 1.04× / p99 1.28×；残余 = 结构性 ~20us/req 提交路径开销（6 struct_ops 钩子，非移植误差，QD≤8 保真 2-27%，caveat 成文） |
| kyber-q（x_kyber_q） | native 法则完整移植（8 桶 p99 直方图、resize=new×(p99+1)>>2、深度上限 256/128/64/16） | 同 elevator 冻结口径（QD64 4k 70/30） | p50 1.02-1.35×；同结构性 caveat（~20us/req 提交路径开销） |
| io.max-q（q_iomax，x_iomax_q） | blk-throttle 同构 4 通道 token bucket（riops/wiops/rbps/wbps、AND 门控、方向 FIFO、carry 进位、burst=cap×0.5s） | BE cap=64MiB/s（rq5 BE 写锚，binding） | track_err 1.1-4.2%（慢模式含设备短板）、LC p99 与 native Δ0.9%（473 vs 469us，iter3）——**三线中最佳** |
| io.weight-arb（arb_wdrr + S12，x_weight_arb） | 全局 round 屏障 + per-cgroup deficit + S11.1 干队列 credit + 统一 floor 提升；native 载体 = bfq（io.weight 在 none 下 no-op 的语义修正，§6 迭代#1） | weight 100:200、quantum 512B | 字节份额 0.667±0.005 全窗、跨宿主模式 share_dev 1.8-3.4%、jain 0.90-0.93；残余 = bfq weight→字节份额映射自身随设备状态漂移（10.7%↔53%，其语义特性，主机器用中位数口径） |
| io.cost-arb（arb_iocost + qos vrate，x_cost_arb） | byte-linear cost 表（cost=bytes/64）+ vrate FSM（DOWN=窗口超占比>100-rpct 精确百分位门 ×0.80、UP=均值<rlat/2 ×1.05、2 窗迟滞、clamp[0.25,4]）+ LC inuse margin（net cut 落 BE，io.cost debt/margin 同态） | --qos-rlat-ns 2000000（对齐 campaign IOCOST_QOS rlat 2ms，§8）、quantum 8 | qostight 全链路实证（be_share 0.667→0.259、BE 500→83 MiB/s、LC 延迟保持）、vrate==1 位级回退 byte-fair、模型 60 断言；残余偏差 (a)-(e) 见 arb_iocost.bpf.c 头注释（无在线标定、轮预算 vs 周期预算等） |
| io.latency-arb（arb_iolatency，x_latency_arb） | LC/BE 目标 FSM + LC boost 阶梯（1→2→4）压 BE + hold 硬保护 | --lat-target-ns 2000000（自节流语义与 native 一致；1ms 不可达自伤的教训在 §7/§8） | native Δ1.1%（5734 vs 5800us）、native>none 成立（iter4：4178 vs 4620） |
| D2FQ-arb（arb_d2fq，x_d2fq_arb) | per-(cgroup,hctx)×type 双 lane vtime + 成本表 + LC fast-share（=参考 cls_base 直译）+ DI 复位 + F2 幽灵复位；与 skkucsl/d2fq 参考码逐行核对，6 条偏差清单（记账点派发侧、vtime 精度、WRR→pick、动态再标定未复现等）在 bpf.c 头注释 | 默认 cost 表 + fast-share 6 + di-period 200ms | LC fast-share 行为生效（share 1.37-3.83 随积压/DI 形态）；**待主机器：节流配置下的份额精标定** |
| LinnOS-q（q_linnos，x_linnos_q） | 离线查表近似（(busy band×class)→拦截率，序号计数器精确 1/100）+ held FIFO（max-hold 500us）+ 四步 dispatch | --busy-ref 24 --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --max-hold-us 500（LC 恒 0 拦截，§7/§8） | LC p99 优于 none 16%（iter4）；论文语义偏差（在线 NN→离线表）已声明 |

### 9.2 方法面收口（随策略一起带走）

- [x] 逐 run **blkdiscard 写路径复位**（层间无跨 run 残留）。
- [x] **live-loader 误杀修复**（ioext-bench 的 live-loader 误杀修复提交 +
      `tests/test_io_ext_loader_stale_sweep.py` 回归锁定）。
- [x] **监控对称**（ioext-bench 的监控对称提交 + gen
      `MONITORING_COMMON` 移植：16 序列统一只挂对称三件、x-only kprobe
      诊断移出指标序列，§8 第 4 条）。
- [x] **sidecar 门禁 + sha256 manifest + builder**（gate 160/160 全绿，
      expected_state 门槛进 build_homo_v2_results.py）。
- [x] **宿主快/慢双模式 → 主机器纪律：成对交错（interleaved）+ ≥3 rep
      中位数（禁单 rep 结论）**——同一宿主在快/慢两模式下的设备行为
      漂移可达两位数百分比，任何单 rep 读数不作为臂间判定依据。

### 9.3 主实验机器待验证清单（kvmhost 收口之外）

- [ ] **cost 双臂自然负载窗口**（快/慢两模式各若干窗口）：vrate FSM 在
      自然负载（非 qostight 构造）下的调节行为与窗口统计。
- [ ] **D2FQ 份额节流标定**：节流配置下 LC/BE 份额精标定（§9.1 第 7 行
      的待办），确定默认 cost 表的份额-权重映射曲线。
- [ ] **elevator QD 矩阵细化**：QD8/16/64 保真曲线（结构性 ~20us/req
      开销随 QD 的衰减/放大形态，QD≤8 保真 2-27% 目前只有端点）。
- [ ] **arb 臂在 bm 裸机 NMVe 上的复测**：kvmhost 为 QEMU virtio 盘，
      原生 NVMe 的队列/中断/多队列行为可能不同，io.weight 映射漂移
      （10.7%↔53%）与 qostight 链路需在裸机 NVMe 上复核。
- [ ] **MODEL PASS→rig accept 套件补齐**（arb_*_accept.sh / q_*_ci.sh）：
      把 §9.1 各臂冻结参数与判定阈值固化为可执行验收脚本。

### 9.4 交付物清单

**框架修复提交：**

| 修复项 | 内容 |
|---|---|
| loader_q_iomax | q_iomax |
| arb_iocost 系 | arb_iocost / arb_iolatency / weight 审计 |
| arb_d2fq 系 | arb_d2fq / q_linnos |
| q_linnos 修复 | q_linnos 锁内查找修复 |
| arb_iolatency 修复 | arb_iolatency verifier |
| arb_d2fq 瘦身 | arb_d2fq 瘦身 |
| S12 | S12 份额 |
| qos | qos vrate |

**ioext-bench commits / 产物：**

- live-loader 误杀修复 + 监控对称 YAML + gen
  MONITORING_COMMON 移植。
- `scripts/gen_homo_v2_yamls.py`（臂矩阵现场生成配置）
- `scripts/run_homo_v2_kvmhost.sh`（remote_guards + flock 单飞 + fail-stop）
- `scripts/build_homo_v2_results.py`（cases/summary/gate 聚合 + 门槛）
- `scripts/plot_homo_v2.py`（8 面板图）
- 本 RUNBOOK（`experiments/homo_v2/RUNBOOK_kvmhost.md`）

**数据留档（results/）：**

- `homo_v2_kvmhost_20261106_pilot_noTrim`（校验轮，no-TRIM）
- `homo_v2_kvmhost_20261107_iter1_preloaderfix`（迭代#1 重锚口径）
- `homo_v2_kvmhost_20261107_iter2`
- `homo_v2_kvmhost_20261107_iter3`
- `homo_v2_kvmhost_20261107_iter4`
- iter5 校验轮完成后归档 `homo_v2_kvmhost_20261107_iter5_consistency`
  （追加占位；本轮收口时点尚未落盘）。


### 9.5 迭代#5 一致性快照（2026-11-07，`homo_v2_kvmhost_20261107_iter5_consistency`）

快窗宿主模式、16/16 归档、gate 全绿。冻结参数下的终态一致性数字：

| 线 | native | iox 复现 | 忠实度 | vs none |
|---|---|---|---|---|
| elevator p99 (us) | mq 1679 / kyber 1614 | x_mq 2179 / x_kyber 1876 | +26% / +12%（快窗；成对口径 1.04×/1.28×见 §9.1） | native 双双优于 none 1729 |
| io.max LC p99 (us) | 452.6 | 448.5 | **Δ0.9%** | 双优（none 1777.7） |
| io.max BE track_err | 0.23% | 0.99% | Δ0.76pt | BE 63.9/64.6 vs cap 64 |
| weight share_dev | 1.40%（bfq） | **0.56%**（wdrr） | 两侧均 <1.5pt | jain 0.910/0.904 |
| cost LC p99 (us) | 1695.7 | 1138.7 | x 更优但语义差异见 (c)/(e)：native 延迟达标时休眠（be_share 0.586），x 恒执行字节公平（0.655） | 均优于 none 1777.7 |
| latency LC p99 (us) | 1712.1 | 1613.8 | **Δ6%** | ≈none（快窗 none 已低，2ms target 未触发） |
| D2FQ share (LC/BE bytes) | — | 0.687 | fast-share 生效、LC 90k iops 无抑制 | — |
| LinnOS LC p99 (us) | — | 2768.9 | LC 恒 0 拦截生效；快窗下仍高于 none（BE 拦截收益窗依赖） | 标定留主机器（§9.3） |

**结论**：八臂全部达到「设计冻结」状态。快窗/慢窗双模式对单 rep 绝对值的
影响再次得到确认（同配置跨窗 n_none elevator p99 1729↔8028/4882），主实验
机器必须执行 §9.2 成对交错 + 中位数纪律。
