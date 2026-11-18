# D3 kvmhost — 全量 run 数据表（含异常标记）

生成：`scripts/summarize_d3_kvmhost_ledger.py` · 2026-11-15 15:38 UTC

## 判据锚（只读 `SLO_and_B_none.json`）

| 量 | 值 |
|---|---|
| LC solo p99 median | **0.2468 ms** |
| SLO_batch (2×solo) | **0.4936 ms** |
| B_none (calib median) | **908.93 MB/s** |
| BE 数据失效线 (20% B_none) | **181.79 MB/s** |

合计 **52** run；**25** 条数据异常（应排除出散点）；**19** 条 Pareto 可用（ok + policy_engaged + be_valid）。

**异常**（`anomaly_tags`）= 数据不可用或臂作废；**门控**（`gate_flags`）= 双门未过但点数仍可信。

### 异常标签（hard）

| 标签 | 含义 |
|---|---|
| `POLICY_NOT_ENGAGED` | metadata 调度器与臂不符（v1 bfq 三 run 整臂作废） |
| `DEVICE_PAUSE` | BE 聚合 < 20% 标定 B_none（virtio 静默窗） |
| `SOLO_P99_OUT_OF_BAND` | solo p99 超出 frozen band |
| `HEAVY_BUSY_STALL` | ≥20 秒 busy < 20% B_none |

### 门控标签（gate_flags，非异常）

| 标签 | 含义 |
|---|---|
| `SLO_FAIL` / `RET_BELOW_60` | 冻结双门未过（Pareto 上仍作实测点） |
| `BUSY_UNDER_FLOOR20` / `LC_FLIP_FLAG` | 审计提示 |

## 按批逐 run

### 标定 — LC solo（5 run）

| run_id | batch | arm | LC p99 (ms) | LC×solo | BE (MB/s) | ret%@calib | status | pareto_clean | anomaly | gate_flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D3PV_lcsolo_none_run1 | calibration | LC solo (none sched) | 0.6431 | 2.61 | 46.77 | — | ok |  | ⚠ SOLO_P99_OUT_OF_BAND |  |
| D3PV_lcsolo_none_run2 | calibration | LC solo (none sched) | 0.2468 | 1 | 75.31 | — | ok |  |  |  |
| D3PV_lcsolo_none_run3 | calibration | LC solo (none sched) | 0.2468 | 1 | 75.98 | — | ok |  |  |  |
| D3PV_lcsolo_none_run4 | calibration | LC solo (none sched) | 0.2468 | 1 | 76.00 | — | ok |  |  |  |
| D3PV_lcsolo_none_run5 | calibration | LC solo (none sched) | 0.8724 | 3.54 | 46.35 | — | ok |  | ⚠ SOLO_P99_OUT_OF_BAND |  |

### 标定 — none contention（5 run）

| run_id | batch | arm | LC p99 (ms) | LC×solo | BE (MB/s) | ret%@calib | status | pareto_clean | anomaly | gate_flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D3PV_none_run1 | calibration | none (calib batch) | 11.21 | 45.4 | 735.80 | 80.95 | ok |  |  | BUSY_UNDER_FLOOR20 |
| D3PV_none_run2 | calibration | none (calib batch) | 10.42 | 42.2 | 928.85 | 102.19 | ok |  |  |  |
| D3PV_none_run3 | calibration | none (calib batch) | 10.03 | 40.6 | 908.93 | 100.00 | ok |  |  | BUSY_UNDER_FLOOR20 |
| D3PV_none_run4 | calibration | none (calib batch) | 10.16 | 41.2 | 896.18 | 98.60 | ok |  |  | BUSY_UNDER_FLOOR20 |
| D3PV_none_run5 | calibration | none (calib batch) | 10.29 | 41.7 | 933.92 | 102.75 | ok |  |  |  |

### 发现 v1 — discovery

