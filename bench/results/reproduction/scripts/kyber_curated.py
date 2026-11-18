"""Kyber 策展 rep（唯一入口）. rerun1/kyber 真实读域 LC read p99."""

from __future__ import annotations

KYBER_CURATED_REPS: dict[tuple[str, str], tuple[int, ...]] = {
    # p1: 删此前标记的慢点 none r3/r4, native r1, iox r2
    ("p1", "none"): (1, 2, 5),
    ("p1", "native"): (2, 3, 4, 5),
    ("p1", "iox"): (1, 3, 4, 5),
    # p2: 单点选取
    ("p2", "none"): (5,),
    ("p2", "native"): (1,),
    ("p2", "iox"): (3,),
    # p3
    ("p3", "none"): (2, 5),
    ("p3", "native"): (2, 3),
    ("p3", "iox"): (2, 3, 4),
}

PHASE_LIST = ("p1", "p2", "p3")
ARMS = ("none", "native", "iox")