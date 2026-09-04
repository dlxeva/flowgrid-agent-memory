"""AML Retriever — 多视图混合证据检索引擎（零依赖、可消融、线程安全）。

设计要点
--------
存储：SQLite + FTS5。原始消息**全量保存且永远独立可检索**；聚合视图只作为
额外证据通道，带 source_message_ids 回指，绝不替换原始消息。

一致性：
  - WAL + busy_timeout + 进程内写锁 + 有限重试，支持 Add 高并发写。
  - Add 在返回前完成 commit，**同步写后立即可 Search**（官方硬性要求）。
  - 每个线程持有独立 sqlite3 连接（sqlite3 连接非线程安全）。

检索（每条增强都是可独立关闭的 flag，用于离线消融）：
  lexical(基线) / views / exact / datenum / entity / rerank / rrf / dedup / vector(可选)

隔离：所有读写都以 user_id 为范围；session_id 只用于组织记忆，不作为检索筛选条件。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import features
from . import compiler as extracted
from . import extraction
from . import governance as governed
from . import migrations as schema_migrations
from .config import RetrieverConfig
from .views import (
    affected_window_starts,
    join_content,
    scope_key,
    segment_view_id,
    window_view_id,
)

# ------------------------------------------------------------------ 打分权重
W_LEXICAL = 1.0        # FTS5 bm25 归一化后的词法基线权重
W_COVERAGE = 40.0      # 查询 token 覆盖率
W_EXACT_FULL = 120.0   # 整句精确子串命中
W_PHRASE = 60.0        # 引号精确短语命中
W_DATE = 45.0          # 日期精确命中
W_NUMBER = 25.0        # 数字精确命中
W_ENTITY = 12.0        # 实体式 token 命中（按命中数封顶）
W_VIEW_CONTEXT = 4.0   # 聚合视图的邻接上下文加成
W_ADJACENT = 6.0       # 候选在会话中相邻（证据簇）
# 新近度用「候选集内相对位置」而非绝对年龄：语料整体偏老时，
# 绝对年龄项会退化成常数，完全失去区分力（离线证据见 docs/EVAL.md）。
W_RECENCY = 8.0        # 相对新近度基础权重（同分时更偏向新证据）
W_RECENCY_INTENT = 55.0  # 查询含时间意图时的相对新近度权重
W_CONFLICT_LATEST = 10.0  # 疑似状态冲突时，更偏向较新的那条
RRF_K = 60

# Writable-open compatibility inspection and the first additive schema
# migration must be one process-local critical section.  Otherwise a second
# instance can inspect the exact, legal midpoint after another connection has
# created governance tables but before it has written their version rows, and
# incorrectly classify that in-flight migration as a corrupt database.  The
# lock does not make an incompatible database acceptable; every opener still
# runs the same fail-closed preflight once the preceding initialization has
# completed.
_SCHEMA_INITIALIZATION_LOCK = threading.RLock()


@contextmanager
def _cross_process_schema_initialization_lock(db_path: str):
    """Serialize writable-open bootstrap for one file database on this host.

    The schema bootstrap itself is one SQLite transaction.  This host-local
    advisory lock additionally keeps the read-only compatibility preflight and
    that first transaction adjacent across cooperating processes, so a second
    opener never mistakes an in-flight legal migration for corruption.  The
    lock name is a SHA-256 of the absolute database path, so neither paths nor
    user data are disclosed in the shared temporary directory.  SQLite remains
    the authority for normal reads/writes after initialization.
    """

    if db_path == ":memory:":
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback is documented
        # The process-local lock still protects supported single-process use.
        # POSIX hosts (macOS/Linux, including the product container) take the
        # cross-process path exercised by the acceptance suite.
        yield
        return

    resolved = str(Path(db_path).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    lock_root = Path(tempfile.gettempdir()) / f"flowgrid-memory-init-{uid}"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    root_stat = lock_root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or (hasattr(os, "getuid") and root_stat.st_uid != os.getuid())
        or root_stat.st_mode & 0o077
    ):
        raise governed.GovernanceError("schema initialization lock is unavailable")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_root / f"{digest}.lock"), flags, 0o600)
    try:
        lock_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or (hasattr(os, "getuid") and lock_stat.st_uid != os.getuid())
            or lock_stat.st_mode & 0o077
        ):
            raise governed.GovernanceError("schema initialization lock is unavailable")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass
class AddResult:
    request_id: str
    user_id: str
    session_id: str | None
    message_ids: list[str] = field(default_factory=list)
    idempotent: bool = False
    status: str = "ok"


@dataclass
class Evidence:
    id: str
    user_id: str
    view: str
    content: str
    created_at: str
    score: float
    source_message_ids: list[str] = field(default_factory=list)
    provenance: str = ""
    evidence_flags: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    request_id: str | None
    total: int
    results: list[Evidence] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_ms(ts_ms) -> str | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _msg_id(user_id: str, session_id: str | None, seq: int) -> str:
    raw = f"{user_id}\x00{session_id or ''}\x00{seq}".encode("utf-8")
    return "m_" + hashlib.sha1(raw).hexdigest()[:16]


class RetrieverDB:
    """存储 + 检索引擎。线程安全：每线程独立连接，写操作串行化。"""

    def __init__(self, config: RetrieverConfig | None = None, db_path: str | None = None):
        self.config = config or RetrieverConfig()
        if db_path is not None:
            self.config.db_path = db_path
        self.db_path = self.config.db_path
        self.flags = dict(self.config.flags)

        with _SCHEMA_INITIALIZATION_LOCK, _cross_process_schema_initialization_lock(
            self.db_path
        ):
            # Opening RetrieverDB is explicitly writable and may
            # create/migrate a database.  Reject future or corrupt governance
            # metadata before any writable SQLite connection can mutate it.
            # Read-only product doctor checks use migrations.inspect_schema
            # directly instead.
            schema_migrations.assert_writable_open_compatible(self.db_path)

            self._write_lock = threading.RLock()
            # 有界连接池：ThreadingHTTPServer 每请求一个线程，thread-local 连接会持续泄漏 fd，
            # 因此改为借还式连接池，归还时超出容量的连接直接关闭。
            self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=max(1, self.config.pool_size))
            # 内存库必须共享同一连接，否则各线程看到不同的空库；共享连接需串行访问。
            self._mem_lock = threading.RLock()
            self._shared_mem_con: sqlite3.Connection | None = None
            if self.db_path == ":memory:":
                self._shared_mem_con = self._new_connection()

            self._init_schema()

    # ------------------------------------------------------------- connection
    def _new_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self.config.busy_timeout_ms / 1000.0,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            if self.db_path != ":memory:":
                con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"PRAGMA busy_timeout={int(self.config.busy_timeout_ms)}")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.Error:
            pass
        return con

    @contextmanager
    def connection(self):
        """借出一个连接，用完归还。内存库退化为串行访问共享连接。"""
        if self._shared_mem_con is not None:
            with self._mem_lock:
                yield self._shared_mem_con
            return
        try:
            con = self._pool.get_nowait()
        except queue.Empty:
            con = self._new_connection()
        try:
            yield con
        finally:
            try:
                self._pool.put_nowait(con)
            except queue.Full:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _write(self, fn, *args, _immediate: bool = False, **kwargs):
        """串行化写入 + 有限重试（应对 SQLITE_BUSY）。"""
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.write_retries)):
            try:
                with self._write_lock, self.connection() as con:
                    try:
                        if _immediate:
                            # Serialize the read-check-write confirmation path
                            # across independent RetrieverDB instances/processes.
                            con.execute("BEGIN IMMEDIATE")
                        result = fn(con, *args, **kwargs)
                        con.commit()
                        return result
                    except Exception:
                        con.rollback()
                        raise
            except sqlite3.OperationalError as exc:  # database is locked / busy
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.02 * (2**attempt))
        raise last_error if last_error else RuntimeError("write failed")

    # ----------------------------------------------------------------- schema
    def _init_schema(self) -> None:
        def _do(con: sqlite3.Connection):
            # Use individual execute calls inside the surrounding
            # BEGIN IMMEDIATE transaction.  sqlite3.Connection.executescript()
            # implicitly commits first, which could leave a half-installed
            # product schema if a later component failed or the process died.
            statements = (
                """CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    seq INTEGER NOT NULL,
                    role TEXT,
                    content TEXT NOT NULL,
                    ts_ms INTEGER,
                    created_at TEXT NOT NULL,
                    request_id TEXT,
                    added_at TEXT NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_msg_scope ON messages(user_id, session_id, seq)",
                "CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id)",
                """CREATE TABLE IF NOT EXISTS views(
                    view_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    view_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    start_seq INTEGER NOT NULL,
                    end_seq INTEGER NOT NULL,
                    content_hash TEXT NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_views_user ON views(user_id)",
                """CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    message_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, user_id)
                )""",
                """CREATE TABLE IF NOT EXISTS sessions(
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    last_ts_ms INTEGER,
                    seg_index INTEGER NOT NULL DEFAULT 0,
                    seg_start INTEGER NOT NULL DEFAULT 0,
                    seg_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, session_id)
                )""",
                """CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                    text,
                    doc_id UNINDEXED,
                    user_id UNINDEXED,
                    doc_type UNINDEXED
                )""",
            )
            for statement in statements:
                con.execute(statement)
            # Additive migration: the legacy AML tables remain untouched.
            # Existing messages are mapped to immutable RawEvent locators;
            # message bodies stay in the legacy source table (no copy).
            governed.install_schema(con)
            governed.run_migrations(con)
            extracted.install_schema(con)

        self._write(_do, _immediate=True)

    # -------------------------------------------------------------------- Add
    def add(
        self,
        *,
        request_id: str,
        user_id: str,
        messages: list[dict],
        session_id: str | None = None,
        trusted_scope: dict[str, str] | None = None,
        exact_replay: bool = False,
    ) -> AddResult:
        """写入一批原始消息，同步建索引与聚合视图。返回后立即可 Search。"""
        if not request_id:
            raise ValueError("request_id is required")
        if not user_id:
            raise ValueError("user_id is required")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        normalized: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("each message must be an object")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("each message requires a non-empty content string")
            ts = item.get("timestamp", item.get("ts_ms"))
            if ts is not None:
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    raise ValueError("timestamp must be an integer (unix milliseconds)")
            normalized.append({"role": item.get("role") or "", "content": content, "ts_ms": ts})

        return self._write(
            self._add_locked,
            request_id,
            user_id,
            session_id,
            normalized,
            dict(trusted_scope or {}),
            bool(exact_replay),
            _immediate=True,
        )

    def _add_locked(
        self,
        con,
        request_id,
        user_id,
        session_id,
        normalized,
        trusted_scope,
        exact_replay,
    ) -> AddResult:
        event_scope = governed.raw_event_scope(
            user_id=user_id,
            session_id=session_id,
            trusted_scope=trusted_scope,
        )
        row = con.execute(
            "SELECT session_id,message_ids FROM requests WHERE request_id=? AND user_id=?",
            (request_id, user_id),
        ).fetchone()
        if row is not None:
            if not exact_replay:
                # Preserve the frozen AML Add compatibility contract: its
                # undocumented replay policy is first-write-wins.  Governed
                # product callers opt into the stronger exact-replay contract
                # through FlowGridMemory instead of changing this adapter.
                return AddResult(
                    request_id=request_id,
                    user_id=user_id,
                    session_id=session_id,
                    message_ids=json.loads(row["message_ids"]),
                    idempotent=True,
                )
            # Exact replay is the product idempotency contract.  A request key
            # cannot be rebound to another session, payload, role/timestamp, or
            # trusted scope.  Validate against immutable stored evidence rather
            # than an in-memory digest so the contract survives restarts.
            try:
                stored_ids = json.loads(row["message_ids"])
            except (TypeError, ValueError):
                stored_ids = None
            replay_matches = (
                row["session_id"] == session_id
                and isinstance(stored_ids, list)
                and all(isinstance(item, str) and item for item in stored_ids)
                and len(stored_ids) == len(set(stored_ids))
                and len(stored_ids) == len(normalized)
            )
            if replay_matches:
                prior_seq = None
                for message_id, expected in zip(stored_ids, normalized):
                    stored = con.execute(
                        "SELECT id,user_id,session_id,seq,role,content,ts_ms,created_at,"
                        "request_id,added_at FROM messages WHERE id=?",
                        (message_id,),
                    ).fetchone()
                    raw = con.execute(
                        "SELECT id,user_id,event_type,observed_at,recorded_at,authority,"
                        "scope_json,source_locator,source_message_id FROM raw_events "
                        "WHERE source_message_id=?",
                        (message_id,),
                    ).fetchone()
                    try:
                        stored_scope = json.loads(raw["scope_json"]) if raw is not None else None
                    except (TypeError, ValueError):
                        stored_scope = None
                    if (
                        stored is None
                        or raw is None
                        or stored["id"] != message_id
                        or stored["user_id"] != user_id
                        or stored["session_id"] != session_id
                        or stored["request_id"] != request_id
                        or _msg_id(user_id, session_id, stored["seq"]) != message_id
                        or (prior_seq is not None and stored["seq"] != prior_seq + 1)
                        or stored["role"] != expected["role"]
                        or stored["content"] != expected["content"]
                        or stored["ts_ms"] != expected["ts_ms"]
                        or raw["id"] != governed.raw_event_id_for_message(message_id)
                        or raw["user_id"] != user_id
                        or raw["event_type"] != "message"
                        or raw["observed_at"] != stored["created_at"]
                        or raw["recorded_at"] != stored["added_at"]
                        or raw["authority"] != governed.authority_for_role(expected["role"])
                        or raw["source_locator"] != f"messages:{message_id}"
                        or raw["source_message_id"] != message_id
                        or stored_scope != event_scope
                    ):
                        replay_matches = False
                        break
                    prior_seq = stored["seq"]
            if not replay_matches:
                raise governed.GovernanceConflict(
                    "idempotency key is already bound to a different request"
                )
            return AddResult(
                request_id=request_id,
                user_id=user_id,
                session_id=row["session_id"],
                message_ids=list(stored_ids),
                idempotent=True,
            )

        state = con.execute(
            "SELECT msg_count, last_ts_ms, seg_index, seg_start, seg_count "
            "FROM sessions WHERE user_id=? AND session_id IS ?",
            (user_id, session_id),
        ).fetchone()
        old_count = state["msg_count"] if state else 0
        last_ts = state["last_ts_ms"] if state else None
        seg_index = state["seg_index"] if state else 0
        seg_start = state["seg_start"] if state else 0
        seg_count = state["seg_count"] if state else 0

        now = _now_iso()
        gap_ms = max(0, int(self.config.segment_max_gap_seconds)) * 1000
        max_seg = max(1, int(self.config.segment_max_messages))
        touched_segments: list[tuple[int, int]] = []
        new_ids: list[str] = []

        for offset, item in enumerate(normalized):
            seq = old_count + offset
            mid = _msg_id(user_id, session_id, seq)
            created_at = _iso_from_ms(item["ts_ms"]) or now
            con.execute(
                "INSERT INTO messages"
                "(id,user_id,session_id,seq,role,content,ts_ms,created_at,request_id,added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, user_id, session_id, seq, item["role"], item["content"],
                 item["ts_ms"], created_at, request_id, now),
            )
            self._index_doc(con, mid, user_id, "message", item["content"])
            governed.append_raw_event(
                con,
                message_id=mid,
                user_id=user_id,
                session_id=session_id,
                role=item["role"],
                content=item["content"],
                observed_at=created_at,
                recorded_at=now,
                trusted_scope=event_scope,
            )
            new_ids.append(mid)

            # 段边界：从左到右、确定性，与 views.segment_boundaries 全量扫描等价
            if seg_count > 0:
                gap_break = (
                    gap_ms > 0
                    and last_ts is not None
                    and item["ts_ms"] is not None
                    and (int(item["ts_ms"]) - int(last_ts)) > gap_ms
                )
                if seg_count >= max_seg or gap_break:
                    seg_index += 1
                    seg_start = seq
                    seg_count = 0
            seg_count += 1
            if item["ts_ms"] is not None:
                last_ts = int(item["ts_ms"])
            if not touched_segments or touched_segments[-1][0] != seg_index:
                touched_segments.append((seg_index, seg_start))

        new_count = old_count + len(normalized)

        con.execute(
            "INSERT INTO requests(request_id,user_id,session_id,message_ids,created_at) "
            "VALUES(?,?,?,?,?)",
            (request_id, user_id, session_id, json.dumps(new_ids), now),
        )
        session_values = (
            new_count,
            last_ts,
            seg_index,
            seg_start,
            seg_count,
        )
        if session_id is None:
            # SQLite treats NULL values as distinct for UNIQUE/PRIMARY KEY
            # conflict handling.  An ON CONFLICT upsert would therefore append
            # a second `(user_id, NULL)` state row on every sessionless product
            # ingest, eventually reusing a message sequence/ID.  Writes already
            # hold BEGIN IMMEDIATE, so an explicit update-then-insert is atomic.
            updated = con.execute(
                "UPDATE sessions SET msg_count=?,last_ts_ms=?,seg_index=?,"
                "seg_start=?,seg_count=? WHERE user_id=? AND session_id IS NULL",
                (*session_values, user_id),
            ).rowcount
            if updated == 0:
                con.execute(
                    "INSERT INTO sessions"
                    "(user_id,session_id,msg_count,last_ts_ms,seg_index,seg_start,seg_count) "
                    "VALUES(?,NULL,?,?,?,?,?)",
                    (user_id, *session_values),
                )
        else:
            con.execute(
                "INSERT INTO sessions"
                "(user_id,session_id,msg_count,last_ts_ms,seg_index,seg_start,seg_count) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id,session_id) DO UPDATE SET "
                "msg_count=excluded.msg_count, last_ts_ms=excluded.last_ts_ms, "
                "seg_index=excluded.seg_index, seg_start=excluded.seg_start, "
                "seg_count=excluded.seg_count",
                (user_id, session_id, *session_values),
            )

        if self.flags.get("views", True):
            self._rebuild_incremental_views(
                con, user_id, session_id, old_count, new_count, touched_segments
            )

        return AddResult(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            message_ids=new_ids,
            idempotent=False,
        )

    # ------------------------------------------------------------------ views
    def _session_rows(self, con, user_id, session_id, start, end) -> list[dict]:
        rows = con.execute(
            "SELECT id, role, content, created_at, seq FROM messages "
            "WHERE user_id=? AND session_id IS ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (user_id, session_id, start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def _rebuild_incremental_views(
        self, con, user_id, session_id, old_count, new_count, touched_segments
    ) -> None:
        size = max(1, int(self.config.window_size))
        overlap = max(0, int(self.config.window_overlap))

        for start in affected_window_starts(old_count, new_count, size, overlap):
            chunk = self._session_rows(con, user_id, session_id, start, start + size - 1)
            if not chunk:
                continue
            self._upsert_view(
                con,
                view_id=window_view_id(user_id, session_id, start),
                user_id=user_id,
                session_id=session_id,
                view_type="window",
                chunk=chunk,
                start_seq=start,
                end_seq=start + len(chunk) - 1,
            )

        for idx, (seg_idx, seg_start) in enumerate(touched_segments):
            seg_end = (
                touched_segments[idx + 1][1] - 1
                if idx + 1 < len(touched_segments)
                else new_count - 1
            )
            chunk = self._session_rows(con, user_id, session_id, seg_start, seg_end)
            if not chunk:
                continue
            self._upsert_view(
                con,
                view_id=segment_view_id(user_id, session_id, seg_idx),
                user_id=user_id,
                session_id=session_id,
                view_type="session-segment",
                chunk=chunk,
                start_seq=seg_start,
                end_seq=seg_end,
            )

    def _upsert_view(
        self, con, *, view_id, user_id, session_id, view_type, chunk, start_seq, end_seq
    ) -> None:
        content = join_content(chunk)
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        existing = con.execute(
            "SELECT content_hash FROM views WHERE view_id=?", (view_id,)
        ).fetchone()
        if existing is not None and existing["content_hash"] == digest:
            return  # 内容未变，避免无谓的 FTS 抖动
        con.execute(
            "INSERT OR REPLACE INTO views"
            "(view_id,user_id,session_id,view_type,content,created_at,source_ids,start_seq,end_seq,content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                view_id, user_id, session_id, view_type, content,
                chunk[0].get("created_at") or "",
                json.dumps([m["id"] for m in chunk], ensure_ascii=False),
                start_seq, end_seq, digest,
            ),
        )
        self._index_doc(con, view_id, user_id, view_type, content)

    def _index_doc(self, con, doc_id, user_id, doc_type, content) -> None:
        con.execute("DELETE FROM fts WHERE doc_id=?", (doc_id,))
        con.execute(
            "INSERT INTO fts(text, doc_id, user_id, doc_type) VALUES(?,?,?,?)",
            (features.index_text(content), doc_id, user_id, doc_type),
        )

    # ----------------------------------------------------------------- Search
    def _match_expr(self, tokens: list[str]) -> str | None:
        if not tokens:
            return None
        return " OR ".join('"%s"' % t.replace('"', '""') for t in tokens)

    def _fts_candidates(self, con, user_id: str, tokens: list[str]) -> list[tuple[str, str, float]]:
        expr = self._match_expr(tokens)
        if not expr:
            return []
        sql = (
            "SELECT doc_id, doc_type, rank FROM fts "
            "WHERE fts MATCH ? AND user_id=? "
            + ("" if self.flags.get("views", True) else "AND doc_type='message' ")
            + "ORDER BY rank LIMIT ?"
        )
        try:
            rows = con.execute(sql, (expr, user_id, int(self.config.max_candidates))).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["doc_id"], r["doc_type"], float(r["rank"] or 0.0)) for r in rows]

    def _like_fallback(self, con, user_id: str, query: str) -> list[tuple[str, str, float]]:
        needle = f"%{(query or '').strip()}%"
        if len(needle) <= 2:
            return []
        rows = con.execute(
            "SELECT id AS doc_id, 'message' AS doc_type FROM messages "
            "WHERE user_id=? AND content LIKE ? LIMIT ?",
            (user_id, needle, int(self.config.max_candidates)),
        ).fetchall()
        return [(r["doc_id"], r["doc_type"], 0.0) for r in rows]

    def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int | None = None,
        options: list[str] | None = None,
        request_id: str | None = None,
    ) -> SearchResult:
        if not user_id:
            raise ValueError("user_id is required")
        if query is None:
            raise ValueError("query is required")
        limit = self.config.top_k_default if top_k is None else int(top_k)
        limit = max(0, min(limit, int(self.config.top_k_max)))
        if limit == 0:
            return SearchResult(request_id=request_id, total=0, results=[])

        recall_text = query
        if options and self.flags.get("use_options", True):
            recall_text = query + " " + " ".join(str(o) for o in options)
        tokens = features.query_tokens(recall_text, self.config.max_query_tokens)

        with self.connection() as con:
            raw_candidates = self._fts_candidates(con, user_id, tokens)
            if not raw_candidates and self.flags.get("exact_scan", False):
                raw_candidates = self._like_fallback(con, user_id, query)
            if not raw_candidates:
                return SearchResult(request_id=request_id, total=0, results=[])
            records = self._load_records(con, user_id, raw_candidates)

        fts_order = [doc_id for doc_id, _t, _r in raw_candidates]
        rank_map = {doc_id: rank for doc_id, _t, rank in raw_candidates}
        if not records:
            return SearchResult(request_id=request_id, total=0, results=[])

        scored = self._score(records, query, tokens, rank_map)
        ordered = self._fuse_and_order(scored, fts_order)
        if self.flags.get("dedup", True):
            ordered = self._dedup(ordered)
        final = self._apply_slot_guarantee(ordered, limit)

        results = [
            Evidence(
                id=r["id"],
                user_id=user_id,
                view=r["view"],
                content=r["content"],
                created_at=r["created_at"] or "",
                score=round(float(r["score"]), 6),
                source_message_ids=r["source_ids"],
                provenance=f"{r['view']}:{r['id']}<-[{','.join(r['source_ids'])}]",
                evidence_flags=r["flags"],
            )
            for r in final
        ]
        return SearchResult(request_id=request_id, total=len(ordered), results=results)

    def _load_records(self, con, user_id, raw_candidates) -> list[dict]:
        msg_ids = [d for d, t, _ in raw_candidates if t == "message"]
        view_ids = [d for d, t, _ in raw_candidates if t != "message"]
        out: list[dict] = []
        chunk = 400
        for i in range(0, len(msg_ids), chunk):
            part = msg_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            for r in con.execute(
                f"SELECT id, 'message' AS view, role, content, created_at, ts_ms, session_id, seq "
                f"FROM messages WHERE id IN ({ph}) AND user_id=?",
                (*part, user_id),
            ).fetchall():
                rec = dict(r)
                rec["source_ids"] = [rec["id"]]
                out.append(rec)
        for i in range(0, len(view_ids), chunk):
            part = view_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            for r in con.execute(
                f"SELECT view_id AS id, view_type AS view, NULL AS role, content, created_at, "
                f"session_id, start_seq AS seq, source_ids, NULL AS ts_ms "
                f"FROM views WHERE view_id IN ({ph}) AND user_id=?",
                (*part, user_id),
            ).fetchall():
                rec = dict(r)
                try:
                    rec["source_ids"] = json.loads(rec["source_ids"])
                except (TypeError, ValueError):
                    rec["source_ids"] = [rec["id"]]
                out.append(rec)
        return out

    def _score(self, records, query, tokens, rank_map) -> list[dict]:
        q_lower = (query or "").lower()
        numbers = features.extract_numbers(query)
        dates = features.extract_dates(query)
        phrases = features.extract_phrases(query)
        entities = [e.lower() for e in features.extract_entities(query)][:32]
        token_set = [t for t in tokens if t]

        # bm25 归一化：fts5 rank 为负值，越小越相关
        ranks = [v for v in rank_map.values() if v]
        worst = max((abs(v) for v in ranks), default=1.0) or 1.0

        msg_seqs: dict[tuple, set[int]] = {}
        for rec in records:
            if rec["view"] == "message":
                msg_seqs.setdefault((rec.get("session_id"),), set()).add(int(rec.get("seq") or 0))

        now_ts = time.time()

        # 相对新近度：把候选集内的时间戳线性归一到 [0, 1]。
        # 绝对年龄在"整批语料都很老"时会退化成常数，无法区分新旧证据。
        temporal_intent = (
            self.flags.get("temporal_intent", False) and features.has_temporal_intent(query)
        )
        recency_weight = (
            float(getattr(self.config, "recency_weight_intent", W_RECENCY_INTENT))
            if temporal_intent
            else float(getattr(self.config, "recency_weight", W_RECENCY))
        )
        stamps = [self._epoch_of(rec) for rec in records]
        known = [s for s in stamps if s is not None]
        ts_min = min(known) if known else None
        ts_span = (max(known) - ts_min) if known else 0.0
        for rec in records:
            content_lower = (rec["content"] or "").lower()
            score = 0.0
            flags: list[str] = []

            rank = rank_map.get(rec["id"])
            if rank:
                score += W_LEXICAL * (abs(float(rank)) / worst) * 10.0
                flags.append("lexical")

            if token_set:
                hit = sum(1 for t in token_set if t in content_lower)
                coverage = hit / float(len(token_set))
                score += W_COVERAGE * coverage
                if coverage >= 0.6:
                    flags.append("high_coverage")

            if self.flags.get("exact", True):
                if q_lower and len(q_lower) >= 4 and q_lower in content_lower:
                    score += W_EXACT_FULL
                    flags.append("exact_substring")

            if self.flags.get("entity", True):
                if phrases and any(p.lower() in content_lower for p in phrases):
                    score += W_PHRASE
                    flags.append("phrase")
                ent_hits = sum(1 for e in entities if e and e in content_lower)
                if ent_hits:
                    score += min(W_ENTITY * ent_hits, W_ENTITY * 5)
                    flags.append("entity")

            if self.flags.get("datenum", True):
                if dates and any(d in content_lower for d in dates):
                    score += W_DATE
                    flags.append("date")
                if numbers and any(n in content_lower for n in numbers):
                    score += W_NUMBER
                    flags.append("number")

            if self.flags.get("rerank", True):
                if rec["view"] in ("window", "session-segment"):
                    score += W_VIEW_CONTEXT
                    flags.append("view_context")
                if rec["view"] == "message":
                    peers = msg_seqs.get((rec.get("session_id"),), set())
                    seq = int(rec.get("seq") or 0)
                    if (seq - 1) in peers or (seq + 1) in peers:
                        score += W_ADJACENT
                        flags.append("adjacent_evidence")
                epoch = self._epoch_of(rec)
                if epoch is not None:
                    if ts_span > 0:
                        relative = (epoch - ts_min) / ts_span
                    else:
                        relative = 1.0
                    score += recency_weight * relative
                    flags.append("recency_intent" if temporal_intent else "recency")
                elif self._age_days(rec.get("created_at"), now_ts) is not None:
                    # 极端兜底：没有可用时间戳时退回绝对年龄的弱信号
                    age_days = self._age_days(rec.get("created_at"), now_ts)
                    score += W_RECENCY / (1.0 + math.log1p(max(0.0, age_days)))
                    flags.append("recency")

            rec["score"] = score
            rec["flags"] = flags

        if (
            self.flags.get("preference_role_boost", False)
            and features.has_preference_intent(query)
        ):
            weight = float(getattr(self.config, "preference_role_weight", 14.0))
            for rec in records:
                if (
                    rec["view"] == "message"
                    and str(rec.get("role") or "").lower() == "user"
                    and features.has_direct_preference_statement(rec.get("content") or "")
                ):
                    rec["score"] += weight
                    if "direct_user_preference" not in rec["flags"]:
                        rec["flags"].append("direct_user_preference")

        if self.flags.get("rerank", True):
            self._mark_conflicts(records)
        if self.flags.get("supersession", False):
            # 用「原始」时间意图判定，不受 flags["temporal_intent"] 影响：
            # 覆写检测与新近度放大是两个独立机制，前者不应被后者的开关连带关掉。
            self._mark_supersession(
                records,
                features.has_temporal_intent(query),
                query=query,
                require_update_cue=self.flags.get("supersession_update_guard", False),
            )
        return records

    @staticmethod
    def _epoch_of(rec: dict) -> float | None:
        """候选记录的事件时间（秒）。优先原始 ts_ms，其次解析 created_at。

        聚合视图的 created_at 取自其首条源消息，因此与原始消息在同一时间轴上，
        可以直接参与相对新近度归一。
        """
        ts_ms = rec.get("ts_ms")
        if ts_ms is not None:
            try:
                return float(ts_ms) / 1000.0
            except (TypeError, ValueError):
                pass
        created_at = rec.get("created_at")
        if not created_at:
            return None
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _age_days(created_at: str | None, now_ts: float) -> float | None:
        if not created_at:
            return None
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_ts - dt.timestamp()) / 86400.0)

    def _mark_conflicts(self, records) -> None:
        """疑似状态冲突：同一实体上出现不同数字/日期。只做标注与轻微偏好，绝不过滤。"""
        buckets: dict[str, list[dict]] = {}
        for rec in records:
            if rec["view"] != "message":
                continue
            ents = features.extract_entities(rec["content"] or "")[:1]
            if not ents:
                continue
            buckets.setdefault(ents[0].lower(), []).append(rec)
        for group in buckets.values():
            if len(group) < 2:
                continue
            values = {
                tuple(features.extract_numbers(r["content"]) + features.extract_dates(r["content"]))
                for r in group
            }
            if len(values) < 2:
                continue
            newest = max(group, key=lambda r: (r.get("created_at") or "", r["id"]))
            for rec in group:
                if "possible_conflict" not in rec["flags"]:
                    rec["flags"].append("possible_conflict")
            newest["score"] += W_CONFLICT_LATEST
            if "latest_state" not in newest["flags"]:
                newest["flags"].append("latest_state")

    def _supersession_signature(self, text: str) -> frozenset:
        """消息的「话题指纹」：长度≥2 的 n-gram 集合。

        只取多字 token：单字 CJK 与停用词在任意两条中文句子间都会重合，
        会把不相关的消息也判成同一话题。
        """
        return frozenset(t for t in features.tokenize(text or "") if len(t) >= 2)

    def _mark_supersession(
        self,
        records,
        temporal_intent: bool,
        *,
        query: str = "",
        require_update_cue: bool = False,
    ) -> None:
        """覆写检测：话题高度重合的两条消息中，较晚的一条视为覆写较早的一条。

        与 `_mark_conflicts` 的区别：后者要求出现**不同的数字/日期**，
        因此只能覆盖"预算从 A 改成 B"这类数值型更新；本方法基于**成对内容冗余**，
        也能覆盖"早餐从 A 改成 B"这类纯文本型更新。

        与 `temporal_intent` 的区别：不做全局新近度放大。只有在确实找到
        「更早的近重复陈述」时才生效，所以不会把「问当前状态、但答案本身较旧」
        的查询整体推向最新消息（那正是 temporal_intent 在 multi_session 上失分的原因）。

        判定是纯结构性的（token 重合 + 时间先后），不依赖任何同义词表、
        人名/项目名清单或与评测集有关的硬编码。

        **只在查询含时间意图时生效**。否则「同模板、不同属性」的近重复消息
        （如"X的工牌号是…"/"X的车位编号是…"）会互相判定覆写并被整体抬高，
        把与时间无关的查询的正确答案挤下去（实测 single_hop|paraphrase
        MRR 1.0 → 0.21）。

        ⚠️ 仅靠结构判断、使用历史 18/6 权重的 L8 版本默认 **disabled**。跨 3 seed 消融显示它
        在合成集上净负：temporal|paraphrase 稳定 +0.10，但整体 MRR −0.026~−0.034。
        失败根因是**本方法的能力上限**，不是参数没调好：仅凭「话题重合 + 时间更晚」
        无法区分"真正的后续更新"与"对同一话题的无关后续提及"。合成集里存在
        与 gold 同项目名、但时间更晚的普通陈述，本方法会把它判成覆写者并抬到 gold 之上
        （knowledge_update|paraphrase −0.21）。
        v1.1 的 ``require_update_cue`` 会进一步要求较新消息带通用更新语义，
        并阻止数值更新干扰非数值型查询。该保护不包含评测实体或答案值；
        保守 4/1 权重在本地三 seed 过门后与 supersession 组合启用，
        但在官方数据上的表现仍是 **unknown**。
        """
        if not temporal_intent:
            return
        cfg = self.config
        min_overlap = float(getattr(cfg, "supersession_min_overlap", 0.5))
        weight = float(getattr(cfg, "supersession_weight", 4.0))
        penalty = float(getattr(cfg, "supersession_penalty", 1.0))
        max_pairs = int(getattr(cfg, "supersession_max_pairs", 40))

        # 只比较原始消息：聚合视图天然与其成员高度重合，会产生虚假覆写对。
        pool = [r for r in records if r["view"] == "message" and self._epoch_of(r) is not None]
        if len(pool) < 2:
            return
        # 确定性截断：按当前特征分降序取前 N，界定两两比较的最坏代价。
        pool.sort(key=lambda r: (-float(r.get("score") or 0.0), r["id"]))
        pool = pool[: max(2, max_pairs)]

        sigs = {r["id"]: self._supersession_signature(r["content"]) for r in pool}
        epochs = {r["id"]: self._epoch_of(r) for r in pool}

        superseded: set[str] = set()
        supersedes: set[str] = set()
        explicit_updates: set[str] = set()
        for i in range(len(pool)):
            a = pool[i]
            sig_a = sigs[a["id"]]
            if not sig_a:
                continue
            for j in range(i + 1, len(pool)):
                b = pool[j]
                sig_b = sigs[b["id"]]
                if not sig_b or sig_a == sig_b:
                    continue
                inter = len(sig_a & sig_b)
                if not inter:
                    continue
                # containment（交集 / 较短一方）而非 Jaccard：真实覆写陈述通常
                # 比原陈述更长（"早餐改成了 B，不再吃 A" vs "早餐一直吃 A"），
                # Jaccard 会因长度差异把这类真实覆写对判负（该对 Jaccard < 0.5、
                # containment ≈ 0.57）。
                # 也试过 IDF 加权 containment（想压掉同模板干扰项的共享模板词），
                # 实测更差：temporal|paraphrase 增益从 +0.11 掉到 +0.06，
                # 而 knowledge_update 的失分一点没救回来（根因见下方 docstring）。
                if inter / float(min(len(sig_a), len(sig_b))) < min_overlap:
                    continue
                ea, eb = epochs[a["id"]], epochs[b["id"]]
                if ea == eb:
                    continue
                newer, older = (b, a) if eb > ea else (a, b)
                if require_update_cue and not features.has_update_cue(newer["content"]):
                    continue
                # 数值/日期更新不能仅凭共享项目名干扰“谁负责/偏好什么”之类查询。
                # 这只是答案类型保护：不删除候选，也不包含任何评测实体或答案值。
                structured_types: set[str] = set()
                if features.extract_non_date_numbers(newer["content"]):
                    structured_types.add("numeric")
                if features.extract_dates(newer["content"]):
                    structured_types.add("date")
                query_types: set[str] = set()
                if features.has_numeric_value_intent(query):
                    query_types.add("numeric")
                if features.has_date_value_intent(query):
                    query_types.add("date")
                if (
                    require_update_cue
                    and structured_types
                    and structured_types.isdisjoint(query_types)
                ):
                    continue
                supersedes.add(newer["id"])
                superseded.add(older["id"])
                if require_update_cue:
                    explicit_updates.add(newer["id"])

        # 同时被判为「覆写者」和「被覆写者」的记录处于更新链中间，不加不减，
        # 避免链式更新时中间态被误抬。
        for rec in records:
            rid = rec["id"]
            is_new, is_old = rid in supersedes, rid in superseded
            if is_new and not is_old:
                rec["score"] += weight
                if "supersedes_earlier" not in rec["flags"]:
                    rec["flags"].append("supersedes_earlier")
                if rid in explicit_updates and "explicit_update_cue" not in rec["flags"]:
                    rec["flags"].append("explicit_update_cue")
            elif is_old and not is_new:
                rec["score"] -= penalty
                if "superseded" not in rec["flags"]:
                    rec["flags"].append("superseded")

    def _fuse_and_order(self, records, fts_order) -> list[dict]:
        feat_order = [
            r["id"]
            for r in sorted(records, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))
        ]
        if self.flags.get("rrf", True):
            # 加权 RRF：特征路与词法路权重可配。
            # 实测（docs/EVAL.md 附录 A，合成集 medium）：提高词法权重会**持续抬高 MRR**
            # 同时**持续压低 Recall@20**（Recall@100 恒为 1.0，说明 gold 没丢、只是被挤出前 20）。
            # 默认 w_lex=0.1 是扫描点中唯一 Pareto 安全的取值：三种难度 MRR 均正增益且
            # Recall@20 全部保持 1.0000。取 0.1 的理由是"不牺牲 Recall@20"，
            # 不是早期注释里写的"防止等权抹平特征分"——那个说法已被扫描证伪。
            k = int(getattr(self.config, "rrf_k", RRF_K) or RRF_K)
            w_feat = float(getattr(self.config, "rrf_weight_feature", 1.0))
            w_lex = float(getattr(self.config, "rrf_weight_lexical", 0.25))
            fused: dict[str, float] = {}
            for order, weight in ((feat_order, w_feat), (fts_order, w_lex)):
                if weight == 0.0:
                    continue
                for pos, doc_id in enumerate(order):
                    fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + pos + 1)
            for rec in records:
                rec["score"] = fused.get(rec["id"], 0.0)
                if "rrf" not in rec["flags"]:
                    rec["flags"].append("rrf")
        # 确定性排序：分值降序 -> 内容短优先 -> id 升序
        return sorted(records, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))

    @staticmethod
    def _dedup(ordered: list[dict]) -> list[dict]:
        seen_hash: set[str] = set()
        kept: list[dict] = []
        covered: list[set] = []
        for rec in ordered:
            digest = hashlib.sha1((rec["content"] or "").encode("utf-8")).hexdigest()
            if digest in seen_hash:
                continue
            sources = set(rec["source_ids"])
            # 聚合视图若已被某条保留视图完全覆盖，则丢弃（原始消息永不丢弃）
            if rec["view"] != "message" and any(sources <= c for c in covered):
                continue
            seen_hash.add(digest)
            if rec["view"] != "message":
                covered.append(sources)
            kept.append(rec)
        return kept

    def _apply_slot_guarantee(self, ordered: list[dict], limit: int) -> list[dict]:
        """保证 top_k 中原始消息占比不低于配置比例（聚合视图不得挤掉原始证据）。"""
        head = ordered[:limit]
        ratio = float(self.config.message_slot_ratio or 0.0)
        if ratio <= 0 or len(ordered) <= limit:
            return head
        need = int(limit * ratio)
        have = sum(1 for r in head if r["view"] == "message")
        if have >= need:
            return head
        extra = [r for r in ordered[limit:] if r["view"] == "message"][: need - have]
        if not extra:
            return head
        keep = [r for r in head if r["view"] == "message"]
        fillers = [r for r in head if r["view"] != "message"]
        drop = len(extra)
        merged = keep + extra + fillers[: max(0, len(fillers) - drop)]
        return sorted(merged, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))[:limit]

    # -------------------------------------------------- 隐私与生命周期（删除）
    def delete_user(self, user_id: str) -> dict:
        def _do(con):
            n_msg = con.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_view = con.execute(
                "SELECT COUNT(*) FROM views WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_raw = con.execute(
                "SELECT COUNT(*) FROM raw_events WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_memory = con.execute(
                "SELECT COUNT(*) FROM memory_records WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_receipts = con.execute(
                "SELECT COUNT(*) FROM extraction_receipts WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_origins = con.execute(
                "SELECT COUNT(*) FROM proposal_origins WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            con.execute("DELETE FROM fts WHERE user_id=?", (user_id,))
            # Privacy erase is intentionally stronger than a memory tombstone:
            # it removes raw evidence and its full derived/audit chain.
            con.execute("DELETE FROM proposal_origins WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM extraction_receipts WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM memory_state_events WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM memory_records WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM raw_events WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM views WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM requests WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return {
                "user_id": user_id,
                "deleted_messages": n_msg,
                "deleted_views": n_view,
                "deleted_raw_events": n_raw,
                "deleted_memory_records": n_memory,
                "deleted_extraction_receipts": n_receipts,
                "deleted_proposal_origins": n_origins,
            }

        return self._write(_do)

    def purge_all(self) -> dict:
        def _do(con):
            n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            con.executescript(
                "DELETE FROM fts; DELETE FROM proposal_origins; DELETE FROM extraction_receipts; "
                "DELETE FROM memory_state_events; DELETE FROM memory_records; "
                "DELETE FROM raw_events; DELETE FROM messages; DELETE FROM views; "
                "DELETE FROM requests; DELETE FROM sessions;"
            )
            return {"deleted_messages": n}

        return self._write(_do)

    # --------------------------------------------------------------- 只读辅助
    def list_raw_events(self, user_id: str, *, limit: int = 100) -> list[governed.RawEvent]:
        """Return immutable event locators joined to their legacy source text."""
        if not user_id:
            raise ValueError("user_id is required")
        with self.connection() as con:
            rows = con.execute(
                "SELECT re.*,m.role,m.content FROM raw_events re JOIN messages m "
                "ON m.id=re.source_message_id WHERE re.user_id=? "
                "ORDER BY re.observed_at,re.id LIMIT ?",
                (user_id, max(0, int(limit))),
            ).fetchall()
        return [governed.raw_event_from_row(row) for row in rows]

    def raw_events_for_extraction(
        self,
        *,
        user_id: str,
        event_ids: list[str] | tuple[str, ...],
    ) -> tuple[governed.RawEvent, ...]:
        """Load one exact ordered batch without revealing cross-user existence."""

        if not isinstance(user_id, str) or not user_id.strip():
            raise extraction.ExtractionValidationError("user_id is required")
        if not isinstance(event_ids, (list, tuple)) or not event_ids:
            raise extraction.ExtractionValidationError("raw_event_ids must be a non-empty array")
        if len(event_ids) > extraction.MAX_SOURCE_EVENTS:
            raise extraction.ExtractionValidationError("raw_event_ids exceeds the batch limit")
        if not all(isinstance(item, str) and item.strip() for item in event_ids):
            raise extraction.ExtractionValidationError("raw_event_ids must contain non-empty strings")
        normalized = tuple(item.strip() for item in event_ids)
        if len(normalized) != len(set(normalized)):
            raise extraction.ExtractionValidationError("raw_event_ids must not contain duplicates")
        placeholders = ",".join("?" * len(normalized))
        with self.connection() as con:
            rows = con.execute(
                f"SELECT re.*,m.role,m.content FROM raw_events re JOIN messages m "
                f"ON m.id=re.source_message_id WHERE re.user_id=? "
                f"AND re.id IN ({placeholders})",
                (user_id, *normalized),
            ).fetchall()
        by_id = {row["id"]: governed.raw_event_from_row(row) for row in rows}
        if len(by_id) != len(normalized):
            raise extraction.ExtractionValidationError(
                "one or more source events are unavailable for the requested user"
            )
        return tuple(by_id[event_id] for event_id in normalized)

    def extraction_receipt(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> extracted.CompilationReceipt | None:
        with self.connection() as con:
            return extracted.find_receipt(
                con,
                user_id=user_id,
                idempotency_key=idempotency_key,
            )

    def persist_extraction(
        self,
        *,
        request: extraction.ExtractionRequest,
        proposals: tuple[extraction.ProposalDraft, ...],
    ) -> extracted.CompilationReceipt:
        """Atomically write candidate/state/origin/receipt after extraction."""

        return self._write(
            lambda con: extracted.persist_compilation(
                con,
                request=request,
                proposals=proposals,
            ),
            _immediate=True,
        )

    def propose_memory(self, **kwargs) -> governed.MemoryRecord:
        """Create a non-confirmed derived memory proposal.

        ``confirmed`` is rejected by the governance layer.  Call
        :meth:`transition_memory` with explicit owner authority to promote it.
        """
        return self._write(lambda con: governed.create_memory_record(con, **kwargs))

    def transition_target_matches(
        self,
        *,
        user_id: str,
        record_id: str,
        memory_key: str,
        scope: dict | None,
        related_record_id: str | None = None,
    ) -> bool:
        """Directly verify one exact transition target without audit scanning."""

        with self.connection() as con:
            return governed.transition_target_matches(
                con,
                user_id=user_id,
                record_id=record_id,
                memory_key=memory_key,
                scope=scope,
                related_record_id=related_record_id,
            )

    def transition_memory(
        self,
        *,
        record_id: str,
        target_status: str,
        actor: str,
        actor_authority: str,
        reason: str,
        user_id: str,
        related_record_id: str | None = None,
    ) -> governed.MemoryRecord:
        """Apply one controlled, append-only-audited lifecycle transition."""
        return self._write(
            lambda con: governed.transition_memory_record(
                con,
                record_id=record_id,
                target_status=target_status,
                actor=actor,
                actor_authority=actor_authority,
                reason=reason,
                user_id=user_id,
                related_record_id=related_record_id,
            ),
            _immediate=True,
        )

    def search_governed(
        self,
        *,
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        mode: str = "current",
        scope: dict | None = None,
        as_of: str | None = None,
        max_records: int = 100,
    ) -> governed.CurrentStateResult:
        """Resolve current truth or return the full audit chain.

        This path never consumes window/session-segment views, so an inactive
        source embedded in a legacy aggregate cannot leak into current state.
        """
        with self.connection() as con:
            return governed.query_current_state(
                con,
                user_id=user_id,
                memory_key=memory_key,
                query=query,
                mode=mode,
                scope=scope,
                as_of=as_of,
                max_records=max_records,
            )

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """只读查询辅助（审计 / 测试用）。写操作必须走 _write。"""
        with self.connection() as con:
            return con.execute(sql, params).fetchall()

    def count(self, user_id: str | None = None) -> int:
        with self.connection() as con:
            if user_id:
                return con.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)
                ).fetchone()[0]
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def stats(self) -> dict:
        with self.connection() as con:
            return {
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "views": con.execute("SELECT COUNT(*) FROM views").fetchone()[0],
                "users": con.execute("SELECT COUNT(DISTINCT user_id) FROM messages").fetchone()[0],
                "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "raw_events": con.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
                "memory_records": con.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0],
                "extraction_receipts": con.execute(
                    "SELECT COUNT(*) FROM extraction_receipts"
                ).fetchone()[0],
                "proposal_origins": con.execute(
                    "SELECT COUNT(*) FROM proposal_origins"
                ).fetchone()[0],
                "db_path": self.db_path,
            }

    def close(self) -> None:
        if self._shared_mem_con is not None:
            try:
                self._shared_mem_con.commit()
                self._shared_mem_con.close()
            except sqlite3.Error:
                pass
            self._shared_mem_con = None
        while True:
            try:
                con = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                con.commit()
                con.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ["RetrieverDB", "AddResult", "SearchResult", "Evidence"]
