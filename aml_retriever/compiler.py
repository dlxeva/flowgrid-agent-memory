"""Atomic persistence compiler for validated extraction proposals.

Extraction itself is intentionally absent from this module.  A host invokes an
extractor before opening a write transaction, then this compiler revalidates
the entire output and commits candidate records, origins, and a success receipt
in one ``BEGIN IMMEDIATE`` transaction supplied by :class:`RetrieverDB`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import governance as governed
from .extraction import (
    ExtractionConflict,
    ExtractionRequest,
    ExtractionValidationError,
    ProposalDraft,
    canonical_json,
    sha256_canonical,
    validate_proposals,
)


EXTRACTION_SCHEMA_VERSION = 1


_EXPECTED_TABLE_LAYOUTS: dict[
    str,
    tuple[tuple[str, str, int, object, int], ...],
] = {
    # name, declared type, NOT NULL, default, PK ordinal.  Exact layouts are
    # versioned together with ``EXTRACTION_SCHEMA_VERSION``; accepting a
    # same-named but weaker table would silently void idempotency/audit claims.
    "extraction_meta": (
        ("key", "TEXT", 1, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "extraction_receipts": (
        ("user_id", "TEXT", 1, None, 1),
        ("idempotency_key", "TEXT", 1, None, 2),
        ("request_digest", "TEXT", 1, None, 0),
        ("extractor_fingerprint", "TEXT", 1, None, 0),
        ("output_fingerprint", "TEXT", 1, None, 0),
        ("proposal_count", "INTEGER", 1, None, 0),
        ("record_ids_json", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
    ),
    "proposal_origins": (
        ("record_id", "TEXT", 1, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("idempotency_key", "TEXT", 1, None, 0),
        ("request_digest", "TEXT", 1, None, 0),
        ("extractor_fingerprint", "TEXT", 1, None, 0),
        ("proposal_index", "INTEGER", 1, None, 0),
        ("proposal_fingerprint", "TEXT", 1, None, 0),
        ("evidence_spans_json", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ),
}


_IMMUTABILITY_TRIGGER_SQL = {
    "extraction_receipts_no_update": """CREATE TRIGGER IF NOT EXISTS extraction_receipts_no_update
        BEFORE UPDATE ON extraction_receipts
        BEGIN
            SELECT RAISE(ABORT, 'extraction receipts are immutable');
        END""",
    "proposal_origins_no_update": """CREATE TRIGGER IF NOT EXISTS proposal_origins_no_update
        BEFORE UPDATE ON proposal_origins
        BEGIN
            SELECT RAISE(ABORT, 'proposal origins are immutable');
        END""",
}


def _normalized_schema_sql(value: str | None) -> str:
    normalized = " ".join(str(value or "").lower().split())
    # SQLite omits this clause in sqlite_master.sql after creation.
    return normalized.replace("create trigger if not exists", "create trigger")


def _table_layout(
    con: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, object, int], ...]:
    return tuple(
        (
            row["name"],
            str(row["type"]).upper(),
            int(row["notnull"]),
            row["dflt_value"],
            int(row["pk"]),
        )
        for row in con.execute(f"PRAGMA table_info({table})")
    )


def _verify_table_layouts(con: sqlite3.Connection) -> None:
    if any(
        _table_layout(con, table) != expected
        for table, expected in _EXPECTED_TABLE_LAYOUTS.items()
    ):
        raise ExtractionValidationError("extraction schema layout is incompatible")


@dataclass(frozen=True)
class CompilationReceipt:
    """Durable success result for one ``(user_id, idempotency_key)``."""

    user_id: str
    idempotency_key: str
    request_digest: str
    extractor_fingerprint: str
    output_fingerprint: str
    proposal_count: int
    record_ids: tuple[str, ...]
    created_at: str
    status: str = "success"
    idempotent: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "extractor_fingerprint": self.extractor_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "proposal_count": self.proposal_count,
            "record_ids": list(self.record_ids),
            "created_at": self.created_at,
            "status": self.status,
            "idempotent": self.idempotent,
        }


def install_schema(con: sqlite3.Connection) -> None:
    """Version-gated, concurrent-safe additive extraction migration.

    A database advertising a future version or a malformed v1 layout is
    rejected instead of being opened under assumptions this code cannot prove.
    The surrounding RetrieverDB write commits or rolls back the transaction.
    """

    con.execute(
        "CREATE TABLE IF NOT EXISTS extraction_meta("
        "key TEXT NOT NULL PRIMARY KEY,value TEXT NOT NULL)"
    )
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    # Validate the meta table before selecting from it so a same-named table
    # missing ``value`` or its primary key fails with our fixed safe error.
    if _table_layout(con, "extraction_meta") != _EXPECTED_TABLE_LAYOUTS["extraction_meta"]:
        raise ExtractionValidationError("extraction schema layout is incompatible")
    row = con.execute(
        "SELECT value FROM extraction_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        current = 0
    else:
        try:
            current = int(row["value"])
        except (TypeError, ValueError):
            raise ExtractionValidationError("extraction schema version is invalid") from None
    if current < 0:
        raise ExtractionValidationError("extraction schema version is invalid")
    if current > EXTRACTION_SCHEMA_VERSION:
        raise ExtractionValidationError("extraction schema version is newer than this runtime")

    if current < 1:
        con.execute(
            """CREATE TABLE IF NOT EXISTS extraction_receipts(
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            extractor_fingerprint TEXT NOT NULL,
            output_fingerprint TEXT NOT NULL,
            proposal_count INTEGER NOT NULL,
            record_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY(user_id,idempotency_key)
        )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_extraction_receipt_digest "
            "ON extraction_receipts(user_id,request_digest)"
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS proposal_origins(
            record_id TEXT NOT NULL PRIMARY KEY,
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            extractor_fingerprint TEXT NOT NULL,
            proposal_index INTEGER NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            evidence_spans_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposal_origins_receipt "
            "ON proposal_origins(user_id,idempotency_key,proposal_index)"
        )
        for trigger_sql in _IMMUTABILITY_TRIGGER_SQL.values():
            con.execute(trigger_sql)

    _verify_table_layouts(con)
    triggers = {
        row["name"]: row["sql"]
        for row in con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('extraction_receipts_no_update','proposal_origins_no_update')"
        )
    }
    if set(triggers) != set(_IMMUTABILITY_TRIGGER_SQL) or any(
        _normalized_schema_sql(triggers[name])
        != _normalized_schema_sql(expected_sql)
        for name, expected_sql in _IMMUTABILITY_TRIGGER_SQL.items()
    ):
        raise ExtractionValidationError("extraction schema immutability guards are missing")

    if current < EXTRACTION_SCHEMA_VERSION:
        con.execute(
            "INSERT INTO extraction_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(EXTRACTION_SCHEMA_VERSION),),
        )


