# io.weight → arb_wdrr 映射与语义审计（ARB_IO_WEIGHT_MAP）

> **审计基准**：`ioext_arb_common.h`（`ARB_WDRR_QUANTUM_BYTES=512`、`ARB_WDRR_MIN_BUDGET_BYTES=4096`，F1 sweep 20261021 / S6 / S10 之后的现行值）、
> `arb_wdrr.bpf.c`、`tests/io/arb_wdrr_model.py`（DRR 核心 spec）。
> **可执行审计**：`tests/io/arb_io_weight_model.py`（本文件的每个结论都有对应断言；
> 若 `ioext_arb_common.h` 的量子/下限改动而本表未更新，该测试会 FAIL）。
> **审计日期**：2026-11-06。

---

## 1. 审计结论

**arb_wdrr 已能表达 io.weight 语义（结论：等价，带两个已量化的边界 caveat），无需改动代码。**

| io.weight 语义要求 | arb_wdrr 现状 | 判定 |
|---|---|---|
| 值域 1..10000 | `-W/--arb-weight` 为 u32（`iosched_policy_meta.arb_weight`），无额外值域限制；`ew = weight × boost`（wdrr 的 boost 恒 1） | ✅ 全域可填 |
| weight 比 ≈ 长期份额比 | round budget = `512B × weight`（严格线性），饱和积压下份额比 == 预算比；F1 硬件证据（kvmhost，D2 flat cg8，q=512）加权 Jain 0.9823，max/min 5.29；S11 起饱和 coloc（homo v2：LC 4K rr QD32 w100 / BE 64K sw QD16 w200，设备 burst/stall regime）下字节份额 1:2 由 arm-on-dry + hold-while-inflight 守住（模型 `homo_coloc_byte_share_1_to_2`，硬件修复前实测 0.765） | ✅ 线性无隐藏缩放 |
| 无额外隐藏开销 | charge = 字节精确（读写无差别、无 cost 模型、无 size 乘子），requeue 退款精确等于已 charge 字节 | ✅ 见 §5 开销清单 |
| 比例外唯一非线性 | round budget 绝对下限 4096B：`512×w < 4096`（即 **w < 8**）时 ew 被抬到 8 → 1..7 不可区分 | ⚠️ caveat 1（§3.1） |
| 大权重保真 | 预算 ≳ 单 visit 深度时 visit 变 pending-bound（F1：q=2048 时 top weights pending-limited，Jain 0.974→轻微退化）；S10 round-hold 保证大预算跨 pending-refill 周期仍绑定，浅队列 regime 下 100:1 比例仍然存活（模型实测 ≈96） | ⚠️ caveat 2（§3.2） |

**映射规则（实验直接照填）**：

```
-W <native io.weight>          # identity，w >= 8 时严格保真
```

| native io.weight | arb_wdrr `-W` | round budget（字节） | 说明 |
|---|---|---|---|
| 1 | 1 | 4096（floor） | 与 2..7 不可区分（caveat 1） |
| 2 | 2 | 4096（floor） | 同上 |
| 5 | 5 | 4096（floor） | 同上 |
| 7 | 7 | 4096（floor） | 同上 |
| 8 | 8 | 4096 | floor 之上最低档，恰好等于下限 |
| 10 | 10 | 5120 | 线性 |
| 50 | 50 | 25600 | 线性 |
| **100（native 默认）** | **100（loader 默认，无需填）** | 51200 | 双方默认一致 |
| 200 | 200 | 102400 | 线性 |
| 500 | 500 | 256000 | 线性 |
| 1000 | 1000 | 512000 | F1 实测保真区上沿附近（4K IO ≈ 128 请求/visit） |
| 2000 | 2000 | 1024000 | caveat 2 区：浅队列下靠 S10 维持 |
| 5000 | 5000 | 2560000 | 同上 |
| 10000 | 10000 | 5120000 | 同上（模型实测浅队列比例 ≈96，见 §3.2） |

---

## 2. 为什么 WDRR 能表达 io.weight（机制）

- io.weight（cgroup v2）语义：1..10000 的比例配额，默认 100；饱和竞争下长期服务份额比 == 权重比。
- arb_wdrr 的公平单位是**字节**：每轮预算 `quantum(512B) × weight`，deficit 按派发字节扣减；
  饱和积压下长期字节份额比 == 权重比（`tests/io/arb_wdrr_model.py` case1/case3 +
  F1 硬件闭环）。这正是 io.weight 需要的全部机制——无 cost 模型、无延迟反馈。
- strict 模式（`IOSCHED_ARB_STRICT`）保证 generic fallback 不绕过权重决策；
  liveness 由 `arb_drr_pick` 保证（有人 pending 时必返回非 0）。
- io.weight 默认值 100 恰好等于 loader 的 `-W` 默认（`IOSCHED_DEFAULT_ARB_WEIGHT=100`）。

