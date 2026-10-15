"""Opt-in conservative literal gate for assistant-adjacent references.

Finite lexical risk detector, NOT a coreference or entailment resolver. A draft
using a risky user event must remain a contiguous literal excerpt of its cited
spans on that event. This can reject valid paraphrases and explicit resolutions.
Whole-batch failure; never silently salvage or confirm individual drafts.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import re
import unicodedata

from .extraction import ExtractorIdentity, sha256_canonical
from .extraction_constraints import ConstraintViolation
from .user_evidence_policy import require_user_authored_evidence

POLICY = 'flowgrid.literal-adjacent-reference-evidence/v1'
REFERENCE = re.compile(r'\b(?:there|that|it)\b|那里|那儿|那个', re.I)


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def risky_event_ids(request):
    events = request.raw_events
    return frozenset(event.id for index, event in enumerate(events)
        if event.role == 'user' and index > 0 and events[index - 1].role == 'assistant'
        and REFERENCE.search(event.content))


def reference_violations(request, proposals):
    """Validated diagnostics, proposal indices only; no acceptance or repairs."""
    request.assert_integrity()
    proposals = require_user_authored_evidence(request, proposals)
    risks = risky_event_ids(request)
    failing = []
    for index, proposal in enumerate(proposals):
        cited_risks = risks.intersection(span.source_event_id for span in proposal.evidence_spans)
        for event_id in cited_risks:
            quotes = [normalized(span.quote) for span in proposal.evidence_spans
                      if span.source_event_id == event_id]
            content = normalized(proposal.content)
            if not content or not any(content in quote for quote in quotes):
                failing.append(index)
                break
    return tuple(failing)


def require_literal_reference_evidence(request, proposals):
    proposals = require_user_authored_evidence(request, proposals)
    if reference_violations(request, proposals):
        raise ConstraintViolation('reference_risk_requires_literal_cited_excerpt')
    return proposals


class ReferenceEvidenceConstrainedExtractor:
    def __init__(self, extractor, *, enabled=False):
        if type(enabled) is not bool or not isinstance(extractor.identity, ExtractorIdentity) or not callable(extractor.extract):
            raise ConstraintViolation('invalid_reference_adapter_configuration')
        self._base = extractor
        self._base_identity = extractor.identity
        self._enabled = enabled
        self._identity = ExtractorIdentity(
            name='flowgrid.reference-evidence-constrained', version='1', implementation=POLICY,
            config_digest=sha256_canonical({'base': self._base_identity.fingerprint,
                'enabled': enabled, 'policy': POLICY, 'pattern': REFERENCE.pattern,
                'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'entailment_verified': False, 'whole_batch_failure': True}),
            deterministic=self._base_identity.deterministic)

    @property
    def identity(self):
        return self._identity

    def extract(self, request):
        request.assert_integrity()
        if not self._enabled:
            raise ConstraintViolation('reference_adapter_disabled')
        if request.extractor != self.identity or self._base.identity != self._base_identity:
            raise ConstraintViolation('reference_adapter_identity_changed')
        proposals = self._base.extract(replace(request, extractor=self._base_identity))
        request.assert_integrity()
        if self._base.identity != self._base_identity:
            raise ConstraintViolation('reference_adapter_identity_changed')
        return require_literal_reference_evidence(request, proposals)