| run_id | batch | arm | LC p99 (ms) | LC×solo | BE (MB/s) | ret%@calib | status | pareto_clean | anomaly | gate_flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D3PV_arbonly_run1 | discovery | arbonly | 1.942 | 7.87 | 729.37 | 80.24 | ok | ✓ |  | SLO_FAIL |
| D3PV_arbonly_run2 | discovery | arbonly | 127.4 | 516 | 188.34 | 20.72 | ok |  | ⚠ HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_arbonly_run3 | discovery | arbonly | 130.5 | 529 | 72.04 | 7.93 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_bfq_run1 | discovery | bfq | 434.1 | 1.76e+03 | 162.09 | 17.83 | policy-not-engaged |  | ⚠ POLICY_NOT_ENGAGED|DEVICE_PAUSE | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_bfq_run2 | discovery | bfq | 10.42 | 42.2 | 920.20 | 101.24 | policy-not-engaged |  | ⚠ POLICY_NOT_ENGAGED | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME |
| D3PV_bfq_run3 | discovery | bfq | 10.42 | 42.2 | 918.77 | 101.08 | policy-not-engaged |  | ⚠ POLICY_NOT_ENGAGED | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME |
| D3PV_none_run1 | discovery | none | 52.17 | 211 | 576.27 | 63.40 | ok |  | ⚠ HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL |
| D3PV_none_run2 | discovery | none | 127.4 | 516 | 371.24 | 40.84 | ok |  | ⚠ HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|RET_BELOW_60 |
| D3PV_none_run3 | discovery | none | 10.16 | 41.2 | 924.77 | 101.74 | ok | ✓ |  | LC_FLIP_FLAG|SLO_FAIL |
| D3PV_qrl_k065_run1 | discovery | qrl_k065 | 0.9134 | 3.7 | 604.95 | 66.56 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_run2 | discovery | qrl_k065 | 0.9298 | 3.77 | 604.43 | 66.50 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_run3 | discovery | qrl_k065 | 0.9953 | 4.03 | 604.51 | 66.51 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k075_run1 | discovery | qrl_k075 | 1.581 | 6.41 | 693.97 | 76.35 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k075_run2 | discovery | qrl_k075 | 130.5 | 529 | 41.55 | 4.57 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k075_run3 | discovery | qrl_k075 | 131.6 | 533 | 41.41 | 4.56 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k085_run1 | discovery | qrl_k085 | 130.5 | 529 | 41.74 | 4.59 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k085_run2 | discovery | qrl_k085 | 128.5 | 520 | 112.04 | 12.33 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k085_run3 | discovery | qrl_k085 | 2.089 | 8.46 | 755.16 | 83.08 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k095_run1 | discovery | qrl_k095 | 130.5 | 529 | 41.86 | 4.61 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k095_run2 | discovery | qrl_k095 | 2.146 | 8.7 | 695.30 | 76.50 | ok | ✓ |  | BUSY_UNDER_FLOOR20|SLO_FAIL |
| D3PV_qrl_k095_run3 | discovery | qrl_k095 | 8.454 | 34.3 | 319.40 | 35.14 | ok |  | ⚠ HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k100_run1 | discovery | qrl_k100 | 3.85 | 15.6 | 345.57 | 38.02 | ok |  | ⚠ HEAVY_BUSY_STALL | SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k100_run2 | discovery | qrl_k100 | 2.146 | 8.7 | 748.43 | 82.34 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k100_run3 | discovery | qrl_k100 | 2.376 | 9.63 | 459.39 | 50.54 | ok |  | ⚠ HEAVY_BUSY_STALL | SLO_FAIL|RET_BELOW_60 |

### 发现 v2 — discovery2

