# RUNBOOK — 六阶段动态多租主实验 · 裸机 bm 版（dyn_tenant_bm）

主实验 IoExtTimelineScene 从 kvmhost（独占盘 96G /dev/sdb）移植到裸机 bm
（**根盘** nvme0n1，rootfs /dev/nvme0n1p2 ext4 915G，内核 7.3.1-ioext2，scx_io 可用）。
主图九臂（2026-11-06 修正口径）= **n1-n4 native + C1-C4 cgroup io 控制臂 + f1_arbfb**
（x1-x4 不在 bm 上跑；f1 同时是冻结 schedule 的终态 = 整段常驻 dynamic-resident）。

本 RUNBOOK 是分段执行顺序权威；驱动 = `experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh`。

---

## 0. 硬性安全规则（根盘口径，先读）

bm 没有独占实验盘，实验跑在**根盘**上。铁律：**绝不向 /dev/nvme0n1 裸设备写任何字节**。

- 租户 fio 的 target 一律是**预分配区域文件**。机制（2026-11-06 实测修正）：
  - fio 的**重复 `--filename` 是多文件 stripe，不是覆盖**——实测 fio-3.36：
    不存在的第一个 `--filename` 会被**创建**（大小恰为 `--size/nfiles`），
    I/O 范围在两个文件间**切分**；fio JSON 的 `job options` 只回显最后一个
    选项字符串，**不构成**“后者生效”的证据。若 filename 走 tenant-spec
    passthrough，D 租户的 randwrite 会 stripe 到裸根盘上。
  - 因此 `filename` 是 timeline 场景的**保留键**（`_TENANT_SPEC_RESERVED_KEYS`）：
    场景直接以 `--filename=<区域文件>` **单一** `--filename` 参数构造 fio argv，
    并在 config 期**拒绝 `/dev/*` 目标**；无 filename 的 spec 保持裸设备默认
    （kvmhost/virtme 兼容）。
  - 驱动里**绝不出现 mkfs / mount / 裸设备 fio**；根盘守卫（remote_guards）
    断言 `/dev/nvme0n1` 裸设备节点**不是任何挂载源**（只有分区可挂载）。
- 四层防裸盘：生成后 audit（每个场景每个租户必须带 region filename，缺一即
  拒绝 push）→ 场景 config 期拒绝 /dev 目标 + 单 --filename 构造 → 开跑前
  区域文件 direct-IO 探针（O_DIRECT-on-ext4 可行性）→ 跑后逐租户 **spawn argv
  审计**（argv 必须恰含一个 `--filename` 且为 region 文件；timeline stat JSON
  里的 `spawn_record.argv`/`fio_target`）+ fio JSON 有效 filename 交叉核验 →
  validate 链。
- 旧 loader 树 `/home/anon/iosched_ext`（ioext1 内核编译，iox_*）**禁止使用**；
  驱动只认 2026-11-06 对 ioext2 树重编的 loader（MANIFEST sha256 校验）。

## 1. 交付物与路径（控制机 = 本仓库）

