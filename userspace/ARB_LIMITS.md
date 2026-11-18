# ARB 硬编码上限统一表（ARB_LIMITS）

**维护规则**：所有跨层硬编码容量参数以本文档为唯一权威记录。任何调整必须先改本文档、再同步表中列出的全部联动点。这些上限在实际部署中将开放给用户调控（见 §5），当前阶段的目标是**全层一致 + 一处维护**。

**历史教训**：曾存在 `ARB_MAX_TENANTS=16`（BPF pick 扫描空间）与 loader 注册 64 槽/pinned map 创建 64 条的静默失配——注册落在扫描盲区的租户对 pick 完全不可见，在 strict 模式下其真实排队请求永久饿死（见 `ioext-ssh/ARB_STRICT_HANG_DEEPROOT_AND_FIXPLAN.md` §10）。本文档的存在就是防止此类失配复发。

---

## 1. 核心容量不变量（必须保持一致）

| 符号 | 定义位置 | 当前值 | 消费层 | 语义 |
|---|---|---|---|---|
| `ARB_MAX_TENANTS` | `ioext_arb_common.h` | **64** | BPF 策略公共层 | pick 每轮扫描槽位数；`active_cgroup_ids` BPF 声明尺寸；`tenant_state` 的租户维；pick cursor 模数 |
| `IOSCHED_TENANT_LIMIT` | `ioext_loader_common.h` | **64** | loader 层 | pinned `ioext_active_cgroups` ARRAY 创建尺寸；pinned `ioext_policy_meta` HASH 创建尺寸；注册/注销槽扫描循环上限 |
| `ARB_SCAN_LIMIT` | `ioext_arb_common.h` | `= ARB_MAX_TENANTS`（64） | BPF | `arb_drr_pick` / arb_slo `hold_dispatch` 的扫描步数 |
| `ARB_MAX_HCTX` | `ioext_arb_common.h` | **64** | BPF 策略公共层 | `hctx_state` 尺寸；`tenant_state` 的 hctx 维；arb_slo 的 `inflight_count`/`slo_hold_cnt` 尺寸；pick 的 hctx 查表卫兵 |
| `tenant_state` 总容量 | `arb_{wdrr,slo,cost}.bpf.c` | `ARB_MAX_TENANTS × ARB_MAX_HCTX` = **4096** | BPF | 每 (cg, hctx) 状态条目；**插入失败即 pending 静默丢失 = strict 挂死** |

**不变式**：
- I1：`ARB_MAX_TENANTS ≡ IOSCHED_TENANT_LIMIT`（编译期无法检查，跨 BPF/C 两个头文件）
- I2：`tenant_state max_entries ≡ ARB_MAX_TENANTS × ARB_MAX_HCTX`
- I3：`ARB_MAX_HCTX ≥ 设备实际 hctx 数`（见 §2；当前无运行时 fail-loud，超限时 pick 恒返回 0 → strict 必挂，属已知待办，见 §4）

## 2. 设备侧对应参数（部署时必须核对）

| 参数 | 决定因素 | 当前值/来源 | 约束 |
|---|---|---|---|
| 设备 hctx 数 | virtio-blk: `min(宿主 num-queues, guest nr_cpu)`；NVMe: `num_queues` | virtme 测试盘默认 1:1 vCPU（8 vCPU → 8 hctx；64 vCPU → 64 hctx） | **≤ `ARB_MAX_HCTX`**，否则见 I3 |
| `nr_requests` | guest 内 `/sys/block/<dev>/queue/nr_requests` | 测试脚本统一设 1023 | 保证 elevator 侧积压（配合 BPS 节流） |
| virtio vring 深度 | QEMU 默认 | 256/vq | 设备侧在途上限；fio QD 与其联动决定积压位置 |
| BPS 节流 | `OSEXT_VM_BPS_THROTTLE` | 200 MB/s | 制造真实积压，arbiter 才有仲裁空间 |

## 3. 策略常量（非容量，同表记录便于后续开放调优）

