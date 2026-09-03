"""Replaceable, auditable RawEvent-to-candidate extraction contracts.

The deterministic built-in extractor recognizes only a whole-message
``@flowgrid.memory/v1`` JSON directive.  Ordinary natural language is a valid
input and deliberately produces no proposals; this module does not pretend to
provide general language understanding.

Third-party or model-backed extraction is possible only through an explicitly
injected callable.  The adapter adds no SDK, network client, credentials, or
implicit side effects, and it never controls governed fields such as status,
authority, scope, user, record identifiers, or confirmation state.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence, runtime_checkable

from .governance import MEMORY_TYPES, RawEvent, SCOPE_FIELDS


DIRECTIVE_PREFIX = "@flowgrid.memory/v1"
MAX_PROPOSALS = 100
MAX_SOURCE_EVENTS = 1_000


class ExtractionError(ValueError):
    """Base class for safe extraction errors.

    Messages identify a field or failure class, but never include source text
    or untrusted model output.
    """


class ExtractionValidationError(ExtractionError):
    """The request or a proposed record violated the extractor contract."""


class ExtractionConflict(ExtractionError):
    """An idempotency key was already bound to a different exact request."""


class ExtractorInvocationError(ExtractionError):
    """An injected extractor failed without exposing its exception payload."""


def canonical_json(value: object) -> str:
    """Canonical UTF-8 JSON representation used by every SHA-256 fingerprint."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ExtractionValidationError("value is not canonical-JSON encodable") from None


def sha256_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


EMPTY_CONFIG_DIGEST = sha256_canonical(
    {"schema": "flowgrid.extractor-config/v1", "config": {}}
)


