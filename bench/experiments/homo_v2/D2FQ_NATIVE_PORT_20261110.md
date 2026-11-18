# D2FQ native 移植 与 LinnOS 可行性结论（2026-11-10）

## 为什么 frontier 族此前没有 native 对照

kvmhost 设计收口（RUNBOOK_kvmhost §1）中 frontier 序列只有 IOX 臂（arb_d2fq /
q_linnos），native 参照柱复用 HOMO_ctrl_n_none——因为 D2FQ 与 LinnOS 是研究
调度器，主线内核没有对应机制可作 native 臂。bm 上由此造成 front_d2fq 至今
0 个有效同窗口 pair（repair 表口径）。

## D2FQ：已完成的 native 移植（ioextd2fq 内核）

- 参考实现：`skkucsl/d2fq reference tree`（skkucsl/d2fq 官方开源，Linux
  5.3.10 内核补丁，FAST'21 "Device-Direct Fair Queueing for NVMe SSDs"）。
- 硬件前置验证（go/no-go 门）：bm 控制器 CAP.AMS=01 = WRRU（WRR+urgent）支持，
  PCI BAR0 直读留档；CC.AMS 当前 00（round-robin），新内核启用 WRRU。
- 移植提交：ioext 仓 D2FQ-native 移植提交 +
  `abde241d1`（localversion），tag `v7.3.1-ioextd2fq`，release=7.3.1-ioextd2fq。
  结构：block/d2fq.c+gholder（flow lvt / CAS gvt）、blk-mq ctx 类别重映射 +
  完成路径 vt 记账、O_D2FQ 进程选举（fs read/write/aio/fsync）、9 个
  /proc/sys/kernel/d2fq_*、sysfs queue/d2fq_en（默认 0）、NVMe WRRU 侧
  （SQ 优先级旋转 + d2fq_set_weight + CC.AMS=WRRU）。
- 7 条声明偏差见移植头注释 include/linux/d2fq.h（O_D2FQ 位 26→27、PF_D2FQ
  位、MULTISQ 未移植、d2fq_force_all 调试开关、参考码 5 项 bug 修复——含
  **非 MULTISQ 路径 rq->d2fq 从未赋值导致官方 vt 记账从不执行**的参考码缺陷）。
- 对齐性：bm = 8 CPU / 8 个默认 hctx（1:1），参考码类↔SQ 优先级旋转公式
  （nr_q = ncpus/3 = 4，m % nr_q = 8 % 4 = 0）原封成立，无需偏差。
- rig 侧：新臂 `front_n_d2fq`（scheduler=none + queue/d2fq_en=1 +
  kernel.d2fq_thrsh=8192/md_ratio=70/di_period=50(=200ms)/dw_en=0 +
  force_all=1 声明偏差；cls_base 由参考码 d2fq_en=1 约定=6 == x_d2fq_arb
  冻结 --fast-share 6）。序列 17→18，sidecar 增加 `-- d2fq`
  （d2fq_en + sysctl 证据行），builder expected_state 匹配 d2fq_en=1。

## LinnOS：native 不可行的原因（维持不可做）

1. **没有可对照的参考实现**。本地只有 OSDI'20 论文 PDF
   （LinnOS (OSDI'20) paper）；已核查 GitHub：无可信官方 artifact
   （唯一同名仓库 mihir-b-shah/LinnOS 无 README、无法证实出处，且原论文
   target ~4.x 内核，非官方）。
2. **论文核心是内核 NVMe 提交/完成路径内的在线 NN 推理 + 每设备离线训练**。
   忠实 native = 从论文重建推理引擎与特征管线、在 bm NVMe 上重建训练数据
   采集与训练、驱动级 patch——是独立的研究复现项目，且没有参考实现语义
   可做"同态"对照。
3. **前人已有显式决策**：q_linnos.bpf.c 头注释声明 "OFFLINE LOOKUP-TABLE
   APPROXIMATION, NOT the online neural network of the LinnOS paper"
   （RUNBOOK_kvmhost §9.1："论文语义偏差（在线 NN→离线表）已声明"）；设计上
   native 参照 = front_n_none。因此 front_linnos 的 native 维持不可做，
   对照口径不变（front_n_none + 已声明离线表近似）。

## 下一步（按已批准计划）

1. ioextd2fq deb 构建完成 → virtme-ng 烟测（默认关 d2fq 零行为差异；
   d2fq_en=1 冒烟）。
2. 部署 bm（执行前与用户确认无其他任务）。
3. front 三臂 campaign：HOMO_front_n_d2fq / x_d2fq_arb / n_none × 3 reps。

## 构建与烟测记录（2026-11-10，ioext-kernel-build/ioext-deploy 前置）

- 构建：icecream 分发编译（`make -j16 CC="icecc gcc" bindeb-pkg`，调度器
  127.0.0.1）。deb 产物
  `kernel_src/linux-image-7.3.1-ioextd2fq_7.3.1-27_amd64.deb`
  sha256 `d9526365a8a4f1f63ca90d52e36004e35a7579808e3b40531cae48ac7e90ff60`
  （package rev 27，tag `v7.3.1-ioextd2fq`）。构建/烟测全部
  日志归档于 `experiments/homo_v2/build/ioextd2fq/`。
- 烟测（virtme，virtio 盘，无 NVMe/WRRU 环境）：
  1. boot OK，`uname -r` = 7.3.1-ioextd2fq；
  2. `/proc/sys/kernel/d2fq_{read,write}_weight/thrsh/cls_base/md_ratio/
     di_period/dw_en/dw_period/force_all` 九项全部在线，
     dmesg 无任何 sysctl 注册失败；
  3. `/sys/block/sda/queue/d2fq_en` 存在且默认 0；
  4. **非 NVMe 队列写 1 → 被 -EINVAL 安全拒绝**（无 set_weight hook 防护，
     提交 f599062d9），内核状态不变；
  5. 同盘 fio direct=1 顺序写正常（49.8 MiB/s @16k）。
  device-direct WRR 语义（SQ 优先级旋转 + arbitration feature）只能在
  bm 的 NVMe WRRU 硬件上验证，属部署后验收项。
- 决策记录：修复烟测暴露的 4 个问题时（NULL 防护 / core→late initcall /
  空哨兵项 / WQ_UNBOUND），每次都以"完整重打 deb"保持
  deb == 提交树 == tag 的可追溯性（-23、-25 为中间版，未部署）。
- 待办：部署 bm（执行前与用户确认无其他任务）→ 部署后对 nvme0n1 冒烟
  `queue/d2fq_en=1`（WRRU 真机验证）→ front 三臂 campaign。
