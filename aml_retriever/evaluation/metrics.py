"""评测指标（零依赖实现）。

统一口径
--------
检索结果可能是原始消息，也可能是聚合视图（window / session-segment）。
平台的 Answer 模型看到的是 **结果内容**，因此只要某条结果的
``source_message_ids`` 命中 gold 消息，就算这条 gold 被召回。
所有指标都基于"前 k 条结果的 source_message_ids 并集"计算。
"""
from __future__ import annotations

import math


def hit_sets(results, k: int) -> set[str]:
    """前 k 条结果覆盖到的原始消息 id 并集。"""
    covered: set[str] = set()
    for evidence in results[:k]:
        covered.update(evidence.source_message_ids or [evidence.id])
    return covered


def recall_at_k(results, gold: set[str], k: int) -> float:
    if not gold:
        return float("nan")
    covered = hit_sets(results, k)
    return len(covered & gold) / len(gold)


def reciprocal_rank(results, gold: set[str]) -> float:
    """首个命中任一 gold 消息的结果排名倒数。"""
    if not gold:
        return float("nan")
    for rank, evidence in enumerate(results, start=1):
        source = set(evidence.source_message_ids or [evidence.id])
        if source & gold:
            return 1.0 / rank
    return 0.0


def distractor_leak_at_k(results, distractors: set[str], k: int) -> float:
    """前 k 条结果中命中"已被覆写的旧值"的比例（越低越好）。"""
    if not distractors:
        return float("nan")
    covered = hit_sets(results, k)
    return len(covered & distractors) / len(distractors)


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数，q ∈ [0, 100]。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[int(pos)]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def summarize_latency(values_ms: list[float]) -> dict:
    if not values_ms:
        return {"count": 0}
    return {
        "count": len(values_ms),
        "mean_ms": round(sum(values_ms) / len(values_ms), 3),
        "p50_ms": round(percentile(values_ms, 50), 3),
        "p95_ms": round(percentile(values_ms, 95), 3),
        "p99_ms": round(percentile(values_ms, 99), 3),
        "max_ms": round(max(values_ms), 3),
    }


def mean(values: list[float]) -> float:
    clean = [v for v in values if not math.isnan(v)]
    if not clean:
        return float("nan")
    return sum(clean) / len(clean)


__all__ = [
    "hit_sets",
    "recall_at_k",
    "reciprocal_rank",
    "distractor_leak_at_k",
    "percentile",
    "summarize_latency",
    "mean",
]
