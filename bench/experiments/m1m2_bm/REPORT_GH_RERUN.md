# REPORT —— M2' (g)/(h) 臂 bm 实测 io.cost 系数重跑（r2644 口径）

日期：2026-11-07　执行：m2' g/h measured-coef 重跑子任务
对应 LEDGER 决策 7（bm iocost 系数 = 内核工具文件模式实测）的收尾动作。

## 1. 背景与目的

M2' bm 复刻（`runs/m2fig2/m2fig2_bmr2644_20261105`，rate=2644 九臂）的
(g) io.cost 与 (h) io.cost+io.weight 两臂当时用的是 **linear-from-solo fallback
模型**（由 solo 64K/QD8 探针 3305 MiB/s 按 kvmhost deviation-2 公式派生，
rbps=3465543680 等——高于三条租户合计需求，io.cost QoS 基本不节流）。
2026-11-06 决策 7 在 bm 空闲盘上用内核工具 `iocost_coef_gen.py` 文件模式实测出
设备系数（`experiments/dyn_tenant_bm/contract/iocost_coef.json`，nvme0n1 259:0）：
rbps=1877961028 / wbps=1895474286 / rseqiops=436504 / rrandiops=504812 /
wseqiops=436118 / wrandiops=324051。本任务用它替换 fallback 模型，
只重跑 r2644 口径的 g/h 两臂，其余七臂不动。

## 2. 系数注入方式（零代码改动，fallback 路径未动）

- 注入点就是 `gen_m2_fig2_yamls_bm.py` 的既有约定：calibration JSON 里
  `iocost_model` 键存在即直接采用，缺失才走 solo fallback。
  （`load_calibration()`，scripts/gen_m2_fig2_yamls_bm.py:158-168）
- 新建独立配置目录 `configs/m2fig2_rate2644_ghmeasured/`：
  - `calibration_bm.json` = m2fig2_rate2644 版副本，仅替换 `iocost_model` 为实测值
    （原 fallback 模型保留在 `iocost_model_fallback_previous` 字段，可追溯）；
    **qos 常量原样不动**（rpct/wpct=95, rlat/wlat=20000us, min=50, max=150，
    跨图一致性预声明值，见 LEDGER 决策 7 / C4 qos 口径定案）。
  - 10 个 yaml 由同一生成器 `--calibration <新 json> --output-dir <新目录>` 产出，
    g/h 臂 cgroup `io_cost.model` 已验证为实测六元组。
- 运行开关沿用 runner 既有 `CFG_DIR` 环境变量（r2644 轮同款机制）：
  `CFG_DIR=experiments/m1m2_bm/configs/m2fig2_rate2644_ghmeasured
   RUN_ID=m2fig2_bmr2644_ghmeasured_20261107
   bash run_m2fig2_bm.sh M2F2_g_io_cost M2F2_h_io_cost_weight`
  默认路径（configs/m2fig2_rate2644、fallback 生成逻辑、其余臂）全部未改。
- 协调：整次运行包在 `flock -w 3600 /tmp/ioxhost_work.lock` 内（与其他 bm 实验
  共用锁）；runner 自身仍走远端 /tmp/m2fig2_bm.lock 互斥。

## 3. 运行结果

- RUN_ID=`m2fig2_bmr2644_ghmeasured_20261107`（runs/m2fig2/ 下，calibration.json
  内 `iocost_model_source="measured_iocost_coef_gen_testfile_bm"` 标注清晰）。
- preflight 全过：kernel 7.3.1-ioext2、dev 259:0、数据文件缓存命中（32G
  m2f2_data.bin，probe/solo_data 未触碰）、io.max smoke 63 vs 64 MiB/s PASS。
- **两臂双双 attempt-1 PASS**（远端 validate + 本机 per-arm validate 均 PASS；
  仅已知软 WARN：A-alone plateau < 60% rate cap——实测模型容量 ~1791 MiB/s
  低于 rate 帽 2644，A 独占段被 io.cost 压到 ~1.5 GiB/s，属模型生效的真实形态）。

## 4. 新旧对比（fallback vs measured，r2644 口径，fio 聚合）

数据源：各租户 `*_jobs1_fio.json` 聚合（bw_bytes、clat p99）。
fallback=runs/m2fig2/m2fig2_bmr2644_20261105，measured=runs/m2fig2/m2fig2_bmr2644_ghmeasured_20261107。

### (g) io.cost（M2F2_g_io_cost，三租户同权）

