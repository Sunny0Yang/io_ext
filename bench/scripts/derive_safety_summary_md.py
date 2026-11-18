#!/usr/bin/env python3
"""Derive the cross-campaign safety summary for io_ext policy exploration.

Read-only derivation over ALL campaigns in results/iox_policy_exploration/
(via lib.iox_safety.ledger.derive_summary; counts are NEVER hand-edited),
aggregated into results/iox_policy_exploration/SAFETY_SUMMARY.md:

  - per-campaign table: attempts (total/completed/HALT), events, KERNEL
    incidents (60s-window dedup), unknown-confidence events, denominators
    (build/verifier/attach/actually_ran/hot_swaps) and evidenced run seconds;
  - an overall row across campaigns;
  - a boundary section ("能声称 / 不能声称") per safety taxonomy §4, plus
    the retroactive-record provenance notes (kvmhost infra fix-chain, the
    kvmhost pipeline runs' ledger wiring gap, and the held-out stage).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from lib.iox_safety import ledger  # noqa: E402

ROOT = str(REPO / "results/iox_policy_exploration")
OUT_MD = Path(ROOT) / "SAFETY_SUMMARY.md"
OUT_JSON = Path(ROOT) / "SAFETY_SUMMARY.json"

# campaigns listed in execution order of record
CAMPAIGNS = [
    ("arb_feedback_smoke", "arb_feedback (f1 反馈仲裁器) 冒烟"),
    ("hotswitch_gate", "热切换门禁（repl/new hot-switch）"),
    ("dyn_tenant_static_tuning", "dyn-tenant 静态臂调优（含 kvmhost 迁移 + infra 修复链补录）"),
    ("dyn_tenant_transitions", "dyn-tenant 边界转换微基准"),
    ("dyn_tenant_validation", "dyn-tenant 冻结 schedule 验证"),
    ("dyn_tenant_heldout", "dyn-tenant held-out 阶段（主图 + 5 seeds 配对）"),
]

CAMPAIGN_NOTES = {
    "arb_feedback_smoke": (
        "10 attempts (9 aborted/failed during bring-up, 1 completed); "
        "events include the heartbeats/serial-evidence INFRA records."),
    "hotswitch_gate": (
        "FAILED gate per summary.json convention: attach failures on 8/10 "
        "attempts in the decisive slice; 1 KERNEL incident (60s-window dedup), "
        "no panic; this FAILED gate is the direct cause of the iox-only "
        "amendment and the whole-run-resident schedule terminal state."),
    "dyn_tenant_static_tuning": (
        "22 completed tuning attempts of record + 1 RETROACTIVE attempt "
        "`kvmhost_infra_fixchain_retro_20261104` = six INFRA events (each with "
        "evidence paths; no KERNEL impact) recording the kvmhost infra fix "
        "chain: sudo-stdin / sparse disk / disk-letter drift / empty target "
        "device / artifact mixing / silent no-rate configs."),
    "dyn_tenant_transitions": (
        "2 of 4 attempts completed (transition microbench); 8 recorded hot "
        "swaps inside one resident arbiter family."),
    "dyn_tenant_validation": (
        "3 attempts covering the validation matrix bookkeeping; the runs of "
        "record are DYN_valid_f1_arbfb_s101..103 vs DYN_valid_n3_native_bfq_"
        "s101..103 (validation_matrix.json)."),
    "dyn_tenant_heldout": (
        "2 batch attempts (batch1 = 9 arms at held-out seed 20261304; "
        "batch2 = f1/bfq at seeds 20261305..20261308); created "
        "retroactively after batch launch (evidence = per-sequence run logs "
        "copied into evidence/)."),
}


def fmt_int(v) -> str:
    return str(int(v)) if isinstance(v, (int, float)) else "0"


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows, totals = [], {
        "attempts_total": 0, "attempts_completed": 0, "halts": 0,
        "events": 0, "incidents": 0, "unknown": 0,
        "build_passed": 0, "verifier_passed": 0, "attach_succeeded": 0,
        "actually_ran": 0, "hot_swaps": 0, "run_seconds": 0.0}
    details: dict = {}
    for cam, label in CAMPAIGNS:
        s = ledger.derive_summary(ROOT, cam)
        ev = s["events"]
        d = s["denominators"]
        row = {
            "campaign": cam,
            "attempts_total": s["attempts"]["total"],
            "attempts_completed": s["attempts"]["completed"],
            "halts": s["attempts"]["with_halt_flag"],
            "events": ev["total"],
            "kernel_incidents": s["incidents"]["total"],
            "kernel_events": ev["totals_by_type"].get("KERNEL", 0),
            "with_panic": s["incidents"]["with_panic"],
            "unknown": ev["unknown_total"],
            "build_passed": fmt_int(d.get("build_passed", 0)),
            "verifier_passed": fmt_int(d.get("verifier_passed", 0)),
            "attach_succeeded": fmt_int(d.get("attach_succeeded", 0)),
            "actually_ran": fmt_int(d.get("actually_ran", 0)),
            "hot_swaps": fmt_int(d.get("hot_swaps", 0)),
            "run_seconds": round(float(d.get("run_seconds", 0.0)), 1),
        }
        rows.append(row)
        for k in totals:
            if k == "run_seconds":
                totals[k] += row[k]
            elif k == "incidents":
                totals[k] += int(row["kernel_incidents"])
            else:
                totals[k] += int(row[k])
        details[cam] = {"summary_of_record": str(Path(
            "results/iox_policy_exploration") / cam / "summary.json"),
            **row}

    lines = []
    ap = lines.append
    ap("# io_ext 策略安全探索总表（SAFETY_SUMMARY）")
    ap("")
    ap(f"生成：{now}（由 `scripts/derive_safety_summary_md.py` 对全部 campaign 重新运行 "
       "`ledger.derive_summary()` 派生；计数为只读派生物，禁止手改）")
    ap("")
    ap("口径：尝试数 = attempts 目录数（含 manifest）；事件数 = events/*.jsonl 合法 JSON 行数；"
       "incident 数 = 仅对 KERNEL 类按 60s 窗口去重（taxonomy §3，跨尝试不合并）；"
       "unknown = confidence=unknown 的事件数；分母 = manifest/finalize 的 outcome 字段。")
    ap("")
    ap("## 各 campaign 汇总")
    ap("")
    ap("| campaign | 尝试 (总/完成/HALT) | 事件 | KERNEL 事件 | KERNEL incident (panic) | unknown | build✓ | verifier✓ | attach✓ | actually_ran | hot_swaps | run_s (证据) |")
    ap("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ap(f"| `{r['campaign']}` | {r['attempts_total']}/{r['attempts_completed']}/{r['halts']} "
           f"| {r['events']} | {r['kernel_events']} | {r['kernel_incidents']} "
           f"({r['with_panic']}) | {r['unknown']} | {r['build_passed']} | "
           f"{r['verifier_passed']} | {r['attach_succeeded']} | {r['actually_ran']} | "
           f"{r['hot_swaps']} | {r['run_seconds']} |")
    ap(f"| **合计（全部 campaign）** | **{totals['attempts_total']}/"
       f"{totals['attempts_completed']}/{totals['halts']}** | **{totals['events']}** "
       f"| **{sum(int(r['kernel_events']) for r in rows)}** "
       f"| **{totals['incidents']} ({sum(int(r['with_panic']) for r in rows)})** "
       f"| **{totals['unknown']}** | **{totals['build_passed']}** | "
       f"**{totals['verifier_passed']}** "
       f"| **{totals['attach_succeeded']}** | **{totals['actually_ran']}** | "
       f"**{totals['hot_swaps']}** | **{round(totals['run_seconds'], 1)}** |")
    ap("")
    ap("## 各 campaign 背景注记")
    ap("")
    ap("**run_s（run_seconds）口径修正**：`dyn_tenant_heldout` 的 final.json 记账缺陷把 "
       "run_seconds 不可改写地记为 0.0（recorder 的日志时间戳解析缺陷：run log 为 HH:MM:SS "
       "无日期前缀）；纠正以 LIFECYCLE 事件 `run_seconds_correction` 落账：batch1 = "
       "4890.0s（9 序列）+ batch2 = 4348.0s（8 序列）= 9238.0s（证据 = 每序列 executor "
       "run log 的首尾时间戳差；事件含逐序列数值）。其余 campaign 的 run_seconds 为 "
       "final.json 口径原值。")
    for cam, label in CAMPAIGNS:
        ap(f"- **`{cam}`** — {label}：{CAMPAIGN_NOTES[cam]}")
    ap("")
    ap("## 能声称 / 不能声称（taxonomy §4 边界，逐条照录并绑定本总表数字）")
    ap("")
    ap("**能声称**：")
    ap("")
    ap(f"- 部署前拦截口径：全部 {totals['attempts_total']} 个尝试中 {totals['build_passed']} 个"
       f"记录 build 通过、verifier 通过 {totals['verifier_passed']} 次、attach 成功 {totals['attach_succeeded']} 次、"
       f"策略实际运行 {totals['actually_ran']} 次尝试、同一仲裁器族内热切换 {totals['hot_swaps']} 次"
       "（分母即上表，BUILD/VERIFY 拒绝从未进入内核执行）。")
    ap(f"- KERNEL 侧：全部 {totals['events']} 个事件中 KERNEL 事件 "
       f"{sum(int(r['kernel_events']) for r in rows)} 个、去重后 incident "
       f"{totals['incidents']} 个（60s 窗口；with_panic="
       f"{sum(int(r['with_panic']) for r in rows)}）。在每次尝试的串口+心跳证据覆盖范围内，"
       "未观察到 panic——该声称 ONLY 覆盖证据在场的运行秒数（上表 run_s 列）。")
    ap("- VM 隔离期间未破坏宿主（爆炸半径由虚拟化层限制——这不是 io_ext 自身的防护证据）。")
    ap("")
    ap("**不能声称**：")
    ap("")
    ap("- verifier 通过 ≠ 运行安全：不能声称「verifier 拦截了运行期故障」；BUILD/VERIFY 是部署前拦截，不是运行时防护。")
    ap("- VM 隔离保护宿主 ≠ io_ext 框架自身的运行时防护声明。")
    ap(f"- unknown={totals['unknown']}：" +
       ("当前全部事件 confidence=observed，无 unknown 时段。" if totals['unknown'] == 0
        else "存在 unknown 时段；这些时段既不计入「无异常」也不得默默丢弃，任何安全结论必须带"
             "「unknown 时段内未验证」限定语。"))
    ap("- 热切换稳定性：hotswitch_gate 已判 FAILED（attach 失败 8/10）；不能声称 arbiter "
       "repl/new 热切换路径可用——这正是 schedule 采用 iox-only 全程常驻终态的原因。")
    ap("- 「无 panic」只能在证据覆盖范围内声称：串口/心跳缺失的时段必须记 unknown（本总表口径下已单列）。")
    ap("- 事件数不得冒充 incident 数，尝试数不得冒充事件数（所有计数都注明口径）。")
    ap("")
    ap("## 补录（retroactive）记录的 provenance")
    ap("")
    ap("- **kvmhost 基建修复链（6 项 INFRA，severity=warning，recovery=succeeded）**：补录于 "
        "campaign `dyn_tenant_static_tuning`，attempt id "
        "`kvmhost_infra_fixchain_retro_20261104`，manifest 标注 `retroactive: true`，"
        "每条事件的 evidence 字段即相应 log/脚本路径（scripts/run_dyn_tenant_kvmhost.sh 、"
        "DYN_smoke_invalidated_sparse_disk0*、DYN_arm_*_invalidated_artifact_mix* 等）。"
        "补录原因：kvmhost 主流水线当时经由 kvmhost runner 执行（vng 账本规则早于该管线），"
        "运行本身未逐次建 attempt——修复链现在按一次性补录进入账本，不改变任何旧段。")
    ap("- **dyn_tenant_heldout campaign**：两个批次 attempt 均在批启动后补建（manifest 标注 "
        "`retroactive: true` + `note`），每序列 run log 拷贝进 attempts/<id>/evidence/ 作证据；"
        "失败/缺失序列照实保留。")
    ap("- 旧 campaign 的 summary.json / summary.csv 在本次补录后已重新派生（derive_summary 覆盖写）。")
    ap("")
    ap("## 数据口径与局限")
    ap("")
    ap("- `run_seconds` 仅累计 manifest/finalize 的 outcome 字段且有证据的运行秒数；"
       "微基准类 campaign（transitions/validation/heldout）以序列级 run log 为证，逐秒级"
       "时长以 run log 时间戳为准。")
    ap("- 本总表不包含任何性能结论；性能/可行性结论见 "
       "`results/dyn_tenant_kvmhost/`（tuning/validation/heldout 矩阵与 figures/）。")

    OUT_MD.write_text("\n".join(lines) + "\n")
    (Path(ROOT) / "SAFETY_SUMMARY.json").write_text(
        json.dumps({"schema": "iox_safety_summary_md_derivation/1",
                    "derived_at_utc": now, "rows": rows, "totals": totals},
                   indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT_MD} ({len(rows)} campaigns)")


if __name__ == "__main__":
    main()