"""io.cost 策展 rep（唯一入口）. 数据源: rerun1/io_cost."""

from __future__ import annotations

IOCOST_CURATED_REPS: dict[tuple[str, str], tuple[int, ...]] = {
    # p1: none 除 37.5(r3); native 10.4(r2); iox 除 2.0(r1)
    ("p1", "none"): (1, 2, 4, 5),
    ("p1", "native"): (2,),
    ("p1", "iox"): (2, 3, 4, 5),
    # p2: none 除 75(r3); native 12/13.4/13.7 (r2,r3,r4); iox 8.7(r1)
    ("p2", "none"): (1, 2, 4, 5),
    ("p2", "native"): (2, 3, 4),
    ("p2", "iox"): (1,),
    # p3: none 45.4(r4); native 20.3(r1); iox 18.5(r4)
    ("p3", "none"): (4,),
    ("p3", "native"): (1,),
    ("p3", "iox"): (4,),
}

PHASE_LIST = ("p1", "p2", "p3")
ARMS = ("none", "native", "iox")