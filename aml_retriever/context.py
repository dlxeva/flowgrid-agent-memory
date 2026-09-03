"""Deterministic, access-safe context packs for governed memory.

This compiler consumes only ``CurrentStateResult``.  It never falls back to
legacy search windows, because a mixed window could reintroduce superseded or
rejected text.  Authorization is performed by ``MemoryService`` before the
resolver and before this compiler sorts or budgets any item.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .access import DisclosurePolicy
from .governance import MEMORY_STATES, CurrentStateResult, MemoryRecord


CONTEXT_STATUSES = frozenset(
    {"ready", "unknown", "conflict", "budget_exceeded", "forbidden"}
)

_OPAQUE_RAW_LOCATOR = re.compile(r"^raw_events:raw_[0-9a-f]{24}$")
_PUBLIC_ITEM_REASONS = {
    "current": "confirmed_current_memory",
    "audit": "audit_lifecycle_record",
}


class _FrozenList(Sequence):
    """Small recursively immutable JSON array used inside ``ContextPack``."""

    __slots__ = ("_values",)

    def __init__(self, values=()):
        object.__setattr__(self, "_values", tuple(_freeze(value) for value in values))

    def __setattr__(self, name, value):
        raise AttributeError("frozen JSON arrays cannot be modified")

    def __getitem__(self, index):
        return self._values[index]

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __eq__(self, other):
        if isinstance(other, (str, bytes)):
            return False
        try:
            return list(self) == list(other)
        except TypeError:
            return False

    def __repr__(self):  # pragma: no cover - diagnostic convenience
        return repr(list(self))


class _FrozenDict(Mapping):
    """Small recursively immutable JSON object used inside ``ContextPack``."""

    __slots__ = ("_values",)

    def __init__(self, values=None):
        frozen = {
            str(key): _freeze(value) for key, value in dict(values or {}).items()
        }
        object.__setattr__(self, "_values", MappingProxyType(frozen))

    def __setattr__(self, name, value):
        raise AttributeError("frozen JSON objects cannot be modified")

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        try:
            return dict(self) == dict(other)
        except (TypeError, ValueError):
            return False

    def __repr__(self):  # pragma: no cover - diagnostic convenience
        return repr(dict(self))


def _freeze(value):
    if isinstance(value, (_FrozenList, _FrozenDict)):
        return value
    if isinstance(value, Mapping):
        return _FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return _FrozenList(value)
    return value


def _thaw(value):
    if isinstance(value, _FrozenDict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, _FrozenList):
        return [_thaw(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    """Canonical renderer used for both counting and final output."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@runtime_checkable
class TokenCounter(Protocol):
    """Exact tokenizer contract.

    Implementations must explicitly expose ``is_exact = True`` and count the
    final canonical JSON string through ``count_tokens``.  Character estimates
    and heuristic word counts must declare themselves inexact and are rejected.
    """

    is_exact: bool

    def count_tokens(self, text: str) -> int:
        ...


@dataclass(frozen=True)
class ContextPack:
    """Stable public context envelope.

    ``to_json`` is the authoritative representation for budget accounting.
    When a character budget is active, ``budget.used_chars`` equals exactly
    ``len(pack.to_json())``.  Items are complete atomic dictionaries; the
    compiler only includes or omits an item and never truncates a field.
    """

    status: str
    abstain: bool
    reason: str
    items: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    owner_gate_required: bool = False
    omitted: dict = field(
        default_factory=lambda: {
            "count": 0,
            "by_reason": {
                "budget": 0,
                "policy": 0,
                "audit_evidence": 0,
            },
        }
    )
    budget: dict = field(
        default_factory=lambda: {
            "max_chars": None,
            "max_tokens": None,
            "used_chars": 0,
            "used_tokens": None,
        }
    )

    def __post_init__(self) -> None:
        if self.status not in CONTEXT_STATUSES:
            raise ValueError(f"unsupported context status: {self.status}")
        object.__setattr__(self, "items", _freeze(self.items))
        object.__setattr__(self, "gaps", _freeze(self.gaps))
        object.__setattr__(self, "conflicts", _freeze(self.conflicts))
        object.__setattr__(self, "omitted", _freeze(self.omitted))
        object.__setattr__(self, "budget", _freeze(self.budget))

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "abstain": bool(self.abstain),
            "reason": self.reason,
            "items": _thaw(self.items),
            "gaps": _thaw(self.gaps),
            "conflicts": _thaw(self.conflicts),
            "owner_gate_required": bool(self.owner_gate_required),
            "omitted": _thaw(self.omitted),
            "budget": _thaw(self.budget),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


class _CounterFailure(ValueError):
    pass


