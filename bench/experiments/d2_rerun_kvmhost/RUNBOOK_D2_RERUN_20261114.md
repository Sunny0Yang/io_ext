# D2 kvmhost 重跑 · 2026-11-14

## 动机（旧数据判废依据）
1. 旧 kvmhost D2 数据（`results/isol_bench/paper/d2_fairness/D2_saturated_weighted_*`，约 2026-08 采集）同配置 iter 间聚合带宽波动 >10×，大量 run 落 ~38 MB/s 地板值（介质相位塌缩）。
2. 解析 bug：旧绘图 `tenants_native()` 按步序取每权重第一个 fio json → **native 臂取到 isolation（solo）步数据而非 contention 步**；`steady_iters` 把 solo/塌缩/正常三种语义的数据混和中位数。JainW 部分由 solo 数据算出，无效。
3. 设备锚漂移：RECAL 320 MiB/s vs 实测 solo ~525、contention ~720–900 MB/s；io.max share 按旧锚分帽，带宽被人为砍半。
4. 图：p99 `clip_on=False` 飞出图顶；16 色堆叠不可读；双 y 轴重叠；主信息与"公平好+总带宽高"错位。

## 新契约
- 主指标：contention 总带宽（聚合 MB/s）+ JainW（w-indexed，逐 iter 计算）；辅助：read p99（单独小条，log 轴，clip_on）。
- 臂集（7）：none, bfq, io_max, io_cost, iox_arb_wdrr, iox_arb_slo, iox_arb_cost；cg=2/4/8/16；iter=3；共 84 序列，单步 contention-only。
- 上机先标定：solo 4k QD256×2（canary 锚+model iops）、1M seq read（model bps）、numjobs=2/4/8/16 各 12s（各 cg 的 contention 参考带 → io.max share 按各 cg 实测参考带分帽）。
- 卫生门：序列前 6s canary（<0.6×solo 锚 → 冷却 45s 重试 ≤3）；序列后聚合门（<0.5×该 cg 参考带 → suspect 重跑一次）。
- 看门狗：每序列硬超时 900s；超时先取证（dmesg/PSI/io.stat/进程态/日志尾 → experiments/d2_kvmhost_rerun/forensics/）再 recover（pkill；fio D-state 则重启）；设备字母每序列重发现，漂移则重生成 yaml。重排队 pass 收尾。
- 判读：按 iter 报中位数+min/max，不跨 iter 混列；suspect/PHASE-LOW 的 run 保留但打标，不静默丢弃。

## 文件
- `scripts/gen_d2_kvmhost_rerun_yaml.py` — 由 `paper_sdb/d2_fairness.yml` 派生（设备/模型/重锚/筛臂）
- `scripts/run_d2_rerun_kvmhost.sh` — 标定+canary+看门狗+取证+重排队+拉回
- `scripts/orchestrate_d2_rerun_kvmhost.sh` — 等 2h → 确认 kvmhost 空闲（本地无驱动进程 + 远端无 executor/fio/loader，连续 2 次）→ 启动
- `scripts/analyze_d2_kvmhost_rerun.py` — 解析 + 重绘图
- 标定：`experiments/d2_kvmhost_rerun/calibration_bm_kvmhost_sdb.json`；结果：`results/d2_rerun_kvmhost/`

## 时长预估（启动时打印）
- 标定 ~2.5 min；每序列 nominal cg2≈150s / cg4≈170s / cg8≈210s / cg16≈270s（+canary ~25s）
- 全批 84 序列 nominal ≈ 3.7–4.5 h；含重排队/楔死余量预估 ≤ 8 h
