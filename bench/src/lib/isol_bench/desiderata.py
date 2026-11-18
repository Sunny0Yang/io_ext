"""Desiderata identifiers for isol-bench (IISWC 2025).

Each desideratum maps to one upstream experiment directory:

| ID           | Name                          | Upstream dir      |
|--------------|-------------------------------|-------------------|
| D1_OVERHEAD  | Overhead & scalability        | 1-overhead/       |
| D2_FAIRNESS  | Proportional fairness         | 2-fairness/       |
| D3_PRIO_UTIL | Priority vs utilization       | 3-prio-vs-util/   |
| D4_BURST     | Priority burst support        | 4-bursts/         |
"""

D1_OVERHEAD = "D1_overhead"
D2_FAIRNESS = "D2_fairness"
D3_PRIO_UTIL = "D3_prio_vs_util"
D4_BURST = "D4_burst"

ALL_DESIDERATA = (D1_OVERHEAD, D2_FAIRNESS, D3_PRIO_UTIL, D4_BURST)