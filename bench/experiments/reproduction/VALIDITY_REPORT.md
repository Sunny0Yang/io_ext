# VALIDITY_REPORT — io_ext 算法复现实验有效性审查（独立审查员 E）

- 审查日期：2026-11-14
- 审查人：子智能体 E（独立于实验执行与其他审查智能体；未修改任何数据、图、矩阵或既有报告）
- 审查对象：`results/campaign/`（112 run）、`results/kyber_depth_native/`、`results/io_cost_vrate_native/`、`results/stray_logs/`（37 文件）、`RESULTS/`（8 图 + data CSV + 脚本）、`EXPERIMENT_MATRIX.md`（含附录 E：E1–E11 / F1–F9 / D1–D3）、`ALGORITHM_AUDIT.md`、`ENVIRONMENT_REPORT.md`、`guest/ioext_repro_lib.sh`（runner）
- 审查方法：通读计划/审计/环境文档与全部 11 个分析脚本核对口径；用 `python3 -I` 从原始 fio JSON 与 loader stdout **独立复算**关键数字（附录 A），不复述 DATA_SUMMARY；目检全部关键图 PNG。

---

## 0. 总裁定（先给结论）

**本轮证据不足以支撑无限定语的"论文级复现声明"**；足以支撑的是一组**逐算法、带限定语的语义/行为级声明**。

- 8 个算法中：**io.max 支撑**（限速语义在 32 MiB/s 工作点与 native 精确等价）；**io_weight（IOX 侧）、linnos（查表执行面）支撑但范围受限**；**kyber、io_cost、io_latency 部分支撑**（IOX 反馈机制被内部状态证实，但与 native 的行为等价性被数据**否定**或无法建立）；**mq_deadline 部分支撑/机制无法判定**（外部指标对机制无分辨力）；**d2fq 不支撑**（权重→份额在正式 5 rep 中完全失效，份额恒 0.500）。
- 三个最重要的否定性发现（全部有数字证据，且 DATA_SUMMARY 均已如实记录）：
  1. **d2fq 权重不分化**：权重 100:400 下 t400 份额 5 rep 全部 0.4996–0.5005（`results/campaign/d2fq/rep{1..5}/iox/fio_d2fq_{lc,be}.json`），与 none 臂（0.4996–0.5001）无法区分——计划验证目标"virtual time 与流份额正确"**不成立**。
  2. **native kyber 读域深度在 5 rep 中从未收缩**（companion 补采 min=max=256，`results/kyber_depth_native/rep{1..5}/native/depth_trace.log.gz`），而同 workload 同目标（80µs）下 IOX dl[0] 在 64↔256 摆动（5/5 rep）——两控制器决策分歧，归因未定。
  3. **native io.cost vrate 全程恒 150.00**（= qos max 钳位值，伴随补采 5 run ~155 行/run 逐行核实），IOX 同期 0.25×–4.0× 摆动——"vrate 反馈响应 LC 压力变化"在 native 臂无任何响应。
- QC 面干净：110 正式 run `RESULT.json` 全 OK、campaign.log 0 个 FAIL、无择优无删除（本审查用 `RESULTS/scripts/common.py::sanity_report` 独立复核：0 问题）；línnos 2 个诊断 run 未混入正式统计。
- 执行与矩阵文本之间存在 **6 项未记录偏差（U1–U6，§3.4）**，其中 U1（mq_deadline 实际 50/50 而非计划 70/30）与 U5（native 臂多项运行时读回断言缺失/失效）对相应声明的强度有实质影响。

---

## 1. 总口径

### 1.1 环境锚点（来源：`ENVIRONMENT_REPORT.md`）

| 项 | 值 | 来源 |
|---|---|---|
| 宿主 | bm（127.0.0.1），内核 7.3.1-ioext4v3 未动 | ENVIRONMENT_REPORT §0 |
| VM | QEMU 11.0.3，`-cpu host -smp 6 -m 6G`，数据盘 virtio-blk `cache=none discard=unmap`（data.img 64G raw 预分配，落 NVMe nvme0n1p2） | ENVIRONMENT_REPORT §1.4–1.5 |
| guest | Ubuntu 22.04.5，内核 **7.3.1-ioext3**（deb sha256 elided…），fio-3.28，vdb=253:16 ext4 `/data`，lc.bin/be.bin 各 31G（E8） | ENVIRONMENT_REPORT §1.6/§1.8、矩阵 E8 |
| 验收 | NVMe 落盘字节闭合（guest 写 3,807,309,824 B ≈ 宿主 nvme 增量 100.76%）、BTF sha256 与 loader PROVENANCE 逐字节一致、四类 native 接口"真写出"读回验证 | ENVIRONMENT_REPORT §2.A–C |

**环境限定**：一台 VM、一个设备（virtio-blk on NVMe 文件镜像）、一套负载族（4k randread/randwrite + 128k seqwrite）、一个内核版本。所有"复现"声明只能限定为"**该 virtio-VM 栈上的语义/行为级复现**"。

### 1.2 统计口径（来源：矩阵 §1.5、附录 E"统计口径"、`RESULTS/scripts/*.py` 实读核对）

- 每实验 5 rep × 2–3 臂；报告值 = **5 rep 中位数**；无置信区间、无显著性检验（n=5 不支持）。
- 时序图：**D1 缺陷**使 fio 逐窗日志丢失，图内 1s 序列仅为"幸存单 run 示例"（rep5 iox，图上红字标注），全 3臂×5rep 的 1s 序列缺失——kyber 的 p99 时序面板直接留空标注（`RESULTS/kyber/fig.kyber.png` 上面板红字）。
- 哨兵 3×10s QD1 前后夹持；漂移 >20% 记录不淘汰（[MSC]）。
- 排除标准 = runner FAIL / manifest 缺失；本轮 **110/110 全 OK，零排除**（`campaign.log` 末行 DONE、grep FAIL 计数 0；`common.py` sanity 0 问题）。

### 1.3 QC 事实

- campaign id `repro_20261113_183153`，执行顺序 `[mq_deadline io_max io_latency kyber io_cost io_weight d2fq linnos]`（`campaign.log:1`）；110 正式 run rc=0 + linnos `diag/rep0` 2 个诊断 run（未入统计）。
- IOX 激活读回全部成立：8 实验 iox run 的 `state_snapshot.log.gz` 均含运行中 `[scx_io]`、起止 `[none]`（本审查逐实验抽查 rep1）。
- IOX 完成侧**写**记账精确：io_latency 臂 arb_iolatency 账本终值与 fio 三相 io_bytes 之和 **0.0% 偏差**（5/5 rep，本审查复算；对照 `RESULTS/scripts/fig6_io_latency.py:44-61` 的 <5% 门）。