## 3. 两个 caveat（均为已量化、可回避的边界，而非等价性破坏）

### 3.1 caveat 1：低端 floor 压缩（io.weight 1..7）

`arb_drr_pick` 的预算绝对下限（`4096B`，S6/S11）把
`512×w < 4096` 的权重统一抬到 ew=8：**-W 1..7 行为完全相同**，且对大权重的比例
也按 8 计（io.weight 1 vs 100 实际表现为 8:100 而非 1:100）。
该下限是 DRR 的饥饿防护（小权重 + 大请求不至于每轮只拿一个 overshoot），
属于 DRR 粒度结构性限制，与 native BFQ 类调度器自身的量化同源。
（S11/S12 起，大请求场景的自适应部分改为**全组统一 ew_mult 提升**：每轮（全局轮）advance 按
`max ceil(own_max_charge/(512×w))` 同乘所有预算，保证"预算 ≥ 本租户自身最大请求"
的漂移防护的同时**不再压缩权重比例**——pre-S11 的全局 `last_round_max_charge`
下限只抬低于它的键，100:200 + 64K 写流会被压成 1:1.5625；统一提升保持 1:2。）

**需要 1..7 分辨率时的统一缩放配方**（保持比例语义，全组同乘）：

```
-W $((8 * <io.weight>))        # 全组统一 ×8：io.weight 1..10000 -> -W 8..80000
```

- 效果：低端完全保真（io.weight 1:2 → -W 8:16 → 严格 1:2，模型断言 `low_range_x8_recipe_restores_ratio`）。
- 代价：高端预算 ×8（io.weight 10000 → -W 80000 → 40.96MB/round），caveat 2 提前出现。
- 建议按实验的权重组合二选一：全组 ≥ 8 → identity；需要 < 8 → ×8 并把高端权重控制在 ≤ 10000/8 量级。

### 3.2 caveat 2：高端 per-visit 绑定（io.weight ≳ 1000~2000，4K IO）

预算必须在单次 visit 内绑定才有权重分离（S6 硬件教训）。F1 sweep 显示：
- q=512、权重 100..800（预算 51.2KB..409.6KB）：Jain 0.9823 ← 现行常量。
- q=2048（预算 ×4）：top weights pending-limited inside visits，Jain 0.974（轻微退化，非崩溃）。

io.weight 10000 在 q=512 下预算 5.12MB，远超 visit 深度（数十请求）；但 **S10
round-hold**（20261026）让 mid-turn 排空的大预算租户 HOLD 住 round，等待 pending
refill 后继续消耗，因此浅队列饱和 regime 下 100:10000 的比例仍约为 96
（`test_shallow_queue_top_range`，驱动 40000 次派发实测）。保真度按
`min(weight 比, visit 深度比)` 软退化，不会塌回均分（pre-S10 才会）。

**建议**：实验权重比 ≤ 1000:1 时直接 identity；更高比例时接受软退化或加深积压
（提高 fio QD / nr_requests，使 visit 深度 ≥ 预算/平均请求大小）。

## 4. 不要用错策略（类别错误警告）

- **arb_cost / arb_iocost 不是 io.weight**：charge = 代价而非字节，等权重下按
  cost 公平（读写、大小被区别计价）。做 io.weight 同构复现实验时**必须用 arb_wdrr**。
- **arb_slo / arb_iolatency 不是纯 io.weight**：完成延迟反馈会改 boost（有效
  权重 = weight × boost），仅在延迟达标时退化为 io.weight。

## 5. 开销清单（相对 native io.weight 的调度器侧差异）

| 项 | 量级 | 影响 |
|---|---|---|
| 每次 pick 扫描 | O(active tenants)，上限 64 槽 | 派发路径常数开销 |
| 每轮一次 NULL dispatch | 1 次/round（round 边界） | blk-mq 在下一次 completion kick 重试，无 liveness/吞吐损失 |
| S10 idle hold | 最长 20ms（`ARB_ROUND_IDLE_RELEASE_NS`） | 只对 mid-turn 排空租户生效，不影响其他租户派发 |
| map 记账 | 每次 hook 若干次 hash/array 查找 | 与其他 arb_* 一致，无额外隐藏语义开销 |

以上均为实现开销，不改变服务语义（无 cost、无延迟乘子、无隐藏权重缩放）。

## 6. 验证与联动

- 可执行审计：`python3 tests/io/arb_io_weight_model.py`（常量漂移守卫、线性域断言、
  比例极值断言 1250:1/100:1/10:1、4K 实况比例、floor 压缩与 ×8 配方、浅队列
  100:1 存活、无 cost 断言）。
- 本文件改 `ioext_arb_common.h` 的量子/下限前必须先同步
  `tests/io/arb_wdrr_model.py` 与本文件（模型测试第 0 关会拦住）。
