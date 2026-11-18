# policy-fidelity 八臂条带 · 出图脚本（`results/reproduction`）

本目录保留 reproduction（q_none + 7 策略 + native D2FQ）条带图的**出图脚本**。
逐 run 原始数据、各策略汇总 CSV 与成品图不随仓库发布。

| 脚本 | 说明 |
|------|------|
| `scripts/plot_eight_arm_reproduction_strip.py` | 2×4 总览整图（a–g 各 panel 数据装载见脚本内 loader） |
| `scripts/iolat_curated.py` / `iolat_common.py` | io.latency 策展与 fio 路径 |
| `scripts/kyber_curated.py` / `kyber_common.py` | kyber 策展 |
| `scripts/iocost_curated.py` / `iocost_common.py` | io.cost 策展 |
| `scripts/mq_curated.py` / `mq_common.py` | mq-deadline v2 柱图策展 |
| `scripts/iocost_curated.py` 等 | 改 rep 名单后重跑 `plot_eight_arm_reproduction_strip.py` |

各策略单图脚本在 `experiments/reproduction/RESULTS/scripts/fig1-7_*.py`；
campaign 说明见 `experiments/reproduction/EXPERIMENT_MATRIX.md` 与
`FINAL_REPORT.md` / `PAPER_EVIDENCE.md`。