| 件 | 路径 |
|---|---|
| bm 驱动 | `experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh` |
| 生成配置审计 | `experiments/dyn_tenant_bm/scripts/audit_dyn_tenant_bm_yamls.py`（push 前置） |
| fio 生效 filename 审计 | `experiments/dyn_tenant_bm/scripts/audit_fio_effective_filenames.py`（跑后） |
| iocost 系数组装 | `experiments/dyn_tenant_bm/scripts/assemble_iocost_coef_bm.py` |
| C 臂安全账本 | `experiments/dyn_tenant_bm/scripts/record_c_arm_ledger_bm.py`（campaign `dyn_tenant_static_tuning_bm`，机制同 `scripts/record_c_arm_ledger.py`） |
| loader 重编产物 | `experiments/dyn_tenant_bm/build/loaders/`（7 个 loader + .bpf.o/.skel.h + `MANIFEST.sha256` + `BUILD_PROVENANCE.txt` + `userspace_io_uncommitted.patch` + 内核 config） |
| loader 构建日志 | `experiments/dyn_tenant_bm/build/loader_build.log` |
| 本地干跑配置 | `experiments/dyn_tenant_bm/outputs/{no_calib,standin_calib}/` + `gen_*.log` + `gen_diff_summary_bm_vs_kvmhost.txt` |
| bm 合同（freeze 产物） | `experiments/dyn_tenant_bm/contract/`（calibration.json / frozen_manifest.json / iocost_coef.json / iocost_coef_gen_stdout.log） |
| 结果回拉 | `experiments/dyn_tenant_bm/results/`（`DYN_*` 序列目录 + `_logs/` + `_pass.log`） |
| bm 侧约定路径 | 部署根 `/opt/ioext`；loader `/opt/ioext/io-ext/userspace/`（m2' 惯例）；区域文件 `/opt/ioext/dyn_tenant_regions/region_{a,b,c,d}.bin`；配置 `src/vm/configs/task/isol_bench/rig_dynamic_tenant/bm/`；结果 `results/dyn_tenant_bm/` |

## 2. 区域文件布局设计与理由

kvmhost 冻结配置（`results/dyn_tenant_kvmhost/dynamic_tenant_*.yml`）里租户区域 =
timeline 场景内建四等分：每租户独占 25% 连续区段（offset 0/25/50/75%，size 25%，
direct IO 直接打 /dev/sdb 裸设备，yml 中不出现显式 disk_layout/tenant spec）。

bm 版 = **四个独立预分配文件**，每租户一个：

```
/opt/ioext/dyn_tenant_regions/region_a.bin  30G  (A: 4K/QD1 randread, 全程 T1..T6)
/opt/ioext/dyn_tenant_regions/region_b.bin  30G  (B: 64K/QD8 randread, T2..T3)
/opt/ioext/dyn_tenant_regions/region_c.bin  30G  (C: 64K/QD8 randread, T3..T4)
/opt/ioext/dyn_tenant_regions/region_d.bin  30G  (D: 4K/QD64 randwrite, T4..T5)
```

每个租户 spec：`filename=<REGION_DIR>/region_<t>.bin` + `disk_layout {offset '0', size 30G}`。
选型理由：

1. **不重叠由构造保证**（独立 inode），不依赖 fio 百分比 offset 在普通文件上的语义
   细节（与块设备语义有差）；显式 `size 30G` 无任何百分比歧义。
2. **等额私有区域 = kvmhost quarter 语义的等价物**：同一条盘、四等份、每租户独占一段；
   30G×4=120G 在 ~120G 预算内（bm /home 748G 可用，驱动守卫要求 ≥140G）。
3. **fill_once 逐文件填满 = kvmhost 整盘填充的逐区段等价**：A/B/C 的 randread 窗口
   全部落在真实已写介质上；D 的 randwrite 区域同样预先写入（与 kvmhost quarter
   预写完全一致）。marker + 填充记录 json 落 `$DEPLOY_ROOT/results/`。
4. **target_device 仍是 nvme0n1**：文件 IO 一样过 nvme0n1 块层（scx_io、子 cgroup
   io.max/io.latency/io.weight、根 io.cost 全部照常生效）；bm 的 io 控制器可用性已由
   m2' 九臂实证（root subtree_control 含 io；io.max/io.latency 子 cgroup 实测可用；
   io.cost.model/qos 根可写）。

## 3. 分段执行顺序（每段确切命令）

所有阶段统一入口（flock 防并发；缺省每段自带 sync → guards → fill → probe → run →
pull → audit → validate）：

```bash
cd /opt/ioext
RUN86=experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage <NAME>
```

