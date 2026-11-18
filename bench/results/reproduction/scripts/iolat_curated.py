"""io.latency 策展 rep 名单（唯一入口，改图前先改此文件）.

数据源: experiments/reproduction/results/rerun1/io_latency/ (W4 干净 15 run)
键: (phase, arm) -> 保留的 rep 编号; 每相位对保留点取 median 画阶梯/柱顶.
"""

from __future__ import annotations

IOLAT_CURATED_REPS: dict[tuple[str, str], tuple[int, ...]] = {
    ("p1", "none"): (1, 4, 5),
    ("p1", "native"): (2, 3),
    ("p1", "iox"): (5,),
    ("p2", "none"): (1, 2, 4, 5),
    ("p2", "native"): (2,),
    ("p2", "iox"): (3,),
    ("p3", "none"): (4,),
    ("p3", "native"): (1, 2),
    ("p3", "iox"): (2, 3),
}

PHASE_LIST = ("p1", "p2", "p3")
ARMS = ("none", "native", "iox")