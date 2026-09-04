"""Local owner-review channel for governed memory candidates.

The product transports intentionally expose no administrative review tool. This
module provides the human-side counterpart for a trusted local host: inspect
candidate/inferred records with their immutable evidence, then apply an
explicit owner confirmation or rejection through :class:`FlowGridMemory`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .access import PERMISSION_AUDIT, PERMISSION_READ, AccessContext, DisclosurePolicy
from .facade import FlowGridMemory
from .governance import MemoryRecord, RawEvent


OWNER_REVIEW_PURPOSE = "local owner review"
REVIEWABLE_STATUSES = frozenset({"candidate", "inferred"})
REVIEW_DECISIONS = frozenset({"confirm", "reject"})
MAX_REVIEW_ITEMS = 100
MAX_AUDIT_RECORDS = 10_000
_SCOPE_FIELDS = frozenset(
    {"tenant", "user", "project", "agent", "session", "repository"}
)


class OwnerReviewError(ValueError):
    """A local review request could not be completed safely."""


def _safe_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise OwnerReviewError(f"{field} is required")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise OwnerReviewError(f"{field} is invalid")
    return normalized


def normalize_review_scope(
    *,
    user_id: str,
    scope: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return an exact review scope with the concrete user bound into it."""

    if scope is not None and not isinstance(scope, Mapping):
        raise OwnerReviewError("scope is invalid")
    normalized: dict[str, str] = {}
    for key, value in dict(scope or {}).items():
        if not isinstance(key, str) or key not in _SCOPE_FIELDS:
            raise OwnerReviewError("scope is invalid")
        if key in normalized:
            raise OwnerReviewError("scope is invalid")
        normalized[key] = _safe_text(value, field=f"scope.{key}", maximum=256)
    scoped_user = normalized.get("user")
    if scoped_user is not None and scoped_user != user_id:
        raise OwnerReviewError("scope is invalid")
    normalized["user"] = user_id
    return dict(sorted(normalized.items()))


@dataclass(frozen=True)
class OwnerReviewEvidence:
    source_event_id: str
    role: str
    authority: str
    observed_at: str
    scope: dict[str, str]
    source_locator: str
    content: str
    compatible: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "role": self.role,
            "authority": self.authority,
            "observed_at": self.observed_at,
            "scope": dict(self.scope),
            "source_locator": self.source_locator,
            "content": self.content,
            "compatible": self.compatible,
        }


