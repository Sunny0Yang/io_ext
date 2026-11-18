# CALIBRATION_REPORT — D3 kvmhost 标定批（Phase 1）

日期：2026-11-15（终版，取代同日的 NOT-EVALUABLE 中期版）
批次 ID：`d3_kvmhost_calib`（完整批，回滚后新窗口）
结果目录：`results/d3_kvmhost/calibration/`（本地，10/10 run + tar + manifest）
锚文件（单一事实源）：`results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json`

## 判定：CALIBRATION PASS ✅

| 锚 | 值 | 依据 |
|---|---|---|
| LC solo p99（median） | **0.2468 ms** | 5/5 run valid；3/5 run 逐位一致 0.246784 |
| **SLO_batch** | **0.4936 ms** | = 2 × solo 锚（预注册规则） |
| **B_none** | **908.93 MB/s**（十进制） | 5/5 run valid，median BE 聚合 |
| B_none_mib | 866.77 MiB/s | 换算 ×1e6/2²⁰（cap 推导用） |
| solo 健康带 | [0.1234, 0.3702] ms（±50%） | 分析器口径，后续批同批重推 |

## 1. 执行概况

- 平台：kvmhost（hypervisor alloc `e8aa12e6`，快照 `ioext3-homo-v2-base` 回滚 + 重部署）
  ，内核 `7.3.1-ioext3+`，fio-3.41，执行器（框架冻结构建），裸设备 /dev/sdb
- 10 run = 5×`D3PV_lcsolo_none_runN` + 5×`D3PV_none_runN`，**全部 PASS，每 run 87s，
  零楔死零重排**；canary 门（555 MB/s = 0.6×D2 solo 锚 926.6）全部通过
- 逐 run manifest（uname/内核/fio 版/git/sha256/canary/门读数）100% 完整
- 环境快照与中断前首批逐位一致（同快照基线）

## 2. solo 锚审查（计划书 §2.1 要求的稳定性检查）

| run | p99 (ms) | 逐秒中位（前10s / 余窗） | 判读 |
|---|---|---|---|
| run1 | 0.6431 | 64µs / 113µs | 全窗 2.4× 慢，非预热型（前 10s 反而快）；窗口起始 host 活动，未定论 |
| run2 | 0.2468 | 48µs / 47µs | 稳 |
| run3 | 0.2468 | 47µs / 47µs | 稳 |
| run4 | 0.2468 | 47µs / 47µs | 稳 |
| run5 | 0.8724 | 179µs / 51µs | 慢窗集中前 10s——紧跟 canary 41MB/s 瞬态事件的恢复尾，10s 后回到 51µs |

- 机械翻转门（LC 窗 p99 > 5ms，bm 冻结口径）：**零触发**
- 无任何一秒的中位延迟超过 SLO/2（0.247ms）
- 离群不污染锚：median 对 run1/run5 稳健（3 个逐位一致值决定中位数）
- 结论：锚可用；kvmhost 的 virtio 路径 solo p99 ≈ 0.247ms ≈ 2.1× bm（0.1183），
  与"virtio 更慢 → SLO 更宽"的预判一致 → **arb 路线获得实证入场券**

## 3. none contention 与 B_none

- BE 聚合 per run：737 / 930 / 910 / 897 / 935 MB/s → median **908.93**
- **none 臂 LC p99 = 10.0–11.2 ms（40× solo）**——对比 bm 的 none 1.37ms（5.9× solo）：
  virtio 上 BE QD256 洪泛对 LC 的伤害更重，iox 隔离的叙事空间更大
- busy 秒审计（比例地板 20%×B_none ≈ 182 MB/s）：run1 有 12 个 busy 秒低于地板
  （与其 agg=737 最低一致），run3/run4 各 1–2 个——bm 聚合门盲区的修正已生效，
  这些 run 的逐秒数据全部保留可复查

## 4. cap 网格（Phase 2 发现批输入，从锚文件推导）

M(k) = round(k × 866.77 / 4) MiB/s per BE job：

| k | 0.65 | 0.75 | 0.85 | 0.95 | 1.00 |
|---|---|---|---|---|---|
| M(k) | **141** | **163** | **184** | **206** | **217** |

- k=1.00 定义为"绑定边缘参考点"（4×cap ≈ B_none），不等价 none
-arb_wdrr 裁决：SLO 0.4936 略低于 0.5 线，但 bm 最好值 0.3789 占 SLO 77%，
按批判性吸收裁决**纳入发现批**（边际成本 +3 run）

## 5. 与首批（中断批）的关系

首批 4 个 solo run（中断前 PASS）被快照回滚抹除，**未参与本锚定**；本批为
掉线后的完整重跑，符合同批纪律。掉线事件与恢复过程记录于中期版（git 历史）。

## 6. 交付物清单

- `results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json`（单一事实源，verdict=PASS）
- `lc_solo_runs.csv` / `none_contention_runs.csv`（含 within_band、逐秒画像、busy 秒列）
- `calibration_summary.json`
- 原始 fio JSON + bw/lat 逐秒日志 + system_metric 逐秒采样 + manifest ×10
- 分析器 `scripts/analyze_d3_kvmhost_calib.py`（bm 真实数据回归验证过：p99 提取
  命中 bm 锚 0.1183/0.2365）

## 7. Phase 2 解冻

发现批（8 臂 × 3 run 交错：none / bfq / qrl_k065–k100 / iox_arbonly）已按本锚
生成并启动；io_cost 推迟至 v2（io.cost 模型常数需在回滚后设备上重测，避免陈旧
常数混杂）。确认批将按计划书 §4.1 重锚定（同批重测 solo + none）。