### 1.4 已声明偏差的逐条威胁评估（E1–E11、D1–D3）

威胁等级定义：**不构成威胁**（不触及声明核心）/ **降低强度**（声明需加限定语或降格）/ **使声明不可得**（对应验证目标在本轮数据上无法成立）。

| # | 偏差（矩阵附录 E） | 主要受影响声明 | 威胁等级 | 评估依据 |
|---|---|---|---|---|
| E1 | D2FQ/linnos native 臂 VM 内 N/A | d2fq、linnos 的"与 native 对照" | **不构成威胁**（对"native 等价"声明是**使声明不可得**，但该口径在实验前已冻结并如实标注 N/A，非数据缺陷） | virtio 无 NVMe WRRU；ioext3 deb 不含 d2fq（ALGORITHM_AUDIT §10） |
| E2 | LC 4k randread→randwrite（io_cost/io_latency） | io_cost、io_latency 的"读路径"外推 | **降低强度**：两实验变成本轮实际配置下的写路径实验；读侧声明一律不可得 | runner `ioext_repro_lib.sh:344-347` 注释 + 探针证据（guest/裸机同现） |
| E3 | io.latency 2ms→12ms | io_latency 的 target 达标判定 | **降低强度**：12ms 只在阶跃段 binding；实测 p1 达标、p2/p3 超标（§5.6），"target 全程保护"不可声明 | runner :42 `IOLAT_TARGET_NS=12000000`；复算 p99 |
| E4 | kyber 2ms/10ms→80µs/1ms | kyber 的 target 达标判定 | **降低强度**：p1 三臂均低于 80µs（无控制必要），p2 三臂均超（52–121µs）→ 外部 P99 上两臂与 none 无分辨差异 | runner :225-227；复算 kyber p99 |
| E5 | io_weight/d2fq 负载改 128k seqwrite QD16 ×2 | io_weight、d2fq 份额检验 | **不构成威胁**（对 io_weight 是正确决策：IOX 份额 0.775 紧贴 0.8）；对 d2fq 的失效本身不背锅（同型饱和流下份额应分化，实际 0.500——是**实现问题**不是负载问题，见 §5.7） | 矩阵 E5 + F1（wdrr 同负载下分化正确） |
| E6 | native io.weight 双写 io.bfq.weight（主）+ io.weight | io_weight native 臂载体有效性 | **降低强度**：主载体写入有代码证据（runner :255-256）但**无运行时读回**（U5）；io.weight 伴写读回成立（快照 `default 400`） | 本审查快照抽查 |
| E7 | kyber sysfs 路径无 kyber 子目录 | kyber native 激活 | 不构成威胁 | runner :226-227 路径与 E7 一致 |
| E8 | 数据文件 31G×2 | 全部（容量类） | 不构成威胁 | fio `size=30G`（job 文件实读），无 ENOSPC 迹象（110 run 全 OK） |
| E9 | agg 日志默认、per-IO 仅 kyber/io_latency LC | 时序图 | **降低强度**（与 D1 叠加后实际全部丢失，见 D1） | job 文件 `log_avg_msec=1000` |
| E10 | io.* 写行必须带 MAJ:MIN | native 臂激活 | 不构成威胁（快照见 `253:16 target=12000` 等正确行） | `io_latency/rep1/native/state_snapshot.log.gz` |
| E11 | io.cost model 现场标定冻结 | io_cost native | **降低强度**：model 行内容有文件（`/opt/ioext_repro/iocost_model.env`，runner :50 引用），但 qos/model 的**运行时读回失效**（U5），激活由 cost.vrate 存在性旁证 | 伴随补采 vrate.log.gz |
| D1 | fio 逐窗/per-IO 日志未入 run 目录（仅 37 文件幸存） | 所有"1s 时序"面板 | **使声明不可得**（对"分 rep 时序判读"这一矩阵 §1.3 口径）：kyber p99 时序留空；io_max/io_weight/d2fq 时序仅单 run 示例；io_latency 用账本 1s 序列补救（IOX 臂） | DATA_SUMMARY §0.1；本审查独立验证幸存日志归属成立（附录 A.8） |
| D2 | native 内部状态为**伴随补采**（独立 run，非 campaign 内观测） | kyber 深度、io_cost vrate 的 native 臂内部观测 | **降低强度**：native 内部状态与 campaign native 臂不是同一次执行，跨 run 平稳性假设未检验（哨兵 kyber/io_max 0.987–1.012 支持设备平稳，部分缓解） | `results/kyber_depth_native/`、`results/io_cost_vrate_native/` |
| D3 | 哨兵 before 读洞文件假象 | 哨兵判读 | 不构成威胁：未写 lc.bin 的 3 实验（kyber/io_max/linnos）比值 0.977–1.012（本审查精确上界 1.0125，D3 写 1.011，纯舍入差，U6）→ 设备稳定性直接证据；写 lc.bin 的 5 实验 0.009–0.148 属机制性文件状态效应，跨臂同条件可比 | `RESULTS/sentinel_drift.csv` 本审查复算 |

F1–F9（冒烟发现）全部被正式数据印证或如实呈现：F2（d2fq 不分化）正式复现（§5.7）、F4（linnos inflight 泄漏）正式复现（§5.8）、F5（iolatency 写侧反馈活跃）正式复现（§5.6）、F6（iocost FSM 摆动）正式复现（§5.5）、F8（kyber 深度动态）正式复现（§5.2）。

---

## 2. 横向有效性

1. **单一环境限制**：所有结论来自一台 VM（6 vCPU）、一个 virtio-blk 数据盘、一套负载族、一个内核（7.3.1-ioext3）。virtio 栈的请求合并/中断/多队列形态（实测 6 hctx，见 io_latency `hold[]` 6 个非零项）与 NVMe/裸机不同；**任何"在 NVMe 上也会如此"的外推都无本轮证据**。矩阵已列 bm 裸机 d2fq 三臂为后续独立项（E1）。
2. **读侧 blkcg 归账损坏的影响边界**（E2，ioext 内核系统性问题，guest+裸机同现）：
   - 受影响：native rq-qos 读侧反馈（io.cost/io.latency 的读路径行为）不可测；IOX 完成侧读归账不可用 → 所有 LC 租户被迫为写。
   - 不受影响：IOX **写**侧完成记账精确（账本 vs fio 0.0% 偏差，§1.3）；native blk-throttle 写限速（io.max 精确贴 cap）；q_kyber 调度器内读延迟采样（dl[0] 有反应——但其 LC 负载是洞读退化负载，见 §5.2）。
   - 结论边界：**本轮不存在任何"读路径调度行为"的有效检验**（kyber 的读域反馈发生在近零真实读流量之上：LC 81–85 万 IOPS、mean clat 8.5–38.6 µs，`fio_p1_lc.json`）。