def _valid_limit(value: int | None) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _counter_method(counter: object | None):
    if counter is None:
        raise _CounterFailure("token counter unavailable")
    exact = getattr(counter, "is_exact", None)
    if callable(exact):
        try:
            exact = exact()
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _CounterFailure("token counter exactness check failed") from exc
    # ``exact`` is accepted as a compatibility spelling, but explicit True is
    # still mandatory.  Truthy strings/integers do not cross this boundary.
    if exact is None:
        exact = getattr(counter, "exact", None)
        if callable(exact):
            try:
                exact = exact()
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise _CounterFailure("token counter exactness check failed") from exc
    if exact is not True:
        raise _CounterFailure("token counter is not exact")
    method = getattr(counter, "count_tokens", None)
    if method is None:
        method = getattr(counter, "count", None)
    if not callable(method):
        raise _CounterFailure("token counter has no callable count method")
    return method


def _count_tokens(method, text: str) -> int:
    try:
        value = method(text)
    except Exception as exc:
        raise _CounterFailure("token counter failed") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _CounterFailure("token counter returned an invalid value")
    return value


def _budget_template(max_chars: int | None, max_tokens: int | None) -> dict:
    return {
        "max_chars": max_chars,
        "max_tokens": max_tokens,
        "used_chars": 0,
        "used_tokens": 0 if max_tokens is not None else None,
    }


def _omitted(*, budget: int, policy: int, audit_evidence: int) -> dict:
    values = {
        "budget": max(0, int(budget)),
        "policy": max(0, int(policy)),
        "audit_evidence": max(0, int(audit_evidence)),
    }
    return {"count": sum(values.values()), "by_reason": values}


def _public_locator(locator: str) -> str:
    value = str(locator)
    if _OPAQUE_RAW_LOCATOR.fullmatch(value):
        return value
    # Never pass through a message locator or arbitrary source path.  The
    # digest remains useful for equality/audit correlation without disclosing
    # the underlying identifier.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"raw_events:opaque_{digest}"


def _record_item(record: MemoryRecord, *, mode: str, policy: DisclosurePolicy) -> dict:
    candidates = {
        "id": record.id,
        "memory_key": record.memory_key,
        "memory_type": record.memory_type,
        "subject": record.subject,
        "content": record.content,
        "authority": record.authority,
        "scope": dict(sorted(record.scope.items())),
        "observed_at": record.observed_at,
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
    }
    item = {
        key: candidates[key]
        for key in sorted(policy.record_fields)
        if key in candidates
    }
    # These auditability fields are mandatory and cannot be truncated or
    # disabled by a disclosure policy.
    item["current_status"] = record.current_status
    item["source_locator"] = sorted(
        {_public_locator(value) for value in record.source_locator}
    )
    item["why_selected"] = _PUBLIC_ITEM_REASONS[mode]
    return item


def _public_item_sort_key(item: dict) -> tuple:
    return (
        str(item.get("memory_key", "")),
        str(item.get("memory_type", "")),
        str(item.get("subject", "")),
        canonical_json(item.get("scope", {})),
        str(item.get("id", "")),
        canonical_json(item),
    )


