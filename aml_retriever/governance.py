"""Governed memory state primitives for the vendor-neutral memory core.

This module deliberately separates three layers:

* ``raw_events`` are immutable evidence copied verbatim from accepted Add
  messages.  They are never rewritten by extraction or state transitions.
* ``memory_records`` are derived claims that point back to raw events.
* ``memory_state_events`` are an append-only audit trail and the authoritative
  source for controlled state changes.  The status column is only a projection
  cache for efficient migration and inspection.

The AML Add/Search adapter does not depend on these shapes.  ``RetrieverDB``
exposes them as an internal core API while preserving the official contract.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone


MEMORY_STATES = frozenset(
    {"candidate", "confirmed", "inferred", "unknown", "superseded", "rejected", "deleted"}
)
MEMORY_TYPES = frozenset({"fact", "preference", "event", "procedure", "judgment"})
AUTHORITIES = frozenset({"user", "owner", "policy", "agent", "system", "external", "unknown"})
CONFIRM_AUTHORITIES = frozenset({"user", "owner", "policy"})
GOVERNANCE_AUTHORITIES = CONFIRM_AUTHORITIES
SCOPE_FIELDS = frozenset({"tenant", "user", "project", "agent", "session", "repository"})
GOVERNANCE_SCHEMA_VERSION = 1
_SCHEMA_VERSION_RE = re.compile(r"0|[1-9][0-9]*")

# Transitions are monotonic.  Terminal states cannot be revived; new evidence
# must create a new record so the old judgment remains auditable.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"confirmed", "inferred", "unknown", "rejected", "deleted"}),
    "inferred": frozenset({"confirmed", "unknown", "rejected", "deleted"}),
    "unknown": frozenset({"confirmed", "rejected", "deleted"}),
    "confirmed": frozenset({"superseded", "rejected", "deleted"}),
    "superseded": frozenset({"deleted"}),
    "rejected": frozenset({"deleted"}),
    "deleted": frozenset(),
}


class GovernanceError(ValueError):
    """Base error for invalid governed-memory operations."""


class GovernanceConflict(GovernanceError):
    """Raised when an operation would silently create competing current truth."""


@dataclass(frozen=True)
class RawEvent:
    id: str
    user_id: str
    event_type: str
    role: str
    content: str
    observed_at: str
    recorded_at: str
    authority: str
    scope: dict[str, str]
    source_locator: str
    source_message_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    user_id: str
    memory_key: str
    memory_type: str
    subject: str
    content: str
    status: str
    source_event_ids: list[str]
    source_locator: list[str]
    observed_at: str
    valid_from: str | None
    valid_until: str | None
    authority: str
    scope: dict[str, str]
    confidence: float | None
    created_by: str
    created_at: str
    updated_at: str
    confirmed_by: str | None
    confirmed_at: str | None
    supersedes_record_id: str | None
    state_reason: str
    why_selected: str = ""

    @property
    def current_status(self) -> str:
        return self.status

    def to_dict(self) -> dict:
        data = asdict(self)
        data["current_status"] = self.status
        return data


@dataclass(frozen=True)
class MemoryStateEvent:
    id: str
    record_id: str
    user_id: str
    from_status: str
    to_status: str
    actor: str
    actor_authority: str
    reason: str
    transitioned_at: str
    related_record_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CurrentStateResult:
    """Structured current-state answer.

    ``unknown`` is a first-class answer, not an empty-list convention.  The
    owner gate is kept outside the records array so record-count or character
    budgets cannot silently discard it.
    """

    mode: str
    current_status: str
    abstain: bool
    reason: str
    records: list[MemoryRecord] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    withheld_record_ids: list[str] = field(default_factory=list)
    owner_gate_required: bool = False
    raw_events: list[RawEvent] = field(default_factory=list)
    state_events: list[MemoryStateEvent] = field(default_factory=list)
    matched_count: int | None = None
    returned_count: int | None = None
    truncated: bool | None = None

    def __post_init__(self) -> None:
        try:
            record_count = len(self.records)
        except TypeError:
            raise ValueError("records must be a sized collection") from None
        returned_count = (
            record_count if self.returned_count is None else self.returned_count
        )
        matched_count = (
            returned_count if self.matched_count is None else self.matched_count
        )
        if (
            isinstance(matched_count, bool)
            or not isinstance(matched_count, int)
            or matched_count < 0
            or isinstance(returned_count, bool)
            or not isinstance(returned_count, int)
            or returned_count < 0
            or returned_count != record_count
            or matched_count < returned_count
        ):
            raise ValueError("invalid current-state completeness metadata")
        expected_truncated = matched_count > returned_count
        truncated = expected_truncated if self.truncated is None else self.truncated
        if not isinstance(truncated, bool) or truncated != expected_truncated:
            raise ValueError("invalid current-state completeness metadata")
        object.__setattr__(self, "matched_count", matched_count)
        object.__setattr__(self, "returned_count", returned_count)
        object.__setattr__(self, "truncated", truncated)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "current_status": self.current_status,
            "abstain": self.abstain,
            "reason": self.reason,
            "records": [record.to_dict() for record in self.records],
            "conflicts": list(self.conflicts),
            "withheld_record_ids": list(self.withheld_record_ids),
            "owner_gate_required": self.owner_gate_required,
            "raw_events": [event.to_dict() for event in self.raw_events],
            "state_events": [event.to_dict() for event in self.state_events],
            "completeness": {
                "matched_count": self.matched_count,
                "returned_count": self.returned_count,
                "truncated": self.truncated,
            },
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_instant(value: str, *, field: str) -> str:
    """Parse an aware ISO-8601 instant and canonicalize it to UTC."""

    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"{field} must be a non-empty timezone-aware ISO-8601 string")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GovernanceError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceError(f"{field} must include an explicit timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


def raw_event_id_for_message(message_id: str) -> str:
    digest = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:24]
    return f"raw_{digest}"


def authority_for_role(role: str) -> str:
    normalized = str(role or "").strip().lower()
    if normalized == "user":
        return "user"
    if normalized == "assistant":
        return "agent"
    if normalized == "system":
        return "system"
    return "external" if normalized else "unknown"


def _json_object(value: dict | None, *, user_id: str) -> tuple[dict[str, str], str]:
    if value is not None and not isinstance(value, dict):
        raise GovernanceError("scope must be an object")
    scope = dict(value or {})
    if any(not isinstance(key, str) for key in scope):
        raise GovernanceError("scope field names must be strings")
    unknown = set(scope) - SCOPE_FIELDS
    if unknown:
        raise GovernanceError(f"unsupported scope fields: {', '.join(sorted(unknown))}")
    for key, item in scope.items():
        if not isinstance(item, str) or not item.strip():
            raise GovernanceError(f"scope.{key} must be a non-empty string")
    if "user" in scope and scope["user"] != user_id:
        raise GovernanceError("scope.user must match user_id")
    scope.setdefault("user", user_id)
    normalized = {key: scope[key].strip() for key in sorted(scope)}
    return normalized, json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def raw_event_scope(
    *,
    user_id: str,
    session_id: str | None,
    trusted_scope: dict | None = None,
) -> dict[str, str]:
    """Return the canonical immutable scope bound to an ingested event.

    Transport adapters may add a trusted tenant/project/agent boundary, but
    neither that boundary nor the concrete user/session may contradict the
    event being stored.  Keeping the complete binding on ``raw_events`` makes
    idempotent replays verifiable without introducing a mutable side receipt.
    """

    combined = dict(trusted_scope or {})
    if "user" in combined and combined["user"] != user_id:
        raise GovernanceError("trusted scope user does not match user_id")
    combined["user"] = user_id
    if session_id is not None:
        if "session" in combined and combined["session"] != session_id:
            raise GovernanceError("trusted scope session does not match session_id")
        combined["session"] = session_id
    normalized, _scope_json = _json_object(combined, user_id=user_id)
    return normalized


def _parse_json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _parse_json_object(value: str | None) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _slot_identity(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    """Exact identity of one governed truth slot.

    ``scope_json`` is canonicalized at write time, so exact string equality is
    intentional here; a project-scoped fact cannot supersede a global or
    differently scoped fact by accident.
    """

    return (
        row["user_id"],
        row["memory_key"],
        row["memory_type"],
        row["subject"],
        row["scope_json"],
    )


def install_schema(con: sqlite3.Connection) -> None:
    """Install the additive v1 governance schema without altering old tables."""

    # Keep every DDL statement inside the caller's transaction.  Python's
    # executescript() commits an open transaction before running its script,
    # which would make partial product schemas observable after interruption.
    statements = (
        """CREATE TABLE IF NOT EXISTS raw_events(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            authority TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            source_message_id TEXT NOT NULL UNIQUE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_raw_events_user ON raw_events(user_id, observed_at)",
        """CREATE TABLE IF NOT EXISTS governance_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS memory_records(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL,
            source_event_ids TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            valid_from TEXT,
            valid_until TEXT,
            authority TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            confidence REAL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmed_by TEXT,
            confirmed_at TEXT,
            supersedes_record_id TEXT,
            state_reason TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE INDEX IF NOT EXISTS idx_memory_current
            ON memory_records(user_id, memory_key, status, valid_from, valid_until)""",
        """CREATE INDEX IF NOT EXISTS idx_memory_user_type
            ON memory_records(user_id, memory_type, status)""",
        """CREATE TABLE IF NOT EXISTS memory_state_events(
            id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            actor TEXT NOT NULL,
            actor_authority TEXT NOT NULL,
            reason TEXT NOT NULL,
            transitioned_at TEXT NOT NULL,
            related_record_id TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS idx_memory_state_event_record
            ON memory_state_events(user_id, record_id, transitioned_at)""",
        """CREATE TRIGGER IF NOT EXISTS raw_events_no_update
        BEFORE UPDATE ON raw_events
        BEGIN
            SELECT RAISE(ABORT, 'raw_events are immutable');
        END""",
        """CREATE TRIGGER IF NOT EXISTS raw_messages_no_update
        BEFORE UPDATE ON messages
        BEGIN
            SELECT RAISE(ABORT, 'raw message evidence is immutable');
        END""",
        """CREATE TRIGGER IF NOT EXISTS ingest_requests_no_update
        BEFORE UPDATE ON requests
        BEGIN
            SELECT RAISE(ABORT, 'ingest request receipts are immutable');
        END""",
        """CREATE TRIGGER IF NOT EXISTS memory_state_events_no_update
        BEFORE UPDATE ON memory_state_events
        BEGIN
            SELECT RAISE(ABORT, 'memory_state_events are append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS memory_records_payload_no_update
        BEFORE UPDATE OF
            user_id,memory_key,memory_type,subject,content,source_event_ids,
            observed_at,valid_from,valid_until,authority,scope_json,confidence,
            created_by,created_at,supersedes_record_id
        ON memory_records
        BEGIN
            SELECT RAISE(ABORT, 'derived memory payload is immutable');
        END""",
    )
    for statement in statements:
        con.execute(statement)