3. **5 rep 无 CI**：臂间差距远超 rep 方差的结论（io_max 贴 cap、io_weight iox 份额、io_latency iox LC p99）稳健；臂间差 ≤30% 且 rep 间散布同量级的结论（mq_deadline 三臂差、io_latency BE 方向差）**统计功效不足**，只能作观察性描述。
4. **哨兵机制性漂移不淘汰**（[MSC]）判读成立：同实验内三臂同文件状态，比较仍内部有效；哨兵的实际价值在本轮是"未写 lc.bin 实验的设备稳定性证据"（0.977–1.0125）。
5. **无择优、无删除**：110 run 全 OK；DATA_SUMMARY 与全部 data CSV 由脚本从原始数据直出（脚本口径已实读核对，`RESULTS/scripts/`）；línnos diag 不入统计。图上所有数据缺口（p99 面板留空、示例序列标注、N/A 柱）均如实标注，未发现美化性处理。

---

## 3. 本审查新发现（矩阵未记录的执行-计划偏差与缺陷）

| # | 发现 | 证据 | 威胁 |
|---|---|---|---|
| U1 | **mq_deadline 实际 50/50 mix，非矩阵 §2 的 70/30**：job 文件无 `rwmixread`（fio 默认 50），全部 15 run read/write IOPS 比恒 0.500 | `campaign/mq_deadline/rep1/*/jobs/mq.job`（实读无 rwmixread）；DATA_SUMMARY §1 表 IOPS 列；本审查复算 15/15 run ratio=0.500 | **已缓解**：根因为生成器缺陷（非调参）；v2 重跑（`results/mq_deadline_v2/`，job 文件含 `rwmixread=70`，本审查复算 15/15 run read 份额恰 0.700）已按计划冻结负载补齐，原 50/50 集合退出图/统计（见 §4.1 v2 补充判定） |
| U2 | **io_max 实际 QD32，非矩阵 [GAP-C] 的 QD16** | runner `ioext_repro_lib.sh:352` `mk_job_file … randwrite 4k 32 60`；job 文件 `iodepth=32`（DATA_SUMMARY §3 已如实写 QD32，是矩阵文本过期） | 不构成威胁（cap 绑定更充分） |
| U3 | **step 协议实现为每相独立 fio 调用**（p1/p2/p3 三个 job 文件、三次 fio），非矩阵 §12 注 12 的"单 job stonewall 串行" | `ioext_repro_lib.sh:356-369`（`run_fio_pair p1/p2/p3`）；`campaign/io_cost/rep1/native/jobs/` 三个独立 job 文件 | 降低强度：调度器/loader 状态确实跨相保持（快照证实），但相间有 fio 重启间隙（DATA_SUMMARY §0.7 对齐误差 ≤1s 已覆盖）；"相间零间隔"字面未达成 |
| U4 | **linnos BE 实例权重 -W 200**，矩阵 §9 写两实例均 `-W 100` | runner `ioext_repro_lib.sh:317`（`-p 5 -W 200`） | 不构成威胁（q_linnos 拦截只看 band×class，权重只影响 DRR 份额，本轮不检验份额） |
| U5 | **native 臂运行时读回断言缺口**：(a) `state_snapshot` 读 io.cost.model/qos 用 `grep "${BLKDEV}"`（=vdb）而文件行首是 `253:16` → **恒显示 "none"**，qos 落盘从未被运行时证实（含伴随补采 run）；(b) 快照不读 `io.bfq.weight`；(c) kyber native 80µs/1ms 写入无读回断言 | `ioext_repro_lib.sh:112-113`（grep 键缺陷）、:106（快照文件列表无 io.bfq.weight）、:225-227（写无读回）；本审查快照实读（io_cost/rep1/native 与伴随 rep1 均显示 `root io.cost.model: none`） | 降低强度：io_cost native 的 qos 激活由 cost.vrate 存在性旁证（io.stat 打 cost.vrate 需 ioc enabled，blk-iocost.c:3106-3111），kyber native 目标写入仅有代码证据——两处 native 臂激活审计不完整 |
| U6 | D3 写"比值 0.977–1.011"，精确复算上界为 **1.0125**（linnos rep2 iox） | `sentinel_drift.csv` 本审查复算 | 不构成威胁（舍入差异，远低于 20% 阈值） |

另有两条**解释性注意事项**（非偏差）：
- **io_weight none 臂基线不对称**：两条同型 128k QD16 饱和写在无任何权重机制下 BE 份额中位仅 0.2684（0.2072–0.4686）——设备/FS 栈固有偏向 LC。对 native 臂 0.516 的解读必须是"相对 none 基线的上浮"，不能直接读作"BFQ 权重效应 = 0.516/0.8"。该不对称来源未归因。
- **io_latency 的 IOX 账本字节精确而 q_linnos completions=0**：两者同走完成钩子路径，前者 0.0% 偏差、后者计数为 0（5/5 rep）——说明 F4 的泄漏是 q_linnos 策略特定问题，不是完成路径通病。这为"修复 q_linnos 后重跑"的可行性提供了旁证。

---

## 4. 逐实验有效性判定（8 节）

> 判定四档：**支撑 / 部分支撑 / 不支撑 / 无法判定**。所有数字为本审查从原始数据独立复算（附录 A），来源标注文件路径。

### 4.1 mq-deadline —— 判定：**部分支撑（外部行为同型）；机制声明无法判定**（v2 70/30 重跑后维持，读侧同型强化、写侧新增双峰混杂，见 §4.1.1）

