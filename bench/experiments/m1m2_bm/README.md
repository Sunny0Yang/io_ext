# experiments/m1m2_bm —— 裸机 bm 上的 M1/M2' 动机实验（正式论文数据）

把 motivation 证据链从 kvmhost 虚拟机环境搬到裸机 bm（ssh 别名 `bm`），
**本目录是 2026-11-05 起 bm（最终论文数据实验机）上的正式数据与成图**。
控制机（工作站）负责编排与成图，bm 只跑 executor 与数据生成。

机器档案、口径决策、事件时间线一律见 **`LEDGER.md`**（§1-§5），
内核 CONFIG 清单见 **`KERNEL86_CONFIG_GAPS.md`**。

## 关键环境（2026-11-05 终态）

- **内核 7.3.1-ioext2**（io-ext 定制，grub 默认已钉）：scheduler `[none] mq-deadline kyber bfq scx_io`
  （kyber/bfq **内建**，免 modprobe）；cgroup v2；io.cost.model/qos ✓；
  **io.max 与 io.latency 仅出现在子 cgroup（root 不显示=内核语义），m2' 九臂已实证可用**
- 单盘 NVMe **nvme0n1**（1TB DRAM-less consumer NVMe SSD，根分区占满）：**无 mkfs/mount 口径**
  —— 每 case 复位 = drop_caches + 删建数据目录（LEDGER 决策 1）；m2' 用预分配
  32G 文件 + direct=1；fio solo 基线 3305 MiB/s → m2' rate=1536 MiB/s（与 kvmhost 同截断）
- **user NOPASSWD sudo**（/etc/sudoers.d/90-ioext-nopasswd；scenes 内部 run_sudo_shell
  的硬前提，无它 scheduler 切换/drop_caches/cgroup 全静默失败——LEDGER 决策 5）
- 部署根 `/opt/ioext`（代码 src/ + .venv + staging 两个 bundle +
  src/vm/assets；**部署清单含 src/lib 决策 6**）

策展浏览（图 / 数据索引 / 脚本入口）：**`results/m1m2_bm/`**（`figures/` 链至本目录 `figs/`）。

## 三图证据链（成品图在 `figs/`）

| 论文图 | bm 版成品 | 数据 | 脚本 |
| --- | --- | --- | --- |
| Fig.1(a) block-IO fingerprints（用户指定口径） | `figs/m1_block_io_fingerprints_bm.{png,pdf}` | `runs/m1_campaign/m1cacheon_bm_20261105/`（15/15 ok） | `scripts/render_m1_fingerprints_bm.py` |
| m1 补充时序（dense flush 的替代通道） | `figs/m1_timeseries_bm.{png,pdf}` | 同上 + m1matrix | `scripts/render_m1_timeseries_bm.py` |
| m1_2 native scheduler × app P99 冲突热力图 | `figs/m1_2_policy_conflict.{png,pdf}` | `runs/m1_campaign/m1matrix_bm_20261105/`（48/48 ok） | `scripts/build_native_matrix_summary_bm.py` → `render_m1_2_policy_conflict_bm.py` |
| m1_2 设备带宽补充热力图 | `figs/m1_2_policy_conflict_bandwidth.{png,pdf}` | 同一 48-case campaign 的 `/proc/diskstats` | `scripts/render_m1_2_bandwidth_bm.py`；口径见 `M1_2_FIGURE_DESIGN.md` |
| m2' 多租户原生控制 | **最终** `figs/m2_motivation_bmr2644_1x8.{png,pdf}`；**备选** `figs/m2_motivation_bmr2644_1x6.{png,pdf}` | `runs/m2fig2/m2fig2_bmr2644_20261105/`（九臂数据保留） | 仓库根目录 `scripts/plot_m2_fig2.py`，布局分别为 `eight-row` / `motivation-row` |

M2 绘图口径与复现见 `M2_FIGURE_DESIGN.md`。其余旧图、历史脚本备份及活动生成器快照已归档至 `archives/m2_plot_versions_20261106_110935/`；迁移清单及 SHA256 见其中 `archive_manifest.json`。原始数据与实验执行脚本保留原位。

cache=off 对照数据留在 `runs/m1_campaign/m1cacheab_bm_20261105/ab/`
（12/12 ok，gate 修复后可用）——**用户裁定 cache on 口径足够，ab 不进入证据链**。

## 脚本/目录索引

| 路径 | 用途 |
| --- | --- |
| `scripts/ssh_env_bm.sh` | ssh/sudo 环境四件套（**含 sudo 密码，已 .gitignore，勿提交**） |
| `scripts/build_bundles_bm.sh` / `bundles/` | 运行时打包产物（打包只在本机） |
| `scripts/provision_bm.sh` | 代码 rsync + venv + bundle + preflight 编排 |
| `scripts/run_m1_cache_campaign_bm.py` | M1 campaign driver（flock/复位/tar 回收/--resume） |
| `scripts/run_m2fig2_bm.sh` | m2' 九臂 runner（preflight/互斥重试/per-arm validate） |
| `scripts/validate_m2fig2_arm_bm.py` | 九臂判据器（kvmhost 真实数据回放校准 9/9 等价） |
| `runs/` | 回传 tar + 解包 case 目录 + campaign.json（运行即记） |

## 一键复跑链

```bash
# M1（任一 campaign，--resume 断点续跑）
python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
    --campaign {m1cacheon,m1cacheab,m1nativematrix} --repeats 3 \
    --run-id <ID> [--resume]
# m1_1 分析（device 已参数化）
python3 experiments/m1m2_bm/scripts/m1_cache_cases_bm.py --results-root <RUN_ID目录>
# m1_2 分析 + 热力图
python3 experiments/m1m2_bm/scripts/build_native_matrix_summary_bm.py <RUN_ID目录>
python3 experiments/m1m2_bm/scripts/render_m1_2_policy_conflict_bm.py
# m2'
RUN_ID=<ID> bash experiments/m1m2_bm/scripts/run_m2fig2_bm.sh --all
python3 scripts/plot_m2_fig2.py --results experiments/m1m2_bm/runs/m2fig2/<ID> \
    --calibration experiments/m1m2_bm/runs/m2fig2/<ID>/calibration.json \
    --layout eight-row --out experiments/m1m2_bm/figs/m2_motivation_bmr2644_1x8
```

## 与 kvmhost 版的口径差异（正式声明）

1. **无 mkfs 复位**（root 盘）：drop_caches + 删建数据目录替代；shore 的
   db-tpcc-1 是 staging 只读输入，永不删
2. **bare-metal 而非 guest**：无 hypervisor 仲裁，campaign driver 用远端 flock 互斥
3. **root 属主复位件**：scene 经 sudo 拉起 db_bench/ycsb，数据目录会留 root 文件
   —— 复位 rm 必须 `sudo -n`（首轮 12/15 failure 的教训）
4. **shore 必须带 io_ext.cgroup**（会计-only）：无 cgroup_path 时 run_command 不包
   sudo，`chrt -r 99` EPERM（m1matrix 第一轮 12 SHORE 失败教训）
5. m2' file_name 裸盘 → 预分配文件 direct=1；io.max 用论文原值 1500/500 MiB/s
   （rate 未截断到 0.8×solo，与 kvmhost 不同——见 REPORT）
6. 与 kvmhost 定量对齐与设备效应归因见 **`REPORT.md`**
