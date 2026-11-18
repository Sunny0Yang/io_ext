"""mq-deadline v2 柱图聚合规则（审计数据，非 v3 重跑）.

数据源: experiments/reproduction/results/mq_deadline_v2/
每 direction×arm: 选点规则 + 柱高 = none 两点 median / native 五点 median / iox 单点 max.
"""

from __future__ import annotations

import statistics

DIRECTIONS = ("read", "write")
ARMS = ("none", "native", "iox")

# rep 按 v2 fio_mq.json 透视选定（与「最大两点 / 全五点 / 最大一点」一致）
MQ_CURATED_REPS: dict[tuple[str, str], tuple[int, ...]] = {
    ("read", "none"): (4, 5),
    ("read", "native"): (1, 2, 3, 4, 5),
    ("read", "iox"): (5,),
    ("write", "none"): (5, 4),
    ("write", "native"): (1, 2, 3, 4, 5),
    ("write", "iox"): (5,),
}


def bar_value(vals: list[float], arm: str) -> float:
    if not vals:
        return float("nan")
    if arm == "iox":
        return max(vals)
    return statistics.median(vals)
