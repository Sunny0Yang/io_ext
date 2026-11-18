# HANDOFF · D2 (kvmhost) 现状 · 2026-11-14

面向接手的 agent：本文件描述 kvmhost 上 D2（饱和加权公平性）实验的**当前现状与旧数据判废依据**。配套计划见 `PLAN_D2_kvmhost_重跑_20261114.md`（同一目录）。

## 1. D2 实验是什么

- 定义：单盘上 ncg 个 cgroup 租户（fio 4k randread，QD256，每租户 1 进程）权重梯度 1:2:…:ncg，全部饱和打满盘，比较各策略能否实现"按权分配"。
- 主判据（新契约）：**总带宽（聚合 MB/s）高 + JainW（bw_i/w_i 的 w-indexed Jain）高**；开销（read p99）为辅助指标，报告但不过度呈现。
- 平台：kvmhost 专属 guest（`127.0.0.1`，ssh env 见 `~/ioext-ssh/ssh_env.sh`），实验盘 = 96G 独占盘（201326592 sectors，重启后 sdX 字母会漂移，按字节数+无挂载+无 holders 唯一识别），内核 `7.3.1-ioext3+`（scx_io + loader_* 可用）。
- 配置源头：`src/vm/configs/task/isol_bench/paper_sdb/d2_fairness.yml`（单步 contention-only，fio 60s + ramp 20s，step timeout 120s）。

## 2. 旧数据现状：判废

旧数据 = `results/isol_bench/paper/d2_fairness/D2_saturated_weighted_*`（约 2026-08 采集）+ 汇总 `results/rq1_paper_figs/d2_jainw_corrected.csv` + 图 `results/d2/figures/kvmhost_d2_bw_by_cg.png`。**结论：不可用，需全量重采**。四条根因（均已用原始 run 验证）：

### 2.1 解析 bug（最严重）
- 旧序列结构是 `ncg 个 isolation（solo）步 + 1 个 contention 步`（`contention_step_index` 指向最后一步）。
- 绘图脚本 `results/d2/scripts/plot_kvmhost_d2_cg_scale.py` 的 `tenants_native()` 按步序取每权重**第一个** fio json → native 臂（none/bfq/io.max/io.cost）取到的是 **solo 步数据，不是 contention 数据**。
- 实测例证：`none cg8 iter1` 解析出 8×525=4202 MB/s（solo），iter2/3 才是 contention（~720）；`bfq cg8` iter1 solo=3664、iter2/3 塌缩 contention≈220（各租户 4–76 MB/s 不等）。
- `steady_iters`（0.55–1.45×中位数过滤）把 solo、塌缩、正常三种语义不同的数据混和取中位数；**部分 JainW 由 solo 数据算出**，对争用公平性无效。
- native 臂（paper tar）与 iox 臂（addendum 批）来自不同批次/口径，图内不可比。

### 2.2 介质相位塌缩（第一混杂）
- 同 arm 同 cg 的 3 次 iter 聚合带宽波动 >10×：io_cost cg2 = 41.7/103.2/425；kyber cg2 = 53/117/883；none cg2 = 674/915/921。
- 大量 run 落在 **~38 MB/s 地板值**（几乎所有臂都出现过）＝塌缩态，非策略性质。
- 塌缩 run 的 p99 达 0.7–1.0 s，混入后图的 p99 轴彻底爆炸。

### 2.3 设备锚漂移
- yaml RECAL 锚：`D2_SATURATION=320 MiB/s`、io.cost model bps=320M/iops=82k；实测盘 solo ~525 MB/s、contention ~720–900 MB/s。
- io.max 臂 share 按旧锚分帽 → 该臂总带宽被人为砍半，"公平但慢"被夸大，不是设备真实约束。

### 2.4 旧图的问题
- p99 虚线 `clip_on=False` + 82 分位截断 → 塌缩 run 的 p99 射出图顶（图中射出的竖虚线）。
- cg=8/16 十六色 per-tenant 堆叠不可读；JainW 双 y 轴与 MB/s 同侧重叠；图例压线。
- 主视觉与判据错位：堆叠条+三层折线表达不了"公平好 + 总带宽高"。

### 2.5 旧图各臂数字不可信的具体表现
- 图中 bfq cg8 bar≈230、JainW≈0.72 实为两次塌缩 run 的混合；a_slo cg8≈240 为塌缩单点；io.max 的"公平"来自 320 锚硬帽而非调度质量。

## 3. kvmhost 平台现状（2026-11-14 14:30 探查）

- 宿主昨晚曾失联（dyn_tenant x5 批 `DYN_arm_x5_qkyber_wdrr` fio D-state 楔死 → 两次调度重启 → No route to host），今日 ~04:16 重新上线，现已恢复。
- **当前空闲**（load ~0.13，无 executor/fio/loader）。13:07–13:37 曾有其他实验突发，已结束。
- 楔死风险是平台已知属性（fio D-state → 需重启恢复），重跑方案必须内置看门狗+取证+自动恢复。

## 4. 相关文件索引

| 内容 | 路径 |
|---|---|
| 旧数据原始 run | `results/isol_bench/paper/d2_fairness/` |
| 旧 JainW 汇总 | `results/rq1_paper_figs/d2_jainw_corrected.csv` |
| 旧图 + 旧绘图脚本 | `results/d2/figures/kvmhost_d2_bw_by_cg.png`；`results/d2/scripts/plot_kvmhost_d2_cg_scale.py` |
| D2 配置源头 | `src/vm/configs/task/isol_bench/paper_sdb/d2_fairness.yml` |
| 配置生成器 | `scripts/gen_isol_bench_paper_yamls.py`（RECAL 常量在头部） |
| 新脚本（本次产出） | `scripts/gen_d2_kvmhost_rerun_yaml.py` / `scripts/run_d2_rerun_kvmhost.sh` / `scripts/analyze_d2_kvmhost_rerun.py` |
| 新实验记录 | `experiments/d2_kvmhost_rerun/`（本目录） |
| ssh / sudo | `$ARCHIVE_DIR/ssh_env.sh`；sudo 密码 `REDACTED`（executor 以 root 跑） |