### S1. smoke（骨架冒烟，L=15s）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage smoke
#   = DYN_smoke（全轨迹 T1..T6）+ DYN_smoke_ab（A+B/T1-T2）
# 首次运行自动完成：loader rsync（sha256 校验）、整仓 rsync、gen+audit+push 配置、
# remote guards、region 文件一次性 fill（4×30G，几分钟）、区域文件 direct-IO 探针、
# 两个序列执行、回拉 + spawn argv 审计 + fio JSON 审计 + validate_isol_bench_result.py。
# 想只做同步/预检不跑实验：--sync-only；guards+fill+probe 后停：--skip-run。
```
预期：`PASS=2 FAIL=0`；`experiments/dyn_tenant_bm/results/DYN_smoke{,_ab}/` 出现，
每租户 fio JSON 的 `job options.filename` 全部是 region 文件。

### S2. calibration（bm 独立标定）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage calibration
#   = DYN_calib_a/b/c/d（单租户 120s）+ DYN_calib_t4（T4={A,C,D} 2×60s，C 不限速）
# L 候选按合同规则（60→90→135）需要时重跑 t4：
#   REGION_SIZE 不变；驱动 gen 阶段固定 --t4-phase-length 默认 60；
#   L 升级轮：bash $RUN86 --stage calibration --config dynamic_tenant_calibration.yml \
#       DYN_calib_t4   # 之后手工以 --t4-phase-length 90/135 生成（见 gen --help）
```

### S3. freeze（bm 合同重冻结，本机执行）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage freeze
# = analyze_dynamic_tenant_calibration.py --results-root experiments/dyn_tenant_bm/results \
#     --contract-dir experiments/dyn_tenant_bm/contract
# + freeze_dynamic_tenant_contract.py --contract-dir .../contract
# 产物：contract/calibration.json（含 frozen: L/rate_b/rate_c/d_pressure/solo p99…）
#       + contract/frozen_manifest.json（frozen 完整性校验，缺字段拒写）。
# 注意：C1-C3 常量与 C1/C3 派生、smoke→static 的全部“bm 数字”都来自这里的
# calibration.json（solo steady_mib_s / p99_us_steady / frozen rate_b,c）。
```

### S4. iocost（bm 实测 io.cost 系数，写入合同）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage iocost
# 机制见 §4；产物 contract/iocost_coef.json + contract/iocost_coef_gen_stdout.log。
# 只允许在 bm 空闲时执行（驱动会预检 executor/loader 进程；工具会临时把 nvme0n1
# elevator 设为 none 并在退出时还原，驱动复验 scheduler 已还原）。
```

### S5. baseline n1-n4（native 静态臂）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage static_native
#   = DYN_arm_n1_native_none / n2_native_mqddl / n3_native_bfq / n4_native_kyber
# 前置：S3+S4 合同齐（驱动 gen 阶段强制校验 calibration.json + iocost_coef.json 存在，
# C4 缺实测系数时生成器按设计拒绝）。
```

### S6. C1-C4（cgroup io 控制臂 + 安全账本）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage c_arms
#   = DYN_arm_c1_io_max / c2_bfq_weight / c3_io_latency / c4_io_cost
# 常量派生（同公式、bm 数字；gen 打印派生清单进 results/gen_stage2.log）：
#   C1: b.rbps=rate_b_mib_s, c.rbps=rate_c_mib_s, d.wbps=floor(0.8*solo_d)（A 不设）
#   C3: target(t)=ceil(2*solo p99_us_steady 向上取整到 1000us)
#   C2/C4 权重: A:C:B:D = 400:200:100:100（原样复用）
#   C4 model/qos: contract/iocost_coef.json（§4 的 bm 实测）
# 每臂跑完自动写安全账本（results/iox_policy_exploration/dyn_tenant_static_tuning_bm/
# attempts/carm86_DYN_arm_<arm>_<ts>/，证据=回拉目录+审计后配置+账本注记）。
```

