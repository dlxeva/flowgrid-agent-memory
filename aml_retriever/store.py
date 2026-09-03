"""AML Retriever — P0 词法基线（Lexical Baseline）。

本地、独立、可测试的 Add/Search 最小实现。
仅依赖 Python 标准库 + SQLite FTS5，不引入任何第三方依赖。

API 字段契约（"核对 API 字段"）：
  Add 请求:
    request_id : str    # 幂等键，原样回显；同 (request_id, user_id) 重复提交不会重复落库
    user_id    : str    # 隔离维度，所有读写按 user_id 隔离
    content    : str    # 原始消息证据（原样保留，不可改写）
    view       : str='message'  # 视图类型；本切片仅实现 'message'，
                                 # 为未来 'window'/'session-segment' 预留扩展边界
    message_id : str|None       # 可选外部主键；缺省由本模块生成
    created_at : str|None       # 可选 ISO 时间；缺省取当前时间
    metadata   : dict|None      # 预留，本切片不索引不检索
  Add 响应:
    request_id : str    # 回显
    message_id : str    # 落库主键（幂等时返回首次的 id）
    idempotent : bool   # True 表示命中已有 request_id，未重复写入
    status     : 'ok'
  Search 请求:
    request_id : str|None
    user_id    : str
    query      : str
    top_k      : int=10
    view       : str='message'
  Search 响应:
    request_id : str|None
    total      : int
    results    : list[Evidence]  # 按确定性相关性降序，已截断 top_k
  Evidence:
    id, user_id, view, content(原始证据), created_at, score(确定性分值)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 分词：统一处理中文（单字 unigram + 相邻 bigram）、拉丁词、数字、日期。
# 这样无论查询长短（单字"京"、双字"北京"、数字"12345"、日期"2024-01-02"、
# 精确短语"机器学习"）都能确定性地命中。
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r'[一-鿿㐀-䶿豈-﫿]+')


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    cjk: list[str] = []
    lat: list[str] = []

    def flush_cjk() -> None:
        s = "".join(cjk)
        for ch in s:
            tokens.append(ch)  # unigram -> 支持单字检索
        for i in range(len(s) - 1):
            tokens.append(s[i : i + 2])  # bigram -> 支持短语/子串
        cjk.clear()

    def flush_lat() -> None:
        if lat:
            tokens.append("".join(lat))
            lat.clear()

    for ch in text:
        if ('一' <= ch <= '鿿') or ('㐀' <= ch <= '䶿') or ('豈' <= ch <= '﫿'):
            flush_lat()
            cjk.append(ch)
        elif ch.isalnum():
            lat.append(ch)
        else:
            flush_cjk()
            flush_lat()
    flush_cjk()
    flush_lat()
    return tokens


def _build_match(query: str) -> str | None:
    toks = {t for t in tokenize(query) if t}
    if not toks:
        return None
    parts = ['"%s"' % t.replace('"', '""') for t in toks]
    return " OR ".join(parts)


def _score(content_lower: str, query_lower: str, query_tokens: set[str]) -> int:
    """确定性相关性分值：精确子串命中权重最高，其次 token 命中数。"""
    s = 0
    if query_lower and query_lower in content_lower:
        s += 1000
    present = sum(1 for t in query_tokens if t and t in content_lower)
    s += present * 10
    return s


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class AddResult:
    request_id: str
    message_id: str
    idempotent: bool
    status: str = "ok"


@dataclass
class Evidence:
    id: str
    user_id: str
    view: str
    content: str
    created_at: str
    score: int


@dataclass
class SearchResult:
    request_id: str | None
    total: int
    results: list[Evidence] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        must_create = db_path == ":memory:" or not os.path.exists(db_path)
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row
        if must_create:
            self._init_schema()

    def _init_schema(self) -> None:
        c = self._con
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                view TEXT NOT NULL DEFAULT 'message',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                request_id TEXT,
                added_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                PRIMARY KEY (request_id, user_id)
            )"""
        )
        # FTS5：text 为索引列；mid/user_id 为 UNINDEXED 冗余列，便于回查。
        c.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS fts
               USING fts5(text, mid UNINDEXED, user_id UNINDEXED)"""
        )
        c.commit()

    # -- Add ---------------------------------------------------------------
    def add(
        self,
        *,
        request_id: str,
        user_id: str,
        content: str,
        view: str = "message",
        message_id: str | None = None,
        created_at: str | None = None,
        metadata: dict | None = None,
    ) -> AddResult:
        if not request_id:
            raise ValueError("request_id is required")
        if not user_id:
            raise ValueError("user_id is required")
        if content is None:
            raise ValueError("content is required")

        # 幂等：同 (request_id, user_id) 直接返回首次落库结果
        row = self._con.execute(
            "SELECT message_id FROM requests WHERE request_id=? AND user_id=?",
            (request_id, user_id),
        ).fetchone()
        if row is not None:
            return AddResult(
                request_id=request_id,
                message_id=row["message_id"],
                idempotent=True,
                status="ok",
            )

        mid = message_id or uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        c_at = created_at or now
        indexed = " ".join(tokenize(content))
        self._con.execute(
            "INSERT INTO messages(id, user_id, view, content, created_at, request_id, added_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (mid, user_id, view, content, c_at, request_id, now),
        )
        self._con.execute(
            "INSERT INTO fts(text, mid, user_id) VALUES(?,?,?)",
            (indexed, mid, user_id),
        )
        self._con.execute(
            "INSERT INTO requests(request_id, user_id, message_id) VALUES(?,?,?)",
            (request_id, user_id, mid),
        )
        self._con.commit()
        return AddResult(request_id=request_id, message_id=mid, idempotent=False, status="ok")

    # -- Search ------------------------------------------------------------
    def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int = 10,
        view: str = "message",
        request_id: str | None = None,
    ) -> SearchResult:
        if not user_id:
            raise ValueError("user_id is required")
        mt = _build_match(query)
        if mt is None:
            return SearchResult(request_id=request_id, total=0, results=[])

        mids = [
            r[0]
            for r in self._con.execute("SELECT mid FROM fts WHERE fts MATCH ?", (mt,)).fetchall()
        ]
        if not mids:
            return SearchResult(request_id=request_id, total=0, results=[])

        placeholders = ",".join("?" * len(mids))
        rows = self._con.execute(
            f"SELECT * FROM messages WHERE id IN ({placeholders}) AND user_id=? AND view=? "
            f"ORDER BY id",
            (*mids, user_id, view),
        ).fetchall()

        q_lower = (query or "").lower()
        q_tokens = {t for t in tokenize(query) if t}
        scored = []
        for r in rows:
            content_lower = (r["content"] or "").lower()
            sc = _score(content_lower, q_lower, q_tokens)
            scored.append((sc, len(r["content"] or ""), r["id"], r))
        # 确定性排序：分值降序 -> 内容更短优先 -> id 升序
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))

        results = [
            Evidence(
                id=r["id"],
                user_id=r["user_id"],
                view=r["view"],
                content=r["content"],
                created_at=r["created_at"],
                score=sc,
            )
            for sc, _len, _id, r in scored[: max(0, top_k)]
        ]
        return SearchResult(request_id=request_id, total=len(scored), results=results)

    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ["Store", "AddResult", "SearchResult", "Evidence", "tokenize"]
