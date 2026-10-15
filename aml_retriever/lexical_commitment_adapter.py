"""Opt-in bounded recurrence/expectation lexical postconditions.

Presence of matching words is necessary only, not meaning, action identity,
negation, conditionality, truth or confirmation. No runtime/default wiring.
"""
from dataclasses import replace
import re
import unicodedata

from .extraction import ExtractorIdentity, sha256_canonical
from .extraction_constraints import ConstraintViolation
from .user_evidence_policy import require_user_authored_evidence


RECURRENCE = {
    'daily': r'\b(?:daily|every day|each day|every morning|every evening)\b|每天|每日',
    'weekly': r'\b(?:weekly|every week|each week)\b|每周|每週',
    'monthly': r'\b(?:monthly|every month|each month)\b|每月',
    'yearly': r'\b(?:yearly|annually|every year|each year)\b|每年',
}
PATTERNS = {key: re.compile(value, re.I) for key,value in RECURRENCE.items()}
EXPECTATION = re.compile(r'\b(?:look(?:ing)? forward|hope|hopes|hoping|eager|excited|considering|considers)\b|期待|希望|考虑|考慮',re.I)
ASSERTED_PLAN = re.compile(r'\b(?:plans? to|planning to|intends? to|intending to)\b|计划|計劃|打算',re.I)
EXPLICIT_PLAN = re.compile(r'\b(?:I|we)\s+(?:plan|intend)\s+to\b|\b(?:I am|I\x27m|we are|we\x27re)\s+planning\s+to\b|计划|計劃|打算',re.I)


def recurrence_markers(text):
    text=unicodedata.normalize('NFKC',text)
    return frozenset(key for key,pattern in PATTERNS.items() if pattern.search(text))


def check_lexical_commitment(request, proposals):
    proposals=require_user_authored_evidence(request,proposals)
    events={e.id:e for e in request.raw_events}
    for proposal in proposals:
        quotes=[unicodedata.normalize('NFKC',s.quote) for s in proposal.evidence_spans
                if events[s.source_event_id].role=='user']
        supported=set()
        for quote in quotes:supported.update(recurrence_markers(quote))
        if not recurrence_markers(proposal.content).issubset(supported):
            raise ConstraintViolation('recurrence_not_in_cited_user_evidence')
        # Check each quote separately; concatenating spans must not construct
        # a commitment phrase that no individual source ever expressed.
        if (ASSERTED_PLAN.search(proposal.content)
            and any(EXPECTATION.search(q) for q in quotes)
            and not any(EXPLICIT_PLAN.search(q) for q in quotes)):
            raise ConstraintViolation('expectation_to_plan_requires_explicit_evidence')
    return proposals


class LexicalCommitmentConstrainedExtractor:
    def __init__(self, extractor, *, enabled=False):
        if type(enabled) is not bool or not isinstance(extractor.identity,ExtractorIdentity) or not callable(extractor.extract):
            raise ConstraintViolation('invalid_lexical_commitment_configuration')
        self._base=extractor;self._base_identity=extractor.identity;self._enabled=enabled
        self._identity=ExtractorIdentity(name='flowgrid.lexical-commitment-constrained',version='1',
            implementation='flowgrid.lexical-commitment/v1',
            config_digest=sha256_canonical({'base_fingerprint':self._base_identity.fingerprint,
                'enabled':enabled,'recurrence_patterns':RECURRENCE,'expectation':EXPECTATION.pattern,
                'asserted_plan':ASSERTED_PLAN.pattern,'explicit_plan':EXPLICIT_PLAN.pattern,
                'evidence_scope':'cited_user_spans_only','semantic_entailment':False}),
            deterministic=self._base_identity.deterministic)

    @property
    def identity(self):return self._identity

    def extract(self, request):
        request.assert_integrity()
        if not self._enabled:raise ConstraintViolation('lexical_commitment_adapter_disabled')
        if request.extractor!=self.identity or self._base.identity!=self._base_identity:
            raise ConstraintViolation('lexical_commitment_identity_changed')
        proposals=self._base.extract(replace(request,extractor=self._base_identity))
        request.assert_integrity()
        if self._base.identity!=self._base_identity:raise ConstraintViolation('lexical_commitment_identity_changed')
        return check_lexical_commitment(request,proposals)
