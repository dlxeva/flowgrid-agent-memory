"""Stable, transport-neutral facade for the governed local memory product.

This facade intentionally does not expose ``RetrieverDB`` or a generic SQL
escape hatch.  Future REST and MCP adapters can bind authenticated principals
to :class:`AccessContext` and then call these typed operations without gaining
an alternate path around current-state resolution, disclosure policy, or
owner-authority transitions.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Mapping, Sequence

from .access import AccessContext, DisclosurePolicy, authorize_memory_read
from .api import MemoryService
from .compiler import CompilationReceipt
from .config import RetrieverConfig
from .context import ContextPack, TokenCounter
from .extraction import MemoryExtractor
from .governance import (
    GOVERNANCE_AUTHORITIES,
    CurrentStateResult,
    GovernanceError,
    MemoryRecord,
    raw_event_id_for_message,
)


@dataclass(frozen=True)
class IngestReceipt:
    request_id: str
    user_id: str
    session_id: str | None
    raw_event_ids: tuple[str, ...]
    idempotent: bool
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "raw_event_ids": list(self.raw_event_ids),
            "idempotent": self.idempotent,
            "status": self.status,
        }


@dataclass(frozen=True)
class AuthorizedMemoryResult:
    allowed: bool
    reason: str
    state: CurrentStateResult | None

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "state": self.state.to_dict() if self.state is not None else None,
        }


@dataclass(frozen=True)
class PrivacyEraseReceipt:
    user_id: str
    deleted_messages: int
    deleted_views: int
    deleted_raw_events: int
    deleted_memory_records: int
    deleted_extraction_receipts: int
    deleted_proposal_origins: int

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "deleted_messages": self.deleted_messages,
            "deleted_views": self.deleted_views,
            "deleted_raw_events": self.deleted_raw_events,
            "deleted_memory_records": self.deleted_memory_records,
            "deleted_extraction_receipts": self.deleted_extraction_receipts,
            "deleted_proposal_origins": self.deleted_proposal_origins,
        }


class FlowGridMemory:
    """Governed single-process memory facade with explicit close semantics.

    ``db_path`` is mandatory.  Pass ``":memory:"`` explicitly for an
    ephemeral instance; no caller is silently routed to a working-directory
    database.  Constructing this class is a writable open and may initialize
    or additively migrate a compatible database.
    """

    def __init__(self, *, db_path: str, config: RetrieverConfig | None = None):
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path must be an explicit non-empty path or ':memory:'")
        cfg = copy.deepcopy(config) if config is not None else RetrieverConfig()
        cfg.db_path = db_path
        self._service: MemoryService | None = MemoryService(cfg)

    def _active(self) -> MemoryService:
        service = self._service
        if service is None:
            raise RuntimeError("FlowGridMemory is closed")
        return service

    def ingest_raw_events(
        self,
        *,
        request_id: str,
        user_id: str,
        session_id: str | None,
        messages: Sequence[Mapping[str, object]],
        trusted_scope: Mapping[str, str] | None = None,
    ) -> IngestReceipt:
        result = self._active().add(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            messages=[dict(item) for item in messages],
            trusted_scope=dict(trusted_scope or {}),
            exact_replay=True,
        )
        return IngestReceipt(
            request_id=result.request_id,
            user_id=result.user_id,
            session_id=result.session_id,
            raw_event_ids=tuple(raw_event_id_for_message(item) for item in result.message_ids),
            idempotent=result.idempotent,
            status=result.status,
        )

    def extract_candidates(
        self,
        *,
        user_id: str,
        raw_event_ids: Sequence[str],
        idempotency_key: str,
        trusted_scope: Mapping[str, str] | None = None,
        extractor: MemoryExtractor | None = None,
    ) -> CompilationReceipt:
        """Extract candidate-only records; confirmation is never implicit."""

        return self._active().compile_events(
            user_id=user_id,
            raw_event_ids=list(raw_event_ids),
            idempotency_key=idempotency_key,
            trusted_scope=dict(trusted_scope or {}),
            extractor=extractor,
        )

    def propose_memory(
        self,
        *,
        user_id: str,
        memory_key: str,
        content: str,
        source_event_ids: Sequence[str],
        status: str,
        authority: str,
        created_by: str,
        memory_type: str = "fact",
        subject: str | None = None,
        scope: Mapping[str, str] | None = None,
        observed_at: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        confidence: float | None = None,
        supersedes_record_id: str | None = None,
        state_reason: str = "",
    ) -> MemoryRecord:
        if status not in {"candidate", "inferred"}:
            raise GovernanceError("facade proposals must start as candidate or inferred")
        return self._active().propose_memory(
            user_id=user_id,
            memory_key=memory_key,
            content=content,
            source_event_ids=list(source_event_ids),
            memory_type=memory_type,
            subject=subject,
            status=status,
            authority=authority,
            scope=dict(scope or {}),
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            confidence=confidence,
            created_by=created_by,
            supersedes_record_id=supersedes_record_id,
            state_reason=state_reason,
        )

    def transition_memory(
        self,
        *,
        user_id: str,
        record_id: str,
        target_status: str,
        actor: str,
        actor_authority: str,
        reason: str,
        related_record_id: str | None = None,
    ) -> MemoryRecord:
        return self._active().transition_memory(
            record_id=record_id,
            target_status=target_status,
            actor=actor,
            actor_authority=actor_authority,
            reason=reason,
            user_id=user_id,
            related_record_id=related_record_id,
        )

    def query_memory(
        self,
        *,
        user_id: str,
        access_context: AccessContext,
        memory_key: str | None = None,
        query: str | None = None,
        mode: str = "current",
        scope: Mapping[str, str] | None = None,
        as_of: str | None = None,
        max_records: int = 100,
        disclosure_policy: DisclosurePolicy | None = None,
    ) -> AuthorizedMemoryResult:
        """Authorize before current/audit retrieval and return no body on denial."""

        policy = disclosure_policy if disclosure_policy is not None else DisclosurePolicy()
        if not isinstance(policy, DisclosurePolicy):
            return AuthorizedMemoryResult(False, "access_denied", None)
        decision = authorize_memory_read(
            access_context,
            user_id=user_id,
            requested_scope=dict(scope or {}),
            mode=mode,
            disclosure_policy=policy,
        )
        if not decision.allowed:
            return AuthorizedMemoryResult(False, decision.reason, None)
        state = self._active().search_governed(
            user_id=user_id,
            memory_key=memory_key,
            query=query,
            mode=mode,
            scope=dict(decision.effective_scope),
            as_of=as_of,
            max_records=max_records,
        )
        return AuthorizedMemoryResult(True, "authorized", state)

    def query_current(self, **kwargs) -> AuthorizedMemoryResult:
        return self.query_memory(mode="current", **kwargs)

    def query_audit(self, **kwargs) -> AuthorizedMemoryResult:
        return self.query_memory(mode="audit", **kwargs)

    def compile_context(
        self,
        *,
        user_id: str,
        access_context: AccessContext,
        memory_key: str | None = None,
        query: str | None = None,
        mode: str = "current",
        scope: Mapping[str, str] | None = None,
        as_of: str | None = None,
        max_records: int = 100,
        max_chars: int | None = None,
        max_tokens: int | None = None,
        token_counter: TokenCounter | object | None = None,
        disclosure_policy: DisclosurePolicy | None = None,
    ) -> ContextPack:
        return self._active().compile_context(
            user_id=user_id,
            access_context=access_context,
            memory_key=memory_key,
            query=query,
            mode=mode,
            scope=dict(scope or {}),
            as_of=as_of,
            max_records=max_records,
            max_chars=max_chars,
            max_tokens=max_tokens,
            token_counter=token_counter,
            disclosure_policy=disclosure_policy,
        )

    def erase_user(
        self,
        *,
        user_id: str,
        actor: str,
        actor_authority: str,
        reason: str,
    ) -> PrivacyEraseReceipt:
        """Physically erase one user after an explicit governance authority gate."""

        if actor_authority not in GOVERNANCE_AUTHORITIES:
            raise GovernanceError("privacy erase requires user/owner/policy authority")
        if not isinstance(actor, str) or not actor.strip():
            raise GovernanceError("privacy erase actor is required")
        if not isinstance(reason, str) or not reason.strip():
            raise GovernanceError("privacy erase reason is required")
        result = self._active().delete_user(user_id)
        return PrivacyEraseReceipt(
            user_id=str(result["user_id"]),
            deleted_messages=int(result["deleted_messages"]),
            deleted_views=int(result["deleted_views"]),
            deleted_raw_events=int(result["deleted_raw_events"]),
            deleted_memory_records=int(result["deleted_memory_records"]),
            deleted_extraction_receipts=int(result["deleted_extraction_receipts"]),
            deleted_proposal_origins=int(result["deleted_proposal_origins"]),
        )

    def close(self) -> None:
        service, self._service = self._service, None
        if service is not None:
            service.close()

    @property
    def closed(self) -> bool:
        return self._service is None

    def __enter__(self) -> "FlowGridMemory":
        self._active()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = [
    "FlowGridMemory",
    "IngestReceipt",
    "AuthorizedMemoryResult",
    "PrivacyEraseReceipt",
]