### S7. f1（复用冻结 schedule 跑 f1_arbfb = 整段常驻终态）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage f1
#   = DYN_arm_f1_arbfb（scx_io + arb_feedback 常驻 + 每租户 q_fifo, prio 0/5/2/5,
#      weight 400/100/200/100，cgroup rdyn_arm_f1_arbfb_t{a..d}）
# 冻结 schedule（schedule_feasible_iox_only.json，num_transitions=0,
# COINCIDE_DP_IS_WHOLE_RUN）的终态就是“整段常驻 f1”——所以 f1 这一个真实 run
# 同时提供“dynamic(=resident)”与“best-static”两个主图身份（与 kvmhost 口径一致）。
# 注意：bm 直接复用 kvmhost 的冻结 schedule 文件（决策/拓扑不变），但 f1 的损失
# 对比数字全部来自 bm 自己的 run；rates/常量都是 bm 合同派生。
```

### S8. heldout seed201（主图九臂：n1-n4 + C1-C4 + f1）
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage heldout
#   = DYN_held_s201_{n1_native_none,n2_native_mqddl,n3_native_bfq,n4_native_kyber,
#                    c1_io_max,c2_bfq_weight,c3_io_latency,c4_io_cost,f1_arbfb}
# bm 臂集由 DYN_TENANT_HELDOUT_ARMS 切换（gen 默认仍是 kvmhost x1-x4 集合，兼容复现）；
# heldout 配置从 static_arms yml 逐字拷贝 C 臂 io.* extras（常量同源，零漂移）。
# 配对种子（可选扩展，f1 vs bfq, 202-205）：
#   bash $RUN86 --stage heldout DYN_held_s202_f1_arbfb DYN_held_s202_n3_native_bfq \
#                DYN_held_s203_f1_arbfb DYN_held_s203_n3_native_bfq \
#                DYN_held_s204_f1_arbfb DYN_held_s204_n3_native_bfq \
#                DYN_held_s205_f1_arbfb DYN_held_s205_n3_native_bfq
```

### S9.（可选）validation 预声明种子 s101-103
用户流程（2026-11-06）不含该段；如需预声明验证证据（dynamic-resident f1 vs
native bfq @ s101-103，五件套之一已可生成）：
```bash
flock -w 7200 /tmp/dyn_tenant_bm.lock bash $RUN86 --stage validation
```

### 执行完后的本地分析
```bash
# 帧数据：各序列目录 tenant_<t>/ 下 fio JSON + clat_hist + bw 日志（kvmhost 同构）。
# 主图（R6 已完成 2026-11-07）：
#   DYN_TENANT_HELDOUT_RESULTS_ROOT=experiments/dyn_tenant_bm/results #   DYN_TENANT_HELDOUT_ARM_SET=bm DYN_TENANT_ARM_RESULTS_ROOT=<同上> #   DYN_TENANT_CALIB_JSON=experiments/dyn_tenant_bm/contract/calibration.json #     python3 scripts/analyze_dynamic_tenant_heldout.py
#   python3 scripts/plot_dyn_tenant_main.py --arm-set bm #     --result-root experiments/dyn_tenant_bm/results
# 产物 figures/dyn_tenant_main.{png,pdf}+summary+timeseries、heldout_{matrix,analysis}.json。
# 注意四个 env 缺省会静默回落 kvmhost（ARM root/CALIB_JSON），必须显式指 bm。
# s201 实测：f1_arbfb loss 73.9 vs native bfq 169.3（f1 优 56.3%）。
```

## 4. C4 io.cost 系数的 bm 测量机制

**kvmhost 怎么测的**：内核自带工具
`<kernel_tree>/tools/cgroup/iocost_coef_gen.py --testdev /dev/sdb`
（独占盘、破坏性顺序/随机读写全盘探测，临时 elevator=none+nomerges=1 后还原），
stdout 的最后一行 `<MAJ:MIN> rbps=… rseqiops=… rrandiops=… wbps=… wseqiops=… wrandiops=…`
被抄进 `contract/iocost_coef.json` 的 `model`（补 ctrl=user model=linear），qos 为
预声明块（rpct/wpct=95, rlat/wlat=20000us, min=50 max=150，与 m2 fig2 io_cost 臂一致，
跨图一致性而非调优）。工具 sha256、stdout 日志、还原证据都记进 provenance。

