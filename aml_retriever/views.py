"""AML Retriever — 视图构造与可追溯 provenance（零依赖）。

三类视图：
  - message         : 单条原始消息，1:1，永远独立可检索
  - window          : 会话内按 seq 的确定性滑动窗口，聚合 N 条连续消息
  - session-segment : 按会话边界（消息数上限 / 时间间隙）聚合成段

硬性规则：
  1. 聚合视图只是"指向原始消息的索引"，**绝不替换**原始消息；
  2. 每条聚合视图必须能回指 source_message_ids（provenance）；
  3. 窗口大小与段边界完全确定性、可配置；
  4. 视图 id 全局唯一（含 user/session 指纹），避免跨 user 冲突。

本模块提供两套等价实现：
  - build_* : 全量扫描（参考实现，测试基准）
  - *_starts / segment_boundaries : 供存储层做增量重建，且结果与全量扫描一致
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class ViewRecord:
    view_id: str
    view_type: str
    content: str
    created_at: str
    source_message_ids: list[str] = field(default_factory=list)
    session_id: str | None = None
    start_seq: int = 0
    end_seq: int = 0


def scope_key(user_id: str, session_id: str | None) -> str:
    """(user_id, session_id) 的稳定短指纹，用于生成全局唯一 view_id。"""
    raw = f"{user_id}\x00{session_id or ''}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def window_view_id(user_id: str, session_id: str | None, start: int) -> str:
    return f"w_{scope_key(user_id, session_id)}_{start}"


def segment_view_id(user_id: str, session_id: str | None, index: int) -> str:
    return f"s_{scope_key(user_id, session_id)}_{index}"


def join_content(messages: list[dict]) -> str:
    """聚合视图正文：保留 role 前缀，便于统一回答模型理解上下文。"""
    parts = []
    for m in messages:
        role = (m.get("role") or "").strip()
        text = m.get("content") or ""
        parts.append(f"{role}: {text}" if role else text)
    return "\n".join(parts)


# --------------------------------------------------------------------- window
def window_starts(total: int, size: int, overlap: int) -> list[int]:
    """确定性窗口起点。总是包含覆盖到末尾的最后一个窗口（可能不满）。"""
    size = max(1, int(size))
    step = max(1, size - max(0, int(overlap)))
    if total <= 0:
        return []
    starts = list(range(0, total, step))
    # 去掉完全被上一个窗口覆盖的冗余起点（末尾不满窗时可能出现）
    return [s for s in starts if s == 0 or s < total]


def affected_window_starts(old_total: int, new_total: int, size: int, overlap: int) -> list[int]:
    """增量：仅返回内容会因为新增消息而变化的窗口起点。"""
    size = max(1, int(size))
    starts = window_starts(new_total, size, overlap)
    # 一个窗口 [s, s+size) 只要覆盖到 >= old_total 的位置，其内容就会变化
    return [s for s in starts if s + size > old_total]


def build_windows(
    messages: list[dict], user_id: str, session_id: str | None, size: int, overlap: int
) -> list[ViewRecord]:
    total = len(messages)
    out: list[ViewRecord] = []
    for start in window_starts(total, size, overlap):
        chunk = messages[start : start + max(1, size)]
        if not chunk:
            continue
        out.append(
            ViewRecord(
                view_id=window_view_id(user_id, session_id, start),
                view_type="window",
                content=join_content(chunk),
                created_at=chunk[0].get("created_at") or "",
                source_message_ids=[m["id"] for m in chunk],
                session_id=session_id,
                start_seq=start,
                end_seq=start + len(chunk) - 1,
            )
        )
    return out


# ------------------------------------------------------------ session segment
def segment_boundaries(
    messages: list[dict], max_messages: int, max_gap_seconds: int
) -> list[tuple[int, int]]:
    """从左到右确定性切段，返回 [(start_idx, end_idx_inclusive), ...]。

    切段规则（顺序敏感、append-only 时增量与全量结果一致）：
      - 当前段消息数达到 max_messages -> 新段
      - 与上一条消息的时间间隙 > max_gap_seconds -> 新段
    """
    max_messages = max(1, int(max_messages))
    gap_ms = max(0, int(max_gap_seconds)) * 1000
    bounds: list[tuple[int, int]] = []
    start = 0
    last_ts: int | None = None
    count = 0
    for i, m in enumerate(messages):
        ts = m.get("ts_ms")
        if count > 0:
            gap_break = (
                gap_ms > 0
                and last_ts is not None
                and ts is not None
                and (int(ts) - int(last_ts)) > gap_ms
            )
            if count >= max_messages or gap_break:
                bounds.append((start, i - 1))
                start = i
                count = 0
        count += 1
        if ts is not None:
            last_ts = ts
    if messages:
        bounds.append((start, len(messages) - 1))
    return bounds


def build_segments(
    messages: list[dict],
    user_id: str,
    session_id: str | None,
    max_messages: int,
    max_gap_seconds: int,
) -> list[ViewRecord]:
    out: list[ViewRecord] = []
    for idx, (start, end) in enumerate(
        segment_boundaries(messages, max_messages, max_gap_seconds)
    ):
        chunk = messages[start : end + 1]
        if not chunk:
            continue
        out.append(
            ViewRecord(
                view_id=segment_view_id(user_id, session_id, idx),
                view_type="session-segment",
                content=join_content(chunk),
                created_at=chunk[0].get("created_at") or "",
                source_message_ids=[m["id"] for m in chunk],
                session_id=session_id,
                start_seq=start,
                end_seq=end,
            )
        )
    return out


def build_all_views(
    messages: list[dict],
    user_id: str,
    session_id: str | None,
    *,
    window_size: int,
    window_overlap: int,
    segment_max_messages: int,
    segment_max_gap_seconds: int,
) -> list[ViewRecord]:
    """全量参考实现：一个会话的全部聚合视图。"""
    return build_windows(messages, user_id, session_id, window_size, window_overlap) + build_segments(
        messages, user_id, session_id, segment_max_messages, segment_max_gap_seconds
    )


__all__ = [
    "ViewRecord",
    "scope_key",
    "window_view_id",
    "segment_view_id",
    "join_content",
    "window_starts",
    "affected_window_starts",
    "build_windows",
    "segment_boundaries",
    "build_segments",
    "build_all_views",
]