- **计划验证目标**：读写优先级与写饥饿保护是否正确实现（图：read/write P99）。
- **证据**：图 `RESULTS/mq_deadline/fig.mq_deadline.png`；数据 `RESULTS/mq_deadline/data.mq_deadline.csv`；原始 `campaign/mq_deadline/rep{1..5}/{none,native,iox}/fio_mq.json`。5 rep 中位数：read p99 none 1417.2 / native 1318.9 / iox 987.1 µs；write p99 none 11600 / native 12124 / iox 8225 µs。native/none：read 0.93、write 1.05（native ≈ none）；iox/none：read 0.70、write 0.71（两方向同比例 -30%）。
- **判定理由**：①"read p99 ≪ write p99"的排序在 **none 臂同样成立**（1417 vs 11600），该模式主要来自负载/设备特性（ext4 日志、写刷盘），**不能作为调度器读写优先级法则的证据**；②native 与 none 在外部 P99 上几乎重合，mq-deadline 法则（read_expire=50ms、writes_starved=2）未产生可分辨外部效应；③IOX 对两个方向同比例改善 ~30%，是机制中立的整体路径效应（与 scx_io 提交路径、merge D0 等系统差混杂，ALGORITHM_AUDIT §1 已声明），与 native **量级不等价**（差 25–32%）。④写饥饿保护：IOX 侧 sel_flip 计数与 native debugfs `starved` 均未采集，无直接证据。
- **有效性威胁**：U1（50/50 而非 70/30，**已由 v2 重跑缓解**，见下）；merge D0 臂间系统差（矩阵已声明）；n=5 无 CI；内部状态零观测。
- **后续工作**：merge 对照（native front_merges=1 vs IOX D0 分离实验，v1/v2 均未做）；采集 IOX sel_flip 与 native debugfs starved/queued 使"写饥饿保护"有直接读数；v2 的写侧快/慢相去混淆（见下）。

#### 4.1.1 v2 补充判定（70/30 计划口径重跑，2026-11-14 追加）

**数据**：`results/mq_deadline_v2/rep{1..5}/{none,native,iox}/`，15 run 全 OK（`RESULT.json` 逐个核验）；job 文件含 `rwmixread=70`（`mq_deadline_v2/rep1/native/jobs/mq.job` 实读）；**15/15 run read IOPS 份额恰 0.700**（本审查逐 run 复算）。激活读回正常（iox 快照 `[scx_io]`）。原 50/50 集合（`campaign/mq_deadline/`）保留但退出图/统计；更新后图为 `RESULTS/mq_deadline/fig.mq_deadline.png`（D 重生成，数据源 `mq_deadline_v2`，点数据 `data.mq_deadline.csv` 已切换）。

**独立复算全表（clat p99, µs）**：

| 臂 | 指标 | rep1 | rep2 | rep3 | rep4 | rep5 | median |
|---|---|---|---|---|---|---|---|
| none | read | 782.3 | 749.6 | 741.4 | 1105.9 | 1089.5 | **782.3** |
| none | write | 905.2 | 839.7 | 782.3 | 4292.6 | 5210.1 | **905.2** |
| native | read | 675.8 | 733.2 | 815.1 | 929.8 | 888.8 | **815.1** |
| native | write | 766.0 | 806.9 | 4079.6 | 11468.8 | 7241.7 | **4079.6** |
| iox | read | 634.9 | 659.5 | 766.0 | 757.8 | 831.5 | **757.8** |
| iox | write | 643.1 | 643.1 | 13959.2 | 4292.6 | 14876.7 | **4292.6** |

**判定逐条**：

1. **原判定"部分支撑（外部行为同型）；机制声明无法判定"维持不变**，但支撑结构变化：
   - **读侧同型被 v2 强化**：三臂 read p99 中位 758–815 µs（极差 7.5%），rep 散布 635–1106 µs 完全重叠，无方向性分歧。v1 中"IOX 两方向系统性偏优 25–32%"的观察**在 v2 未复现**（iox vs native 读侧差仅 3.2%，落在噪声内）——v1 该观察属 50/50 配置/轮次特异，这进一步否定任何"IOX 更优"的解读。
   - **机制声明仍无法判定**：v2 未新增观测面（IOX sel_flip、native debugfs starved 均未采；q_mq_deadline 为冻结副本不支持 stats）；外部 P99 形态仍无机制分辨力。
2. **写侧 rep 双峰（每臂 2–3 rep 在 0.64–0.91 ms、其余在 4.1–14.9 ms）对验证目标的影响**：本审查的时间序列复算显示双峰与**臂/rep 无关、与执行时间完美相关**——前 6 个 run（none r1–3、native r1–2、iox r1–2）全部快相，后 9 个 run 全部慢相；同一 run 内**读、写两个方向与读/写 IOPS 同步退化**（如 iox r3：read IOPS 102k→60k、write 43.9k→25.9k、read p99 635→766 µs），且 `sentinel_after_1` IOPS 随执行顺序单调漂移（25,309→45,608）。这三条共同指向**全局设备状态漂移（快/慢相）**而非调度器差异。因此：
   - **臂间 write p99 中位数比较不可用**：中位数落在哪个相由该臂分到的快相 run 数决定（none 3 快 2 慢→中位在快相 905；native/iox 2 快 3 慢→中位在慢相 4080/4293）——"native/IOX write p99 是 none 的 4.5 倍"这类读数是相分配伪影。
   - **"读写优先级与写饥饿保护是否正确实现"无法从现有数据分辨**：慢相内 write 相对 read 的放大倍数 none 4.3–4.8×、native 5.0–12.3×、iox 5.6–18.2×，n=2–3 且区间重叠；快相内三臂 write/read 比均近 1（0.86–0.99），同样无机制信号。**如实判定：无法从现有数据分辨设备状态效应与算法差异。**
3. **新增有效性威胁**：mq v2 写侧结论受"run 顺序—设备相位"混杂；任何 write 侧跨臂声明需先去混淆（rep 间冷却/随机化 run 顺序，或每 rep 前后加设备状态探针）。读侧结论不受此影响（快慢相读 p99 差 <50% 且三臂同相漂移）。

### 4.2 kyber —— 判定：**部分支撑**

- **计划验证目标**：延迟反馈能否正确驱动动态队列深度调整（图：LC P99 + depth limit 时序）。
- **证据**：图 `RESULTS/kyber/fig.kyber.png`（上面板 p99 时序留空标注，下面板深度）；数据 `RESULTS/kyber/data.kyber.csv`；IOX 内部状态 `campaign/kyber/rep{1..5}/iox/loader_q_kyber.stdout`（每 rep 145–149 个 1s 样本）；native 深度补采 `results/kyber_depth_native/rep{1..5}/native/depth_trace.log.gz`（153–154 行/run）。
  - IOX：5/5 rep 读域 dl[0] 取值集合恰为 {64, 256}（双稳，本审查逐行复算），塌落与 LC 流量启动同步；写域未检验（图未画）。
  - native：5/5 rep **读域深度恒 256**（min=max=256 逐行核实）；写域在 rep1/4/5 相位边界附近短暂落到 1–8（本审查复算）——native 反馈机构**活着**（写域动过）但读域在同一目标（80µs，E4）与同一负载下从未收缩。
  - 外部 LC read p99：p1 中位 52.0–57.1 µs（三臂均在 80µs 目标内、彼此重叠），p2 中位 101.9–119.3 µs（三臂全部超目标），p2/p1 比 1.92/1.87/2.30（none/native/iox）——**三臂外部行为近同型，与 none 无分辨差异**。