def append_raw_event(
    con: sqlite3.Connection,
    *,
    message_id: str,
    user_id: str,
    session_id: str | None,
    role: str,
    content: str,
    observed_at: str,
    recorded_at: str | None = None,
    trusted_scope: dict | None = None,
) -> RawEvent:
    """Append one immutable raw event, idempotently by source message id."""

    if not message_id or not isinstance(user_id, str) or not user_id.strip():
        raise GovernanceError("message_id and user_id are required for raw events")
    scope = raw_event_scope(
        user_id=user_id,
        session_id=session_id,
        trusted_scope=trusted_scope,
    )
    scope_json = json.dumps(scope, ensure_ascii=False, sort_keys=True)
    event_id = raw_event_id_for_message(message_id)
    event = RawEvent(
        id=event_id,
        user_id=user_id,
        event_type="message",
        role=str(role or ""),
        content=str(content),
        observed_at=str(observed_at),
        recorded_at=str(recorded_at or utc_now()),
        authority=authority_for_role(role),
        scope=scope,
        source_locator=f"messages:{message_id}",
        source_message_id=message_id,
    )
    existing = con.execute(
        "SELECT re.*,m.role,m.content FROM raw_events re JOIN messages m "
        "ON m.id=re.source_message_id WHERE re.source_message_id=?", (message_id,)
    ).fetchone()
    if existing is not None:
        current = raw_event_from_row(existing)
        immutable_fields = (
            "id", "user_id", "event_type", "role", "content", "observed_at",
            "authority", "scope", "source_locator", "source_message_id",
        )
        if any(getattr(current, name) != getattr(event, name) for name in immutable_fields):
            raise GovernanceConflict(f"raw event for message {message_id!r} already exists and differs")
        return current
    con.execute(
        "INSERT INTO raw_events"
        "(id,user_id,event_type,observed_at,recorded_at,authority,"
        "scope_json,source_locator,source_message_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            event.id, event.user_id, event.event_type, event.observed_at,
            event.recorded_at, event.authority, scope_json,
            event.source_locator, event.source_message_id,
        ),
    )
    return event