@dataclass(frozen=True)
class OwnerReviewItem:
    record_id: str
    memory_key: str
    memory_type: str
    subject: str
    content: str
    current_status: str
    authority: str
    scope: dict[str, str]
    observed_at: str
    valid_from: str | None
    valid_until: str | None
    confidence: float | None
    supersedes_record_id: str | None
    state_reason: str
    evidence_complete: bool
    evidence: tuple[OwnerReviewEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "memory_key": self.memory_key,
            "memory_type": self.memory_type,
            "subject": self.subject,
            "content": self.content,
            "current_status": self.current_status,
            "authority": self.authority,
            "scope": dict(self.scope),
            "observed_at": self.observed_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "confidence": self.confidence,
            "supersedes_record_id": self.supersedes_record_id,
            "state_reason": self.state_reason,
            "evidence_complete": self.evidence_complete,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class OwnerReviewQueue:
    user_id: str
    scope: dict[str, str]
    total_pending: int
    returned_count: int
    has_more: bool
    items: tuple[OwnerReviewItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "mode": "review_queue",
            "user_id": self.user_id,
            "scope": dict(self.scope),
            "total_pending": self.total_pending,
            "returned_count": self.returned_count,
            "has_more": self.has_more,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class OwnerReviewDecision:
    record_id: str
    decision: str
    previous_status: str
    current_status: str
    actor: str
    evidence_verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "mode": "review_decision",
            "record_id": self.record_id,
            "decision": self.decision,
            "previous_status": self.previous_status,
            "current_status": self.current_status,
            "actor": self.actor,
            "evidence_verified": self.evidence_verified,
        }


class OwnerReviewSession:
    """Inspect and decide exact-scope candidates on one trusted local host."""

    def __init__(
        self,
        *,
        memory: FlowGridMemory,
        user_id: str,
        actor: str,
        scope: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(memory, FlowGridMemory):
            raise TypeError("memory must be FlowGridMemory")
        self._memory = memory
        self.user_id = _safe_text(user_id, field="user_id", maximum=256)
        self.actor = _safe_text(actor, field="actor", maximum=256)
        self.scope = normalize_review_scope(user_id=self.user_id, scope=scope)
        self._access = AccessContext(
            principal_id=self.actor,
            authority="owner",
            scopes=self.scope,
            permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
            purpose=OWNER_REVIEW_PURPOSE,
            allowed_users=frozenset({self.user_id}),
        )
        self._policy = DisclosurePolicy(
            allowed_audit_purposes=frozenset({OWNER_REVIEW_PURPOSE})
        )

    def _audit_state(self):
        result = self._memory.query_audit(
            user_id=self.user_id,
            access_context=self._access,
            scope=self.scope,
            max_records=MAX_AUDIT_RECORDS,
            disclosure_policy=self._policy,
        )
        if not result.allowed or result.state is None:
            raise OwnerReviewError("review access denied")
        state = result.state
        # Older result envelopes do not expose truncation. Reaching the hard
        # audit ceiling is therefore treated as incomplete rather than silently
        # claiming an exhaustive owner queue.
        if bool(getattr(state, "truncated", False)) or len(state.records) >= MAX_AUDIT_RECORDS:
            raise OwnerReviewError("review audit window is incomplete")
        return state

    @staticmethod
    def _evidence_compatible(record: MemoryRecord, event: RawEvent) -> bool:
        return (
            event.user_id == record.user_id
            and all(event.scope.get(key) == value for key, value in record.scope.items())
        )

    def _item(
        self,
        record: MemoryRecord,
        *,
        raw_by_id: dict[str, RawEvent],
    ) -> OwnerReviewItem:
        evidence: list[OwnerReviewEvidence] = []
        for source_event_id in record.source_event_ids:
            event = raw_by_id.get(source_event_id)
            if event is None:
                continue
            compatible = self._evidence_compatible(record, event)
            evidence.append(
                OwnerReviewEvidence(
                    source_event_id=event.id,
                    role=event.role,
                    authority=event.authority,
                    observed_at=event.observed_at,
                    scope=dict(event.scope),
                    source_locator=event.source_locator,
                    content=event.content,
                    compatible=compatible,
                )
            )
        evidence_complete = bool(record.source_event_ids) and (
            len(record.source_event_ids) == len(set(record.source_event_ids))
            and len(evidence) == len(record.source_event_ids)
            and all(item.compatible for item in evidence)
        )
        return OwnerReviewItem(
            record_id=record.id,
            memory_key=record.memory_key,
            memory_type=record.memory_type,
            subject=record.subject,
            content=record.content,
            current_status=record.current_status,
            authority=record.authority,
            scope=dict(record.scope),
            observed_at=record.observed_at,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
            confidence=record.confidence,
            supersedes_record_id=record.supersedes_record_id,
            state_reason=record.state_reason,
            evidence_complete=evidence_complete,
            evidence=tuple(evidence),
        )

    def _pending_items(self) -> list[OwnerReviewItem]:
        state = self._audit_state()
        raw_by_id = {event.id: event for event in state.raw_events}
        records = [
            record
            for record in state.records
            if record.current_status in REVIEWABLE_STATUSES
            and dict(record.scope) == self.scope
        ]
        records.sort(key=lambda item: (item.created_at, item.memory_key, item.id))
        return [self._item(record, raw_by_id=raw_by_id) for record in records]

    def list_pending(
        self,
        *,
        limit: int = 20,
        record_id: str | None = None,
    ) -> OwnerReviewQueue:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REVIEW_ITEMS:
            raise OwnerReviewError("review limit is invalid")
        selected_id = None
        if record_id is not None:
            selected_id = _safe_text(record_id, field="record_id", maximum=256)
        pending = self._pending_items()
        if selected_id is not None:
            pending = [item for item in pending if item.record_id == selected_id]
        total = len(pending)
        returned = tuple(pending[:limit])
        return OwnerReviewQueue(
            user_id=self.user_id,
            scope=self.scope,
            total_pending=total,
            returned_count=len(returned),
            has_more=total > len(returned),
            items=returned,
        )

    def decide(
        self,
        *,
        record_id: str,
        decision: str,
        reason: str,
    ) -> OwnerReviewDecision:
        safe_record_id = _safe_text(record_id, field="record_id", maximum=256)
        safe_decision = _safe_text(decision, field="decision", maximum=32).casefold()
        safe_reason = _safe_text(reason, field="reason", maximum=2_000)
        if safe_decision not in REVIEW_DECISIONS:
            raise OwnerReviewError("review decision is invalid")
        matching = [
            item for item in self._pending_items() if item.record_id == safe_record_id
        ]
        if len(matching) != 1:
            raise OwnerReviewError("review target is unavailable")
        target = matching[0]
        if safe_decision == "confirm" and not target.evidence_complete:
            raise OwnerReviewError("confirmation evidence is incomplete")
        target_status = "confirmed" if safe_decision == "confirm" else "rejected"
        updated = self._memory.transition_memory(
            user_id=self.user_id,
            record_id=target.record_id,
            target_status=target_status,
            actor=self.actor,
            actor_authority="owner",
            reason=safe_reason,
        )
        return OwnerReviewDecision(
            record_id=updated.id,
            decision=safe_decision,
            previous_status=target.current_status,
            current_status=updated.current_status,
            actor=self.actor,
            evidence_verified=target.evidence_complete,
        )


__all__ = [
    "OWNER_REVIEW_PURPOSE",
    "REVIEWABLE_STATUSES",
    "REVIEW_DECISIONS",
    "MAX_REVIEW_ITEMS",
    "OwnerReviewError",
    "OwnerReviewEvidence",
    "OwnerReviewItem",
    "OwnerReviewQueue",
    "OwnerReviewDecision",
    "OwnerReviewSession",
    "normalize_review_scope",
]