- **判定理由**：IOX 的"延迟反馈驱动深度调整"**机制存在性**被内部状态证实（dl[0] 动态）；但 (a) LC P99 时序不可恢复（D1），"反馈保护了 LC"无法从时序层面检验；(b) 分相 P99 显示两臂控制器面对同样超目标情形做出**相反决策**（IOX 收、native 不收），行为等价性不成立；(c) 归因未定——LC 为洞读退化负载（818k–847k IOPS、mean clat 8.5–38.6µs，DATA_SUMMARY §0.4），native 不收缩可能是**正确控制**（多数请求远快于目标）也可能是 timer/inline 评估相位差（ALGORITHM_AUDIT §2 声明偏差 (1)），本轮数据无法区分。
- **有效性威胁**：洞读效应（最高）；D2 补采非同 run（跨 run 平稳性假设）；U5-c（native 目标写入无读回）；IOX limit 喂值 clamp 到 queue_async_depth 未记录实际值（ALGORITHM_AUDIT §2 声明偏差 (2)）。
- **后续工作**：**洞读归因实验**（正式跑前真实写满 lc.bin 再测，或对比洞读/实读两轮的 native 读域响应与 IOX dl[0]）；采集 native `kyber_adjust` tracepoint 与 IOX 评估时刻对齐；记录 guest `q->async_depth`。

### 4.3 io.max —— 判定：**支撑**（限定：32 MiB/s 单工作点、外部带宽口径）

- **计划验证目标**：token bucket 限速与字节记账正确（图：带宽时序 vs 32MiB/s 线）。
- **证据**：图 `RESULTS/io_max/fig.io_max.png`；原始 `campaign/io_max/rep{1..5}/{none,native,iox}/fio_iomax.json`。5 rep mean bw：native 31.995–31.999（median 31.997）、iox 31.997–31.999（median 31.998）、none 112.7–174.1 MiB/s（median 147.3）——两限速臂与 32 MiB/s 的偏差 **<0.005 MiB/s（<0.02%）**。幸存 1s 示例序列（rep5 iox，`results/stray_logs/lat_iomax_bw.1.log`，本审查独立验证归属成立）：60 行 31.91–32.12 MiB/s，mean 32.01，逐窗 |偏差| mean 0.027 / max 0.121 MiB/s。
- **判定理由**：两机制在 whole-run 口径下限速精度一致到小数点后两位，none 对照排除"负载自身就慢"的解释；native 激活读回完整（快照 `wbps=33554432`，E10 格式正确）。机制层面（token vs slice、burst 形态）不在外部带宽口径的分辨范围内，但**限速语义声明**被充分支撑。
- **有效性威胁**：单一速率点（32 MiB/s）——审计 #3-1 指出低速率通道（8–16 MiB/s）两臂 burst 形态可分辨，本轮未测；记账层差异（bio vs request，已声明）；1s 序列仅单 run（D1）。
- **后续工作**（若要更强声明）：补 8/16 MiB/s 两档 + 大请求（128k）边界档，检验 burst 约定差异的实证形态。

### 4.4 io.weight / arb_wdrr —— 判定：**支撑（IOX 语义级）**；与 native BFQ 的等价性**不支撑**

- **计划验证目标**：权重→长期字节份额（图：份额；注明语义级实现非 BFQ 完整复现）。
- **证据**：图 `RESULTS/io_weight/fig.io_weight.png`；原始 `campaign/io_weight/rep{1..5}/{none,native,iox}/fio_w_{lc,be}.json`。BE 份额（io_bytes 口径，本审查全表见附录 A.1）：none 0.2072–0.4686（median 0.2684）；native 0.4147–0.7561（median 0.5160）；**iox 0.7579–0.7960（median 0.7751，目标 0.8，缺口 3.1%，极差 0.038）**。示例 1s 份额序列（rep5 iox，`stray_logs/lat_w_{lc,be}_bw.1.log`，归属验证：份额 0.7921 与该 run fio JSON 字比 0.7921 **精确相等**）全程贴 0.8，仅 t≈37–40s、74–86s 短暂跌至 0.6–0.75。聚合吞吐三臂同量级（median 393/440/442 MiB/s）——IOX 份额控制无系统性吞吐代价。native 激活：快照 `[bfq]` + `be/io.weight: default 400` 读回（E6 伴写）；主载体 io.bfq.weight 写入有代码证据无读回（U5-b）。
- **判定理由**：①IOX 权重→份额语义在两租户争用条件下**被支撑**（0.775 vs 0.8，rep 间极差 0.038，远紧于 native 的 0.341）；②"语义级实现、非 BFQ 完整复现"这句计划给定的话**被证据支撑且是必要的**——native BFQ 同权重下 median 仅 0.5160（0.4147–0.7561），距 0.8 很远且漂移大，若论文声称"与 native io.weight 行为一致"将被自己的数据否定；③none 臂基线本身不对称（0.268），native 的 0.516 只能读作"相对基线上浮"，BFQ 权重的确切传导效率未归因。
- **有效性威胁**：U5-b（io.bfq.weight 无运行时读回）；none 基线不对称未归因；BFQ 无 v2 统计面（ALGORITHM_AUDIT #4-3），native 份额只有外部口径。
- **后续工作**：io.bfq.weight 读回补采 + 载体双向验证（100:400 与 400:100 两档，ALGORITHM_AUDIT 待办 #4）；BFQ 份额 0.516 的归因（write-only 流 + virtio 下的 BFQ idling/budget 行为）。

### 4.5 io.cost / arb_iocost —— 判定：**部分支撑**