| 租户 | bw fallback (MiB/s) | bw measured | Δbw | p99 clat fallback (ms) | p99 measured | Δp99 |
| --- | --- | --- | --- | --- | --- | --- |
| A | 1283.0 | 881.2 | -31.3% | 1089.5 | 1613.8 | +48.1% |
| B | 1493.2 | 1010.0 | -32.4% | 1073.2 | 1531.9 | +42.7% |
| C |  853.3 |  655.9 | -23.1% | 1171.5 | 1777.7 | +51.7% |

读法：fallback 模型容量（rbps=3465 MB/s ≈ 3305 MiB/s）高于合计需求，QoS 形同虚设，
三租户几乎贴各自 rate 帽；实测模型容量 ~1791 MiB/s 使 io.cost 真正参与调控，
三方被压到 656-1010 MiB/s、p99 上浮 43-52%——**同权竞争下的份额分化
（B 1010 / A 881 / C 656）来自 iocost 对 I/O 尺寸/类型的成本核算**
（64K 随机读 rrandiops=504812 的成本模型），不再是"帽内自由跑"。

### (h) io.cost + io.weight（A=1000 / B=1 / C=100）

| 租户 | bw fallback (MiB/s) | bw measured | Δbw | p99 clat fallback (ms) | p99 measured | Δp99 |
| --- | --- | --- | --- | --- | --- | --- |
| A | 2428.5 | 895.6 | -63.1% |  913.4 | 1597.4 | +74.9% |
| B |  854.9 | 938.6 |  +9.8% |  913.4 | 1564.7 | +71.3% |
| C |  232.5 | 661.1 | +184.3% | 2342.9 | 1761.3 | -24.8% |

读法：
- fallback 轮 h 臂的 A=2428/C=232 是"高容量模型 + 权重"的极端分层：io.cost 本身
  不约束，io.weight 在近乎无竞争的带宽上放大分层，B(weight=1)/C(100) 被压极低。
- 实测轮 io.cost 先把蛋糕收到 ~1791 MiB/s，权重在**真竞争预算**上分配：
  A(1000) 895.6 / C(100) 661.1 仍保持 1.35× 分层（validator 软判据 A vs C 分离 PASS），
  B(weight=1) 938.6 看似不低，但其时间线在 A/C 常驻的 10-50s 竞争窗内被压制、
  主要在 A 退出后的 50-70s catch-up（与 kvmhost h 臂"竞争期饿死、A 结束后回升"同型）。
- C 的 p99 从 2342.9→1761.3 ms 改善：fallback 轮 C 在 QoS 不设防时被权重挤到
  长尾；实测轮 QoS 的 min=50 保护生效。

## 5. 口径与红线核查

- 只重跑 r2644 口径 g/h 两臂各一轮（MAX_RETRIES=2 未触发）；其余七臂沿用
  m2fig2_bmr2644_20261105 数据，历史未动。
- 全程仅文件 IO（32G 预分配 m2f2_data.bin，direct=1），probe/solo_data 完好，
  绝无裸设备访问。
- 跑后 bm 状态核查并修复：g/h 臂 scene 在 root 写入的 io.cost.qos/model 会被
  遗留 enable=1（与 LEDGER 2026-11-06 "RECAL 毒"同型残留，runner reset 只在
  下一序列前清理）——已显式还原：`io.cost.qos` 259:0 **enable=0**、
  `io.cost.model` 259:0 **ctrl=auto**（需带 MAJ:MIN 前缀，裸 "ctrl=auto" 会
  EINVAL）；scheduler 复核 [none]（nvme 默认，与跑前一致）；无 ib_m2f2_*/m2f2_*
  残留 cgroup、无 fio 进程。
- 注意：bm 远端 `src/vm/configs/task/isol_bench/rig_m2_fig2_bm/` 现在是
  measured 版 yaml（rsync 覆盖）；后续若在 bm 重跑其他臂，用各自 CFG_DIR
  重新推送即可，本地 configs 目录仍是唯一事实源。

## 6. 结论

实测系数让 (g)/(h) 从"帽内近自由跑"变成 io.cost 真实调控下的竞争形态：
(g) 同权三方按成本模型分摊（bw -23~-32%，p99 +43~+52%）；(h) 权重分层在
真预算上保持（A/C 1.35×）且 min 保护改善 C 长尾（p99 -25%）。两轮数据均
完整存档，成图可按 REPORT §m2' 同款 plot 指向新 runs 目录。
