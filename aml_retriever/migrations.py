"""Read-only schema inspection and fail-closed startup gates.

Opening :class:`~aml_retriever.retriever.RetrieverDB` is a mutating operation:
it may create a new SQLite file and apply additive migrations.  Product
adapters and ``doctor`` therefore use the functions in this module when they
only need to inspect compatibility.  The inspector always opens an existing
database with SQLite ``mode=ro`` and never constructs ``RetrieverDB``.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .compiler import EXTRACTION_SCHEMA_VERSION
from .extraction import ExtractionValidationError
from .governance import GOVERNANCE_SCHEMA_VERSION, GovernanceError


_VERSION_RE = re.compile(r"0|[1-9][0-9]*")
_REQUIRED_V1_TABLES = frozenset(
    {"raw_events", "governance_meta", "memory_records", "memory_state_events"}
)
_REQUIRED_V1_TRIGGERS = frozenset(
    {
        "raw_events_no_update",
        "raw_messages_no_update",
        "ingest_requests_no_update",
        "memory_state_events_no_update",
        "memory_records_payload_no_update",
    }
)
_REQUIRED_V1_INDEXES = frozenset(
    {
        "idx_raw_events_user",
        "idx_memory_current",
        "idx_memory_user_type",
        "idx_memory_state_event_record",
    }
)

_EXTRACTION_V1_TABLES = frozenset(
    {"extraction_meta", "extraction_receipts", "proposal_origins"}
)
_EXTRACTION_V1_TRIGGERS = frozenset(
    {"extraction_receipts_no_update", "proposal_origins_no_update"}
)
_EXTRACTION_V1_INDEXES = frozenset(
    {"idx_extraction_receipt_digest", "idx_proposal_origins_receipt"}
)

# The only non-empty pre-governance database accepted for additive migration
# is the exact AML Retriever v1.1 storage base from commit cdae7db.  FTS5
# shadow tables are included so an arbitrary SQLite database cannot be
# mistaken for that baseline merely because it has a ``messages`` table.
_AML_LEGACY_LAYOUTS = {
    "messages": (
        ("id", "TEXT", 0, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("session_id", "TEXT", 0, None, 0),
        ("seq", "INTEGER", 1, None, 0),
        ("role", "TEXT", 0, None, 0),
        ("content", "TEXT", 1, None, 0),
        ("ts_ms", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("request_id", "TEXT", 0, None, 0),
        ("added_at", "TEXT", 1, None, 0),
    ),
    "views": (
        ("view_id", "TEXT", 0, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("session_id", "TEXT", 0, None, 0),
        ("view_type", "TEXT", 1, None, 0),
        ("content", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("source_ids", "TEXT", 1, None, 0),
        ("start_seq", "INTEGER", 1, None, 0),
        ("end_seq", "INTEGER", 1, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
    ),
    "requests": (
        ("request_id", "TEXT", 1, None, 1),
        ("user_id", "TEXT", 1, None, 2),
        ("session_id", "TEXT", 0, None, 0),
        ("message_ids", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ),
    "sessions": (
        ("user_id", "TEXT", 1, None, 1),
        ("session_id", "TEXT", 0, None, 2),
        ("msg_count", "INTEGER", 1, "0", 0),
        ("last_ts_ms", "INTEGER", 0, None, 0),
        ("seg_index", "INTEGER", 1, "0", 0),
        ("seg_start", "INTEGER", 1, "0", 0),
        ("seg_count", "INTEGER", 1, "0", 0),
    ),
    "fts": (
        ("text", "", 0, None, 0),
        ("doc_id", "", 0, None, 0),
        ("user_id", "", 0, None, 0),
        ("doc_type", "", 0, None, 0),
    ),
    "fts_data": (
        ("id", "INTEGER", 0, None, 1),
        ("block", "BLOB", 0, None, 0),
    ),
    "fts_idx": (
        ("segid", "", 1, None, 1),
        ("term", "", 1, None, 2),
        ("pgno", "", 0, None, 0),
    ),
    "fts_content": (
        ("id", "INTEGER", 0, None, 1),
        ("c0", "", 0, None, 0),
        ("c1", "", 0, None, 0),
        ("c2", "", 0, None, 0),
        ("c3", "", 0, None, 0),
    ),
    "fts_docsize": (
        ("id", "INTEGER", 0, None, 1),
        ("sz", "BLOB", 0, None, 0),
    ),
    "fts_config": (
        ("k", "", 1, None, 1),
        ("v", "", 0, None, 0),
    ),
}

_AML_LEGACY_INDEX_SQL = {
    "idx_msg_scope": "CREATE INDEX idx_msg_scope ON messages(user_id, session_id, seq)",
    "idx_msg_user": "CREATE INDEX idx_msg_user ON messages(user_id)",
    "idx_views_user": "CREATE INDEX idx_views_user ON views(user_id)",
}

_AML_LEGACY_FTS_SQL = """CREATE VIRTUAL TABLE fts USING fts5(
    text,
    doc_id UNINDEXED,
    user_id UNINDEXED,
    doc_type UNINDEXED
)"""

# Exact v1 layouts are part of the compatibility contract.  Merely finding a
# table with the expected name is insufficient: weaker NOT NULL/PK guarantees
# would silently invalidate user isolation, source binding, or audit order.
# Tuple fields: (name, declared type, NOT NULL, default, PK ordinal).
_GOVERNANCE_V1_LAYOUTS = {
    "governance_meta": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "raw_events": (
        ("id", "TEXT", 0, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("event_type", "TEXT", 1, None, 0),
        ("observed_at", "TEXT", 1, None, 0),
        ("recorded_at", "TEXT", 1, None, 0),
        ("authority", "TEXT", 1, None, 0),
        ("scope_json", "TEXT", 1, None, 0),
        ("source_locator", "TEXT", 1, None, 0),
        ("source_message_id", "TEXT", 1, None, 0),
    ),
    "memory_records": (
        ("id", "TEXT", 0, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("memory_key", "TEXT", 1, None, 0),
        ("memory_type", "TEXT", 1, None, 0),
        ("subject", "TEXT", 1, None, 0),
        ("content", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("source_event_ids", "TEXT", 1, None, 0),
        ("observed_at", "TEXT", 1, None, 0),
        ("valid_from", "TEXT", 0, None, 0),
        ("valid_until", "TEXT", 0, None, 0),
        ("authority", "TEXT", 1, None, 0),
        ("scope_json", "TEXT", 1, None, 0),
        ("confidence", "REAL", 0, None, 0),
        ("created_by", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("confirmed_by", "TEXT", 0, None, 0),
        ("confirmed_at", "TEXT", 0, None, 0),
        ("supersedes_record_id", "TEXT", 0, None, 0),
        ("state_reason", "TEXT", 1, "''", 0),
    ),
    "memory_state_events": (
        ("id", "TEXT", 0, None, 1),
        ("record_id", "TEXT", 1, None, 0),
        ("user_id", "TEXT", 1, None, 0),
        ("from_status", "TEXT", 1, None, 0),
        ("to_status", "TEXT", 1, None, 0),
        ("actor", "TEXT", 1, None, 0),
        ("actor_authority", "TEXT", 1, None, 0),
        ("reason", "TEXT", 1, None, 0),
        ("transitioned_at", "TEXT", 1, None, 0),
        ("related_record_id", "TEXT", 0, None, 0),
    ),
}

_GOVERNANCE_V1_TRIGGER_SQL = {
    "raw_events_no_update": """CREATE TRIGGER raw_events_no_update
        BEFORE UPDATE ON raw_events BEGIN
        SELECT RAISE(ABORT, 'raw_events are immutable'); END""",
    "raw_messages_no_update": """CREATE TRIGGER raw_messages_no_update
        BEFORE UPDATE ON messages BEGIN
        SELECT RAISE(ABORT, 'raw message evidence is immutable'); END""",
    "ingest_requests_no_update": """CREATE TRIGGER ingest_requests_no_update
        BEFORE UPDATE ON requests BEGIN
        SELECT RAISE(ABORT, 'ingest request receipts are immutable'); END""",
    "memory_state_events_no_update": """CREATE TRIGGER memory_state_events_no_update
        BEFORE UPDATE ON memory_state_events BEGIN
        SELECT RAISE(ABORT, 'memory_state_events are append-only'); END""",
    "memory_records_payload_no_update": """CREATE TRIGGER memory_records_payload_no_update
        BEFORE UPDATE OF
        user_id,memory_key,memory_type,subject,content,source_event_ids,
        observed_at,valid_from,valid_until,authority,scope_json,confidence,
        created_by,created_at,supersedes_record_id
        ON memory_records BEGIN
        SELECT RAISE(ABORT, 'derived memory payload is immutable'); END""",
}

_GOVERNANCE_V1_INDEX_SQL = {
    "idx_raw_events_user": (
        "CREATE INDEX idx_raw_events_user ON raw_events(user_id, observed_at)"
    ),
    "idx_memory_current": (
        "CREATE INDEX idx_memory_current "
        "ON memory_records(user_id, memory_key, status, valid_from, valid_until)"
    ),
    "idx_memory_user_type": (
        "CREATE INDEX idx_memory_user_type "
        "ON memory_records(user_id, memory_type, status)"
    ),
    "idx_memory_state_event_record": (
        "CREATE INDEX idx_memory_state_event_record "
        "ON memory_state_events(user_id, record_id, transitioned_at)"
    ),
}

_EXTRACTION_V1_LAYOUTS = {
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

_EXTRACTION_V1_TRIGGER_SQL = {
    "extraction_receipts_no_update": """CREATE TRIGGER extraction_receipts_no_update
        BEFORE UPDATE ON extraction_receipts BEGIN
        SELECT RAISE(ABORT, 'extraction receipts are immutable'); END""",
    "proposal_origins_no_update": """CREATE TRIGGER proposal_origins_no_update
        BEFORE UPDATE ON proposal_origins BEGIN
        SELECT RAISE(ABORT, 'proposal origins are immutable'); END""",
}

_EXTRACTION_V1_INDEX_SQL = {
    "idx_extraction_receipt_digest": (
        "CREATE INDEX idx_extraction_receipt_digest "
        "ON extraction_receipts(user_id,request_digest)"
    ),
    "idx_proposal_origins_receipt": (
        "CREATE INDEX idx_proposal_origins_receipt "
        "ON proposal_origins(user_id,idempotency_key,proposal_index)"
    ),
}


@dataclass(frozen=True)
class SchemaReport:
    """Safe, structured result of a read-only governance schema inspection."""

    status: str
    compatible: bool
    database_exists: bool
    read_only: bool
    current_version: int | None
    supported_version: int
    initialization_required: bool
    migration_required: bool
    reason: str
    missing_objects: tuple[str, ...] = ()
    extraction_status: str = "unknown"
    extraction_current_version: int | None = None
    extraction_supported_version: int = EXTRACTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["missing_objects"] = list(self.missing_objects)
        return data


def _report(
    *,
    status: str,
    compatible: bool,
    database_exists: bool,
    current_version: int | None,
    initialization_required: bool = False,
    migration_required: bool = False,
    reason: str,
    missing_objects: tuple[str, ...] = (),
    extraction_status: str = "unknown",
    extraction_current_version: int | None = None,
) -> SchemaReport:
    return SchemaReport(
        status=status,
        compatible=compatible,
        database_exists=database_exists,
        read_only=True,
        current_version=current_version,
        supported_version=GOVERNANCE_SCHEMA_VERSION,
        initialization_required=initialization_required,
        migration_required=migration_required,
        reason=reason,
        missing_objects=tuple(sorted(missing_objects)),
        extraction_status=extraction_status,
        extraction_current_version=extraction_current_version,
    )


def _parse_version(value: object) -> int:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise GovernanceError("governance schema version is invalid")
    return int(value)


def _table_layout(con: sqlite3.Connection, table: str) -> tuple:
    return tuple(
        (
            row[1],
            str(row[2]).upper(),
            int(row[3]),
            row[4],
            int(row[5]),
        )
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _governance_layouts_are_valid(con: sqlite3.Connection) -> bool:
    return all(
        _table_layout(con, table) == expected
        for table, expected in _GOVERNANCE_V1_LAYOUTS.items()
    )


def _governance_triggers_are_valid(con: sqlite3.Connection) -> bool:
    rows = {
        str(row[0]): _normalized_sql(row[1])
        for row in con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    return all(
        rows.get(name) == _normalized_sql(sql)
        for name, sql in _GOVERNANCE_V1_TRIGGER_SQL.items()
    )


def _named_sql_is_valid(
    con: sqlite3.Connection,
    *,
    object_type: str,
    expected: dict[str, str],
) -> bool:
    rows = {
        str(row[0]): _normalized_sql(row[1])
        for row in con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type=?", (object_type,)
        ).fetchall()
    }
    return all(rows.get(name) == _normalized_sql(sql) for name, sql in expected.items())


def _aml_legacy_is_exact(
    con: sqlite3.Connection,
    *,
    tables: set[str],
    triggers: set[str],
    indexes: set[str],
    views: set[str],
) -> bool:
    explicit_indexes = {name for name in indexes if not name.startswith("sqlite_")}
    if (
        tables != set(_AML_LEGACY_LAYOUTS)
        or triggers
        or views
        or explicit_indexes != set(_AML_LEGACY_INDEX_SQL)
    ):
        return False
    if any(
        _table_layout(con, table) != expected
        for table, expected in _AML_LEGACY_LAYOUTS.items()
    ):
        return False
    if not _named_sql_is_valid(
        con,
        object_type="index",
        expected=_AML_LEGACY_INDEX_SQL,
    ):
        return False
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts'"
    ).fetchone()
    return row is not None and _normalized_sql(row[0]) == _normalized_sql(
        _AML_LEGACY_FTS_SQL
    )


def _initialized_product_storage_is_exact(
    con: sqlite3.Connection,
    *,
    tables: set[str],
    triggers: set[str],
    indexes: set[str],
    views: set[str],
    extraction_ready: bool,
) -> bool:
    """Validate the complete single-purpose product schema inventory.

    A ready governance component is not sufficient if an AML storage table was
    dropped or a hostile trigger/view was added: writable open would otherwise
    recreate the table and silently lose idempotency/history.  Internal SQLite
    objects are ignored, while every application-owned object is exact.
    """

    expected_tables = (
        set(_AML_LEGACY_LAYOUTS)
        | set(_REQUIRED_V1_TABLES)
        | (set(_EXTRACTION_V1_TABLES) if extraction_ready else set())
    )
    expected_triggers = set(_REQUIRED_V1_TRIGGERS) | (
        set(_EXTRACTION_V1_TRIGGERS) if extraction_ready else set()
    )
    expected_indexes = (
        set(_AML_LEGACY_INDEX_SQL)
        | set(_REQUIRED_V1_INDEXES)
        | (set(_EXTRACTION_V1_INDEXES) if extraction_ready else set())
    )
    application_tables = {name for name in tables if not name.startswith("sqlite_")}
    explicit_indexes = {name for name in indexes if not name.startswith("sqlite_")}
    if (
        application_tables != expected_tables
        or triggers != expected_triggers
        or explicit_indexes != expected_indexes
        or views
    ):
        return False
    if any(
        _table_layout(con, table) != expected
        for table, expected in _AML_LEGACY_LAYOUTS.items()
    ):
        return False
    if not _named_sql_is_valid(
        con,
        object_type="index",
        expected=_AML_LEGACY_INDEX_SQL,
    ):
        return False
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts'"
    ).fetchone()
    return row is not None and _normalized_sql(row[0]) == _normalized_sql(
        _AML_LEGACY_FTS_SQL
    )


def _schema_is_empty(
    *,
    tables: set[str],
    triggers: set[str],
    indexes: set[str],
    views: set[str],
) -> bool:
    return not tables and not triggers and not indexes and not views


@dataclass(frozen=True)
class _ComponentReport:
    status: str
    compatible: bool
    current_version: int | None
    reason: str


def _inspect_extraction_component(
    con: sqlite3.Connection,
    *,
    tables: set[str],
    triggers: set[str],
    indexes: set[str],
) -> _ComponentReport:
    present = (
        tables & _EXTRACTION_V1_TABLES
        | triggers & _EXTRACTION_V1_TRIGGERS
        | indexes & _EXTRACTION_V1_INDEXES
    )
    if not present:
        return _ComponentReport(
            "uninitialized", True, 0, "extraction schema has not been initialized"
        )
    missing = (
        {f"table:{name}" for name in _EXTRACTION_V1_TABLES - tables}
        | {f"trigger:{name}" for name in _EXTRACTION_V1_TRIGGERS - triggers}
        | {f"index:{name}" for name in _EXTRACTION_V1_INDEXES - indexes}
    )
    if missing:
        return _ComponentReport(
            "corrupt", False, None, "extraction schema layout is incompatible"
        )
    if _table_layout(con, "extraction_meta") != _EXTRACTION_V1_LAYOUTS["extraction_meta"]:
        return _ComponentReport(
            "corrupt", False, None, "extraction schema layout is incompatible"
        )
    row = con.execute(
        "SELECT value FROM extraction_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return _ComponentReport(
            "corrupt", False, None, "extraction schema version row is missing"
        )
    try:
        current = _parse_version(row[0])
    except GovernanceError:
        return _ComponentReport(
            "corrupt", False, None, "extraction schema version is invalid"
        )
    if current > EXTRACTION_SCHEMA_VERSION:
        return _ComponentReport(
            "future", False, current, "extraction schema is newer than this runtime"
        )
    if current < EXTRACTION_SCHEMA_VERSION:
        return _ComponentReport(
            "corrupt", False, current, "extraction schema version is incomplete"
        )
    if any(
        _table_layout(con, table) != expected
        for table, expected in _EXTRACTION_V1_LAYOUTS.items()
    ):
        return _ComponentReport(
            "corrupt", False, current, "extraction schema layout is incompatible"
        )
    rows = {
        str(row[0]): _normalized_sql(row[1])
        for row in con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    if any(
        rows.get(name) != _normalized_sql(sql)
        for name, sql in _EXTRACTION_V1_TRIGGER_SQL.items()
    ):
        return _ComponentReport(
            "corrupt", False, current, "extraction schema immutability guards are missing"
        )
    if not _named_sql_is_valid(
        con,
        object_type="index",
        expected=_EXTRACTION_V1_INDEX_SQL,
    ):
        return _ComponentReport(
            "corrupt", False, current, "extraction schema indexes are incompatible"
        )
    return _ComponentReport("ready", True, current, "extraction schema is compatible")


def _object_names(con: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (object_type,)
        ).fetchall()
    }


def inspect_schema(db_path: str) -> SchemaReport:
    """Inspect an existing file database without creating or migrating it.

    ``:memory:`` has no persistent object to inspect and is reported as
    ``ephemeral``.  A missing path is not opened, so callers can prove that a
    doctor check did not accidentally create the requested database.
    """

    if not isinstance(db_path, str) or not db_path.strip():
        return _report(
            status="invalid_path",
            compatible=False,
            database_exists=False,
            current_version=None,
            reason="database path is required",
        )
    if db_path == ":memory:":
        return _report(
            status="ephemeral",
            compatible=True,
            database_exists=False,
            current_version=None,
            initialization_required=True,
            reason="in-memory database is initialized only when explicitly opened",
        )

    path = Path(db_path).expanduser()
    if not path.exists():
        return _report(
            status="missing",
            compatible=True,
            database_exists=False,
            current_version=None,
            initialization_required=True,
            reason="database does not exist; read-only inspection did not create it",
        )
    if not path.is_file():
        return _report(
            status="invalid_path",
            compatible=False,
            database_exists=True,
            current_version=None,
            reason="database path is not a regular file",
        )

    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return _report(
            status="unreadable",
            compatible=False,
            database_exists=True,
            current_version=None,
            reason="database could not be opened read-only",
        )
    try:
        con.execute("PRAGMA query_only=ON")
        tables = _object_names(con, "table")
        triggers = _object_names(con, "trigger")
        indexes = _object_names(con, "index")
        views = _object_names(con, "view")
        extraction = _inspect_extraction_component(
            con,
            tables=tables,
            triggers=triggers,
            indexes=indexes,
        )
        if not extraction.compatible:
            return _report(
                status=extraction.status,
                compatible=False,
                database_exists=True,
                current_version=None,
                reason=extraction.reason,
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        governance_present = bool(
            tables & _REQUIRED_V1_TABLES
            | triggers & _REQUIRED_V1_TRIGGERS
            | indexes & _REQUIRED_V1_INDEXES
        )
        if not governance_present:
            # Only an empty schema or the exact cdae7db AML storage base may
            # enter the additive governance migration.  A ready extraction
            # component without governance is never a supported predecessor.
            if extraction.status != "uninitialized":
                return _report(
                    status="corrupt",
                    compatible=False,
                    database_exists=True,
                    current_version=None,
                    reason="governance schema is missing from an initialized extraction database",
                    extraction_status=extraction.status,
                    extraction_current_version=extraction.current_version,
                )
            if not (
                _schema_is_empty(
                    tables=tables,
                    triggers=triggers,
                    indexes=indexes,
                    views=views,
                )
                or _aml_legacy_is_exact(
                    con,
                    tables=tables,
                    triggers=triggers,
                    indexes=indexes,
                    views=views,
                )
            ):
                return _report(
                    status="corrupt",
                    compatible=False,
                    database_exists=True,
                    current_version=None,
                    reason="governance schema predecessor is not an exact AML legacy database",
                    extraction_status=extraction.status,
                    extraction_current_version=extraction.current_version,
                )
            return _report(
                status="uninitialized",
                compatible=True,
                database_exists=True,
                current_version=0,
                initialization_required=True,
                migration_required=True,
                reason="governance schema has not been initialized",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        missing = tuple(
            sorted(
                {f"table:{name}" for name in _REQUIRED_V1_TABLES - tables}
                | {f"trigger:{name}" for name in _REQUIRED_V1_TRIGGERS - triggers}
                | {f"index:{name}" for name in _REQUIRED_V1_INDEXES - indexes}
            )
        )
        if missing:
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=None,
                reason="governance schema is missing required objects",
                missing_objects=missing,
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if _table_layout(con, "governance_meta") != _GOVERNANCE_V1_LAYOUTS["governance_meta"]:
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=None,
                reason="governance schema metadata layout is invalid",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        row = con.execute(
            "SELECT value FROM governance_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=None,
                reason="governance schema version row is missing",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        try:
            current = _parse_version(row[0])
        except GovernanceError:
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=None,
                reason="governance schema version is invalid",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if current > GOVERNANCE_SCHEMA_VERSION:
            return _report(
                status="future",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="governance schema is newer than this runtime",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if current < GOVERNANCE_SCHEMA_VERSION:
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="governance schema version is incomplete",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if not _governance_layouts_are_valid(con):
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="governance schema layout is incompatible",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if not _governance_triggers_are_valid(con):
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="governance schema immutability trigger is incompatible",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if not _named_sql_is_valid(
            con,
            object_type="index",
            expected=_GOVERNANCE_V1_INDEX_SQL,
        ):
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="governance schema indexes are incompatible",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if not _initialized_product_storage_is_exact(
            con,
            tables=tables,
            triggers=triggers,
            indexes=indexes,
            views=views,
            extraction_ready=extraction.status == "ready",
        ):
            return _report(
                status="corrupt",
                compatible=False,
                database_exists=True,
                current_version=current,
                reason="product storage schema inventory is incompatible",
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        if extraction.status != "ready":
            return _report(
                status="migration_required",
                compatible=True,
                database_exists=True,
                current_version=current,
                initialization_required=True,
                migration_required=True,
                reason=extraction.reason,
                extraction_status=extraction.status,
                extraction_current_version=extraction.current_version,
            )
        return _report(
            status="ready",
            compatible=True,
            database_exists=True,
            current_version=current,
            reason="governance schema is compatible",
            extraction_status=extraction.status,
            extraction_current_version=extraction.current_version,
        )
    except sqlite3.Error:
        return _report(
            status="corrupt",
            compatible=False,
            database_exists=True,
            current_version=None,
            reason="governance schema could not be inspected",
        )
    finally:
        con.close()


def assert_writable_open_compatible(db_path: str) -> SchemaReport:
    """Fail closed before a writable ``RetrieverDB`` connection is opened."""

    report = inspect_schema(db_path)
    if not report.compatible:
        if report.reason.startswith("extraction schema"):
            raise ExtractionValidationError(report.reason)
        raise GovernanceError(report.reason)
    return report


__all__ = ["SchemaReport", "inspect_schema", "assert_writable_open_compatible"]