- **计划验证目标**：vrate 反馈响应 LC 压力变化（图：vrate 时序；归一化后只比方向）。
- **证据**：图 `RESULTS/io_cost/fig.io_cost.png`；IOX 内部状态 `campaign/io_cost/rep{1..5}/iox/loader_arb_iocost.stdout`（~152 样本/rep，其中活动样本 ~121）；native vrate 伴随补采 `results/io_cost_vrate_native/rep{1..4}/native/vrate.log.gz`（rep5 不完整已排除）。
  - IOX：vrate_fp 非零范围 64–1024（=0.25×–4.00×）全覆盖，down/up 步进 106–235/114–274（本审查逐 rep 复算，与 DATA_SUMMARY §5 一致）。**但相位均值不锁相**：p1→p2 方向 5 rep 中 3 降（1.45→1.02、1.10→0.28、0.42→0.26）、1 升（1.87→2.31）、1 平（2.27→2.22）；p3（QD 恢复 32）5 rep 全部触底（0.26–0.46×）——FSM 活跃且双向，但对 workload 阶跃的**响应方向跨 rep 不一致**。
  - native：vrate 全程恒 **150.00**（rep1–4 各 ~155 行逐行核实，0 个变化；恰等于 qos `max=150` 钳位值）——LC QD 32→64→32 阶跃期间 native blk-iocost **零响应**。
- **判定理由**：①"vrate 反馈存在且双向活动"（IOX）被内部状态支撑；②"响应 LC 压力变化"只弱支撑（摆动幅度大但相位方向不一致、p3 恒触底说明进入钳位/饱和态）；③native-IOX"只比方向"的对比中 native **无方向可言**（恒钳位值），方向一致性声明不成立；④native 恒 150.00 的机理未归因：可能是"设备在模型口径下未超目标"（正确不动作），也可能是与 E2 同族的记账失效（missed 统计不触发）——U5-a 使 qos 落盘读回缺失，tracepoint `iocost_ioc_vrate_adj` 未采。
- **有效性威胁**：E2（LC=write）；U5-a（qos 无运行时读回）；D2（native 数据为伴随 run）；两臂 vrate 语义不可比（矩阵已声明，图已分轴归一化并注明）。
- **后续工作**：**归因实验**：开 blkcg_debug_stats/tracepoint 拿 native missed_ppm/rq_wait，判定"恒 150"是"无违例"还是"反馈失效"；写一个更小的 max（如 120）验证钳位假设；IOX 侧做一次 phase-locked 阶跃（更长相位、更慢过渡）检验方向一致性。

### 4.6 io.latency / arb_iolatency（最高优先级）—— 判定：**部分支撑**

- **计划验证目标**：SLO 保护行为正确 + IOX 与 Native 控制方向是否一致（图：LC P99 + BE 带宽时序）。
- **证据**：图 `RESULTS/io_latency/fig.io_latency.png`；原始 `campaign/io_latency/rep{1..5}/{none,native,iox}/fio_p{1,2,3}_{lc,be}.json`；IOX FSM `loader_arb_iolatency.stdout`。
  - LC write p99 中位（ms）：none 90.7/101.2/105.4（p1/p2/p3）；native 97.0/83.4/94.9；**iox 7.5/26.3/27.4**。IOX 比 none/native 低 3–13 倍；但 12ms 目标仅 p1 达标，p2/p3 超标（rep 间离散 4.4–81.3 ms）。
  - IOX 反馈链：boost 5/5 rep 到满档 4；slo 状态 0/1/2（NORMAL/PRESSURE/RECOVERY）全出现，PRESSURE 窗口 41–324 个/rep（LC 租户；本审查按 cgroup 拆分复算）；hold 门累计 55,602–131,556 次/rep。
  - BE whole-run 均带宽（MiB/s，中位）：none 207.1、native 249.3、iox **136.3**（native rep 散布 163.0–320.1）。
  - 账本可信度：IOX 完成账本 vs fio 三相字节 **0.0% 偏差**（5/5 rep，本审查复算）。
- **判定理由**：①IOX "SLO 保护行为"部分成立：LC 大幅受保护 + boost/hold/FSM 全链路活跃 + BE 被压（-34% vs none）——方向（保 LC、压 BE）与机制设计一致；②**IOX 与 native 控制方向一致的命题被否定**：native 臂 LC p99 与 none 重叠（97.0 vs 90.7 ms，保护未显现），BE +20%（249.3 vs 207.1，散布大）——实测形态是"native 无可见保护、BE 不减；IOX 强保护、BE 明显减"，即计划预见并在 ALGORITHM_AUDIT §6/HANDOFF §5.8 标注的**机制分歧（native 自节流 vs IOX boost 压 BE）在可观测层成立**——即使只看"最终 P99 接近与否"，本轮两臂 P99 也差 3–13 倍，不存在"结果接近因此机制可互换"的误读空间；③"保护正确"仍受限：12ms 目标 p2/p3 未达标（E3 的 12ms 只在阶跃段 binding 的设计意图只实现了一半），native 自节流是否真的发生（max_depth 是否收缩）无观测（blkcg_debug_stats 未开）。
- **有效性威胁**：E2/E3（LC=write、target=12ms）；门限不同源（IOX >1% vs native ≥10%，ALGORITHM_AUDIT #6-1——FSM 触发时刻两臂天然不可对齐）；n=5 无 CI 使 BE 方向差的功效不足；native depth 状态未采。
- **后续工作**：**native 归因**：blkcg_debug_stats=1 采 LC cgroup `depth` 时序，确认 native scale-down 是否发生（区分"机制未触发"与"触发但无效"）；门限对齐轮（IOX 门限参数化到 10%）；BE 方向结论加 rep 或加压（QD 阶梯更深）以提高功效。

### 4.7 D2FQ / arb_d2fq —— 判定：**不支撑**