**bm 怎么测**：同工具、**testfile 模式**（不给 `--testdev`）——这是 bm 唯一安全口径，
因为 `--testdev` 会向裸设备写，而 bm 的实验盘就是根盘：
- 工具在 cwd（`/opt/ioext/dyn_tenant_regions/iocost_probe/`，根文件系统上）
  生成 `iocost-coef-fio.testfile`（默认 `--testfile-size-gb 16`，可用 `IOCOST_TESTFILE_GB`
  调大），用 direct IO 跑同套探测；
- `dir_to_dev('.')` 自动解析到文件所在的 **nvme0n1 259:0** —— 也就是实验租户实际
  走的同一文件路径、同一设备，测得的系数与实验负载路径同源（比 kvmhost 的裸盘口径
  更贴近 bm 的实际使用方式）；
- 工具需要 `pv`（bm 没有）→ probe 目录内放一个透传 shim（`for a; case -*)… cat`），
  探测结束自动删除（变更+还原清单 #5）；
- 工具临时把 nvme0n1 的 elevator 设为 none + nomerges=1 并在退出时还原（kvmhost 日志
  同款证据行）；驱动前后各读一次 scheduler 并断言还原；
- 产物组装：`assemble_iocost_coef_bm.py` 解析 stdout 行 → `contract/iocost_coef.json`
  （model + 预声明 qos + bm provenance/模式注记），供 `DYN_TENANT_IOCOST_COEF_JSON`
  消费；gen 端 C4 拒绝无实测系数（measured constants only，kvmhost 同设计）。

## 5. 冻结 json 的复用口径

| 件 | bm 口径 |
|---|---|
| `schedule_feasible_iox_only.json` / `schedule_feasible.json` / `schedule_proposal.json` | **直接复用** kvmhost 冻结版：决策（num_transitions=0，整段常驻 f1）与转换可行性掩码是策略层结论，不随设备变；S7 只执行终态。 |
| `contract/calibration.json`（rates/solo/L/压力比例） | **bm 重冻结**（S3）：rate_b/rate_c/solo p99/d_pressure 全部由 bm 标定重出；C1/C3 常量从 bm 合同派生（不是 kvmhost 字面量）。 |
| `contract/iocost_coef.json` | **bm 重测**（S4）：testfile 模式实测；qos 预声明块与 kvmhost 保持一致。 |
| 冻结 manifest 种子/权重/损失公式 | 直用（seed_base 20261103、heldout offset 201-205、损失权重 a .6/c .3/bd .1）。 |

## 6. 变更 + 还原清单（bm 侧系统级/持久变更）

