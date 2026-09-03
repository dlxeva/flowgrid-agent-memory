"""消融评测执行器。

每个消融档位都会 **重建一次索引**，因为 ``views`` 开关同时影响写入期的
视图构建（retriever.py:360），复用同一个库会让消融结论失真。
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from ..config import RetrieverConfig, DEFAULT_FLAGS, vector_backend_available
from ..retriever import RetrieverDB
from . import metrics as M
from .dataset import Dataset, make_dataset, SCALES

# 官方披露的生产参数：Add 以 20 条消息为一块切分
ADD_CHUNK_SIZE = 20
# 官方正式评测固定 top_k=100
OFFICIAL_TOP_K = 100

_OFF = {name: False for name in DEFAULT_FLAGS}

_L1 = {**_OFF, "use_options": True, "views": True}
_L2 = {**_L1, "exact": True}
_L3 = {**_L2, "datenum": True, "entity": True, "rerank": True}
_L4 = {**_L3, "dedup": True}
_L5 = {**_L4, "rrf": True}                    # 加权 RRF，v1.0 默认 / v1.1 直接基线
_L6 = {**_L5, "temporal_intent": True}        # 对照组：时间意图放大新近度
_L7 = {**_L5, "vector": True}                 # 可选向量路（依赖不可用时自动跳过）
_L8 = {**_L5, "supersession": True}           # 候选组：成对覆写检测
_L9 = {**_L8, "supersession_update_guard": True}  # v1.1 默认：覆写 + 更新保护
_L10 = {**_L9, "preference_role_boost": True}     # 候选：再加用户偏好证据

# L0→L5 是 v1.0 累进主线；v1.1 在 L5 上加入受保护覆写形成 L9。
# L6/L8 是**对照组而非推荐档**：它们分别量化负增益与未受保护的安全代价。
ABLATION_LADDER: list[tuple[str, dict]] = [
    ("L0_lexical_baseline",     {**_OFF, "use_options": True}),
    ("L1_plus_views",           _L1),
    ("L2_plus_exact",           _L2),
    ("L3_plus_context",         _L3),
    ("L4_plus_dedup",           _L4),
    ("L5_plus_weighted_rrf",    _L5),
    ("L6_temporal_intent_ctrl", _L6),
    ("L7_plus_vector",          _L7),
    ("L8_supersession_ctrl",    _L8),
    ("L9_guarded_supersession", _L9),
    ("L10_preference_ctrl",     _L10),
]

# 主线档位（累进），对照组不参与「只增不减」检查
MAINLINE_STAGES = ("L0_lexical_baseline", "L1_plus_views", "L2_plus_exact",
                   "L3_plus_context", "L4_plus_dedup", "L5_plus_weighted_rrf",
                   "L9_guarded_supersession")
# 生产默认配置对应的档位名，供报告标注
PRODUCTION_STAGE = "L9_guarded_supersession"
# v1.1 生产档的直接对照组：原 v1.0 默认 L5。L6/L8 继续分别保留为
# 时间意图放大负对照与无保护覆写安全对照，但不再承担“生产基线”角色。
CONTROL_STAGE = "L5_plus_weighted_rrf"


@dataclass
class StageResult:
    stage: str
    flags: dict
    skipped: bool = False
    skip_reason: str = ""
    index: dict = field(default_factory=dict)
    overall: dict = field(default_factory=dict)
    by_kind: dict = field(default_factory=dict)
    by_difficulty: dict = field(default_factory=dict)
    # kind × difficulty 交叉表，键形如 "temporal|paraphrase"。
    # 短板只在交叉维度上可见：temporal 整体尚可，但 temporal×paraphrase 可能极低。
    by_kind_difficulty: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "flags": self.flags,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "index": self.index,
            "overall": self.overall,
            "by_kind": self.by_kind,
            "by_difficulty": self.by_difficulty,
            "by_kind_difficulty": self.by_kind_difficulty,
            "latency": self.latency,
        }


def _round(value: float, digits: int = 4):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def build_index(db: RetrieverDB, dataset: Dataset) -> tuple[dict, dict]:
    """按官方 20 条/块的切分策略灌库，返回 (索引统计, gold 键 → 引擎消息 id)。"""
    id_map: dict[str, str] = {}
    started = time.perf_counter()
    add_calls = 0
    for sess in dataset.sessions:
        messages = sess["messages"]
        for offset in range(0, len(messages), ADD_CHUNK_SIZE):
            chunk = messages[offset:offset + ADD_CHUNK_SIZE]
            result = db.add(
                request_id=f"{sess['session_id']}-c{offset // ADD_CHUNK_SIZE}",
                user_id=sess["user_id"],
                session_id=sess["session_id"],
                messages=chunk,
            )
            add_calls += 1
            for local_idx, engine_id in enumerate(result.message_ids):
                id_map[f"{sess['session_id']}#{offset + local_idx}"] = engine_id
    elapsed = time.perf_counter() - started
    # db.stats() 里的 db_path 是本机临时目录（跑完即删），落进产物只会泄漏本机路径
    # 且永远无法复现，对读者零价值 —— 只保留行数统计。
    stats = {k: v for k, v in db.stats().items() if k != "db_path"}
    index = {
        "add_calls": add_calls,
        "messages": dataset.message_count,
        "elapsed_s": round(elapsed, 3),
        "messages_per_s": round(dataset.message_count / elapsed, 1) if elapsed > 0 else None,
        "rows": stats,
    }
    return index, id_map


def run_stage(stage: str, flags: dict, dataset: Dataset, *, workdir: str,
              top_k: int = OFFICIAL_TOP_K) -> StageResult:
    """跑完一个消融档位：重建索引 → 逐条查询 → 汇总指标。"""
    if flags.get("vector"):
        available, reason = vector_backend_available()
        if not available:
            return StageResult(stage=stage, flags=dict(flags), skipped=True,
                               skip_reason=f"vector backend unavailable: {reason}")

    db_path = os.path.join(workdir, f"{stage}.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass

    config = RetrieverConfig(db_path=db_path, top_k_default=top_k, top_k_max=max(top_k, 100))
    config.flags = dict(flags)
    db = RetrieverDB(config)
    try:
        index, id_map = build_index(db, dataset)

        latencies: list[float] = []
        rows: list[dict] = []
        for query in dataset.queries:
            gold = {id_map[g] for g in query.gold if g in id_map}
            distractors = {id_map[d] for d in query.distractors if d in id_map}
            started = time.perf_counter()
            result = db.search(user_id=query.user_id, query=query.text, top_k=top_k)
            latencies.append((time.perf_counter() - started) * 1000.0)
            hits = result.results
            rows.append({
                "kind": query.kind,
                "difficulty": query.difficulty,
                "recall@20": M.recall_at_k(hits, gold, 20),
                "recall@100": M.recall_at_k(hits, gold, 100),
                "mrr": M.reciprocal_rank(hits, gold),
                "leak@10": M.distractor_leak_at_k(hits, distractors, 10),
                "returned": len(hits),
            })

        def agg(subset: list[dict]) -> dict:
            if not subset:
                return {}
            return {
                "queries": len(subset),
                "recall@20": _round(M.mean([r["recall@20"] for r in subset])),
                "recall@100": _round(M.mean([r["recall@100"] for r in subset])),
                "mrr": _round(M.mean([r["mrr"] for r in subset])),
                "distractor_leak@10": _round(M.mean([r["leak@10"] for r in subset])),
                "avg_returned": _round(M.mean([float(r["returned"]) for r in subset]), 2),
            }

        scored = [r for r in rows if not math.isnan(r["recall@100"])]
        by_kind = {}
        for kind in sorted({r["kind"] for r in rows}):
            subset = [r for r in rows if r["kind"] == kind and not math.isnan(r["recall@100"])]
            by_kind[kind] = agg(subset) if subset else {
                "queries": sum(1 for r in rows if r["kind"] == kind),
                "note": "gold 为空，不计入 Recall/MRR",
                "avg_returned": _round(M.mean(
                    [float(r["returned"]) for r in rows if r["kind"] == kind]), 2),
            }
        by_difficulty = {}
        for level in sorted({r["difficulty"] for r in rows}):
            subset = [r for r in scored if r["difficulty"] == level]
            if subset:
                by_difficulty[level] = agg(subset)

        by_kind_difficulty = {}
        for kind in sorted({r["kind"] for r in rows}):
            for level in sorted({r["difficulty"] for r in rows}):
                subset = [r for r in scored
                          if r["kind"] == kind and r["difficulty"] == level]
                if subset:
                    by_kind_difficulty[f"{kind}|{level}"] = agg(subset)

        return StageResult(
            stage=stage,
            flags=dict(flags),
            index=index,
            overall=agg(scored),
            by_kind=by_kind,
            by_difficulty=by_difficulty,
            by_kind_difficulty=by_kind_difficulty,
            latency=M.summarize_latency(latencies),
        )
    finally:
        db.close()


def run_ladder(dataset: Dataset, *, workdir: str | None = None,
               top_k: int = OFFICIAL_TOP_K,
               ladder: list[tuple[str, dict]] | None = None,
               on_stage=None) -> list[StageResult]:
    ladder = ladder or ABLATION_LADDER
    owned = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="aml-eval-")
    os.makedirs(workdir, exist_ok=True)
    results: list[StageResult] = []
    try:
        for stage, flags in ladder:
            result = run_stage(stage, flags, dataset, workdir=workdir, top_k=top_k)
            results.append(result)
            if on_stage:
                on_stage(result)
    finally:
        if owned:
            for name in os.listdir(workdir):
                try:
                    os.remove(os.path.join(workdir, name))
                except OSError:
                    pass
            try:
                os.rmdir(workdir)
            except OSError:
                pass
    return results


# ---------------------------------------------------------------------------
# 多 seed 聚合（Phase D）：同一 (scale, difficulty) 跑多个 seed，
# 报告**每个 seed** 的完整结果 + **跨 seed 的 mean/min/max** 聚合。
# 目的：确认指标在合成数据随机种子间稳定，而非某个 seed 上的偶然。
# 不做参数搜索、不猜测官方语义、不依赖任何第三方库。
# ---------------------------------------------------------------------------

# 参与跨 seed 聚合的标量指标
_OVERALL_KEYS = ("recall@20", "recall@100", "mrr", "distractor_leak@10", "avg_returned")
_LATENCY_KEYS = ("p50_ms", "p95_ms", "p99_ms", "max_ms")
_CROSS_KEYS = ("recall@20", "recall@100", "mrr", "distractor_leak@10")


def _collect(values: list[float]) -> dict | None:
    """对一组标量求 mean/min/max；空则返回 None。"""
    vals = [v for v in values
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    return {
        "mean": _round(sum(vals) / len(vals)),
        "min": _round(min(vals)),
        "max": _round(max(vals)),
        "n": len(vals),
    }


def aggregate_across_seeds(per_seed: list[list[StageResult]]) -> list[dict]:
    """把多个 seed 的 ``run_ladder`` 结果按档位对齐后做跨 seed 聚合。

    ``per_seed[i]`` 是 seed i 的 ``list[StageResult]``，须与 ``ABLATION_LADDER``
    同序（``run_ladder`` 保证）。返回每个档位一个聚合 dict：
    ``overall`` / ``latency`` 为各指标的 mean/min/max，``by_kind_difficulty``
    为 kind×difficulty 交叉表的跨 seed 聚合（**短板只在交叉维度可见**）。
    """
    if not per_seed:
        return []
    n_stages = len(per_seed[0])
    out: list[dict] = []
    for i in range(n_stages):
        stage = per_seed[0][i].stage
        flags = dict(per_seed[0][i].flags)
        skipped = any(ps[i].skipped for ps in per_seed)

        overall_agg = {k: _collect([(ps[i].overall or {}).get(k) for ps in per_seed])
                       for k in _OVERALL_KEYS}
        lat_agg = {k: _collect([(ps[i].latency or {}).get(k) for ps in per_seed])
                   for k in _LATENCY_KEYS}

        cross_keys = sorted({k for ps in per_seed for k in (ps[i].by_kind_difficulty or {})})
        cross_agg = {}
        for ck in cross_keys:
            cell = {}
            for mk in _CROSS_KEYS:
                vals = [(ps[i].by_kind_difficulty or {}).get(ck, {}).get(mk) for ps in per_seed]
                cell[mk] = _collect(vals)
            cross_agg[ck] = cell

        out.append({
            "stage": stage,
            "flags": flags,
            "skipped": skipped,
            "overall": overall_agg,
            "latency": lat_agg,
            "by_kind_difficulty": cross_agg,
        })
    return out


def run_ladder_seeds(seeds: list[int], *, scale: str, difficulty: str,
                     suite: str = "classic",
                     top_k: int = OFFICIAL_TOP_K,
                     ladder: list[tuple[str, dict]] | None = None,
                     on_stage=None) -> dict:
    """对多个 seed 各跑一遍完整消融梯度，返回逐 seed 结果与跨 seed 聚合。

    每个 seed 独立重建索引（与单 seed 行为一致），临时库跑完即清。
    """
    ladder = ladder or ABLATION_LADDER
    workdir = tempfile.mkdtemp(prefix="aml-eval-multi-")
    os.makedirs(workdir, exist_ok=True)
    per_seed: list[list[StageResult]] = []
    try:
        for seed in seeds:
            ds = make_dataset(seed=seed, scale=scale, difficulty=difficulty, suite=suite)
            results = run_ladder(ds, workdir=workdir, top_k=top_k, ladder=ladder,
                                 on_stage=on_stage)
            per_seed.append(results)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "seeds": list(seeds),
        "scale": scale,
        "difficulty": difficulty,
        "suite": suite,
        "top_k": top_k,
        "per_seed": per_seed,
        "aggregate": aggregate_across_seeds(per_seed),
    }


__all__ = ["ABLATION_LADDER", "CONTROL_STAGE", "MAINLINE_STAGES", "PRODUCTION_STAGE", "StageResult", "build_index",
           "run_stage", "run_ladder", "aggregate_across_seeds", "run_ladder_seeds",
           "ADD_CHUNK_SIZE", "OFFICIAL_TOP_K"]
