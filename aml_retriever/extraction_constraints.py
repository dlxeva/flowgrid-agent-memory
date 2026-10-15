"""Opt-in postconditions. No provider, default wiring, or confirmation rights.

Observed/recorded timestamps are never validity evidence on their own. Explicit
validity requires a host-attested field binding plus a literal timestamp or a
reproducible, source-bound date resolution. Neither proves sentence meaning.
"""
from dataclasses import dataclass, replace
import re

from .extraction import (EvidenceSpan, ExtractionRequest, ExtractionValidationError,
                         ExtractorIdentity, _aware_iso, sha256_canonical, validate_proposals)
from .date_expression import DateResolution, DateExpressionError, validate_resolution

TIMESTAMP = re.compile(r"(?<![\w:+-])\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})(?![\w:+-])")


class ConstraintViolation(ExtractionValidationError):
    """Content-free rejection code, not a filtered/partially accepted batch."""


@dataclass(frozen=True)
class ValidityBinding:
    """Trusted host attestation of a field's meaning, never model-emitted.

    An ISO token alone does not establish that a date means effective-from or
    valid-until. The host must separately establish that meaning and ambiguity.
    Without a resolution only literal aware ISO dates are accepted. A supplied
    resolution uses a bounded expression grammar, not sentence understanding.
    """
    memory_key: str
    field: str
    value: str
    evidence: EvidenceSpan
    resolution: DateResolution | None = None

    def __post_init__(self):
        if not isinstance(self.memory_key, str) or not self.memory_key.strip():
            raise ConstraintViolation('invalid_validity_binding')
        if self.field not in {'valid_from', 'valid_until'} or not isinstance(self.evidence, EvidenceSpan):
            raise ConstraintViolation('invalid_validity_binding')
        value = _aware_iso(self.value, field='validity_binding.value')
        if value is None:
            raise ConstraintViolation('invalid_validity_binding')
        if self.resolution is not None:
            try:
                resolution=validate_resolution(self.resolution)
            except DateExpressionError:
                raise ConstraintViolation('invalid_date_resolution') from None
            if resolution.expression!=self.evidence.quote or resolution.value!=value:
                raise ConstraintViolation('date_resolution_evidence_mismatch')
            if self.field=='valid_until' and resolution.granularity=='day':
                raise ConstraintViolation('explicit_cutoff_boundary_required')
            object.__setattr__(self,'value',value)
            return
        tokens = []
        for token in TIMESTAMP.findall(self.evidence.quote):
            try:
                tokens.append(_aware_iso(token, field='validity_binding.literal'))
            except ExtractionValidationError:
                continue
        if value not in tokens:
            raise ConstraintViolation('validity_literal_not_supported')
        object.__setattr__(self, 'value', value)


def check_constraints(request, proposals, *, verbatim_keys=(), validity_bindings=()):
    """Validate the entire batch before persistence; never repair or drop items."""
    keys = frozenset(verbatim_keys)
    bindings = tuple(validity_bindings)
    if any(not isinstance(k, str) or not k for k in keys) or any(not isinstance(b, ValidityBinding) for b in bindings):
        raise ConstraintViolation('invalid_constraint_configuration')
    proposals = validate_proposals(request, proposals)
    events = {event.id: event for event in request.raw_events}
    for binding in bindings:
        span = binding.evidence
        event = events.get(span.source_event_id)
        if event is None or span.end > len(event.content) or event.content[span.start:span.end] != span.quote:
            raise ConstraintViolation('validity_binding_outside_request')
        if binding.resolution is not None:
            try:
                validate_resolution(binding.resolution)
            except DateExpressionError:
                raise ConstraintViolation('invalid_date_resolution') from None
            if binding.resolution.anchor is not None and _aware_iso(binding.resolution.anchor,field='binding.anchor')!=_aware_iso(event.observed_at,field='source.observed_at'):
                raise ConstraintViolation('date_anchor_outside_source')
    for proposal in proposals:
        if proposal.memory_key in keys and not any(proposal.content in span.quote for span in proposal.evidence_spans):
            raise ConstraintViolation('content_not_verbatim_in_cited_evidence')
        for field in ('valid_from', 'valid_until'):
            value = getattr(proposal, field)
            if value is None:
                continue
            supported = any(
                binding.memory_key == proposal.memory_key and binding.field == field and binding.value == value
                and any(span.source_event_id == binding.evidence.source_event_id
                        and span.start <= binding.evidence.start and binding.evidence.end <= span.end
                        for span in proposal.evidence_spans)
                for binding in bindings
            )
            if not supported:
                raise ConstraintViolation('validity_not_explicitly_bound')
    return proposals


class ConstrainedExtractor:
    """Explicit host composition; different identity for each policy snapshot."""
    def __init__(self, extractor, *, verbatim_keys=(), validity_bindings=()):
        if not isinstance(extractor.identity, ExtractorIdentity) or not callable(extractor.extract):
            raise ConstraintViolation('invalid_base_extractor')
        self._base = extractor
        self._base_identity = extractor.identity
        self._keys = frozenset(verbatim_keys)
        self._bindings = tuple(validity_bindings)
        if any(not isinstance(k, str) or not k for k in self._keys) or any(not isinstance(b, ValidityBinding) for b in self._bindings):
            raise ConstraintViolation('invalid_constraint_configuration')
        # Detach host DTOs from references retained by an injected runner.
        self._bindings=tuple(ValidityBinding(b.memory_key,b.field,b.value,
            EvidenceSpan(b.evidence.source_event_id,b.evidence.start,b.evidence.end,b.evidence.quote),
            DateResolution(**b.resolution.to_dict()) if b.resolution is not None else None)
            for b in self._bindings)
        policy='flowgrid.extraction-postconditions/v2' if any(b.resolution is not None for b in self._bindings) else 'flowgrid.extraction-postconditions/v1'
        config = {'policy': policy,
                  'base_fingerprint': self._base_identity.fingerprint,
                  'verbatim_keys': sorted(self._keys),
                  'validity_bindings': [{'memory_key': b.memory_key, 'field': b.field,
                                        'value': b.value, 'evidence': b.evidence.to_dict(),
                                        **({'resolution':b.resolution.to_dict()} if b.resolution is not None else {})}
                                       for b in self._bindings]}
        self._identity = ExtractorIdentity(name='flowgrid.constrained-extractor', version=policy.rsplit('/',1)[1][1:],
            implementation=policy, config_digest=sha256_canonical(config),
            deterministic=self._base_identity.deterministic)

    @property
    def identity(self):
        return self._identity

    def extract(self, request: ExtractionRequest):
        request.assert_integrity()
        if request.extractor != self.identity or self._base.identity != self._base_identity:
            raise ConstraintViolation('constraint_identity_changed')
        inner_request = replace(request, extractor=self._base_identity)
        proposals = self._base.extract(inner_request)
        if self._base.identity != self._base_identity:
            raise ConstraintViolation('constraint_identity_changed')
        return check_constraints(request, proposals, verbatim_keys=self._keys, validity_bindings=self._bindings)
