# PLAN · D2 (kvmhost) 重跑计划 · 2026-11-14

面向接手的 agent：旧数据判废依据见同目录 `HANDOFF_D2_kvmhost_现状_20261114.md`。本文件描述重跑方案、判据、防卡机制与时间预算。脚本已全部备好并通过语法/dry-run 检查。

## 1. 目标与判据

- **主指标**（同等重要）：
  1. contention 总带宽（聚合 MB/s）— 越高越好；
  2. JainW（w-indexed Jain on bw_i/w_i）— 越高越好；
- **辅助指标**：read p99（逐 iter 均值→中位数），单独小条呈现（log 轴、clip_on），不做主视觉。
- **报告口径**：每 (arm,cg) n=3，逐 iter 独立算 JainW/聚合带宽，报告中位数 ± [min,max]；**禁止跨 iter 混列**（旧图的教训）。塌缩/低相位 run 打标保留（SUSPECT / PHASE-LOW），不静默丢弃。

## 2. 实验矩阵（改为两阶段：先 pilot 对齐，后全量）

**Phase 0（pilot，先跑这个）**：
- 臂集：`none`（分母）+ `io_cost`（native 公平存在性证明）+ **`iox_arb_wdrr`（单 arb 主候选）**，可选加 `iox_q_ratelimit`（公平确定性对照，见策略分析文档）；
- cg ∈ {4, 8}，n=2 → 12–16 序列，**~40–60 min**；
- 目的：验证标定锚/canary/后门/看门狗链路 + 拿到 arb_wdrr 在 kvmhost 的干净基线 → **与用户/另一 agent 对齐后再放全量**。

**Phase 1（对齐后全量）**：
- 补齐 bfq、io_max、iox_arb_slo、iox_arb_cost × cg{2,4,8,16} × n=3，以及 pilot 臂缺失的 cg/iter → 共 84 序列口径不变。

fio shape 不变：4k randread，QD256，runtime 60s + ramp 20s，direct=1。

## 3. 上机执行流程（脚本已实现）

入口：`scripts/run_d2_rerun_kvmhost.sh --sync`（建议 `flock /tmp/d2_rerun_kvmhost.lock`）。步骤：

1. **预检守卫**：设备唯一性识别（96G + 无挂载 + 无 holders）、scx_io 可用、远端 .venv 存在、无残留 executor/fio。
2. **再标定**（~2.5 min，root，direct=1 打原始盘）：
   - solo 4k randread QD256 ×20s ×2 → canary 锚 + model_iops；
   - 1M seq read QD8 ×10s → io.cost model bps；
   - numjobs=2/4/8/16 各 12s → **各 cg 的 contention 参考带**。
   产出 `experiments/d2_kvmhost_rerun/calibration_bm_kvmhost_sdb.json`。
3. **生成并推送 yaml**：`scripts/gen_d2_kvmhost_rerun_yaml.py` 从 `paper_sdb/d2_fairness.yml` 派生 `d2_kvmhost_rerun.yml`：
   - target_device/file_name → 运行时发现的设备字母；
   - io.cost model 六字段 → 新测 bps/iops；
   - **io.max share = ref_agg(cg) × w/Σw**（按各 cg 实测参考带分帽，废除全局 320 旧锚）；
   - 序列过滤到 84 条。
4. **逐序列执行**（root，`vm.executor` 经 `sudo -S`，与 dyn_tenant 同约定）：
   - **canary 门**：6s QD256 探针，<0.6×solo 锚 → 冷却 45s 重试 ≤3，仍低则打 PHASE-LOW 标记照跑；
   - **看门狗**：每序列硬超时 **420s**（cg16 因租户多给 540s；nominal 150–270s，启动时打印逐序列预估）；
   - **超时/失败处理**：先取证后恢复——dmesg 尾 60 行、/proc/pressure、进程态（pid/stat/wchan）、ib_d2_* cgroup io.stat、盘 stat、executor 日志尾 → `experiments/d2_kvmhost_rerun/forensics/<SEQ>_<hhmmss>.txt`；然后 pkill executor/fio/loader，fio 仍 D-state 则自动重启；
   - **重启自适应**：每序列前重新发现设备字母，漂移则重生成并重推 yaml；
   - **聚合后门**：解析该序列全部 fio json 求和，<0.5×该 cg 参考带 → SUSPECT；
   - 失败/SUSPECT 序列进**重排队 pass**（末尾各重试一次）；结果每 10 序列增量拉回。
