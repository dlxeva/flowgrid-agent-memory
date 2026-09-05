"""Zero-dependency conformance probes for host-supplied memory extractors.

The harness exercises the real callable adapter and compilation pipeline.  It
does not duplicate proposal, evidence, governance, or idempotency validation.
Reports deliberately contain only stable case identifiers, statuses, and
fixed result codes; source text and exception payloads never enter a report.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from aml_retriever.access import (
    PERMISSION_AUDIT,
    PERMISSION_READ,
    AccessContext,
    DisclosurePolicy,
)
from aml_retriever.extraction import (
    MAX_PROPOSALS,
    CallableMemoryExtractor,
    EvidenceSpan,
    ExtractionConflict,
    ExtractionValidationError,
    ExtractorIdentity,
    ExtractorInvocationError,
    MemoryExtractor,
    ProposalDraft,
)
from aml_retriever.facade import FlowGridMemory


REPORT_SCHEMA = "flowgrid.extractor-conformance/v1"
_USER_ID = "flowgrid-conformance-user"
_SCOPE = {"project": "flowgrid-conformance"}
_PURPOSE = "extractor conformance"
_SOURCE = "The user prefers conformance-blue."
_NEGATIVE_SOURCE = "A fictional example says someone might prefer conformance-blue."


@dataclass(frozen=True)
class ConformanceCase:
    """One disclosure-safe conformance result."""

    id: str
    status: str
    code: str

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail"}:
            raise ValueError("conformance case status must be pass or fail")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "code": self.code}


@dataclass(frozen=True)
class ConformanceReport:
    """Stable report for machine-readable CI and local developer feedback."""

    security_contract: tuple[ConformanceCase, ...]
    behavioral_quality: tuple[ConformanceCase, ...] = ()

    @property
    def passed(self) -> bool:
        return all(
            case.status == "pass"
            for case in self.security_contract + self.behavioral_quality
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": REPORT_SCHEMA,
            "passed": self.passed,
            "security_contract": [case.to_dict() for case in self.security_contract],
            "behavioral_quality": [case.to_dict() for case in self.behavioral_quality],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _case(case_id: str, passed: bool, ok_code: str, fail_code: str) -> ConformanceCase:
    return ConformanceCase(
        id=case_id,
        status="pass" if passed else "fail",
        code=ok_code if passed else fail_code,
    )


def _controlled_identity(name: str) -> ExtractorIdentity:
    return ExtractorIdentity(
        name=f"flowgrid.conformance.{name}",
        version="1",
        implementation="controlled-security-probe",
        deterministic=True,
    )


def _valid_proposal(request) -> ProposalDraft:
    source = request.raw_events[0]
    return ProposalDraft(
        memory_key="conformance.preference",
        memory_type="preference",
        subject="$user",
        content=source.content,
        evidence_spans=(
            EvidenceSpan(source.id, 0, len(source.content), source.content),
        ),
        confidence=1.0,
    )


def _ingest(memory: FlowGridMemory, *, request_id: str, content: str) -> tuple[str, ...]:
    receipt = memory.ingest_raw_events(
        request_id=request_id,
        user_id=_USER_ID,
        session_id="flowgrid-conformance-session",
        messages=({"role": "user", "content": content},),
        trusted_scope=_SCOPE,
    )
    return receipt.raw_event_ids


def _audit_is_candidate_only(
    memory: FlowGridMemory,
    record_ids: tuple[str, ...],
) -> bool:
    if not record_ids:
        return True
    access = AccessContext(
        principal_id="flowgrid-conformance",
        authority="owner",
        scopes=_SCOPE,
        permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
        purpose=_PURPOSE,
        allowed_users=frozenset({_USER_ID}),
    )
    result = memory.query_audit(
        user_id=_USER_ID,
        access_context=access,
        scope=_SCOPE,
        max_records=MAX_PROPOSALS,
        disclosure_policy=DisclosurePolicy(
            allowed_audit_purposes=frozenset({_PURPOSE})
        ),
    )
    if not result.allowed or result.state is None:
        return False
    by_id = {record.id: record for record in result.state.records}
    return all(
        record_id in by_id and by_id[record_id].status == "candidate"
        for record_id in record_ids
    )


def _target_output_case(extractor: MemoryExtractor) -> ConformanceCase:
    try:
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(memory, request_id="target-output", content=_SOURCE)
            receipt = memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="target-output",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
            candidate_only = _audit_is_candidate_only(memory, receipt.record_ids)
            if not candidate_only:
                return ConformanceCase(
                    "extractor.output_contract", "fail", "NON_CANDIDATE_OUTPUT"
                )
            code = "OUTPUT_ACCEPTED" if receipt.proposal_count else "OUTPUT_ABSTAINED"
            return ConformanceCase("extractor.output_contract", "pass", code)
    except Exception:
        return ConformanceCase("extractor.output_contract", "fail", "OUTPUT_REJECTED")


def _candidate_only_case() -> ConformanceCase:
    try:
        extractor = CallableMemoryExtractor(
            _controlled_identity("candidate-only"), lambda request: [_valid_proposal(request)]
        )
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(memory, request_id="candidate-only", content=_SOURCE)
            receipt = memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="candidate-only",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
            passed = receipt.proposal_count == 1 and _audit_is_candidate_only(
                memory, receipt.record_ids
            )
    except Exception:
        passed = False
    return _case(
        "core.candidate_only",
        passed,
        "CANDIDATE_ONLY_ENFORCED",
        "CANDIDATE_ONLY_NOT_ENFORCED",
    )


def _rejection_case(
    *,
    case_id: str,
    name: str,
    function: Callable[[object], object],
    expected: type[BaseException],
    ok_code: str,
) -> ConformanceCase:
    rejected = False
    try:
        extractor = CallableMemoryExtractor(_controlled_identity(name), function)
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(memory, request_id=name, content=_SOURCE)
            memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key=name,
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
    except expected:
        rejected = True
    except Exception:
        rejected = False
    return _case(case_id, rejected, ok_code, "CONTROL_PROBE_NOT_ENFORCED")


def _span_bounds_case() -> ConformanceCase:
    def invalid_span(request):
        proposal = _valid_proposal(request)
        source = request.raw_events[0]
        return [
            {
                **proposal.to_dict(),
                "evidence_spans": [
                    {
                        "source_event_id": source.id,
                        "start": 0,
                        "end": len(source.content) + 1,
                        "quote": source.content,
                    }
                ],
            }
        ]

    return _rejection_case(
        case_id="core.source_span_bounds",
        name="source-span",
        function=invalid_span,
        expected=ExtractionValidationError,
        ok_code="SOURCE_SPAN_REJECTED",
    )


def _governance_fields_case() -> ConformanceCase:
    def forbidden_fields(request):
        proposal = _valid_proposal(request).to_dict()
        proposal["status"] = "confirmed"
        proposal["authority"] = "owner"
        return [proposal]

    return _rejection_case(
        case_id="core.governance_fields",
        name="governance-fields",
        function=forbidden_fields,
        expected=ExtractionValidationError,
        ok_code="GOVERNANCE_FIELDS_REJECTED",
    )


def _input_integrity_case() -> ConformanceCase:
    def mutate(request):
        request.trusted_scope["project"] = "untrusted-rebinding"
        return []

    return _rejection_case(
        case_id="core.input_integrity",
        name="input-integrity",
        function=mutate,
        expected=ExtractorInvocationError,
        ok_code="INPUT_MUTATION_REJECTED",
    )


def _exception_sanitization_case() -> ConformanceCase:
    sentinel = "FLOWGRID-CONFORMANCE-EXCEPTION-SENTINEL"

    def fail(_request):
        raise RuntimeError(sentinel)

    sanitized = False
    try:
        extractor = CallableMemoryExtractor(_controlled_identity("exception"), fail)
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(memory, request_id="exception", content=_SOURCE)
            memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="exception",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
    except ExtractorInvocationError as exc:
        sanitized = sentinel not in str(exc) and exc.__cause__ is None
    except Exception:
        sanitized = False
    return _case(
        "core.exception_sanitization",
        sanitized,
        "EXCEPTION_SANITIZED",
        "EXCEPTION_NOT_SANITIZED",
    )


def _batch_limit_case() -> ConformanceCase:
    return _rejection_case(
        case_id="core.batch_limit",
        name="batch-limit",
        function=lambda request: [_valid_proposal(request)] * (MAX_PROPOSALS + 1),
        expected=ExtractionValidationError,
        ok_code="BATCH_LIMIT_REJECTED",
    )


def _idempotency_cases() -> tuple[ConformanceCase, ConformanceCase]:
    calls = 0

    def produce(request):
        nonlocal calls
        calls += 1
        return [_valid_proposal(request)]

    replay_ok = False
    conflict_ok = False
    try:
        extractor = CallableMemoryExtractor(_controlled_identity("idempotency"), produce)
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(memory, request_id="idempotency", content=_SOURCE)
            first = memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="idempotency",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
            replay = memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="idempotency",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
            replay_ok = (
                calls == 1
                and not first.idempotent
                and replay.idempotent
                and first.record_ids == replay.record_ids
            )
            try:
                conflicting = CallableMemoryExtractor(
                    _controlled_identity("idempotency-conflict"), produce
                )
                memory.extract_candidates(
                    user_id=_USER_ID,
                    raw_event_ids=event_ids,
                    idempotency_key="idempotency",
                    trusted_scope=_SCOPE,
                    extractor=conflicting,
                )
            except ExtractionConflict:
                conflict_ok = calls == 1
    except Exception:
        replay_ok = False
        conflict_ok = False
    return (
        _case(
            "core.idempotent_replay",
            replay_ok,
            "IDEMPOTENT_REPLAY_ENFORCED",
            "IDEMPOTENT_REPLAY_NOT_ENFORCED",
        ),
        _case(
            "core.idempotency_conflict",
            conflict_ok,
            "IDEMPOTENCY_CONFLICT_ENFORCED",
            "IDEMPOTENCY_CONFLICT_NOT_ENFORCED",
        ),
    )


def _behavioral_case(extractor: MemoryExtractor) -> ConformanceCase:
    abstained = False
    try:
        with FlowGridMemory(db_path=":memory:") as memory:
            event_ids = _ingest(
                memory,
                request_id="behavioral-negative",
                content=_NEGATIVE_SOURCE,
            )
            receipt = memory.extract_candidates(
                user_id=_USER_ID,
                raw_event_ids=event_ids,
                idempotency_key="behavioral-negative",
                trusted_scope=_SCOPE,
                extractor=extractor,
            )
            abstained = receipt.proposal_count == 0
    except Exception:
        abstained = False
    return _case(
        "behavioral.negative_text",
        abstained,
        "NEGATIVE_TEXT_ABSTAINED",
        "NEGATIVE_TEXT_PROPOSED_OR_REJECTED",
    )


def run_extractor_conformance(
    extractor: ExtractorIdentity | MemoryExtractor,
    function: Callable[[object], object] | None = None,
    *,
    include_behavioral_quality: bool = False,
) -> ConformanceReport:
    """Run deterministic probes against an extractor and the production boundary.

    Pass either ``(ExtractorIdentity, callable)`` or one ``MemoryExtractor``.
    The optional behavioral profile expects abstention on one synthetic
    hypothetical/third-party negative; it is separate from the security gate.
    """

    interface_ok = False
    active: MemoryExtractor | None = None
    try:
        if isinstance(extractor, ExtractorIdentity):
            if function is not None:
                active = CallableMemoryExtractor(extractor, function)
                interface_ok = True
        elif function is None:
            identity = extractor.identity
            if isinstance(identity, ExtractorIdentity) and callable(
                getattr(extractor, "extract", None)
            ):
                active = extractor
                interface_ok = True
    except Exception:
        interface_ok = False
        active = None

    interface_case = _case(
        "extractor.interface",
        interface_ok,
        "INTERFACE_ACCEPTED",
        "INTERFACE_REJECTED",
    )
    output_case = (
        _target_output_case(active)
        if active is not None
        else ConformanceCase("extractor.output_contract", "fail", "OUTPUT_NOT_RUN")
    )
    replay_case, conflict_case = _idempotency_cases()
    security = (
        interface_case,
        output_case,
        _candidate_only_case(),
        _span_bounds_case(),
        _governance_fields_case(),
        _input_integrity_case(),
        _exception_sanitization_case(),
        _batch_limit_case(),
        replay_case,
        conflict_case,
    )
    behavioral = (
        (_behavioral_case(active),)
        if include_behavioral_quality and active is not None
        else (
            (ConformanceCase("behavioral.negative_text", "fail", "BEHAVIORAL_NOT_RUN"),)
            if include_behavioral_quality
            else ()
        )
    )
    return ConformanceReport(security, behavioral)


__all__ = [
    "REPORT_SCHEMA",
    "ConformanceCase",
    "ConformanceReport",
    "run_extractor_conformance",
]