- **计划验证目标**：virtual time 与流份额正确（图：累计字节份额；Native N/A；当前实现是静态近似，不得描述为完整 D2FQ 复现）。
- **证据**：图 `RESULTS/d2fq/fig.d2fq.png`；原始 `campaign/d2fq/rep{1..5}/{none,iox}/fio_d2fq_{lc,be}.json`。t400 份额（io_bytes 口径）：**none 0.4996–0.5001、iox 0.4998–0.5005，10/10 run 全部 0.500**（目标 0.8；权重 100:400，两租户同为 128k seqwrite QD16 饱和写，E5 口径）。示例 1s 累计份额曲线（rep5 iox，`stray_logs/lat_t100/t400_bw.1.log`，归属验证 0.5000 精确匹配）全程贴 0.50。
- **判定理由**：同型饱和流下"份额==权重比"是本实验设计的干净检验（矩阵 E5 专门为此排除读写成本不对称），结果 arb_d2fq 的权重**完全不分化**，与 none（无权重机制）不可区分——计划验证目标不成立；冒烟 F2 的发现被正式 5 rep 原样复现，未做中途修改，数据可信。归因（vtime 记账、gvt 选举、pick 排序键、DI 复位、dispatch 归账偏斜——账本 `tenants[].ch/ds` 与按 hctx 高度偏斜，本审查抽查 rep1）未做，**不能区分"实现 bug"与"语义近似在该负载下的失效"**。矩阵的"静态近似"限定语不足以覆盖此结果：静态近似也应产生 100:400 的长期份额。
- **有效性威胁**：E1（无 native 臂，无法对照参考实现分离"参考实现也这样"的可能——但 FAST'21 参考码与 ioextd2fq port 的权重语义明确，0.500 与任何 D2FQ/WFQ 语义都矛盾）；arb_d2fq 内部状态（vt/gvt/band）已随 stats 落盘，归因所需数据在，分析未做。
- **后续工作**：**归因优先级最高**（见 PAPER_EVIDENCE §3 清单）：用已落盘的 `arb_d2fq` stats（vt[2]/ch[2]/ds[2]/gvt/band_picks）重建 vtime 轨迹与 pick 决策；对照 arb_wdrr 同负载同权重（F1 已证 wdrr 分化正确）定位差异层；修复后重跑。**在归因完成前，论文对 d2fq 只能写"权重未分化为已知未解决问题"**。

### 4.8 LinnOS / q_linnos —— 判定：**部分支撑**

- **计划验证目标**：busy band × class 查表能否产生预期 BE 拦截（图：目标 vs 实际拦截率；table-driven approximation 声明）。
- **证据**：图 `RESULTS/linnos/fig.linnos.png`；原始 `campaign/linnos/rep{1..5}/iox/loader_q_be.stdout`（终值累计计数）。5/5 rep：band0=12、band1=8、band2 = 3.6M–4.8M 次插入（band2 占 **99.9995%**）；band2 BE 实际拦截率 **60.000%**（如 rep1：2,628,479/4,380,763），与目标 60% 精确一致；band1 实际 100%（8/8，确定性 1/100 序号抽签在 8 个样本上的体现，无统计意义）；band0 实际 0%（12 个样本）。`completions=0`、`inflight==ins`（单调增长）5/5 rep——F4 泄漏正式复现；本审查逐窗复算：每 rep 仅 **1 个 1s 窗**有 band0 插入、1 个窗有 band1 插入，其余全部 band2。拦截语义 = 500µs hold 后软释放（`disp_held_soft` == band2 BE 拦截数，evac=0、lost=0）——**无请求被丢弃**。
- **判定理由**：①"查表执行面"被精确支撑：被激活的唯一 band（band2）上 5/5 rep 拦截率与表值零偏差（确定性抽签）；②**"busy band × class 查表产生预期拦截"的分带维度没有真正被检验**——busy 代理因 completions=0 单调增长，插入在启动 1–2s 后恒落 band2，band0/1 只有合计 20 个样本；③LC 恒 0 拦截由配置表保证（LC 实例 ir 表全 0，runner :315-316），非机制保证（ALGORITHM_AUDIT §8 风险 ② 的判据口径正确）；④table-driven approximation 声明被支撑（离线查表、无在线学习，与论文在线 NN 的差距已在实现头注释声明）。
- **有效性威胁**：F4（band 覆盖退化）——本轮数据无法回答"busy 跨档时拦截率是否逐档精确"；busy 代理从 dispatch 起算且 inflight 泄漏使 band 判定失真（#8-1）；洞读 LC（同 kyber）。
- **后续工作**：修复 completions 钩子或改用外部 inflight 代理（dispatch−clear 的差分、或 blktrace）→ 重跑 4 段 QD 阶梯使 band0/1/2 均被实质占据；busy_ref 扫描（16/24/32）验证 band 覆盖灵敏度；hold 代价面实验（当前 disp_held_soft 全量软释放意味着"拦截≈500µs 延迟贴条"，与论文"拦截=不合规请求被拒"的语义差异应显式写）。

---

## 5. 审查附录 A：独立复算数字清单

> 全部由本审查用 `python3 -I` 从 `results/campaign/` 原始 JSON / loader stdout / 伴随补采直接计算；与 DATA_SUMMARY 对照结论：**全部一致**（例外：附录 U6 哨兵上界舍入差 1.011→1.0125）。

### A.1 io_weight BE 份额全表（io_bytes 口径）

| arm | rep | lc MiB | be MiB | BE share |
|---|---|---|---|---|
| none | 1–5 | 19703.9 / 24662.4 / 27982.8 / 37116.4 / 24552.6 | 17377.0 / 8089.0 / 7311.6 / 13614.1 / 10856.5 | 0.4686 / 0.2470 / 0.2072 / 0.2684 / 0.3066（med **0.2684**） |
| native | 1–5 | 20077.9 / 11167.6 / 20034.0 / 19170.5 / 29341.6 | 14223.5 / 34626.9 / 17056.0 / 20434.4 / 33594.4 | 0.4147 / 0.7561 / 0.4599 / 0.5160 / 0.5338（med **0.5160**） |
| iox | 1–5 | 6486.8 / 7988.4 / 14325.0 / 8952.4 / 12358.4 | 20671.0 / 25007.1 / 55911.6 / 30854.9 / 47085.2 | 0.7611 / 0.7579 / 0.7960 / 0.7751 / 0.7921（med **0.7751**） |

聚合吞吐中位（两租户合计/90s）：none 393.4 / native 440.1 / iox 442.3 MiB/s。

### A.2 io_max 各 rep mean bw（MiB/s，fio JSON bw 字段）

| arm | rep1–5 | median |
|---|---|---|
| none | 149.107 / 174.147 / 130.303 / 147.331 / 112.670 | 147.331 |
| native | 31.995 / 31.999 / 31.997 / 31.997 / 31.999 | **31.997** |
| iox | 31.997 / 31.997 / 31.999 / 31.998 / 31.999 | **31.998** |

示例 1s 序列（rep5 iox）：60 行，mean 32.01，min 31.91，max 32.12，逐窗 |偏差| mean 0.027 / max 0.121 MiB/s。

### A.3 kyber 深度（读域）

| 臂 | rep | 样本/行数 | 读域 min–max | 取值集合 |
|---|---|---|---|---|
| iox dl[0] | 1–5 | 149/145/148/148/146 | 64–256 | 每 rep 恰 {64, 256}（双稳） |
| native（companion） | 1–5 | 154/153/153/153/153 | **256–256（恒）** | {256} |
| native 写域（companion） | 1/4/5 | 同上 | 1–128 | rep1 {1,4,9,30,64,96,120,128}；rep4 {1,2,4,6,8,32,54,128}；rep5 {1,2,4,6,27,32,128}；rep2/3 恒 {128} |