5. **收尾**：全量拉回 `results/d2_rerun_kvmhost/`，打印 `SUMMARY: total/PASS/FAIL/SUSPECT`。

## 4. 分析与图（`scripts/analyze_d2_kvmhost_rerun.py`）

- 解析 `results/d2_kvmhost_rerun/<SEQ>/step_0/*/*fio.json`（兼容 per-scene 与 collapsed json）→ `analysis/d2_rerun_by_arm.csv`。
- 新图 `analysis/fig_d2_rerun_by_cg.png`：2×4 面板（上行 BW+JainW，下行 p99 辅助条）：
  - 上行：总带宽中位数条（none=浅蓝 / native knob=橙 / iox arb=深蓝）+ min–max 误差线 + n=3 散点；右轴 JainW 折线（带 range 竖线）+ 红=none 的 JainW 参考线；
  - 下行：p99 中位数点，log 轴，clip_on=True——辅助地位，不再射出图顶；
  - 无 per-tenant 堆叠（16 色不可读的老问题直接移除）。

## 5. 时间预算

| 阶段 | 预估 |
|---|---|
| 同步 + 守卫 + 标定 | ~10 min |
| **Phase 0 pilot（12–16 序列）** | **~40–60 min** |
| 对齐（人工/另一 agent） | — |
| Phase 1 全量（84 序列口径，nominal cg2≈150s / cg4≈170s / cg8≈210s / cg16≈270s） | ~4 h |
| 重排队 pass（预计 ≤20% 序列） | ~1 h |
| 含楔死/重启余量 | 全量阶段 **≤8 h 封顶** |
| 分析+画图（本地） | ~10 min |

判定战役是否卡死：看 runner 日志（`PROGRESS i/84 <SEQ> est=…` 行）；单序列超过 900s 必然触发看门狗（脚本内置，无需人工盯）。

## 6. 成功标准 / 产出验收

1. 84 序列全部有有效 contention 数据（或打标的 SUSPECT/PHASE-LOW + 取证记录）；
2. none 臂聚合带宽落在标定参考带 ±30% 内（数据可信度锚）；
3. 每臂 JainW 跨 iter 极差 ≤0.05 视为稳定；塌缩（<0.5×参考带）迭代被标记而非混入；
4. 新图直接回答：哪些臂同时做到 JainW 高 + 总带宽高，p99 只作辅助注解；
5. 产出物：`results/d2_rerun_kvmhost/`（原始）+ `analysis/d2_rerun_by_arm.csv` + `analysis/fig_d2_rerun_by_cg.png/pdf` + 本目录下 RUNBOOK/结论补充。

## 7. 风险与预案

| 风险 | 预案 |
|---|---|
| fio D-state 楔死（平台已知，昨晚 x5 批实锤） | 看门狗 900s → 取证 → pkill；仍 D-state → 自动 reboot + 设备重发现；失败序列重排队一次 |
| 宿主再次 No route to host | runner 的 ssh 全部带 ConnectTimeout；本地 `results/d2_rerun_kvmhost/` 每 10 序列增量拉回，灾难最多损 10 序列 |
| 介质相位塌缩污染 | canary 前门 + 聚合后门 + SUSPECT 重跑；全部打标可见 |
| 其他会话在 kvmhost 上跑实验 | 守卫拒绝在 executor/fio 存在时开跑；开跑后本战役持 `/tmp/d2_rerun_kvmhost.lock`（flock 单飞） |
| io.max 新锚分帽与旧图不可比 | 预期内：旧图本身不可信；对比报告按"新锚下的相对结论"呈现 |
