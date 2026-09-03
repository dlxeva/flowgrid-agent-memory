"""AML Retriever — 运行配置（零依赖）。

配置来源优先级：显式参数 > 环境变量 > JSON 配置文件 > 内置默认值。
所有检索增强都以 flag 形式暴露，便于离线消融（ablation）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

# 检索增强开关。每一项都可独立关闭，用于消融实验。
# 默认值只反映"当前有离线证据支持"的配置，无证据的一律默认关闭。
DEFAULT_FLAGS: dict[str, bool] = {
    "views": True,       # 聚合视图（window / session-segment）参与检索
    "exact": True,       # 精确子串 / token 覆盖率加权
    "datenum": True,     # 数字与日期精确匹配加权
    "entity": True,      # 引用短语 / 实体式 token 加权
    "rerank": True,      # 时间、邻接上下文、provenance、冲突提示软重排
    # 时间意图：查询含「现在 / 最近 / latest」等标记时额外抬高相对新近度权重。
    # 默认关闭。证据见 docs/EVAL.md 附录 B（medium/seed=20260806，2026-08-06 实测）：
    # 它对目标 temporal 类的增益微乎其微（mixed 该类 MRR 0.5392 → 0.5407，+0.0015），
    # 却拉低整体表现（mixed 总 MRR 0.6631 → 0.6561，Recall@20 1.0 → 0.9922；
    # paraphrase 总 MRR 0.5687 → 0.5591）。收益来自「相对新近度」本身，
    # 而非在其上再叠一层意图放大，故保持 disabled。
    "temporal_intent": False,
    # 加权 RRF 默认开启，并配合**低词法权重**（rrf_weight_lexical=0.1）。
    # 证据见 docs/EVAL.md 附录 A（medium/seed=20260806，2026-08-06 实测）：
    # w_lex=0.1 是扫描点中唯一的 Pareto 安全点——三种难度 MRR 均为正增益
    # （+0.0009 ~ +0.0060），且 Recall@20 保持 1.0000、延迟无实质变化。
    # 更大的权重能换来更高 MRR（mixed w=1.0 时 +0.0325），但要付出 Recall@20 代价
    # （mixed 0.8750、paraphrase 0.7708），属于用召回换排序，未作为默认。
    "rrf": True,         # FTS rank 与特征分值两路加权 RRF 融合
    "dedup": True,       # 候选去重（同内容 / 完全被覆盖的聚合视图）
    "use_options": True, # Search 的 options 字段参与候选召回
    "exact_scan": False, # FTS 无命中时的兜底 LIKE 全扫（代价高，默认关闭）
    "vector": False,     # 可选向量检索：环境无可用依赖时恒为 False
    # 覆写检测（supersession）：在候选集内找「话题高度重合但更晚」的消息对，
    # 判定后者覆写前者，抬高后者、轻降前者。与 temporal_intent 的区别是
    # 它不做全局新近度放大，只在**成对冗余**成立时局部生效，因此不会误伤
    # 「问当前状态但答案本身较旧」的查询。默认值由 Phase D 跨 seed 消融决定，
    # v1.1 只启用“覆写 + 显式更新保护”的组合；无保护的覆写仍是不安全对照。
    # medium/mixed 三 seed 下，保守 4/1 权重保持 Recall@20 不退、Recall@100=1，
    # MRR 每个 seed 均提升。官方数据上的影响仍 unknown，见 docs/EVAL.md 附录 E。
    "supersession": True,
    # 显式更新保护：仅允许带通用更新语义（如“更新为 / no longer”）的较新消息
    # 覆写旧消息。它只在 supersession 同时开启时生效；v1.1 将两者组合启用。
    "supersession_update_guard": True,
    # 个性化证据来源：偏好类查询中，轻度抬高用户本人直接陈述，绝不删除
    # assistant 转述或其他原始证据。代理集虽显示正收益，但场景宽度仍不足，默认关闭。
    "preference_role_boost": False,
}


@dataclass
class RetrieverConfig:
    # 存储
    db_path: str = ":memory:"

    # 视图构造（确定性、可配置）
    window_size: int = 3
    window_overlap: int = 1
    segment_max_messages: int = 12
    segment_max_gap_seconds: int = 1800

    # 检索
    top_k_default: int = 10
    top_k_max: int = 100          # 官方正式评测固定 100
    max_candidates: int = 400     # FTS 候选上限，界定单次 Search 的最坏代价
    max_query_tokens: int = 64    # 查询 token 上限，界定 FTS 查询规模
    message_slot_ratio: float = 0.5  # top_k 中保底留给原始消息的比例

    # 加权 RRF 融合参数（仅在 flags["rrf"] 开启时生效）。
    # 权重扫描（docs/EVAL.md 附录 A）显示：w_lexical 越大 MRR 越高、Recall@20 越低。
    # 默认取 0.1 —— 唯一「MRR 有增益且 Recall@20 不退化」的取值；
    # 若下游更看重排序而非 20 条内召回，可上调至 0.25/0.5（已实测，非默认）。
    rrf_k: int = 60
    rrf_weight_feature: float = 1.0
    rrf_weight_lexical: float = 0.1

    # 相对新近度权重。把候选集内时间戳归一到 [0,1] 后加权，
    # 取代原先的绝对年龄项（语料整体偏老时绝对年龄会退化成常数）。
    # intent 档仅在 flags["temporal_intent"] 开启时生效，实测收益为负，故默认不启用。
    recency_weight: float = 8.0
    recency_weight_intent: float = 12.0

    # 覆写检测参数（仅在 flags["supersession"] 开启时生效）。
    # min_overlap 是两条消息「谈的是同一件事」的判定阈值，按 containment
    # （交集 / 较短一方的 token 数，token 取长度≥2 的 n-gram）计算。
    # max_pairs 限制参与两两比较的候选数，界定 O(n^2) 的最坏代价。
    # v1.1 三 seed 参数扫描的 Pareto 安全点：4/1 保持 Recall@20 不退化，
    # 同时稳定提升 MRR；14/4 与 18/6 的增益更大但会在一个 seed 上损伤 Recall@20。
    supersession_weight: float = 4.0
    supersession_penalty: float = 1.0
    supersession_min_overlap: float = 0.5
    supersession_max_pairs: int = 40

    # 偏好类查询中，“role=user + 第一人称偏好陈述”的软加权。
    # 只改变排序，不过滤任何候选，且在 RRF 前进入特征路排序。
    preference_role_weight: float = 14.0

    # 一致性 / 并发
    busy_timeout_ms: int = 10000
    write_retries: int = 5
    pool_size: int = 24  # 读连接池容量（官方 Search 并发默认 32，留出借还余量）

    # HTTP wrapper
    host: str = "127.0.0.1"
    port: int = 8080
    auth_mode: str = "none"       # none | bearer | token | x-api-key
    api_key: str = ""
    add_path: str = "/add"
    search_path: str = "/search"
    health_path: str = "/health"

    flags: dict = field(default_factory=lambda: dict(DEFAULT_FLAGS))

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, path: str | None = None) -> "RetrieverConfig":
        cfg = cls()
        path = path or os.environ.get("AML_CONFIG") or ""
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                cfg = cfg.merge(json.load(fh))

        env_map = {
            "AML_DB_PATH": ("db_path", str),
            "AML_WINDOW_SIZE": ("window_size", int),
            "AML_WINDOW_OVERLAP": ("window_overlap", int),
            "AML_SEGMENT_MAX_MESSAGES": ("segment_max_messages", int),
            "AML_SEGMENT_MAX_GAP_SECONDS": ("segment_max_gap_seconds", int),
            "AML_TOP_K_DEFAULT": ("top_k_default", int),
            "AML_TOP_K_MAX": ("top_k_max", int),
            "AML_MAX_CANDIDATES": ("max_candidates", int),
            "AML_RRF_K": ("rrf_k", int),
            "AML_RRF_W_FEATURE": ("rrf_weight_feature", float),
            "AML_RRF_W_LEXICAL": ("rrf_weight_lexical", float),
            "AML_RECENCY_W": ("recency_weight", float),
            "AML_RECENCY_W_INTENT": ("recency_weight_intent", float),
            "AML_HOST": ("host", str),
            "AML_PORT": ("port", int),
            "AML_AUTH_MODE": ("auth_mode", str),
            "AML_API_KEY": ("api_key", str),
            "AML_ADD_PATH": ("add_path", str),
            "AML_SEARCH_PATH": ("search_path", str),
            "AML_HEALTH_PATH": ("health_path", str),
        }
        for env_key, (attr, caster) in env_map.items():
            raw = os.environ.get(env_key)
            if raw is not None and raw != "":
                try:
                    setattr(cfg, attr, caster(raw))
                except (TypeError, ValueError):
                    pass

        # AML_FLAG_<NAME>=0/1 覆盖单个开关
        for key, value in os.environ.items():
            if key.startswith("AML_FLAG_"):
                name = key[len("AML_FLAG_") :].lower()
                if name in cfg.flags:
                    cfg.flags[name] = value.strip().lower() in ("1", "true", "yes", "on")
        return cfg

    def merge(self, data: dict) -> "RetrieverConfig":
        out = RetrieverConfig(**{**asdict(self)})
        out.flags = dict(self.flags)
        for key, value in (data or {}).items():
            if key == "flags" and isinstance(value, dict):
                for flag_name, flag_value in value.items():
                    if flag_name in out.flags:
                        out.flags[flag_name] = bool(flag_value)
            elif hasattr(out, key):
                setattr(out, key, value)
        return out

    def with_flags(self, **overrides) -> "RetrieverConfig":
        """返回一个只改动 flag 的副本（消融实验用）。"""
        out = RetrieverConfig(**{**asdict(self)})
        out.flags = dict(self.flags)
        for name, value in overrides.items():
            out.flags[name] = bool(value)
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["api_key"] = "***" if self.api_key else ""  # 不外泄密钥
        return data


def vector_backend_available() -> tuple[bool, str]:
    """探测运行环境是否已有可用向量依赖。

    严格只做探测，绝不安装依赖、绝不联网下载模型。
    返回 (available, reason)。
    """
    for module in ("numpy", "sentence_transformers", "faiss"):
        try:
            __import__(module)
        except Exception:
            continue
        else:
            return True, f"{module} importable"
    return False, "no local vector dependency (numpy/sentence_transformers/faiss) importable"


__all__ = ["RetrieverConfig", "DEFAULT_FLAGS", "vector_backend_available"]