def backfill_raw_events(con: sqlite3.Connection) -> int:
    """Backfill existing messages non-destructively; returns inserted count."""

    before = con.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    rows = con.execute(
        "SELECT id,user_id,session_id,role,content,created_at,added_at FROM messages ORDER BY id"
    ).fetchall()
    for row in rows:
        append_raw_event(
            con,
            message_id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            role=row["role"] or "",
            content=row["content"],
            observed_at=row["created_at"],
            recorded_at=row["added_at"],
        )
    after = con.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    return int(after - before)


def run_migrations(con: sqlite3.Connection) -> int:
    """Run each data migration once, safely across concurrent starters.

    Schema DDL is additive and idempotent.  The potentially O(N) legacy
    message backfill is version-gated, then rechecked under ``BEGIN
    IMMEDIATE`` so two processes cannot both perform it or race on UNIQUE
    source locators.
    """

    meta_layout = tuple(
        (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in con.execute("PRAGMA table_info(governance_meta)").fetchall()
    )
    if meta_layout != (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ):
        raise GovernanceError("governance schema metadata layout is invalid")

    def read_version() -> int:
        row = con.execute(
            "SELECT value FROM governance_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return 0
        value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
        if not isinstance(value, str) or _SCHEMA_VERSION_RE.fullmatch(value) is None:
            raise GovernanceError("governance schema version is invalid")
        return int(value)

    current = read_version()
    if current > GOVERNANCE_SCHEMA_VERSION:
        raise GovernanceError("governance schema is newer than this runtime")
    if current == GOVERNANCE_SCHEMA_VERSION:
        return current

    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    current = read_version()
    if current > GOVERNANCE_SCHEMA_VERSION:
        raise GovernanceError("governance schema is newer than this runtime")
    if current < 1:
        backfill_raw_events(con)
        current = 1
        con.execute(
            "INSERT INTO governance_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(current),),
        )
    return current


def raw_event_from_row(row: sqlite3.Row) -> RawEvent:
    return RawEvent(
        id=row["id"],
        user_id=row["user_id"],
        event_type=row["event_type"],
        role=row["role"],
        content=row["content"],
        observed_at=row["observed_at"],
        recorded_at=row["recorded_at"],
        authority=row["authority"],
        scope=_parse_json_object(row["scope_json"]),
        source_locator=row["source_locator"],
        source_message_id=row["source_message_id"],
    )


def memory_record_from_row(row: sqlite3.Row, *, why_selected: str = "") -> MemoryRecord:
    source_event_ids = _parse_json_list(row["source_event_ids"])
    return MemoryRecord(
        id=row["id"],
        user_id=row["user_id"],
        memory_key=row["memory_key"],
        memory_type=row["memory_type"],
        subject=row["subject"],
        content=row["content"],
        status=row["status"],
        source_event_ids=source_event_ids,
        source_locator=[f"raw_events:{event_id}" for event_id in source_event_ids],
        observed_at=row["observed_at"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        authority=row["authority"],
        scope=_parse_json_object(row["scope_json"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        confirmed_by=row["confirmed_by"],
        confirmed_at=row["confirmed_at"],
        supersedes_record_id=row["supersedes_record_id"],
        state_reason=row["state_reason"] or "",
        why_selected=why_selected,
    )


def state_event_from_row(row: sqlite3.Row) -> MemoryStateEvent:
    return MemoryStateEvent(
        id=row["id"],
        record_id=row["record_id"],
        user_id=row["user_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        actor=row["actor"],
        actor_authority=row["actor_authority"],
        reason=row["reason"],
        transitioned_at=row["transitioned_at"],
        related_record_id=row["related_record_id"],
    )


def create_memory_record(
    con: sqlite3.Connection,
    *,
    user_id: str,
    memory_key: str,
    content: str,
    source_event_ids: list[str] | None,
    memory_type: str = "fact",
    subject: str | None = None,
    status: str = "candidate",
    authority: str = "agent",
    scope: dict | None = None,
    observed_at: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    created_by: str = "agent",
    supersedes_record_id: str | None = None,
    state_reason: str = "",
    record_id: str | None = None,
) -> MemoryRecord:
    """Create a derived record without silently creating confirmed truth.

    Direct creation in ``confirmed`` is intentionally forbidden.  Even a claim
    extracted from user-authored evidence must pass an explicit confirmation
    transition, making promotion an auditable action rather than an extractor
    side effect.
    """

    if (
        not isinstance(user_id, str)
        or not user_id.strip()
        or not str(memory_key).strip()
        or not isinstance(content, str)
        or not content.strip()
    ):
        raise GovernanceError("user_id, memory_key and non-empty content are required")
    if memory_type not in MEMORY_TYPES:
        raise GovernanceError(f"unsupported memory_type: {memory_type}")
    if status not in MEMORY_STATES:
        raise GovernanceError(f"unsupported memory status: {status}")
    if status == "confirmed":
        raise GovernanceError("confirmed records require an explicit transition")
    if status in {"superseded", "rejected", "deleted"}:
        raise GovernanceError(f"new records cannot start in terminal status {status!r}")
    if authority not in AUTHORITIES:
        raise GovernanceError(f"unsupported authority: {authority}")
    if not isinstance(created_by, str) or not created_by.strip():
        raise GovernanceError("created_by must be a non-empty string")
    if confidence is not None:
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise GovernanceError("confidence must be between 0 and 1")
    normalized_scope, scope_json = _json_object(scope, user_id=user_id)
    if memory_type == "preference" and (not isinstance(subject, str) or not subject.strip()):
        raise GovernanceError("preference memory requires an explicit subject")
    normalized_subject = str(subject or user_id).strip()
    event_ids = [str(item) for item in (source_event_ids or [])]
    if len(event_ids) != len(set(event_ids)):
        raise GovernanceError("source_event_ids must not contain duplicates")
    if event_ids:
        placeholders = ",".join("?" * len(event_ids))
        rows = con.execute(
            f"SELECT id,user_id,observed_at FROM raw_events WHERE id IN ({placeholders})",
            tuple(event_ids),
        ).fetchall()
        found = {row["id"]: row for row in rows}
        missing = [event_id for event_id in event_ids if event_id not in found]
        if missing:
            raise GovernanceError(f"unknown raw event ids: {', '.join(missing)}")
        if any(found[event_id]["user_id"] != user_id for event_id in event_ids):
            raise GovernanceError("source raw events must belong to the same user")
        if observed_at is None:
            observed_at = max(str(found[event_id]["observed_at"]) for event_id in event_ids)
    elif status != "unknown":
        raise GovernanceError("derived memory requires at least one source raw event")
    if memory_type == "preference":
        if not event_ids:
            raise GovernanceError("preference memory requires source evidence")
        if not observed_at:
            raise GovernanceError("preference memory requires observed_at")
        if not normalized_scope:
            raise GovernanceError("preference memory requires scope")
    if not observed_at:
        observed_at = utc_now()
    observed_at = _normalize_instant(str(observed_at), field="observed_at")
    valid_from = (
        _normalize_instant(valid_from, field="valid_from") if valid_from is not None else None
    )
    valid_until = (
        _normalize_instant(valid_until, field="valid_until") if valid_until is not None else None
    )
    if valid_from and valid_until and valid_from > valid_until:
        raise GovernanceError("valid_from must not be after valid_until")

    if supersedes_record_id:
        prior = con.execute(
            "SELECT * FROM memory_records WHERE id=? AND user_id=?",
            (supersedes_record_id, user_id),
        ).fetchone()
        if prior is None:
            raise GovernanceError("supersedes_record_id does not exist")
        new_slot = (user_id, memory_key, memory_type, normalized_subject, scope_json)
        if _slot_identity(prior) != new_slot:
            raise GovernanceError(
                "superseded record must share user_id, memory_key, memory_type, subject, and scope"
            )

    now = utc_now()
    rid = record_id or f"mem_{uuid.uuid4().hex}"
    con.execute(
        "INSERT INTO memory_records"
        "(id,user_id,memory_key,memory_type,subject,content,status,source_event_ids,"
        "observed_at,valid_from,valid_until,authority,scope_json,confidence,created_by,"
        "created_at,updated_at,confirmed_by,confirmed_at,supersedes_record_id,state_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, user_id, memory_key, memory_type, normalized_subject, content, status,
            json.dumps(event_ids, ensure_ascii=False), observed_at, valid_from, valid_until,
            authority, scope_json, confidence, created_by, now, now, None, None,
            supersedes_record_id, str(state_reason or ""),
        ),
    )
    _append_state_event(
        con,
        record_id=rid,
        user_id=user_id,
        from_status="",
        to_status=status,
        actor=created_by,
        actor_authority=authority,
        reason=str(state_reason or "record_created"),
        transitioned_at=now,
    )
    row = con.execute("SELECT * FROM memory_records WHERE id=?", (rid,)).fetchone()
    return memory_record_from_row(row)


def _append_state_event(
    con: sqlite3.Connection,
    *,
    record_id: str,
    user_id: str,
    from_status: str,
    to_status: str,
    actor: str,
    actor_authority: str,
    reason: str,
    transitioned_at: str,
    related_record_id: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO memory_state_events"
        "(id,record_id,user_id,from_status,to_status,actor,actor_authority,reason,"
        "transitioned_at,related_record_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            f"tr_{uuid.uuid4().hex}", record_id, user_id, from_status, to_status,
            actor, actor_authority, reason, transitioned_at, related_record_id,
        ),
    )


def transition_memory_record(
    con: sqlite3.Connection,
    *,
    record_id: str,
    target_status: str,
    actor: str,
    actor_authority: str,
    reason: str,
    user_id: str,
    related_record_id: str | None = None,
) -> MemoryRecord:
    """Apply one controlled transition and append its audit event."""

    if target_status not in MEMORY_STATES:
        raise GovernanceError(f"unsupported memory status: {target_status}")
    if not isinstance(user_id, str) or not user_id.strip():
        raise GovernanceError("user_id is required")
    if actor_authority not in AUTHORITIES:
        raise GovernanceError(f"unsupported actor_authority: {actor_authority}")
    if not isinstance(actor, str) or not actor.strip():
        raise GovernanceError("actor must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise GovernanceError("transition reason must be a non-empty string")
    row = con.execute(
        "SELECT * FROM memory_records WHERE id=? AND user_id=?", (record_id, user_id)
    ).fetchone()
    if row is None:
        raise GovernanceError("memory record not found")
    if related_record_id:
        related = con.execute(
            "SELECT * FROM memory_records WHERE id=? AND user_id=?",
            (related_record_id, user_id),
        ).fetchone()
        if related is None:
            raise GovernanceError("related_record_id must belong to the same user")
        if target_status in {"confirmed", "superseded"} and _slot_identity(related) != _slot_identity(row):
            raise GovernanceError("slot-related records must share the exact slot identity")
    latest = con.execute(
        "SELECT to_status FROM memory_state_events WHERE record_id=? AND user_id=? "
        "ORDER BY transitioned_at DESC,id DESC LIMIT 1",
        (record_id, user_id),
    ).fetchone()
    current = latest["to_status"] if latest is not None else row["status"]
    if target_status not in ALLOWED_TRANSITIONS[current]:
        raise GovernanceError(f"transition {current!r} -> {target_status!r} is not allowed")
    if (
        target_status in {"confirmed", "superseded", "rejected", "deleted"}
        and actor_authority not in GOVERNANCE_AUTHORITIES
    ):
        raise GovernanceError(
            "confirmed truth and governance terminal states require user/owner/policy authority"
        )
    if target_status == "confirmed" and row["memory_type"] == "preference":
        if row["subject"] != row["user_id"] or row["authority"] != "user":
            raise GovernanceError(
                "confirmed preference requires the user as subject and direct-user authority"
            )
        source_ids = _parse_json_list(row["source_event_ids"])
        if not source_ids:
            raise GovernanceError("confirmed preference requires direct-user source evidence")
        placeholders = ",".join("?" * len(source_ids))
        sources = con.execute(
            f"SELECT authority FROM raw_events WHERE id IN ({placeholders})",
            tuple(source_ids),
        ).fetchall()
        if len(sources) != len(source_ids) or any(source["authority"] != "user" for source in sources):
            raise GovernanceError(
                "assistant, third-party, or inferred preference evidence cannot be promoted"
            )

    now = utc_now()
    supersedes_id = row["supersedes_record_id"]
    if (
        target_status == "confirmed"
        and supersedes_id
        and not _valid_at(row, now)
    ):
        raise GovernanceConflict(
            "replacement must be currently valid; scheduled or expired supersession "
            "is unsupported"
        )
    if target_status == "confirmed":
        same_slot = con.execute(
            "SELECT * FROM memory_records WHERE user_id=? AND memory_key=? "
            "AND memory_type=? AND subject=? AND scope_json=? "
            "AND id<>? ORDER BY created_at,id",
            (
                row["user_id"], row["memory_key"], row["memory_type"],
                row["subject"], row["scope_json"], record_id,
            ),
        ).fetchall()
        active = []
        for candidate in same_slot:
            state_row = con.execute(
                "SELECT to_status FROM memory_state_events WHERE record_id=? AND user_id=? "
                "ORDER BY transitioned_at DESC,id DESC LIMIT 1",
                (candidate["id"], user_id),
            ).fetchone()
            candidate_status = state_row["to_status"] if state_row is not None else candidate["status"]
            if candidate_status == "confirmed" and _validity_intervals_overlap(candidate, row):
                active.append(candidate)
        if active:
            if len(active) != 1 or not supersedes_id or active[0]["id"] != supersedes_id:
                raise GovernanceConflict(
                    "confirmation would create competing current truth; declare supersedes_record_id"
                )
            prior = active[0]
            con.execute(
                "UPDATE memory_records SET status='superseded',updated_at=?,state_reason=? "
                "WHERE id=? AND user_id=?",
                (now, f"superseded by {record_id}: {reason}", prior["id"], user_id),
            )
            _append_state_event(
                con,
                record_id=prior["id"],
                user_id=prior["user_id"],
                from_status="confirmed",
                to_status="superseded",
                actor=actor,
                actor_authority=actor_authority,
                reason=reason,
                transitioned_at=now,
                related_record_id=record_id,
            )
        elif supersedes_id:
            prior = con.execute(
                "SELECT * FROM memory_records WHERE id=? AND user_id=?",
                (supersedes_id, user_id),
            ).fetchone()
            prior_state = con.execute(
                "SELECT to_status FROM memory_state_events WHERE record_id=? AND user_id=? "
                "ORDER BY transitioned_at DESC,id DESC LIMIT 1",
                (supersedes_id, user_id),
            ).fetchone()
            authoritative_prior = (
                prior_state["to_status"] if prior_state is not None else prior["status"] if prior else None
            )
            if (
                prior is None
                or _slot_identity(prior) != _slot_identity(row)
                or authoritative_prior != "confirmed"
            ):
                raise GovernanceConflict("declared superseded record is no longer current")

    confirmed_by = actor if target_status == "confirmed" else row["confirmed_by"]
    confirmed_at = now if target_status == "confirmed" else row["confirmed_at"]
    con.execute(
        "UPDATE memory_records SET status=?,updated_at=?,confirmed_by=?,confirmed_at=?,"
        "state_reason=? WHERE id=? AND user_id=?",
        (target_status, now, confirmed_by, confirmed_at, reason, record_id, user_id),
    )
    _append_state_event(
        con,
        record_id=record_id,
        user_id=row["user_id"],
        from_status=current,
        to_status=target_status,
        actor=actor,
        actor_authority=actor_authority,
        reason=reason,
        transitioned_at=now,
        related_record_id=related_record_id or supersedes_id,
    )
    updated = con.execute(
        "SELECT * FROM memory_records WHERE id=? AND user_id=?", (record_id, user_id)
    ).fetchone()
    return replace(memory_record_from_row(updated), status=target_status)


def _valid_at(row: sqlite3.Row, as_of: str) -> bool:
    valid_from = row["valid_from"]
    valid_until = row["valid_until"]
    return (not valid_from or str(valid_from) <= as_of) and (not valid_until or as_of < str(valid_until))


def _validity_intervals_overlap(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    """Whether two ``[valid_from, valid_until)`` intervals overlap.

    Missing ``valid_from`` is negative infinity; missing ``valid_until`` is
    positive infinity.  Equal endpoints do not overlap because the intervals
    are half-open.
    """

    left_start, left_end = left["valid_from"], left["valid_until"]
    right_start, right_end = right["valid_from"], right["valid_until"]
    if left_end is not None and right_start is not None and left_end <= right_start:
        return False
    if right_end is not None and left_start is not None and right_end <= left_start:
        return False
    return True


def query_current_state(
    con: sqlite3.Connection,
    *,
    user_id: str,
    memory_key: str | None = None,
    query: str | None = None,
    mode: str = "ordinary",
    scope: dict | None = None,
    as_of: str | None = None,
    max_records: int = 100,
) -> CurrentStateResult:
    """Resolve governed current state without consulting aggregate views.

    Ordinary mode emits only confirmed current truth (or explicit unknown
    boundaries).  Candidate, inferred, superseded, rejected and deleted rows
    are withheld even when their text is a better lexical match.  Audit mode
    returns the full lifecycle plus raw evidence and transition history.
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise GovernanceError("user_id is required")
    if mode == "ordinary":  # compatibility with the FLG terminology
        mode = "current"
    if mode not in {"current", "audit"}:
        raise GovernanceError("mode must be 'current' (or 'ordinary') or 'audit'")
    if max_records <= 0:
        raise GovernanceError("max_records must be > 0")
    normalized_scope, query_scope_json = _json_object(scope, user_id=user_id)
    cutoff = (
        _normalize_instant(as_of, field="as_of") if as_of is not None else utc_now()
    )
    clauses = ["user_id=?"]
    params: list[object] = [user_id]
    if memory_key is not None:
        clauses.append("memory_key=?")
        params.append(memory_key)
    if query:
        clauses.append(
            "(memory_key LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
            "OR subject LIKE ? ESCAPE '\\')"
        )
        literal = str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"%{literal}%"
        params.extend([needle, needle, needle])
    rows = con.execute(
        "SELECT * FROM memory_records WHERE " + " AND ".join(clauses) +
        " ORDER BY memory_key,created_at,id",
        tuple(params),
    ).fetchall()
    # Applicable records may be global or more specifically scoped within the
    # requested context.  A project-scoped record never leaks into a global
    # query, nor into a different project.
    def in_scope(row: sqlite3.Row) -> bool:
        record_scope = _parse_json_object(row["scope_json"])
        return all(
            key == "user" or normalized_scope.get(key) == value
            for key, value in record_scope.items()
        )

    rows = [row for row in rows if in_scope(row)]
    record_ids_all = [row["id"] for row in rows]
    state_history_by_id: dict[str, list[sqlite3.Row]] = {}
    if record_ids_all:
        placeholders = ",".join("?" * len(record_ids_all))
        for event_row in con.execute(
            f"SELECT * FROM memory_state_events "
            f"WHERE user_id=? AND record_id IN ({placeholders}) AND transitioned_at<=? "
            f"ORDER BY transitioned_at,id",
            (user_id, *record_ids_all, cutoff),
        ).fetchall():
            state_history_by_id.setdefault(event_row["record_id"], []).append(event_row)
    # No state event at/before cutoff means the record did not exist yet.
    rows = [row for row in rows if row["id"] in state_history_by_id]

    def status_of(row: sqlite3.Row) -> str:
        return state_history_by_id[row["id"]][-1]["to_status"]

    def record_at_cutoff(row: sqlite3.Row, *, why_selected: str) -> MemoryRecord:
        history = state_history_by_id[row["id"]]
        latest = history[-1]
        confirmed_events = [event for event in history if event["to_status"] == "confirmed"]
        confirmed = confirmed_events[-1] if confirmed_events else None
        return replace(
            memory_record_from_row(row, why_selected=why_selected),
            status=latest["to_status"],
            state_reason=latest["reason"],
            updated_at=latest["transitioned_at"],
            confirmed_by=confirmed["actor"] if confirmed is not None else None,
            confirmed_at=confirmed["transitioned_at"] if confirmed is not None else None,
        )

    if mode == "audit":
        matched_count = len(rows)
        records = [
            record_at_cutoff(
                row,
                why_selected="audit mode preserves full lifecycle as of cutoff",
            )
            for row in rows[:max_records]
        ]
        record_ids = [record.id for record in records]
        event_ids = sorted({event_id for record in records for event_id in record.source_event_ids})
        events: list[RawEvent] = []
        state_events: list[MemoryStateEvent] = []
        if event_ids:
            placeholders = ",".join("?" * len(event_ids))
            events = [
                raw_event_from_row(row)
                for row in con.execute(
                    f"SELECT re.*,m.role,m.content FROM raw_events re JOIN messages m "
                    f"ON m.id=re.source_message_id WHERE re.user_id=? AND re.id IN ({placeholders}) "
                    "ORDER BY re.observed_at,re.id",
                    (user_id, *event_ids),
                ).fetchall()
            ]
        if record_ids:
            placeholders = ",".join("?" * len(record_ids))
            state_events = [
                state_event_from_row(row)
                for row in con.execute(
                    f"SELECT * FROM memory_state_events WHERE user_id=? AND record_id IN ({placeholders}) "
                    "AND transitioned_at<=? ORDER BY transitioned_at,id",
                    (user_id, *record_ids, cutoff),
                ).fetchall()
            ]
        return CurrentStateResult(
            mode="audit",
            current_status="unknown" if not records else "audit",
            abstain=not records,
            reason="no_matching_memory" if not records else "full_audit_history",
            records=records,
            raw_events=events,
            state_events=state_events,
            matched_count=matched_count,
            returned_count=len(records),
            truncated=matched_count > len(records),
        )

    by_base: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        base = (row["memory_key"], row["memory_type"], row["subject"])
        by_base.setdefault(base, []).append(row)
    selected: list[MemoryRecord] = []
    conflicts: list[str] = []
    withheld: list[str] = []
    unknown_ids: list[str] = []
    owner_gate = False
    for _base, group in by_base.items():
        valid = [row for row in group if _valid_at(row, cutoff)]
        pending = [row for row in valid if status_of(row) in {"candidate", "inferred"}]
        hidden = [
            row for row in valid
            if status_of(row) in {
                "candidate", "inferred", "unknown", "superseded", "rejected", "deleted"
            }
        ]
        withheld.extend(row["id"] for row in hidden)
        owner_gate = owner_gate or bool(pending)

        confirmed_by_scope: dict[str, list[sqlite3.Row]] = {}
        for row in valid:
            if status_of(row) == "confirmed":
                confirmed_by_scope.setdefault(row["scope_json"], []).append(row)
        if confirmed_by_scope:
            ranked_slots: list[tuple[tuple[int, int], str, list[sqlite3.Row]]] = []
            for scope_json, slot_rows in confirmed_by_scope.items():
                record_scope = _parse_json_object(scope_json)
                specificity = len([key for key in record_scope if key != "user"])
                rank = (1 if scope_json == query_scope_json else 0, specificity)
                ranked_slots.append((rank, scope_json, slot_rows))
            best_rank = max(item[0] for item in ranked_slots)
            best_slots = [item for item in ranked_slots if item[0] == best_rank]
            if len(best_slots) > 1:
                conflicts.extend(row["id"] for _rank, _scope, slot in best_slots for row in slot)
                continue
            _rank, chosen_scope, chosen_rows = best_slots[0]
            if len(chosen_rows) > 1:
                conflicts.extend(row["id"] for row in chosen_rows)
                continue
            chosen = chosen_rows[0]
            for _rank, scope_json, slot_rows in ranked_slots:
                if scope_json != chosen_scope:
                    withheld.extend(row["id"] for row in slot_rows)
            why = (
                "confirmed, currently valid, and most specific applicable scope; "
                "selected over fallback, non-authoritative, or terminal records"
            )
            selected.append(
                record_at_cutoff(chosen, why_selected=why)
            )
            continue

        explicit_unknown = [row for row in valid if status_of(row) == "unknown"]
        if explicit_unknown:
            unknown_ids.extend(row["id"] for row in explicit_unknown)
        # Multiple different unresolved proposals are a conflict, not a winner.
        if len({row["content"] for row in pending}) > 1:
            conflicts.extend(row["id"] for row in pending)

    if conflicts:
        return CurrentStateResult(
            mode="current",
            current_status="unknown",
            abstain=True,
            reason="conflicting_current_evidence",
            records=[],
            conflicts=sorted(set(conflicts)),
            withheld_record_ids=sorted(set(withheld)),
            owner_gate_required=True,
        )
    if selected:
        selected = sorted(
            selected,
            key=lambda record: (
                record.memory_key, record.memory_type, record.subject,
                json.dumps(record.scope, ensure_ascii=False, sort_keys=True), record.id,
            ),
        )
        matched_count = len(selected)
        selected = selected[:max_records]
        return CurrentStateResult(
            mode="current",
            current_status="confirmed",
            abstain=False,
            reason="confirmed_current_memory" if not owner_gate else "confirmed_with_pending_owner_gate",
            records=selected,
            withheld_record_ids=sorted(set(withheld)),
            owner_gate_required=owner_gate,
            matched_count=matched_count,
            returned_count=len(selected),
            truncated=matched_count > len(selected),
        )
    if unknown_ids:
        return CurrentStateResult(
            mode="current",
            current_status="unknown",
            abstain=True,
            reason="explicit_unknown_state",
            records=[],
            withheld_record_ids=sorted(set(withheld + unknown_ids)),
            owner_gate_required=owner_gate,
        )
    return CurrentStateResult(
        mode="current",
        current_status="unknown",
        abstain=True,
        reason="no_confirmed_current_memory" if rows else "no_matching_memory",
        records=[],
        withheld_record_ids=sorted(set(withheld)),
        owner_gate_required=owner_gate,
    )


__all__ = [
    "MEMORY_STATES",
    "MEMORY_TYPES",
    "AUTHORITIES",
    "CONFIRM_AUTHORITIES",
    "GOVERNANCE_AUTHORITIES",
    "GOVERNANCE_SCHEMA_VERSION",
    "ALLOWED_TRANSITIONS",
    "GovernanceError",
    "GovernanceConflict",
    "RawEvent",
    "MemoryRecord",
    "MemoryStateEvent",
    "CurrentStateResult",
    "install_schema",
    "raw_event_scope",
    "append_raw_event",
    "backfill_raw_events",
    "run_migrations",
    "create_memory_record",
    "transition_memory_record",
    "query_current_state",
]