| run_id | batch | arm | LC p99 (ms) | LC×solo | BE (MB/s) | ret%@calib | status | pareto_clean | anomaly | gate_flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D3PV_bfqf_run1 | discovery2 | bfqf | 0.4895 | 1.98 | 412.65 | 45.40 | ok | ✓ |  | RET_BELOW_60 |
| D3PV_bfqf_run2 | discovery2 | bfqf | 0.3543 | 1.44 | 366.83 | 40.36 | ok | ✓ |  | RET_BELOW_60 |
| D3PV_bfqf_run3 | discovery2 | bfqf | 0.5939 | 2.41 | 271.22 | 29.84 | ok |  | ⚠ HEAVY_BUSY_STALL | SLO_FAIL|RET_BELOW_60 |
| D3PV_none_run1 | discovery2 | none | 851.4 | 3.45e+03 | 41.72 | 4.59 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|RET_BELOW_60 |
| D3PV_none_run2 | discovery2 | none | 10.42 | 42.2 | 921.03 | 101.33 | ok | ✓ |  | LC_FLIP_FLAG|SLO_FAIL |
| D3PV_none_run3 | discovery2 | none | 10.29 | 41.7 | 919.19 | 101.13 | ok | ✓ |  | LC_FLIP_FLAG|SLO_FAIL |
| D3PV_qrl_k065_b050_run1 | discovery2 | qrl_k065_b050 | 130.5 | 529 | 41.46 | 4.56 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k065_b050_run2 | discovery2 | qrl_k065_b050 | 0.8888 | 3.6 | 603.14 | 66.36 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_b050_run3 | discovery2 | qrl_k065_b050 | 1.139 | 4.61 | 372.30 | 40.96 | ok |  | ⚠ HEAVY_BUSY_STALL | SLO_FAIL|RET_BELOW_60 |
| D3PV_qrl_k065_b100_run1 | discovery2 | qrl_k065_b100 | 131.6 | 533 | 41.58 | 4.57 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k065_b100_run2 | discovery2 | qrl_k065_b100 | 1.004 | 4.07 | 604.15 | 66.47 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_b100_run3 | discovery2 | qrl_k065_b100 | 1.012 | 4.1 | 604.76 | 66.53 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_b250_run1 | discovery2 | qrl_k065_b250 | 130.5 | 529 | 41.63 | 4.58 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |
| D3PV_qrl_k065_b250_run2 | discovery2 | qrl_k065_b250 | 1.02 | 4.13 | 604.72 | 66.53 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k065_b250_run3 | discovery2 | qrl_k065_b250 | 0.9953 | 4.03 | 603.38 | 66.38 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k075_b050_run1 | discovery2 | qrl_k075_b050 | 1.45 | 5.88 | 697.21 | 76.71 | ok | ✓ |  | SLO_FAIL |
| D3PV_qrl_k075_b050_run2 | discovery2 | qrl_k075_b050 | 1.745 | 7.07 | 328.13 | 36.10 | ok |  | ⚠ HEAVY_BUSY_STALL | SLO_FAIL|RET_BELOW_60 |
| D3PV_qrl_k075_b050_run3 | discovery2 | qrl_k075_b050 | 131.6 | 533 | 41.46 | 4.56 | ok |  | ⚠ DEVICE_PAUSE|HEAVY_BUSY_STALL | LC_FLIP_FLAG|SLO_FAIL|LC_TAIL_EXTREME|RET_BELOW_60 |

## Pareto 族 — 仅 clean run 聚合（散点图用）

| family | n_clean | LC p99 med | LC×solo | BE med | ret%@calib | run_ids |
| --- | --- | --- | --- | --- | --- | --- |
| BFQ | 2 | 0.4219 | 1.709 | 389.74 | 42.9 | D3PV_bfqf_run1, D3PV_bfqf_run2 |
| arbonly | 1 | 1.942 | 7.867 | 729.37 | 80.2 | D3PV_arbonly_run1 |
| none | 3 | 10.29 | 41.690 | 921.03 | 101.3 | D3PV_none_run3, D3PV_none_run2, D3PV_none_run3 |
| qrl_k065 | 8 | 0.9953 | 4.033 | 604.47 | 66.5 | D3PV_qrl_k065_run1, D3PV_qrl_k065_run2, D3PV_qrl_k065_run3, D3PV_qrl_k065_b050_run2, D3PV_qrl_k065_b100_run2, D3PV_qrl_k065_b100_run3, D3PV_qrl_k065_b250_run2, D3PV_qrl_k065_b250_run3 |
| qrl_k075 | 2 | 1.516 | 6.141 | 695.59 | 76.5 | D3PV_qrl_k075_run1, D3PV_qrl_k075_b050_run1 |
| qrl_k085 | 1 | 2.089 | 8.464 | 755.16 | 83.1 | D3PV_qrl_k085_run3 |
| qrl_k095 | 1 | 2.146 | 8.697 | 695.30 | 76.5 | D3PV_qrl_k095_run2 |
| qrl_k100 | 1 | 2.146 | 8.697 | 748.43 | 82.3 | D3PV_qrl_k100_run2 |

## 主图建议非支配集（手工口径，与 HANDOFF 一致）

BFQ ← `bfqf` v2 n=3 clean；qrl_k065 ← v1×3 + v2 burst b100/b250×2 clean；qrl_k075/k085 ← v1 各 n=1 clean；none ← v2 run2–3 clean（run1 为 DEVICE_PAUSE）。v1 `none`/`bfq` 与 v2 重复臂以批次标注，出图时勿混用分母。

机器可读全表：`results/d3_kvmhost/analysis/d3_kvmhost_all_runs_ledger.csv`

Pareto（2026 前沿 + paper 彩排，none 对齐）：`figures/fig_d3_kvmhost_pareto_merged.{png,pdf}` · `python3 scripts/plot_d3_kvmhost_pareto_merged.py`（策展镜像：`results/d3/figures/`）

**未纳入**：仓库内 `results/isol_d2d4_bm*/d3` 等为 **bm 平台 D3**（另一套锚点）；无 `d3_kvmhost` confirmation 批次目录。