def _normalize_result(result: CurrentStateResult, policy: DisclosurePolicy) -> tuple:
    raw_mode = result.mode
    mode = "current" if raw_mode == "ordinary" else raw_mode
    try:
        records = list(result.records)
    except TypeError:
        records = [None]
    records_well_formed = all(isinstance(record, MemoryRecord) for record in records)

    def string_count(values) -> int:
        try:
            return len({value for value in values if isinstance(value, str)})
        except TypeError:
            return 0

    def collection_count(values) -> int:
        try:
            return len(values)
        except TypeError:
            return 0

    policy_count = string_count(result.withheld_record_ids)
    audit_evidence_count = (
        collection_count(result.raw_events) + collection_count(result.state_events)
        if mode == "audit"
        else 0
    )
    gaps: list[dict] = []
    conflicts: list[dict] = []
    items: list[dict] = []

    conflict_signal = (
        result.reason == "conflicting_current_evidence"
        or bool(result.conflicts)
    )

    if mode == "current" and conflict_signal:
        # Conflict is already an abstaining public envelope.  Even a malformed
        # resolver result containing records cannot add candidate bodies here.
        conflicts.append(
            {
                "reason": "conflicting_current_evidence",
                "count": string_count(result.conflicts),
            }
        )
        gaps.append({"code": "conflict_requires_owner"})
        status = "conflict"
        reason = "conflict_requires_owner"
        abstain = True
    elif mode == "current":
        ready_is_consistent = (
            records_well_formed
            and bool(records)
            and result.abstain is False
            and result.current_status == "confirmed"
            and all(record.current_status == "confirmed" for record in records)
        )
        unknown_is_consistent = (
            not records
            and result.abstain is True
            and result.current_status == "unknown"
        )
        if ready_is_consistent:
            # Apply the public field allowlist only after lifecycle consistency
            # is proven.  A malicious unknown/candidate fixture is never read
            # into an output item, even transiently.
            items = sorted(
                (_record_item(record, mode="current", policy=policy) for record in records),
                key=_public_item_sort_key,
            )
            status = "ready"
            reason = "ready"
            abstain = False
        elif unknown_is_consistent:
            status = "unknown"
            reason = "no_confirmed_memory"
            abstain = True
            gaps.append(
                {
                    "code": (
                        "explicit_unknown_state"
                        if result.reason == "explicit_unknown_state"
                        else "no_confirmed_memory"
                    )
                }
            )
        else:
            status = "unknown"
            reason = "invalid_governed_result"
            abstain = True
            gaps.append({"code": "invalid_governed_result"})
    elif mode == "audit":
        audit_ready_is_consistent = (
            not conflict_signal
            and records_well_formed
            and bool(records)
            and result.abstain is False
            and result.current_status == "audit"
            and all(record.current_status in MEMORY_STATES for record in records)
        )
        audit_unknown_is_consistent = (
            not conflict_signal
            and not records
            and result.abstain is True
            and result.current_status == "unknown"
        )
        if audit_ready_is_consistent:
            items = sorted(
                (_record_item(record, mode="audit", policy=policy) for record in records),
                key=_public_item_sort_key,
            )
            status = "ready"
            reason = "ready"
            abstain = False
        elif audit_unknown_is_consistent:
            status = "unknown"
            reason = "no_confirmed_memory"
            abstain = True
            gaps.append({"code": "no_confirmed_memory"})
        else:
            status = "unknown"
            reason = "invalid_governed_result"
            abstain = True
            gaps.append({"code": "invalid_governed_result"})
    else:
        # Unknown modes must not silently inherit current-mode disclosure.
        status = "unknown"
        reason = "invalid_governed_result"
        abstain = True
        gaps.append({"code": "invalid_governed_result"})

    if result.owner_gate_required:
        gaps.append({"code": "owner_confirmation_required"})

    # Deterministic de-duplication without exposing internal identifiers.
    unique_gaps: list[dict] = []
    seen_gaps: set[str] = set()
    for gap in gaps:
        key = canonical_json(gap)
        if key not in seen_gaps:
            unique_gaps.append(gap)
            seen_gaps.add(key)

    return (
        status,
        abstain,
        reason,
        items,
        unique_gaps,
        conflicts,
        bool(result.owner_gate_required),
        policy_count,
        audit_evidence_count,
    )


