"""Default-off complete-sentence evidence check for finite lexical risks.

Preserves the full containing sentence even when a model crops its quote. This
does not resolve language, objects, reference or general entailment. Valid
paraphrases can fail. Existing frozen reference-v1 stays unchanged.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import re
from .extraction import ExtractorIdentity, sha256_canonical
from .extraction_constraints import ConstraintViolation
from .user_evidence_policy import require_user_authored_evidence
from .reference_evidence_adapter import REFERENCE, normalized, risky_event_ids

POLICY = 'flowgrid.complete-sentence-risk-evidence/v1'
QUALIFIERS = re.compile(r"\b(?:not|never|nothing|no|may|might|if|unless|considering|undecided|haven't|hasn't|didn't|don't|can't|cannot)\b|没有|尚未|不再|未决定|可能|如果|除非|考虑|考慮", re.I)
# Deliberately finite punctuation rules. Decimal dots are not boundaries.
BOUNDARY = re.compile(r'[。！？!?]|(?<!\d)\.(?=\s|$)')


def sentence_ranges(text):
    start = 0
    result = []
    for match in BOUNDARY.finditer(text):
        end = match.end()
        if text[start:end].strip():
            result.append((start, end))
        start = end
    if text[start:].strip():
        result.append((start, len(text)))
    return tuple(result)


def sentence_scope_violations(request, proposals):
    request.assert_integrity()
    proposals = require_user_authored_evidence(request, proposals)
    events = {e.id: e for e in request.raw_events}
    references = risky_event_ids(request)
    failures = []
    for index, proposal in enumerate(proposals):
        for span in proposal.evidence_spans:
            event = events[span.source_event_id]
            if event.role != 'user':
                continue
            ranges = [(start, end) for start, end in sentence_ranges(event.content)
                      if start < span.end and end > span.start]
            risky = any(QUALIFIERS.search(event.content[start:end]) or
                        (event.id in references and REFERENCE.search(event.content[start:end]))
                        for start, end in ranges)
            if risky:
                # The range is extended using raw offsets, not sanitized quotes.
                # Cropping "not" or the trailing conditional cannot pass.
                full = event.content[ranges[0][0]:ranges[-1][1]]
                if normalized(proposal.content) != normalized(full):
                    failures.append(index)
                    break
    return tuple(failures)


def require_complete_sentence_risk_evidence(request, proposals):
    proposals = require_user_authored_evidence(request, proposals)
    if sentence_scope_violations(request, proposals):
        raise ConstraintViolation('risky_quote_requires_complete_source_sentence')
    return proposals


class SentenceScopeConstrainedExtractor:
    def __init__(self, extractor, *, enabled=False):
        if type(enabled) is not bool or not isinstance(extractor.identity, ExtractorIdentity) or not callable(extractor.extract):
            raise ConstraintViolation('invalid_sentence_scope_configuration')
        self._base = extractor
        self._base_identity = extractor.identity
        self._enabled = enabled
        self._identity = ExtractorIdentity('flowgrid.sentence-scope-constrained', '1', POLICY,
            config_digest=sha256_canonical({'base': self._base_identity.fingerprint,
                'enabled': enabled, 'policy': POLICY, 'qualifiers': QUALIFIERS.pattern,
                'boundaries': BOUNDARY.pattern, 'reference_pattern': REFERENCE.pattern,
                'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'reference_code_sha256': hashlib.sha256(Path(__file__).with_name('reference_evidence_adapter.py').read_bytes()).hexdigest(),
                'semantic_entailment': False, 'whole_batch_failure': True}),
            deterministic=self._base_identity.deterministic)

    @property
    def identity(self):
        return self._identity

    def extract(self, request):
        request.assert_integrity()
        if not self._enabled:
            raise ConstraintViolation('sentence_scope_disabled')
        if request.extractor != self.identity or self._base.identity != self._base_identity:
            raise ConstraintViolation('sentence_scope_identity_changed')
        drafts = self._base.extract(replace(request, extractor=self._base_identity))
        request.assert_integrity()
        if self._base.identity != self._base_identity:
            raise ConstraintViolation('sentence_scope_identity_changed')
        return require_complete_sentence_risk_evidence(request, drafts)
