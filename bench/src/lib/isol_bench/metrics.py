"""Fairness & proportional-slowdown metrics — ported from isol-bench.

Source: ``isol-bench/util/util_sysfs/bench.py`` (IISWC 2025).

Semantics preserved exactly:
- ``proportional_slowdown(l, isol)`` returns per-tenant ``achieved / isolated``
  where ``isolated`` is the single-tenant bandwidth under the same knob.
- ``jains_fairness_index_weighted(l, w)`` computes the weighted Jain index:
  ``lw_i = l_i / w_i`` then ``(Σ lw)² / (n · Σ lw²)``.
  Here "weighted" means **dividing achieved BW by the intended weight**,
  not the bandwidth-weighted Jain's. ``w`` is the intended share
  (e.g. ``1`` for unweighted, ``range(1, n+1)`` for weighted experiments).
- ``proportional_slowdown_jains(l, w, isol)`` composes the two:
  proportional slowdown → weighted Jain.

The paper's two D2 metrics (see ``2-fairness/plot.py``):
- "PS"  = ``proportional_slowdown_jains(vsag, weights, rates)``
- "LOAD" = ``jains_fairness_index_weighted(vsag, weights)``  (no isolation normalization)

Aggregation helper ``aggregate_per_tenant`` handles the case where fio
reports more jobs than tenants (per-job vs per-group reporting):
``vsag[int(i // jump)] += vs[i]`` with ``jump = len(vs) / len(weights)``.
"""

from __future__ import annotations

from typing import List, Sequence


__all__ = [
    "proportional_slowdown",
    "jains_fairness_index",
    "jains_fairness_index_weighted",
    "proportional_slowdown_jains",
    "aggregate_per_tenant",
]


def proportional_slowdown(achieved: Sequence[float], isolated: Sequence[float]) -> List[float]:
    """Per-tenant achieved / isolated ratio (proportional slowdown).

    Args:
        achieved: per-tenant observed bandwidth (or IOPS).
        isolated: per-tenant single-tenant reference bandwidth under the same knob.

    Returns:
        list[float] with ``achieved_i / isolated_i``.

    Raises:
        ValueError: if lengths differ or any isolated value is zero.
    """
    if len(achieved) != len(isolated):
        raise ValueError(
            f"proportional_slowdown: length mismatch achieved={len(achieved)} isolated={len(isolated)}"
        )
    out: List[float] = []
    for ll, lisol in zip(achieved, isolated):
        if lisol == 0:
            raise ValueError("proportional_slowdown: isolated value is zero")
        out.append(float(ll) / float(lisol))
    return out


def jains_fairness_index(values: Sequence[float]) -> float:
    """Unweighted Jain's fairness index: ``(Σ x)² / (n · Σ x²)``.

    Source: ``util/plot_utils/plotter.py``.
    """
    n = len(values)
    if n == 0:
        raise ValueError("jains_fairness_index: empty input")
    s = sum(float(v) for v in values)
    ssq = sum(float(v) ** 2 for v in values)
    if ssq == 0:
        return 0.0
    return (s * s) / (n * ssq)


def jains_fairness_index_weighted(achieved: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted Jain's fairness index (isol-bench definition).

    ``lw_i = achieved_i / weight_i`` then unweighted Jain's on ``lw``.

    Args:
        achieved: per-tenant observed bandwidth.
        weights:  per-tenant intended share (same units / scale).

    Returns:
        float in [0, 1].

    Raises:
        ValueError: on length mismatch or zero weight.
    """
    if len(achieved) != len(weights):
        raise ValueError(
            f"jains_fairness_index_weighted: length mismatch achieved={len(achieved)} weights={len(weights)}"
        )
    lw: List[float] = []
    for ll, ww in zip(achieved, weights):
        if ww == 0:
            raise ValueError("jains_fairness_index_weighted: zero weight")
        lw.append(float(ll) / float(ww))
    return jains_fairness_index(lw)


def proportional_slowdown_jains(
    achieved: Sequence[float], weights: Sequence[float], isolated: Sequence[float]
) -> float:
    """Proportional-slowdown-weighted Jain's fairness index (the "PS" metric).

    Composition:
    1. ``ps = proportional_slowdown(achieved, isolated)``
    2. ``return jains_fairness_index_weighted(ps, weights)``
    """
    ps = proportional_slowdown(achieved, isolated)
    return jains_fairness_index_weighted(ps, weights)


def aggregate_per_tenant(per_job_values: Sequence[float], num_tenants: int) -> List[float]:
    """Aggregate per-job fio metrics into per-tenant values.

    When fio reports ``len(per_job_values)`` jobs but there are only
    ``num_tenants`` tenants (e.g. ``numjobs > 1`` per tenant without
    ``group_reporting``), sum jobs in groups of ``jump``:

    ``vsag[int(i // jump)] += vs[i]`` where ``jump = len(vs) / num_tenants``.

    If ``len(per_job_values) == num_tenants`` the input is returned as-is
    (float-cast).
    """
    nvs = len(per_job_values)
    if nvs == num_tenants:
        return [float(v) for v in per_job_values]
    if nvs == 0:
        return []
    if nvs % num_tenants != 0:
        raise ValueError(
            f"aggregate_per_tenant: {nvs} jobs not divisible by {num_tenants} tenants"
        )
    jump = nvs // num_tenants
    out = [0.0] * num_tenants
    for i, v in enumerate(per_job_values):
        out[i // jump] += float(v)
    return out