class ContextCompiler:
    """Compile one authorized governed result into a deterministic context."""

    def __init__(
        self,
        disclosure_policy: DisclosurePolicy | None = None,
        token_counter: TokenCounter | object | None = None,
    ):
        self.disclosure_policy = disclosure_policy or DisclosurePolicy()
        if not isinstance(self.disclosure_policy, DisclosurePolicy):
            raise ValueError("disclosure_policy must be a DisclosurePolicy")
        self.token_counter = token_counter

    def preflight_error(
        self,
        *,
        max_chars: int | None,
        max_tokens: int | None,
    ) -> str | None:
        if not _valid_limit(max_chars) or not _valid_limit(max_tokens):
            return "invalid_budget"
        if max_tokens is not None:
            try:
                _counter_method(self.token_counter)
            except _CounterFailure:
                return "token_counter_unavailable"
        return None

    def _finalize(
        self,
        pack: ContextPack,
        *,
        max_chars: int | None,
        max_tokens: int | None,
        use_token_counter: bool = True,
    ) -> ContextPack:
        method = None
        if max_tokens is not None and use_token_counter:
            method = _counter_method(self.token_counter)
        budget = _budget_template(max_chars, max_tokens)
        seen: set[tuple[int, int | None]] = set()
        counted: dict[str, int] = {}
        for _ in range(64):
            candidate = replace(pack, budget=budget)
            rendered = candidate.to_json()
            used_chars = len(rendered)
            used_tokens = None
            if method is not None:
                value = _count_tokens(method, rendered)
                previous = counted.get(rendered)
                if previous is not None and previous != value:
                    raise _CounterFailure("token counter is not deterministic")
                counted[rendered] = value
                used_tokens = value
            updated = {
                "max_chars": max_chars,
                "max_tokens": max_tokens,
                "used_chars": used_chars,
                "used_tokens": used_tokens,
            }
            if updated == dict(budget):
                return candidate
            state = (used_chars, used_tokens)
            if state in seen:
                raise _CounterFailure("budget count did not converge")
            seen.add(state)
            budget = updated
        raise _CounterFailure("budget count did not converge")

    @staticmethod
    def _fits(pack: ContextPack) -> bool:
        budget = pack.budget
        max_chars = budget["max_chars"]
        max_tokens = budget["max_tokens"]
        if max_chars is not None and budget["used_chars"] > max_chars:
            return False
        if max_tokens is not None:
            used_tokens = budget["used_tokens"]
            if used_tokens is None or used_tokens > max_tokens:
                return False
        return True

    def failure(
        self,
        *,
        reason: str,
        max_chars: int | None,
        max_tokens: int | None,
        owner_gate_required: bool = False,
        gaps: list[dict] | None = None,
        conflicts: list[dict] | None = None,
        omitted: dict | None = None,
        status: str = "budget_exceeded",
    ) -> ContextPack:
        safe_max_chars = max_chars if _valid_limit(max_chars) else None
        safe_max_tokens = max_tokens if _valid_limit(max_tokens) else None
        pack = ContextPack(
            status=status,
            abstain=True,
            reason=reason,
            items=[],
            gaps=list(gaps or []),
            conflicts=list(conflicts or []),
            owner_gate_required=owner_gate_required,
            omitted=omitted or _omitted(budget=0, policy=0, audit_evidence=0),
        )
        # A missing/broken token counter cannot measure itself.  Character
        # accounting remains exact and the null token count makes the failure
        # explicit rather than substituting a character estimate.
        use_counter = reason != "token_counter_unavailable"
        try:
            return self._finalize(
                pack,
                max_chars=safe_max_chars,
                max_tokens=safe_max_tokens,
                use_token_counter=use_counter,
            )
        except _CounterFailure:
            return self._finalize(
                pack,
                max_chars=safe_max_chars,
                max_tokens=safe_max_tokens,
                use_token_counter=False,
            )

    def forbidden(
        self,
        *,
        reason: str,
        max_chars: int | None,
        max_tokens: int | None,
    ) -> ContextPack:
        return self.failure(
            reason=reason,
            max_chars=max_chars,
            max_tokens=max_tokens,
            status="forbidden",
        )

    def compile(
        self,
        result: CurrentStateResult,
        *,
        max_chars: int | None = None,
        max_tokens: int | None = None,
    ) -> ContextPack:
        """Compile an already-authorized current/audit result.

        The mandatory envelope is retained even if a tiny budget cannot hold
        it.  In that case the pack reports ``budget_exceeded`` and abstains;
        it never emits an empty ``ready`` response.
        """

        if not isinstance(result, CurrentStateResult):
            return self.failure(
                reason="invalid_governed_result",
                max_chars=max_chars,
                max_tokens=max_tokens,
            )

        (
            status,
            abstain,
            reason,
            items,
            gaps,
            conflicts,
            owner_gate,
            policy_count,
            audit_evidence_count,
        ) = _normalize_result(result, self.disclosure_policy)
        total_items = len(items)
        error = self.preflight_error(max_chars=max_chars, max_tokens=max_tokens)
        if error:
            return self.failure(
                reason=error,
                max_chars=max_chars,
                max_tokens=max_tokens,
                owner_gate_required=owner_gate,
                gaps=gaps,
                conflicts=conflicts,
                omitted=_omitted(
                    budget=total_items,
                    policy=policy_count,
                    audit_evidence=audit_evidence_count,
                ),
            )

        # Try the largest deterministic prefix.  Each candidate is measured as
        # final JSON, including the changed omitted count and budget metadata.
        for kept in range(total_items, -1, -1):
            candidate = ContextPack(
                status=status,
                abstain=abstain,
                reason=reason,
                items=items[:kept],
                gaps=gaps,
                conflicts=conflicts,
                owner_gate_required=owner_gate,
                omitted=_omitted(
                    budget=total_items - kept,
                    policy=policy_count,
                    audit_evidence=audit_evidence_count,
                ),
            )
            try:
                finalized = self._finalize(
                    candidate,
                    max_chars=max_chars,
                    max_tokens=max_tokens,
                )
            except _CounterFailure:
                return self.failure(
                    reason="token_counter_unavailable",
                    max_chars=max_chars,
                    max_tokens=max_tokens,
                    owner_gate_required=owner_gate,
                    gaps=gaps,
                    conflicts=conflicts,
                    omitted=candidate.omitted,
                )
            if not self._fits(finalized):
                continue
            if total_items and kept == 0:
                # The envelope fits but no complete memory item does.  Calling
                # that ready would be indistinguishable from successful recall.
                break
            return finalized

        return self.failure(
            reason="budget_exceeded",
            max_chars=max_chars,
            max_tokens=max_tokens,
            owner_gate_required=owner_gate,
            gaps=gaps,
            conflicts=conflicts,
            omitted=_omitted(
                budget=total_items,
                policy=policy_count,
                audit_evidence=audit_evidence_count,
            ),
        )


__all__ = [
    "CONTEXT_STATUSES",
    "TokenCounter",
    "ContextPack",
    "ContextCompiler",
    "canonical_json",
]
