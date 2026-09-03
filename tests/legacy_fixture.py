"""Self-contained fixture for the accepted AML Retriever v1.1 storage base.

The schema is transcribed from commit
``cdae7dbd38d73eda33793b30017559bdfb75eff5`` so clean-history clones and
source archives do not require the predecessor commit in local Git history.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


LEGACY_COMMIT = "cdae7dbd38d73eda33793b30017559bdfb75eff5"

LEGACY_SCHEMA = """
CREATE TABLE messages(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT, seq INTEGER NOT NULL,
 role TEXT, content TEXT NOT NULL, ts_ms INTEGER, created_at TEXT NOT NULL,
 request_id TEXT, added_at TEXT NOT NULL
);
CREATE INDEX idx_msg_scope ON messages(user_id, session_id, seq);
CREATE INDEX idx_msg_user ON messages(user_id);
CREATE TABLE views(
 view_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT,
 view_type TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
 source_ids TEXT NOT NULL, start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
 content_hash TEXT NOT NULL
);
CREATE INDEX idx_views_user ON views(user_id);
CREATE TABLE requests(
 request_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT,
 message_ids TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(request_id, user_id)
);
CREATE TABLE sessions(
 user_id TEXT NOT NULL, session_id TEXT, msg_count INTEGER NOT NULL DEFAULT 0,
 last_ts_ms INTEGER, seg_index INTEGER NOT NULL DEFAULT 0,
 seg_start INTEGER NOT NULL DEFAULT 0, seg_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id, session_id)
);
CREATE VIRTUAL TABLE fts USING fts5(
 text, doc_id UNINDEXED, user_id UNINDEXED, doc_type UNINDEXED
);
"""


def create_legacy_database(
    path: str | Path,
    *,
    content: str,
    request_id: str = "legacy-r1",
    user_id: str = "u1",
    session_id: str = "s1",
) -> None:
    """Create a minimal database with the exact accepted predecessor schema."""

    message_id = "m_legacy_fixture_1"
    timestamp = "2023-11-14T22:13:20+00:00"
    con = sqlite3.connect(str(path))
    try:
        con.executescript(LEGACY_SCHEMA)
        con.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (message_id, user_id, session_id, 0, "user", content,
             1_700_000_000_000, timestamp, request_id, timestamp),
        )
        con.execute(
            "INSERT INTO requests VALUES(?,?,?,?,?)",
            (request_id, user_id, session_id, f'["{message_id}"]', timestamp),
        )
        con.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
            (user_id, session_id, 1, 1_700_000_000_000, 0, 0, 1),
        )
        con.execute(
            "INSERT INTO fts(text,doc_id,user_id,doc_type) VALUES(?,?,?,?)",
            (content, message_id, user_id, "message"),
        )
        con.commit()
    finally:
        con.close()