class _FrozenDict(dict):
    """JSON-compatible immutable mapping for trusted input snapshots."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("trusted extraction input is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __deepcopy__(self, _memo):
        # ``dataclasses.asdict`` may copy a RawEvent for a host.  Returning a
        # plain detached dict keeps that convenience without exposing the
        # request's trusted mapping to mutation.
        return dict(self)


def _safe_text(value: object, *, field: str, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ExtractionValidationError(f"{field} exceeds its maximum length")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ExtractionValidationError(f"{field} contains control characters")
    return normalized


def _aware_iso(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExtractionValidationError(f"{field} must be a timezone-aware ISO-8601 string")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ExtractionValidationError(f"{field} must be valid ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExtractionValidationError(f"{field} must include an explicit timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExtractorIdentity:
    """Stable identity of one extractor implementation/configuration."""

    name: str
    version: str
    implementation: str
    config_digest: str = EMPTY_CONFIG_DIGEST
    deterministic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _safe_text(self.name, field="extractor.name", max_length=128))
        object.__setattr__(
            self,
            "version",
            _safe_text(self.version, field="extractor.version", max_length=64),
        )
        object.__setattr__(
            self,
            "implementation",
            _safe_text(
                self.implementation,
                field="extractor.implementation",
                max_length=256,
            ),
        )
        if (
            not isinstance(self.config_digest, str)
            or len(self.config_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.config_digest)
        ):
            raise ExtractionValidationError(
                "extractor.config_digest must be a lowercase SHA-256 digest"
            )
        if not isinstance(self.deterministic, bool):
            raise ExtractionValidationError("extractor.deterministic must be boolean")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        """Full, exact SHA-256 fingerprint; never a shortened display ID."""

        return sha256_canonical(
            {"schema": "flowgrid.extractor-identity/v1", **self.to_dict()}
        )


@dataclass(frozen=True)
class EvidenceSpan:
    """One exact Unicode-codepoint slice of a RawEvent body."""

    source_event_id: str
    start: int
    end: int
    quote: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_event_id",
            _safe_text(self.source_event_id, field="evidence.source_event_id", max_length=256),
        )
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 0:
            raise ExtractionValidationError("evidence.start must be a non-negative integer")
        if isinstance(self.end, bool) or not isinstance(self.end, int) or self.end <= self.start:
            raise ExtractionValidationError("evidence.end must be an integer greater than start")
        if not isinstance(self.quote, str) or not self.quote:
            raise ExtractionValidationError("evidence.quote must be a non-empty string")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProposalDraft:
    """Extractor-controlled candidate payload, intentionally excluding governance."""

    memory_key: str
    memory_type: str
    subject: str
    content: str
    evidence_spans: tuple[EvidenceSpan, ...]
    confidence: float | None = None
    valid_from: str | None = None
    valid_until: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "memory_key",
            _safe_text(self.memory_key, field="proposal.memory_key", max_length=256),
        )
        if self.memory_type not in MEMORY_TYPES:
            raise ExtractionValidationError("proposal.memory_type is unsupported")
        object.__setattr__(
            self,
            "subject",
            _safe_text(self.subject, field="proposal.subject", max_length=512),
        )
        if not isinstance(self.content, str) or not self.content.strip():
            raise ExtractionValidationError("proposal.content must be a non-empty string")
        if len(self.content) > 100_000:
            raise ExtractionValidationError("proposal.content exceeds its maximum length")
        spans = tuple(self.evidence_spans)
        if not spans or not all(isinstance(span, EvidenceSpan) for span in spans):
            raise ExtractionValidationError("proposal.evidence_spans must contain EvidenceSpan values")
        object.__setattr__(self, "evidence_spans", spans)
        if self.confidence is not None:
            if (
                isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not math.isfinite(float(self.confidence))
                or not 0.0 <= float(self.confidence) <= 1.0
            ):
                raise ExtractionValidationError("proposal.confidence must be a finite number from 0 to 1")
            object.__setattr__(self, "confidence", float(self.confidence))
        normalized_from = _aware_iso(self.valid_from, field="proposal.valid_from")
        normalized_until = _aware_iso(self.valid_until, field="proposal.valid_until")
        if normalized_from and normalized_until and normalized_from > normalized_until:
            raise ExtractionValidationError("proposal.valid_from must not be after valid_until")
        object.__setattr__(self, "valid_from", normalized_from)
        object.__setattr__(self, "valid_until", normalized_until)

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_key": self.memory_key,
            "memory_type": self.memory_type,
            "subject": self.subject,
            "content": self.content,
            "evidence_spans": [span.to_dict() for span in self.evidence_spans],
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }

    @property
    def fingerprint(self) -> str:
        return sha256_canonical(
            {"schema": "flowgrid.proposal/v1", "proposal": self.to_dict()}
        )


def _normalize_scope(value: object, *, user_id: str) -> dict[str, str]:
    if value is None:
        source: dict = {}
    elif isinstance(value, dict):
        source = dict(value)
    else:
        raise ExtractionValidationError("trusted_scope must be an object")
    if any(not isinstance(key, str) for key in source):
        raise ExtractionValidationError("trusted_scope keys must be strings")
    if set(source) - set(SCOPE_FIELDS):
        raise ExtractionValidationError("trusted_scope contains unsupported fields")
    normalized: dict[str, str] = {}
    for key in sorted(source):
        item = source[key]
        normalized[key] = _safe_text(
            item,
            field=f"trusted_scope.{key}",
            max_length=512,
        )
    if "user" in normalized and normalized["user"] != user_id:
        raise ExtractionValidationError("trusted_scope.user must match user_id")
    normalized["user"] = user_id
    return {key: normalized[key] for key in sorted(normalized)}


@dataclass(frozen=True)
class ExtractionRequest:
    """Immutable exact input given to an extractor outside any DB transaction."""

    user_id: str
    idempotency_key: str
    raw_events: tuple[RawEvent, ...]
    trusted_scope: dict[str, str]
    extractor: ExtractorIdentity
    _digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _safe_text(self.user_id, field="user_id", max_length=512))
        object.__setattr__(
            self,
            "idempotency_key",
            _safe_text(self.idempotency_key, field="idempotency_key", max_length=512),
        )
        events = tuple(self.raw_events)
        if not events or len(events) > MAX_SOURCE_EVENTS:
            raise ExtractionValidationError("raw_events must contain between 1 and 1000 events")
        if not all(isinstance(event, RawEvent) for event in events):
            raise ExtractionValidationError("raw_events must contain RawEvent values")
        ids = [event.id for event in events]
        if len(ids) != len(set(ids)):
            raise ExtractionValidationError("raw_events must not contain duplicate IDs")
        if any(event.user_id != self.user_id for event in events):
            raise ExtractionValidationError("all raw_events must belong to user_id")
        normalized_scope = _normalize_scope(self.trusted_scope, user_id=self.user_id)
        # A caller may narrow previously unscoped legacy evidence into a
        # trusted project/tenant boundary, but it may never rebind evidence
        # that already carries a different immutable boundary.  Session is an
        # optional task filter: when supplied it must match, while omission
        # permits a project-scoped extraction across that project's sessions.
        strong_scope_fields = frozenset({"tenant", "project", "agent", "repository"})
        for event in events:
            try:
                event_scope = dict(event.scope)
            except (TypeError, ValueError):
                raise ExtractionValidationError("raw event scope is invalid") from None
            for key in strong_scope_fields:
                if key in event_scope and normalized_scope.get(key) != event_scope[key]:
                    raise ExtractionValidationError(
                        "raw event is unavailable in the trusted scope"
                    )
            if (
                "session" in normalized_scope
                and "session" in event_scope
                and normalized_scope["session"] != event_scope["session"]
            ):
                raise ExtractionValidationError(
                    "raw event is unavailable in the trusted scope"
                )
        frozen_events = tuple(
            replace(event, scope=_FrozenDict(dict(event.scope))) for event in events
        )
        object.__setattr__(self, "raw_events", frozen_events)
        object.__setattr__(
            self,
            "trusted_scope",
            _FrozenDict(normalized_scope),
        )
        if not isinstance(self.extractor, ExtractorIdentity):
            raise ExtractionValidationError("extractor identity is required")
        object.__setattr__(self, "_digest", self._compute_digest())

    def digest_payload(self) -> dict[str, object]:
        return {
            "schema": "flowgrid.extraction-request/v1",
            "user_id": self.user_id,
            "idempotency_key": self.idempotency_key,
            "trusted_scope": dict(self.trusted_scope),
            "extractor": self.extractor.to_dict(),
            "extractor_fingerprint": self.extractor.fingerprint,
            "raw_events": [
                {
                    "id": event.id,
                    "user_id": event.user_id,
                    "event_type": event.event_type,
                    "role": event.role,
                    "content": event.content,
                    "observed_at": event.observed_at,
                    "recorded_at": event.recorded_at,
                    "authority": event.authority,
                    "scope": dict(event.scope),
                    "source_locator": event.source_locator,
                    "source_message_id": event.source_message_id,
                }
                for event in self.raw_events
            ],
        }

    def _compute_digest(self) -> str:
        return sha256_canonical(self.digest_payload())

    @property
    def digest(self) -> str:
        """Canonical SHA-256 of the complete exact request, including bodies."""

        return self._digest

    def assert_integrity(self) -> None:
        """Detect even reflective mutation attempts before derived persistence."""

        if self._compute_digest() != self._digest:
            raise ExtractionValidationError("trusted extraction request was mutated")


@runtime_checkable
class MemoryExtractor(Protocol):
    @property
    def identity(self) -> ExtractorIdentity:
        ...

    def extract(self, request: ExtractionRequest) -> Sequence[ProposalDraft]:
        ...


def _strict_json_loads(raw: str, *, field: str) -> object:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ExtractionValidationError(f"{field} contains duplicate object keys")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ExtractionValidationError(f"{field} contains a non-finite number")

    try:
        return json.loads(raw, object_pairs_hook=pairs_hook, parse_constant=reject_constant)
    except ExtractionValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ExtractionValidationError(f"{field} is not valid JSON") from None


def _exact_keys(value: object, *, required: set[str], optional: set[str], field: str) -> dict:
    if not isinstance(value, dict):
        raise ExtractionValidationError(f"{field} must be an object")
    keys = set(value)
    if not required.issubset(keys):
        raise ExtractionValidationError(f"{field} is missing required fields")
    if keys - required - optional:
        # This is what rejects extractor attempts to control status, authority,
        # scope, IDs, confirmation, supersession, or any future undeclared field.
        raise ExtractionValidationError(f"{field} contains forbidden or unknown fields")
    return value


def evidence_span_from_mapping(value: object, *, field: str) -> EvidenceSpan:
    data = _exact_keys(
        value,
        required={"source_event_id", "start", "end", "quote"},
        optional=set(),
        field=field,
    )
    return EvidenceSpan(
        source_event_id=data["source_event_id"],
        start=data["start"],
        end=data["end"],
        quote=data["quote"],
    )


def proposal_from_mapping(value: object, *, field: str) -> ProposalDraft:
    data = _exact_keys(
        value,
        required={"memory_key", "memory_type", "subject", "content", "evidence_spans"},
        optional={"confidence", "valid_from", "valid_until"},
        field=field,
    )
    spans = data["evidence_spans"]
    if not isinstance(spans, list):
        raise ExtractionValidationError(f"{field}.evidence_spans must be an array")
    return ProposalDraft(
        memory_key=data["memory_key"],
        memory_type=data["memory_type"],
        subject=data["subject"],
        content=data["content"],
        evidence_spans=tuple(
            evidence_span_from_mapping(span, field=f"{field}.evidence_spans[{index}]")
            for index, span in enumerate(spans)
        ),
        confidence=data.get("confidence"),
        valid_from=data.get("valid_from"),
        valid_until=data.get("valid_until"),
    )


def proposals_from_envelope(value: object, *, field: str) -> tuple[ProposalDraft, ...]:
    envelope = _exact_keys(value, required={"proposals"}, optional=set(), field=field)
    raw_proposals = envelope["proposals"]
    if not isinstance(raw_proposals, list):
        raise ExtractionValidationError(f"{field}.proposals must be an array")
    if len(raw_proposals) > MAX_PROPOSALS:
        raise ExtractionValidationError(f"{field}.proposals exceeds the batch limit")
    return tuple(
        proposal_from_mapping(item, field=f"{field}.proposals[{index}]")
        for index, item in enumerate(raw_proposals)
    )


def _directive_proposals_from_envelope(
    value: object,
    *,
    event: RawEvent,
    field: str,
) -> tuple[ProposalDraft, ...]:
    """Parse directive proposals with evidence locked to the carrier event.

    The directive schema intentionally has no source-ID or span fields.  This
    prevents one directive from laundering another event in the same batch as
    its provenance.  The immutable whole carrier message is the exact evidence
    span for every proposal it explicitly declares.
    """

    envelope = _exact_keys(value, required={"proposals"}, optional=set(), field=field)
    raw_proposals = envelope["proposals"]
    if not isinstance(raw_proposals, list):
        raise ExtractionValidationError(f"{field}.proposals must be an array")
    if len(raw_proposals) > MAX_PROPOSALS:
        raise ExtractionValidationError(f"{field}.proposals exceeds the batch limit")
    result: list[ProposalDraft] = []
    for index, value_item in enumerate(raw_proposals):
        item_field = f"{field}.proposals[{index}]"
        data = _exact_keys(
            value_item,
            required={"memory_key", "memory_type", "subject", "content"},
            optional={"confidence", "valid_from", "valid_until"},
            field=item_field,
        )
        result.append(
            ProposalDraft(
                memory_key=data["memory_key"],
                memory_type=data["memory_type"],
                subject=data["subject"],
                content=data["content"],
                evidence_spans=(
                    EvidenceSpan(
                        source_event_id=event.id,
                        start=0,
                        end=len(event.content),
                        quote=event.content,
                    ),
                ),
                confidence=data.get("confidence"),
                valid_from=data.get("valid_from"),
                valid_until=data.get("valid_until"),
            )
        )
    return tuple(result)


class DirectiveMemoryExtractor:
    """Strict, zero-dependency extractor for whole-message explicit directives."""

    identity = ExtractorIdentity(
        name="flowgrid.directive",
        version="1",
        implementation="strict-whole-message-json",
        config_digest=sha256_canonical(
            {
                "schema": "flowgrid.extractor-config/v1",
                "prefix": DIRECTIVE_PREFIX,
                "max_proposals": MAX_PROPOSALS,
                "evidence": "whole-carrier-event",
                "subject_alias": "$user",
            }
        ),
        deterministic=True,
    )

    def extract(self, request: ExtractionRequest) -> tuple[ProposalDraft, ...]:
        proposals: list[ProposalDraft] = []
        for event in request.raw_events:
            body = event.content.strip()
            if not body.startswith(DIRECTIVE_PREFIX):
                continue
            suffix = body[len(DIRECTIVE_PREFIX):]
            if not suffix or not suffix[0].isspace() or not suffix.strip():
                raise ExtractionValidationError("directive requires a JSON envelope")
            parsed = _strict_json_loads(suffix.strip(), field="directive")
            proposals.extend(
                _directive_proposals_from_envelope(
                    parsed,
                    event=event,
                    field="directive",
                )
            )
            if len(proposals) > MAX_PROPOSALS:
                raise ExtractionValidationError("directive batch exceeds the proposal limit")
        return tuple(proposals)


class CallableMemoryExtractor:
    """Adapter for a host-injected callable, invoked outside DB transactions.

    The host must supply an explicit stable identity.  The callable may return
    ProposalDraft objects, proposal mappings, a strict ``{"proposals": [...]}``
    envelope, or that envelope serialized as JSON.  Failures are deliberately
    wrapped without echoing exception messages or raw output.
    """

    def __init__(
        self,
        identity: ExtractorIdentity,
        function: Callable[[ExtractionRequest], object],
    ):
        if not isinstance(identity, ExtractorIdentity):
            raise ExtractionValidationError("callable extractor identity is required")
        if not callable(function):
            raise ExtractionValidationError("callable extractor function is required")
        self._identity = identity
        self._function = function

    @property
    def identity(self) -> ExtractorIdentity:
        return self._identity

    def _invoke(self, request: ExtractionRequest) -> object:
        try:
            return self._function(request)
        except TimeoutError:
            # This adapter does not claim hard cancellation.  The host must
            # enforce its own deadline/cancellation/limiter, wait for that
            # outcome, and only then raise TimeoutError across this boundary.
            raise ExtractorInvocationError("extractor invocation timed out") from None
        except Exception:
            raise ExtractorInvocationError("extractor invocation failed") from None

    def extract(self, request: ExtractionRequest) -> tuple[ProposalDraft, ...]:
        # Bypass a potentially shadowing instance attribute; only the
        # constructor-injected function is untrusted, and _invoke sanitizes it.
        value = CallableMemoryExtractor._invoke(self, request)
        try:
            if isinstance(value, str):
                value = _strict_json_loads(value, field="extractor output")
            if isinstance(value, dict):
                return proposals_from_envelope(value, field="extractor output")
            if not isinstance(value, (list, tuple)):
                raise ExtractionValidationError("extractor output must be a proposal array")
            if len(value) > MAX_PROPOSALS:
                raise ExtractionValidationError("extractor output exceeds the proposal limit")
            proposals: list[ProposalDraft] = []
            for index, item in enumerate(value):
                proposals.append(
                    item
                    if isinstance(item, ProposalDraft)
                    else proposal_from_mapping(item, field=f"extractor output[{index}]")
                )
            return tuple(proposals)
        except Exception:
            # Parsed objects and container subclasses originate outside the
            # trust boundary.  Never propagate even a forged ExtractionError
            # carrying model output or source content.
            raise ExtractionValidationError("extractor output is invalid") from None


def validate_proposals(
    request: ExtractionRequest,
    proposals: Sequence[ProposalDraft],
) -> tuple[ProposalDraft, ...]:
    """Strictly validate the complete output before any derived write begins."""

    request.assert_integrity()
    if not isinstance(proposals, (list, tuple)):
        raise ExtractionValidationError("extractor must return a proposal sequence")
    try:
        supplied = tuple(proposals)
    except Exception:
        raise ExtractionValidationError("extractor proposal sequence is invalid") from None
    if len(supplied) > MAX_PROPOSALS:
        raise ExtractionValidationError("proposal batch exceeds the proposal limit")
    if not all(isinstance(item, ProposalDraft) for item in supplied):
        raise ExtractionValidationError("extractor returned a non-ProposalDraft value")
    # Detach the persistence payload from objects still referenced by injected
    # code.  This complements the frozen DTO and prevents post-return mutation
    # of the callable's original proposal/span instances.
    try:
        normalized = tuple(
            ProposalDraft(
                memory_key=item.memory_key,
                memory_type=item.memory_type,
                subject=item.subject,
                content=item.content,
                evidence_spans=tuple(
                    EvidenceSpan(
                        source_event_id=span.source_event_id,
                        start=span.start,
                        end=span.end,
                        quote=span.quote,
                    )
                    for span in item.evidence_spans
                ),
                confidence=item.confidence,
                valid_from=item.valid_from,
                valid_until=item.valid_until,
            )
            for item in supplied
        )
    except Exception:
        raise ExtractionValidationError("extractor proposal snapshot is invalid") from None
    request.assert_integrity()
    event_by_id = {event.id: event for event in request.raw_events}
    for proposal in normalized:
        for span in proposal.evidence_spans:
            event = event_by_id.get(span.source_event_id)
            if event is None:
                raise ExtractionValidationError("evidence source is outside the exact request batch")
            if span.end > len(event.content):
                raise ExtractionValidationError("evidence span is outside its source event")
            if event.content[span.start:span.end] != span.quote:
                raise ExtractionValidationError("evidence span does not exactly match its source event")
    request.assert_integrity()
    # Exact duplicate proposals add no evidence or governance value.  Keep the
    # first occurrence deterministically so retries and record indexes remain
    # stable without silently merging non-identical proposals.
    deduplicated: list[ProposalDraft] = []
    seen_fingerprints: set[str] = set()
    for proposal in normalized:
        fingerprint = proposal.fingerprint
        if fingerprint not in seen_fingerprints:
            seen_fingerprints.add(fingerprint)
            deduplicated.append(proposal)
    return tuple(deduplicated)


__all__ = [
    "DIRECTIVE_PREFIX",
    "ExtractionError",
    "ExtractionValidationError",
    "ExtractionConflict",
    "ExtractorInvocationError",
    "ExtractorIdentity",
    "EvidenceSpan",
    "ExtractionRequest",
    "ProposalDraft",
    "MemoryExtractor",
    "DirectiveMemoryExtractor",
    "CallableMemoryExtractor",
    "canonical_json",
    "sha256_canonical",
    "proposal_from_mapping",
    "proposals_from_envelope",
    "validate_proposals",
]
