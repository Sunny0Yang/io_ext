# kvmhost D2 重跑 · 策展入口（`results/d2`）

本目录保留 D2 重跑的**出图脚本**与最终报告。逐 run 原始数据与汇总 CSV、成品图
不随仓库发布；由 `experiments/d2_rerun_kvmhost/`（runbook + calibration）重跑生成。

| 路径 | 说明 |
|------|------|
| `report/RESULTS_D2_KVMHOST_RERUN_20261114.md` | 最终报告（口径、数字、验收门） |
| `scripts/plot_kvmhost_d2_cg_scale.py` | 论文主图：(a) Jain + p99 + equal-share ref；(b) 堆叠吞吐 w1–w4 |
| `scripts/refresh_kvmhost_d2_figures.sh` | 一键重绘（读 `results/d2_rerun_kvmhost/analysis/d2_rerun_by_arm.csv`） |
