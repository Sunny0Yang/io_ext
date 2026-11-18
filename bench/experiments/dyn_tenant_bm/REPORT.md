# dyn_tenant bm 主实验终版报告（2026-11-08 收束）

## 1. 实验与数据集（终版口径）

- **主图**：`results/figures/dyn_tenant_main.{png,pdf}`（10 面板 = n1-n4 + C1-C4 + f1 双身份），
  数据 = heldout seed201 九臂（`results/DYN_held_s201_*`，2026-11-06 晚采集，
  ioext2 内核 + 当日 loader，设备 QD1 地板 ~50µs 档，九臂同态同批、内部自洽）。
  渲染/验收：`--arm-set bm`，2026-11-07 视觉审查 PASS（见 `figures/dyn_tenant_main_summary.json`）。
- **合同**：`contract/{calibration.json,frozen_manifest.json,iocost_coef.json}`
  （L=60s，rate_b/c=1536M，bm 实测 io.cost 六系数）。
- **判决数据**（`analysis/heldout_s201_*_analysis.json` + `heldout_matrix.json`）：
  综合加权损失 C1 io.max 25.7 < C4 34.4 < N2 40.4 < C3 40.5 < N1 46.3 < N4 50.9
  < **F1 73.9** < N3 bfq 169.3 < C2 412.3；guardrail 可行仅 C1/N4。
  f1 的优势面 = 后台吞吐保护（B 顶满 1510 帽、D 434 MiB/s 全场最高）；
  代价面 = T1/T6 独占期 p99 3.88ms/12ms（见 §3）。

## 2. 异常 → 诊断 → 修复（叙事线，全部证据见 ioext/bugs/）

1. **现象**：f1 独占期 p99 比其余八臂差 24×。
2. **D1 分解**（`D1_OVERHEAD_bm.md`）：框架裸开销无辜（bare/空载/q_fifo 全 48µs），
   锁定 arb_feedback 的 DRR 语义 —— **BUG-003**：独占期预算 exhaust 无 re-kick，
   1 jiffy（4ms）静默/51 IO；slat 干净（5.3µs）、滞留全在 clat。
3. **修复 P0**（sole-demand fast-path，userspace）：探针级验证 **3948→55.6µs（71×）**、
   fastpath_selects 遥测确认（`D1_VERIFY_ioext3.md`）。
4. **多租户面**（`D1_FASTPATH_V2.md`/`D1_KERNEL_REVERT_VERIFY.md`/`D1_ROUND_HOLD_ANALYSIS.md`）：
   **BUG-004** round-hold 涓流（全局 round 被 QD1 holder 钉死 200×，带宽=round速率×
   每轮拨款×权重，定量吻合）——修复候选 S13 feasible-hold **未完成验证**（每次验证
   run 都触发 BUG-002 整机冻结，五次，跨 ioext2/ioext3，两段式信号累积恶化）。
5. **BUG-005** 租户退出 pending 泄漏（T6 独占失效）与 **BUG-006** 空载渐进退化
   一并立案；全部细节/复现方法/证据指针见 `ioext/bugs/BUG-002..006`。

## 3. 终版决策

- **主图采用 11-06 九臂数据集**（调度器为"修复前"状态的真实测量，异常已被
  完整定因并在 caption/报告注记）；**不采用** ioext3 上的 f1 局部重跑
  （独占期修复但多租户涓流，跨内核/跨设备档拼接不成立）。
- fast-path 修复以**探针级证据**入报告（71×，遥测确认），全臂级收益留待
  BUG-004 修复验证后再测（已封存的探索线，见 `D1_ROUND_HOLD_ANALYSIS.md` §收束）。
- bm 当前状态：ioext3 内核、scheduler [none]、零残留；loader 部署树 =
  P0v2（MANIFEST `build/loaders/MANIFEST.sha256`）。

## 4. 一次性索引

- 方法/RUNBOOK：`RUNBOOK_bm.md`；主图脚本：`scripts/plot_dyn_tenant_main.py --arm-set bm`
- 事件全录：`../m1m2_bm/LEDGER.md`（11-06/11-07/11-08 各条）
- 内核 bug：`/opt/ioext-io_ext/bugs/`（BUG-001..006 + evidence/ + patches/）