LC read p99（µs，分相中位）：none 53.0→101.9；native 57.1→107.0；iox 52.0→119.3（p2/p1 = 1.92 / 1.87 / 2.30）。洞读证据：LC p1 IOPS 808k–847k、mean clat 8.5–38.6 µs。

### A.4 io_latency boost/hold/slo（IOX，5 rep）

| rep | boost max | hold 末端累计（6 hctx 求和） | slo 状态（LC 租户窗口分布 NORMAL/PRESSURE/RECOVERY） |
|---|---|---|---|
| 1 | 4 | 119,628 | 297/244/191 |
| 2 | 4 | 131,556 | 302/261/163 |
| 3 | 4 | 72,186 | 304/324/98 |
| 4 | 4 | 55,602 | 642/41/43 |
| 5 | 4 | 84,160 | 453/164/109 |

（BE 租户 5/5 rep 恒 NORMAL。账本终值 vs fio 三相 io_bytes：5/5 rep **0.0% 偏差**。）LC p99 中位：iox 7.5/26.3/27.4 ms vs none 90.7/101.2/105.4 vs native 97.0/83.4/94.9。BE whole-run 中位：none 207.1 / native 249.3 / iox 136.3 MiB/s。

### A.5 d2fq 份额全表（t400，io_bytes）

| arm | rep1–5 | median |
|---|---|---|
| none | 0.5001 / 0.5000 / 0.5000 / 0.5001 / 0.4996 | 0.5000 |
| iox | 0.4998 / 0.5001 / 0.5000 / 0.5005 / 0.5000 | **0.5000**（目标 0.8） |

### A.6 linnos 拦截率全表（终值累计，loader_q_be.stdout）

| rep | ins | band0/band1/band2 | BE 率 b0/b1/b2 % | completions | disp_held_soft |
|---|---|---|---|---|---|
| 1 | 4,380,783 | 12 / 8 / 4,380,763 | 0.000 / 100.000 / **60.000** | 0 | 2,628,487（=ir2 BE） |
| 2 | 3,743,110 | 12 / 8 / 3,743,090 | 0.000 / 100.000 / 60.000 | 0 | 2,245,859 |
| 3 | 4,769,610 | 12 / 8 / 4,769,590 | 0.000 / 100.000 / 60.000 | 0 | 2,861,759 |
| 4 | 3,605,997 | 12 / 8 / 3,605,977 | 0.000 / 100.000 / 60.000 | 0 | 2,163,599 |
| 5 | 4,126,362 | 12 / 8 / 4,126,342 | 0.000 / 100.000 / 60.000 | 0 | 2,475,822 |

逐窗复算：每 rep 仅 1 个 1s 窗出现 band0 插入、1 个窗 band1，其余全 band2（band2 占插入 99.9995%）。目标表：BE 0/40/60，LC 0/0/0（runner :315-319）；busy_ref=24、hold=500µs。

### A.7 io_cost vrate

- IOX（5 rep）：活动样本 ~121/rep；vrate_fp 非零 min 64 / max 1024、923、342、1024、1024；down/up 步进 155/200、153/217、235/114、112/274、106/267。相位均值（×）：p1 [1.45, 1.10, 0.42, 1.87, 2.27]；p2 [1.02, 0.28, 0.26, 2.31, 2.22]；p3 [0.26, 0.29, 0.26, 0.46, 0.27]。
- native（伴随补采 rep1–5）：恒 **150.00**（155/155/155/155/104 行，0 个其他值；= qos max=150 钳位值）。

### A.8 幸存日志归属（stray_logs）独立验证

| 文件 | 幸存序列读数 | 对应 rep5 iox fio JSON | 判定 |
|---|---|---|---|
| lat_iomax_bw.1.log | mean 32.01 MiB/s（60 行） | 31.999 | 匹配 |
| lat_w_{lc,be} | BE 份额 0.7921（90+90 行） | 0.7921 | 精确匹配 |
| lat_t100/t400 | t400 份额 0.5000 | 0.5000 | 精确匹配 |
| mq_bw.1.log | read/write 各 130.6 MiB/s | 130.6 / 130.6 | 精确匹配 |
| lat_p4_{lc,be} | LC 3300.5 MiB/s（DATA_SUMMARY 引）；BE 146.0 | LC 3309.8 / BE 141.9 | 匹配（窗均值 vs 全程均值的正常差） |
| lat_p1/p2/p3_{lc,be}_* | 4k LC 写 ~7–12k IOPS，与任何 rep5 iox run 不符 | — | 不可归属，未用于任何图（DATA_SUMMARY §0.2 口径正确） |

### A.9 哨兵漂移（`RESULTS/sentinel_drift.csv`，110 run 复算）

| 实验 | 比值 median | 范围 | flagged |
|---|---|---|---|
| mq_deadline / io_weight / io_cost / io_latency / d2fq（写过 lc.bin） | 0.017–0.103 | 0.009–0.148 | 70/70（全部属 D3 文件状态效应） |
| kyber / io_max / linnos（未写 lc.bin） | 0.996–1.000 | **0.9769–1.0125** | **0/40** |

---

## 6. 审查结论

1. 本轮 campaign 的**数据质量与诚实度**：高（全 OK、无择优、缺口与退化全部如实标注、冒烟发现被正式数据如实复现而非掩盖）。
2. **可进入论文的声明面**：见 `PAPER_EVIDENCE.md` 的分级汇总表——核心是 io.max（等价）、io_weight IOX 侧（语义级）、io_latency IOX 侧（方向与保护形态）、linnos band2（查表精确）、kyber IOX 侧（反馈存在性）。
3. **不可进入论文的声明**：任何算法的"与 native 等价"无限定语版（io.max 除外；mq-deadline 仅读侧可写"不可区分"，写侧因快/慢相双峰不作臂间声明，见 §4.1.1）；d2fq 的权重语义；mq-deadline 的优先级/饥饿法则；kyber/io_cost 的"反馈响应压力变化"（native 侧无响应、IOX 侧不锁相）。
4. **最优先后续工作**：①d2fq 权重不分化归因（数据已在盘上）；②kyber 洞读归因；③native io_cost/io.latency 反馈惰性归因（blkcg_debug_stats/tracepoint）；④修复 q_linnos 完成钩子后重跑使三带实质占据；⑤U5 的运行时读回补齐。
