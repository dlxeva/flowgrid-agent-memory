"""Zero-dependency, transport-neutral MCP tool core.

This module deliberately imports no MCP SDK.  It binds every operation to a
host-created :class:`TrustedPrincipal` before opening or touching the memory
facade, and exposes only the four safe current-state operations used by the
stdio adapter.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock, RLock
from typing import Callable

from .api import ApiError
from . import context as _context_contract
from .auth import (
    OPERATION_COMPILE_CURRENT,
    OPERATION_EXTRACT,
    OPERATION_INGEST,
    OPERATION_READ_CURRENT,
    AuthorizationError,
    TrustedPrincipal,
    authorize_operation,
)
from .extraction import DirectiveMemoryExtractor, ExtractionError
from .facade import FlowGridMemory
from .governance import GovernanceError
from .host_lifecycle_reads import LifecycleMemoryReader, HostReadError, DeferredReadMemory as _DeferredReadMemory
from .host_lifecycle_writes import LifecycleCandidateWriter, HostWriteError, DeferredCandidateMemory


TOOL_NAMES = (
    "memory_ingest_events",
    "memory_extract_candidates",
    "memory_query_current",
    "memory_compile_context",
)

DEFAULT_MAX_CHARS = 32_768
MAX_MAX_CHARS = 1_000_000
MAX_RECORDS = getattr(_context_contract, 'MAX_CONTEXT_RECORDS', 1_000)
MAX_MESSAGES = 1_000

_DB_LOCKS_GUARD = Lock()
_DB_OPEN_LOCKS: dict[str, RLock] = {}


def _db_open_lock(db_path: str) -> RLock:
    key = db_path if db_path == ":memory:" else str(Path(db_path).resolve())
    with _DB_LOCKS_GUARD:
        lock = _DB_OPEN_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _DB_OPEN_LOCKS[key] = lock
        return lock


def _error(code: str) -> dict[str, object]:
    """Return one of the fixed, non-sensitive public failure envelopes."""

    if code not in {
        "access_denied",
        "invalid_request",
        "operation_failed",
        "token_budget_unsupported",
        "tool_closed",
    }:
        code = "operation_failed"
    return {"status": "error", "error": {"code": code}}


def _safe_string(
    value: object,
    *,
    nullable: bool = False,
    maximum: int = 512,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("invalid string")
    return normalized


def _safe_limit(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid limit")
    if value < minimum or value > maximum:
        raise ValueError("invalid limit")
    return value


def _safe_messages(value: object) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("invalid messages")
    messages = list(value)
    if not messages or len(messages) > MAX_MESSAGES:
        raise ValueError("invalid messages")
    normalized: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise ValueError("invalid message")
        role = _safe_string(item["role"], maximum=64)
        content = item["content"]
        if not isinstance(content, str) or not content or len(content) > 1_000_000:
            raise ValueError("invalid message")
        normalized.append({"role": role, "content": content})
    return normalized


def _optional_query_string(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _safe_string(value, maximum=maximum)


class GovernedMCPTools:
    """Safe four-tool core with an explicit database and trusted identity.

    The facade is opened lazily, after authorization succeeds.  This preserves
    the important property that a denied call cannot create or migrate the
    configured database.  Instances are closeable and usable as context
    managers; calls after close return a fixed envelope.
    """

    def __init__(self, *, db_path: str, principal: TrustedPrincipal,
                 lifecycle_reader_factory=None, lifecycle_writer_factory=None):
        if (
            not isinstance(db_path, str)
            or not db_path.strip()
            or (db_path != ":memory:" and not Path(db_path).is_absolute())
        ):
            raise ValueError("db_path must be explicit")
        if not isinstance(principal, TrustedPrincipal):
            raise TypeError("principal must be TrustedPrincipal")
        if lifecycle_reader_factory is not None and (not callable(lifecycle_reader_factory) or db_path == ':memory:'):
            raise ValueError('lifecycle reader requires trusted factory and existing file')
        if lifecycle_writer_factory is not None and (not callable(lifecycle_writer_factory) or lifecycle_reader_factory is None):
            raise ValueError('candidate writer requires trusted reader and writer factories')
        self._reader_factory = lifecycle_reader_factory
        self._writer_factory = lifecycle_writer_factory
        self._writer = None
        self._reader = None
        self._read_memory = (DeferredCandidateMemory(self._active) if lifecycle_writer_factory is not None
            else _DeferredReadMemory(self._active))
        self._db_path = db_path
        self._principal = principal
        self._memory: FlowGridMemory | None = None
        self._open_lock = _db_open_lock(db_path)
        self._closed = False

    def _active(self) -> FlowGridMemory:
        if self._closed:
            raise RuntimeError("tool core is closed")
        if self._memory is None:
            # Product migrations are additive but their first-open inspection
            # observes several schema objects.  Serialize first open across
            # local adapter instances so one never inspects another's partial
            # initialization transaction.
            with self._open_lock:
                if self._memory is None:
                    if self._writer_factory is not None:
                        # Refuse missing/uninitialized/migration-requiring inputs
                        # before a normal writable open. Path stability and all
                        # schema/file writers remain a trusted host responsibility.
                        with FlowGridMemory.open_read_only(db_path=self._db_path):
                            pass
                        self._memory = FlowGridMemory(db_path=self._db_path)
                    else:
                        self._memory = (FlowGridMemory.open_read_only(db_path=self._db_path)
                            if self._reader_factory is not None else FlowGridMemory(db_path=self._db_path))
        return self._memory

    def _guarded_reader(self):
        if self._reader is None:
            reader = self._reader_factory(self._read_memory)
            if not isinstance(reader, LifecycleMemoryReader) or reader.memory is not self._read_memory:
                raise HostReadError('host_reader_binding_required')
            self._reader = reader
        return self._reader

    def _guarded_writer(self):
        reader = self._guarded_reader()
        if self._writer is None:
            writer = self._writer_factory(self._read_memory)
            names = ('refresh_principal','serialize_user','is_user_active','is_source_active')
            if (not isinstance(writer,LifecycleCandidateWriter) or writer.memory is not self._read_memory
                    or reader.enabled is not True or writer.enabled is not True
                    or any(getattr(writer,name)!=getattr(reader,name) for name in names)):
                raise HostWriteError('host_writer_binding_required')
            self._writer = writer
        return self._writer

    def _authorized(
        self,
        *,
        user_id: str,
        scope: Mapping[str, str] | None,
        operation: str,
    ):
        if self._closed:
            raise RuntimeError("tool core is closed")
        # This opt-in transport mode is read-only until a separate authenticated
        # live writer gate is implemented. Request bodies cannot enable it.
        if self._reader_factory is not None and self._writer_factory is None and operation in {OPERATION_INGEST, OPERATION_EXTRACT}:
            raise AuthorizationError()
        return authorize_operation(
            self._principal,
            user_id=user_id,
            requested_scope=scope,
            operation=operation,
        )

    @staticmethod
    def _call(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return operation()
        except (AuthorizationError, HostReadError, HostWriteError):
            return _error("access_denied")
        except (ApiError, ExtractionError, GovernanceError, TypeError, ValueError):
            return _error("invalid_request")
        except RuntimeError as exc:
            if str(exc) == "tool core is closed":
                return _error("tool_closed")
            return _error("operation_failed")
        except Exception:
            return _error("operation_failed")

    def memory_ingest_events(
        self,
        *,
        request_id: str,
        user_id: str,
        messages: Sequence[Mapping[str, object]],
        session_id: str | None = None,
        scope: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        def invoke() -> dict[str, object]:
            access = self._authorized(
                user_id=user_id,
                scope=scope,
                operation=OPERATION_INGEST,
            )
            safe_request_id = _safe_string(request_id, maximum=256)
            safe_user_id = _safe_string(user_id, maximum=256)
            safe_session_id = _safe_string(session_id, nullable=True, maximum=256)
            safe_messages = _safe_messages(messages)
            options = dict(
                request_id=safe_request_id,
                user_id=safe_user_id,
                session_id=safe_session_id,
                messages=safe_messages,
            )
            if self._writer_factory is not None:
                receipt = self._guarded_writer().ingest_raw_events(principal=self._principal,
                    requested_scope=access.effective_scope, **options)
            else:
                receipt = self._active().ingest_raw_events(trusted_scope=access.effective_scope, **options)
            return {
                "status": "success",
                "idempotent": receipt.idempotent,
                "event_count": len(receipt.raw_event_ids),
                "raw_event_ids": list(receipt.raw_event_ids),
            }

        return self._call(invoke)

    def memory_extract_candidates(
        self,
        *,
        user_id: str,
        raw_event_ids: Sequence[str],
        idempotency_key: str,
        scope: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        def invoke() -> dict[str, object]:
            access = self._authorized(
                user_id=user_id,
                scope=scope,
                operation=OPERATION_EXTRACT,
            )
            safe_user_id = _safe_string(user_id, maximum=256)
            safe_key = _safe_string(idempotency_key, maximum=256)
            if isinstance(raw_event_ids, (str, bytes)) or not isinstance(
                raw_event_ids, Sequence
            ):
                raise ValueError("invalid raw event ids")
            safe_event_ids = [
                _safe_string(value, maximum=256) for value in raw_event_ids
            ]
            if not safe_event_ids or len(safe_event_ids) > MAX_MESSAGES:
                raise ValueError("invalid raw event ids")
            options = dict(
                user_id=safe_user_id,
                raw_event_ids=safe_event_ids,
                idempotency_key=safe_key,
                extractor=DirectiveMemoryExtractor(),
            )
            if self._writer_factory is not None:
                receipt = self._guarded_writer().extract_candidates(principal=self._principal,
                    requested_scope=access.effective_scope, **options)
            else:
                receipt = self._active().extract_candidates(trusted_scope=access.effective_scope, **options)
            return {
                "status": receipt.status,
                "idempotent": receipt.idempotent,
                "proposal_count": receipt.proposal_count,
                "record_ids": list(receipt.record_ids),
            }

        return self._call(invoke)

    def _compile_current(
        self,
        *,
        operation: str,
        user_id: str,
        memory_key: str | None,
        query: str | None,
        scope: Mapping[str, str] | None,
        max_records: int,
        max_chars: int,
    ) -> dict[str, object]:
        access = self._authorized(user_id=user_id, scope=scope, operation=operation)
        safe_user_id = _safe_string(user_id, maximum=256)
        safe_key = _optional_query_string(memory_key, maximum=256)
        safe_query = _optional_query_string(query, maximum=10_000)
        safe_records = _safe_limit(max_records, minimum=1, maximum=MAX_RECORDS)
        safe_chars = _safe_limit(max_chars, minimum=0, maximum=MAX_MAX_CHARS)
        options = dict(
            user_id=safe_user_id,
            memory_key=safe_key,
            query=safe_query,
            mode="current",
            scope=access.effective_scope,
            # MCP exposes no audit/historical permission.  Current tools are
            # deliberately pinned to now so superseded/rejected/deleted memory
            # cannot be revived through a historical cutoff.
            as_of=None,
            max_records=safe_records,
            max_chars=safe_chars,
        )
        if self._reader_factory is not None:
            pack = self._guarded_reader().compile_context(principal=self._principal, **options)
        else:
            pack = self._active().compile_context(access_context=access.access_context,
                disclosure_policy=access.disclosure_policy, **options)
        # ContextPack is the stable static public-field allowlist.  Never
        # serialize CurrentStateResult or other internal lifecycle objects.
        return pack.to_dict()

    def memory_query_current(
        self,
        *,
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        scope: Mapping[str, str] | None = None,
        max_records: int = 100,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict[str, object]:
        return self._call(
            lambda: self._compile_current(
                operation=OPERATION_READ_CURRENT,
                user_id=user_id,
                memory_key=memory_key,
                query=query,
                scope=scope,
                max_records=max_records,
                max_chars=max_chars,
            )
        )

    def memory_compile_context(
        self,
        *,
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        scope: Mapping[str, str] | None = None,
        max_records: int = 100,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        def invoke() -> dict[str, object]:
            # The adapter has no model/version-bound exact tokenizer.  A token
            # request is therefore rejected instead of approximated.
            self._authorized(
                user_id=user_id,
                scope=scope,
                operation=OPERATION_COMPILE_CURRENT,
            )
            if max_tokens is not None:
                return _error("token_budget_unsupported")
            return self._compile_current(
                operation=OPERATION_COMPILE_CURRENT,
                user_id=user_id,
                memory_key=memory_key,
                query=query,
                scope=scope,
                max_records=max_records,
                max_chars=max_chars,
            )

        return self._call(invoke)

    def close(self) -> None:
        memory, self._memory = self._memory, None
        self._reader = None
        self._writer = None
        self._closed = True
        if memory is not None:
            memory.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "GovernedMCPTools":
        if self._closed:
            raise RuntimeError("tool core is closed")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = [
    "TOOL_NAMES",
    "DEFAULT_MAX_CHARS",
    "MAX_MAX_CHARS",
    "GovernedMCPTools",
]