| 常量 | 值 | 作用 |
|---|---|---|
| `ARB_WDRR_QUANTUM_BYTES` | 512 B | 每 unit 有效权重的 round 预算（round 预算 = quantum × weight × ew_mult，S12 起为全局轮）。F1 sweep 20261021 选定：预算须在单次 visit 内耗尽才能绑定权重——20260919 硬件 dump 证明 32 KiB 量子（3.2–26 MB 预算 ≫ 单 visit 深度 ~数十请求）使 visit 全部 pending-bound、refill ∝ 1/w、份额退化为均分 |
| `ARB_WDRR_MIN_BUDGET_BYTES` | 4 KiB | 权重预算绝对下限（一页）。S11 起，大请求场景的自适应部分改为**全组统一提升**（`ew_mult`，见下行），不再用每-hctx `last_round_max_charge`（该机制在零 charge 轮会清零抖动，且只抬低于它的键、压缩权重比例） |
| `ARB_WDRR_MAX_EW_MULT` | 32× | S11/S12 统一提升上限：每轮（全局轮）advance 按 `max ceil(own_max_charge/(quantum×w))` 同乘所有预算（own_max_charge = 本轮各 cgroup 自身最大 charge），保证"预算 ≥ 自身最大请求"的漂移防护且保持权重比例；超出上限则保留结构性单请求 overshoot（F1 caveat 2 的软退化区） |
| `ARB_WDRR_IDLE_CAP_MULT` | 2× | 单轮预算上限（防积攒） |
| `ARB_SLO_WINDOW_NS` | 100 ms | SLO 观测窗口 |
| `ARB_SLO_RECOVERY_WINDOWS` / `ARB_SLO_MIN_SAMPLES` | 3 / 20 | SLO 状态机恢复与采样门槛 |
| `ARB_SLO_BOOST_{NORMAL..MAX}` | 1/2/4/8/16 | boost 序列（有效权重 = base × boost） |
| `ARB_SLO_DEFAULT_PROTECT_INFLIGHT` | 32 | hold_dispatch 的 per-hctx inflight 门槛 |
| `IOSCHED_HEIMDALL_ACTIVE_LIMIT` | 64 | heimdall 策略活跃上限（独立于 arbiter 租户上限，注意区分） |
| q_fifo `io_queue` | 4096 | 每 cgroup 本地策略队列深度上限 |

## 4. CI 可断言的一致性检查（BUG 防线）

1. **注册完备性**：运行结束后 `tenant_state` 中出现的每个 cgid 必须存在于 `active_cgroup_ids`（丢失注册 = BUG-B 复发）
2. **注册-元数据一致**：`hctx_state.skip_no_meta == 0`（占用槽位但 policy_meta 缺失）
3. **账目存在性**：`hctx_state.skip_no_ts == 0`（已注册但 tenant_state 缺失——含 map 满）
4. **守恒律**：quiesce 后每键 `pending == 0 ∧ inflight == 0`（BUG-A 防线，四处记账配对）
5. **flock 有效性**：并发注册 N 租户 × N 次试验全部注册成功（`tests/io/arb_reg_race_tmp.sh` 模式）

**已知待办（fail-loud 执行项，未实施）**：设备 hctx 数 ≥ `ARB_MAX_HCTX` 时应拒绝 strict arbiter 注册而非静默挂死；租户数达到 `IOSCHED_TENANT_LIMIT` 时 loader 应显式报错（当前返回 -ENOSPC 已 fail-loud，但 BPF pick 对盲区无告警——观测计数器 §4.2/4.3 已补齐可见性）。

## 5. 部署调优路线（规划，未实施）

上述容量值后续将开放为用户可调（loader 参数 / sysfs）。联动关系：
- **提高租户数**：同步改 `ARB_MAX_TENANTS` + `IOSCHED_TENANT_LIMIT` + tenant_state 容量（= 乘积），并**重建 pinned maps**（`ensure_pinned_map` 按 `map_matches` 校验旧尺寸，尺寸不符返回 -EINVAL——调优后旧 pin 需清理）
- **提高 hctx 数**：同步 `ARB_MAX_HCTX` × tenant_state 容量；部署前核对设备 hctx 数（§2）
- BPF 侧 `active_cgroup_ids`/`policy_meta` 运行时复用 pinned map（BPF 声明尺寸仅在无 pin 时生效），因此 pinned 创建尺寸是真正的运行时上限