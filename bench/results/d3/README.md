# kvmhost D3 · 图表脚本（`results/d3`）

本目录保留 D3/D4 的**出图脚本入口**。成品图（`experiments/d3_kvmhost/figures/`）
与逐 run 台账 CSV 不随仓库发布，由 `experiments/d3_kvmhost/` 的 campaign 重跑生成。

刷新（写入 `experiments/d3_kvmhost/figures/`）：

```bash
bash bench/results/d3/scripts/refresh_d3_kvmhost_figures.sh
```

调用链：`bench/scripts/plot_d3_kvmhost_main.py` → `plot_d3_kvmhost_pareto_merged.py`
→ `plot_d3_d4_rq13_combo.py` → `plot_d2_d3_d4_rq13_row.py`（论文组合主图）。
台账与叙事见 `experiments/d3_kvmhost/` 下的报告。