def _parse_record_ids(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ExtractionValidationError("stored extraction receipt is invalid")
    return tuple(parsed)


def receipt_from_row(row: sqlite3.Row, *, idempotent: bool = False) -> CompilationReceipt:
    record_ids = _parse_record_ids(row["record_ids_json"])
    try:
        proposal_count = int(row["proposal_count"])
    except (TypeError, ValueError):
        raise ExtractionValidationError("stored extraction receipt is invalid") from None
    digests = (
        row["request_digest"],
        row["extractor_fingerprint"],
        row["output_fingerprint"],
    )
    if (
        proposal_count < 0
        or proposal_count != len(record_ids)
        or row["status"] != "success"
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in digests
        )
    ):
        raise ExtractionValidationError("stored extraction receipt is invalid")
    return CompilationReceipt(
        user_id=row["user_id"],
        idempotency_key=row["idempotency_key"],
        request_digest=row["request_digest"],
        extractor_fingerprint=row["extractor_fingerprint"],
        output_fingerprint=row["output_fingerprint"],
        proposal_count=proposal_count,
        record_ids=record_ids,
        created_at=row["created_at"],
        status=row["status"],
        idempotent=idempotent,
    )


def find_receipt(
    con: sqlite3.Connection,
    *,
    user_id: str,
    idempotency_key: str,
) -> CompilationReceipt | None:
    row = con.execute(
        "SELECT * FROM extraction_receipts WHERE user_id=? AND idempotency_key=?",
        (user_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    receipt = receipt_from_row(row)
    if receipt.record_ids:
        placeholders = ",".join("?" * len(receipt.record_ids))
        record_count = con.execute(
            f"SELECT COUNT(*) FROM memory_records WHERE user_id=? "
            f"AND id IN ({placeholders})",
            (user_id, *receipt.record_ids),
        ).fetchone()[0]
        origin_count = con.execute(
            f"SELECT COUNT(*) FROM proposal_origins WHERE user_id=? "
            f"AND idempotency_key=? AND record_id IN ({placeholders})",
            (user_id, idempotency_key, *receipt.record_ids),
        ).fetchone()[0]
        if record_count != len(receipt.record_ids) or origin_count != len(receipt.record_ids):
            raise ExtractionValidationError("stored extraction receipt is invalid")
    return receipt


def resolve_existing_receipt(
    receipt: CompilationReceipt | None,
    request: ExtractionRequest,
) -> CompilationReceipt | None:
    if receipt is None:
        return None
    if (
        receipt.status != "success"
        or receipt.request_digest != request.digest
        or receipt.extractor_fingerprint != request.extractor.fingerprint
    ):
        raise ExtractionConflict(
            "idempotency key is already bound to a different extraction request"
        )
    return replace(receipt, idempotent=True)


def _record_id(
    *,
    user_id: str,
    idempotency_key: str,
    request_digest: str,
    proposal_index: int,
) -> str:
    material = canonical_json(
        {
            "schema": "flowgrid.extracted-record-id/v1",
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "proposal_index": proposal_index,
        }
    )
    return "mem_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _source_event_ids(proposal: ProposalDraft) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for span in proposal.evidence_spans:
        if span.source_event_id not in seen:
            seen.add(span.source_event_id)
            ordered.append(span.source_event_id)
    return ordered


def _derived_authority(
    source_by_id: dict[str, governed.RawEvent],
    source_ids: list[str],
) -> str:
    """Derive authority conservatively from evidence; never from extractor output."""

    authorities = {source_by_id[event_id].authority for event_id in source_ids}
    return next(iter(authorities)) if len(authorities) == 1 else "unknown"


def _observed_at(
    source_by_id: dict[str, governed.RawEvent],
    source_ids: list[str],
) -> str:
    def instant(event_id: str) -> datetime:
        raw = source_by_id[event_id].observed_at
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            raise ExtractionValidationError("source observed_at is invalid") from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExtractionValidationError("source observed_at must include a timezone")
        return value.astimezone(timezone.utc)

    return max(instant(event_id) for event_id in source_ids).isoformat()


def _origin_spans(proposal: ProposalDraft) -> list[dict[str, object]]:
    """Persist locators and quote hashes, never a second copy of source text."""

    return [
        {
            "source_event_id": span.source_event_id,
            "start": span.start,
            "end": span.end,
            "quote_sha256": hashlib.sha256(span.quote.encode("utf-8")).hexdigest(),
        }
        for span in proposal.evidence_spans
    ]


def _verify_source_snapshot(
    con: sqlite3.Connection,
    request: ExtractionRequest,
) -> dict[str, governed.RawEvent]:
    """Rebind provenance to the exact live DB evidence under the write lock.

    Raw IDs are deterministic and can be reused after privacy erase followed
    by a same-session Add.  Therefore ID/user existence alone is insufficient:
    every immutable event/message field must still equal the pre-extraction
    snapshot before any derived row is inserted.
    """

    expected_by_id = {event.id: event for event in request.raw_events}
    event_ids = tuple(expected_by_id)
    placeholders = ",".join("?" * len(event_ids))
    rows = con.execute(
        f"SELECT re.*,m.role,m.content FROM raw_events re JOIN messages m "
        f"ON m.id=re.source_message_id WHERE re.user_id=? "
        f"AND re.id IN ({placeholders})",
        (request.user_id, *event_ids),
    ).fetchall()
    live_by_id = {
        row["id"]: governed.raw_event_from_row(row)
        for row in rows
    }
    if len(live_by_id) != len(expected_by_id):
        raise ExtractionValidationError("source evidence changed before persistence")
    for event_id, expected in expected_by_id.items():
        live = live_by_id.get(event_id)
        if live is None or live != expected:
            raise ExtractionValidationError("source evidence changed before persistence")
    return live_by_id


def persist_compilation(
    con: sqlite3.Connection,
    *,
    request: ExtractionRequest,
    proposals: tuple[ProposalDraft, ...] | list[ProposalDraft],
) -> CompilationReceipt:
    """Persist one validated batch; caller must already hold BEGIN IMMEDIATE.

    The existing receipt is checked again under the write lock, which makes
    concurrent callers first-success-wins while returning the original IDs to
    all same-digest retries.
    """

    request.assert_integrity()
    _verify_source_snapshot(con, request)
    existing = resolve_existing_receipt(
        find_receipt(
            con,
            user_id=request.user_id,
            idempotency_key=request.idempotency_key,
        ),
        request,
    )
    if existing is not None:
        return existing

    # Revalidate the whole sequence before the first derived INSERT.  Any later
    # database failure still rolls the entire transaction back via RetrieverDB.
    normalized = validate_proposals(request, proposals)
    # ProposalDraft can be subclassed by a hostile lower-level caller.  Field
    # access during validate_proposals' defensive clone may attempt reflective
    # mutation of the request after the first check.  Seal again immediately,
    # then rebind every source from the live DB under BEGIN IMMEDIATE.
    request.assert_integrity()
    sealed_sources = _verify_source_snapshot(con, request)

    # Capture all remaining trusted inputs into local base values, verify once
    # more, and never consult the caller-owned request below this line.
    sealed_user_id = str(request.user_id)
    sealed_idempotency_key = str(request.idempotency_key)
    sealed_request_digest = str(request.digest)
    sealed_scope = dict(request.trusted_scope)
    sealed_extractor_name = str(request.extractor.name)
    sealed_extractor_version = str(request.extractor.version)
    sealed_extractor_fingerprint = str(request.extractor.fingerprint)
    request.assert_integrity()
    output_fingerprint = sha256_canonical(
        {
            "schema": "flowgrid.extraction-output/v1",
            "proposals": [proposal.to_dict() for proposal in normalized],
        }
    )
    created_by = (
        f"extractor:{sealed_extractor_name}@{sealed_extractor_version}:"
        f"{sealed_extractor_fingerprint}"
    )
    record_ids: list[str] = []
    for index, proposal in enumerate(normalized):
        source_ids = _source_event_ids(proposal)
        record_id = _record_id(
            user_id=sealed_user_id,
            idempotency_key=sealed_idempotency_key,
            request_digest=sealed_request_digest,
            proposal_index=index,
        )
        # ``$user`` is the sole trusted subject alias.  It is resolved only by
        # the core from the service-bound request user, never by extractor
        # authority.  All other subjects remain literal entity subjects.
        resolved_subject = (
            sealed_user_id if proposal.subject == "$user" else proposal.subject
        )
        record = governed.create_memory_record(
            con,
            user_id=sealed_user_id,
            memory_key=proposal.memory_key,
            memory_type=proposal.memory_type,
            subject=resolved_subject,
            content=proposal.content,
            source_event_ids=source_ids,
            status="candidate",
            authority=_derived_authority(sealed_sources, source_ids),
            scope=sealed_scope,
            observed_at=_observed_at(sealed_sources, source_ids),
            valid_from=proposal.valid_from,
            valid_until=proposal.valid_until,
            confidence=proposal.confidence,
            created_by=created_by,
            supersedes_record_id=None,
            state_reason="extracted_candidate",
            record_id=record_id,
        )
        con.execute(
            "INSERT INTO proposal_origins"
            "(record_id,user_id,idempotency_key,request_digest,extractor_fingerprint,"
            "proposal_index,proposal_fingerprint,evidence_spans_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                record.id,
                sealed_user_id,
                sealed_idempotency_key,
                sealed_request_digest,
                sealed_extractor_fingerprint,
                index,
                proposal.fingerprint,
                canonical_json(_origin_spans(proposal)),
                record.created_at,
            ),
        )
        record_ids.append(record.id)

    created_at = governed.utc_now()
    con.execute(
        "INSERT INTO extraction_receipts"
        "(user_id,idempotency_key,request_digest,extractor_fingerprint,output_fingerprint,"
        "proposal_count,record_ids_json,created_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            sealed_user_id,
            sealed_idempotency_key,
            sealed_request_digest,
            sealed_extractor_fingerprint,
            output_fingerprint,
            len(normalized),
            canonical_json(record_ids),
            created_at,
            "success",
        ),
    )
    return CompilationReceipt(
        user_id=sealed_user_id,
        idempotency_key=sealed_idempotency_key,
        request_digest=sealed_request_digest,
        extractor_fingerprint=sealed_extractor_fingerprint,
        output_fingerprint=output_fingerprint,
        proposal_count=len(normalized),
        record_ids=tuple(record_ids),
        created_at=created_at,
        status="success",
        idempotent=False,
    )


__all__ = [
    "EXTRACTION_SCHEMA_VERSION",
    "CompilationReceipt",
    "install_schema",
    "find_receipt",
    "resolve_existing_receipt",
    "persist_compilation",
]