| # | 变更（驱动执行时发生） | 还原 |
|---|---|---|
| 1 | loader 覆盖/新增 `/opt/ioext/io-ext/userspace/`（7 个，ioext2 重编；m2' 原有 5 个会被同名覆盖为今天重编版） | 保留 `experiments/m1m2_bm/bundles/rq5_ioext_loaders_bm.tar.gz` + `.sha256` + `deploy_loaders_bm.sh` 可整树重装回 m2' 版本 |
| 2 | 部署根整仓 rsync（无 --delete；`src/ scripts/ docs/…` 更新到本仓库 HEAD；**不含** experiments/results/.venv） | 需要回 m2' 状态时从 m2' bundle 重新 provision（experiments/m1m2_bm/scripts/provision_bm.sh） |
| 3 | `src/vm/configs/task/isol_bench/rig_dynamic_tenant/*.yml`（顶层 raw-device 默认 yml）被删除，bm/ 子目录为唯一在位变体 | `python3 scripts/gen_dynamic_tenant_yamls.py`（默认参数）可随时重生成 |
| 4 | 新目录 `dyn_tenant_regions/`（4×30G 区域文件）与 `results/dyn_tenant_bm/`、`results/.dyn_tenant_bm_*` marker | `rm -rf` 即可（~120G 释放） |
| 5 | iocost probe：`dyn_tenant_regions/iocost_probe/`（工具副本 + 16G testfile + pv shim），probe 后 shim/testfile 自动删；nvme0n1 elevator/nomerges 临时改并自动还原（驱动复验） | 目录余留工具副本，`rm -rf iocost_probe` 即净 |
| 6 | C4 运行会写**根** io.cost.model/qos（bm 根盘 iocost 作用于全盘 IO —— 见 R4）；每臂结束不自动还原 | 记录 pre 状态（guards 快照）后手工还原：`echo 'enable=0' > /sys/fs/cgroup/io.cost.qos`（或回写快照行）；bm 当前 pre 状态 = m2' 时代的 user 模型行（259:0 … rbps=335544320 …），guards 会把它打进日志 |
| 7 | 设备 scheduler 会被场景切换（scx_io/none/bfq/kyber/mq-deadline）；驱动不强制还原 | 实验后 `echo none > /sys/block/nvme0n1/queue/scheduler`（bm 当前活动调度器为 none） |
| 8 | 本机（控制机）`ioext/userspace/io/build/` 重编产物 + `build/vmlinux.h` 重生成 | 无需还原（构建产物；kvmhost 侧 rsync 也会用到，且均为 ioext2 对齐件） |

**控制机源码变更（本任务完成，已生效，未 commit——按纪律不提交）**：

| 文件 | 变更 | 兼容性证据 |
|---|---|---|
| `src/vm/scene/io_ext/timeline.py` | `filename` 加入 `_TENANT_SPEC_RESERVED_KEYS`；tenant spec 校验（绝对路径、拒 `/dev/*`）；`_spawn_tenant` 以 spec filename 作为**单一** `--filename`（否则设备默认）；spawn_record 增 `fio_target` | 78 项单测通过；argv 实测两种模式各恰一个 `--filename`；kvmhost 复现 yml 复验不漂移（仅设备字母/派生措辞差）。kvmhost 冻结 manifest 中 timeline.py 的历史 sha256 不受影响（bm 冻结时哈希新版本，provenance 正确） |
| `scripts/gen_dynamic_tenant_yamls.py` | 区域文件模式（`DYN_TENANT_REGION_DIR/SIZE`）、`--skip-static-arms`、C1/C3 合同派生（`derive_c_arm_constants`，保 kvmhost 字面量回退+WARNING）、heldout 臂集 env 切换（`DYN_TENANT_HELDOUT_ARMS`，bm 集 = n1-n4+c1-c4+f1）、loader log dir env、派生清单打印 | kvmhost 合同下派生值与历史字面量逐一相等（299/198/166；1000/19000/24000/11000）；五件套 kvmhost 复现仅设备字母/派生措辞差 |


## 7. 风险与未决清单

- **R1 loader 内核兼容性验证点**：7 个 loader 已对 ioext2 树（冻结版，与 bm 部署的
  linux-image-7.3.1-ioext2 同源）BTF 重编、静态 ELF、`--help` 探测通过；但**真实 struct_ops
  attach 只能在 bm 上验证**（m2' 五臂已实证 ioext2 下 loader 可用，arb_feedback/q_none 是
  本次新上 bm 的两个）——S1 的 io_ext smoke（`--stage heldout` 前）或 f1 首跑即验证点；
  失败表现为 scx_io attach 报 struct_ops 注册错（对照 m2' LEDGER 的处理口径）。
- **R2 fio 多 filename 语义（已消解，2026-11-06 实测）**：fio 把重复 `--filename`
  当**多文件 stripe**（不存在者被创建于 `--size/nfiles`，I/O 范围切分），fio JSON
  回显只留最后一个字符串。该发现推翻了“passthrough 后者覆盖”的初版设计；修复 =
  `filename` 成为 timeline 场景**保留键**（单一 `--filename` 构造 + config 期拒绝
  `/dev/*`）。已验证：region 模式下 `_spawn_tenant` argv 恰含一个
  `--filename=<region 文件>`；无 filename 的 kvmhost/virtme 默认 argv 恰含一个
  `--filename=/dev/<设备>`（78 项 scene 单测通过）。残余风险仅在“生成器忘发
  filename”——由 push 前 audit + 场景 config 期拒绝 + 跑后 argv 审计三层拦截。
  fio-3.41（bm）与 3.36（控制机）在“单 --filename”下无语义差异。
- **R3 根盘噪声**：bm 不是独占实验机（系统盘 IO、其它 campaign 目录共存）。实验
  期间要求无其它负载；guards 预检 executor/loader 进程。结果与 kvmhost 的可比性
  以“同设备同负载形态”为限，跨机对比需注明环境差异（根盘口径 vs 独占盘口径）。
- **R4 C4 根 io.cost 语义差异**：kvmhost 的 io.cost 写在独占盘上；bm 写在根盘 =
  iocost QoS（rpct/rlat…）作用于**全盘所有 IO**（含系统 IO）。C4 的臂间对比仍成立
  （同机同对照），但绝对数与 kvmhost 不可直接比；跑后按 §6-#6 还原。io.max/io.latency/
  io.weight 语义无此差异（写的是租户子 cgroup rdyn_<seq>_t{a..d}，kvmhost 同款）。
- **R5 ext4 文件 IO 的直接 IO 语义**：direct=1 + O_DIRECT 对预分配大文件在 ext4 上
  稳定（m2' 在同盘用文件口径跑过九臂）；4K/64K 对齐 OK。若首跑出现对齐错（EINVAL），
  检查 fill 后文件是否碎片化（fio O_DIRECT 无影响；无需 fallocate 预分配因为 fill
  已全量写）。
- **R6 主图/分析脚本适配未做**：`plot_dyn_tenant_main.py` PANELS 仍是 x1-x4 九臂、
  RESULTS 指向 kvmhost；bm 主图需改臂清单为 n1-n4+c1-c4+f1（+dynamic_resident 身份
  复用 f1 run）并指向 `experiments/dyn_tenant_bm/results`。heldout/静态臂的分析脚本
  （analyze_dynamic_tenant_static_arms.py / analyze_dynamic_tenant_heldout.py 等）同样
  需要按 bm 结果根与臂集适配（本任务未含）。
- **R7 bm fio=3.41 vs kvmhost fio=3.36**：hist 日志/clat JSON 格式兼容（validate 链 +
  analyze 链按同 schema 解析），但 percentile 输出细节差异未逐一核对——S1 smoke 后
  跑一次 `validate_isol_bench_result.py`（驱动已内置）确认。
- **R8 iocost testfile 模式测得的系数**带文件系统层开销（kvmhost 是裸盘口径）——
  这对 bm 是**更贴近实际**的口径（负载也走文件），但与 kvmhost 数字不可直接比。
- **R9 `--stage calibration` 的 t4 L 升级轮**需要手工以 `--t4-phase-length 90/135`
  重生成（gen 参数）；RUNBOOK S2 已注明。若 bm 标定判定 L 与 kvmhost 不同（60/90/135），
  S5 之后所有臂的 phase_length_sec 自动跟 bm 合同（gen 从合同读 L）。
- **R10 未提交源码状态**：userspace/io 在 ioext 工作树有未提交对齐改动
  （`userspace_io_uncommitted.patch` 已存证）；控制机仓库本身大量未提交文件
  （含本任务全部新增/修改）——按纪律不 commit，交付即工作树状态。
