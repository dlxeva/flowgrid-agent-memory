"""Opt-in literal numeric evidence postcondition, never semantic entailment.

Only standalone Arabic decimal/scientific numerals are checked. Comma thousands
and NFKC presentation variants are normalized; units, objects, dates, frequency,
word numerals, derivations and relations remain outside this necessary check.
No provider, storage, repairs, partial acceptance or confirmation authority.
"""
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from .extraction import ExtractorIdentity, sha256_canonical
from .extraction_constraints import ConstraintViolation
from .user_evidence_policy import require_user_authored_evidence


POLICY = 'flowgrid.literal-numeric-user-evidence/v1'
NUMERAL = re.compile(
    r'(?<![0-9A-Za-z_])[-+]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)'
    r'(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?(?![0-9A-Za-z_])'
)


def numeric_literal_tokens(text):
    """Exact decimal values, no floating-point rounding or inferred arithmetic."""
    if not isinstance(text, str):
        raise ConstraintViolation('numeric_evidence_text_required')
    result = set()
    for match in NUMERAL.finditer(unicodedata.normalize('NFKC', text)):
        token = match.group().replace(',', '')
        if len(token) > 256:
            raise ConstraintViolation('numeric_literal_out_of_bounds')
        try:
            value = Decimal(token)
        except InvalidOperation:
            raise ConstraintViolation('numeric_literal_out_of_bounds') from None
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int) or abs(exponent) > 10000:
            raise ConstraintViolation('numeric_literal_out_of_bounds')
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop(); exponent += 1
        if not any(digits):
            sign, digits, exponent = 0, [0], 0
        result.add((sign, tuple(digits), exponent))
    return frozenset(result)


def require_literal_numeric_user_evidence(request, proposals):
    """Reject the whole validated batch if a draft introduces a new number.

    Search only actual cited user spans, not all events, assistant spans,
    timestamps, memory keys or historical records. Presence is necessary, not
    proof that the quote entails the draft or that its source asserts truth.
    """
    proposals = require_user_authored_evidence(request, proposals)
    events = {event.id: event for event in request.raw_events}
    for proposal in proposals:
        supported = set()
        for span in proposal.evidence_spans:
            if events[span.source_event_id].role == 'user':
                supported.update(numeric_literal_tokens(span.quote))
        if not numeric_literal_tokens(proposal.content).issubset(supported):
            raise ConstraintViolation('numeric_literal_not_in_cited_user_evidence')
    return proposals


class NumericEvidenceConstrainedExtractor:
    """Separate identity; existing/frozen adapters stay unchanged and opt-in."""
    def __init__(self, extractor, *, enabled=False):
        if type(enabled) is not bool or not isinstance(extractor.identity, ExtractorIdentity) or not callable(extractor.extract):
            raise ConstraintViolation('invalid_numeric_adapter_configuration')
        self._base = extractor
        self._base_identity = extractor.identity
        self._enabled = enabled
        self._identity = ExtractorIdentity(
            name='flowgrid.numeric-evidence-constrained', version='1',
            implementation=POLICY,
            config_digest=sha256_canonical({'policy': POLICY,
                'base_fingerprint': self._base_identity.fingerprint, 'enabled': enabled,
                'source': 'cited_user_spans_only', 'entailment_verified': False}),
            deterministic=self._base_identity.deterministic,
        )

    @property
    def identity(self):
        return self._identity

    def extract(self, request):
        request.assert_integrity()
        if not self._enabled:
            raise ConstraintViolation('numeric_adapter_disabled')
        if request.extractor != self.identity or self._base.identity != self._base_identity:
            raise ConstraintViolation('numeric_adapter_identity_changed')
        proposals = self._base.extract(replace(request, extractor=self._base_identity))
        request.assert_integrity()
        if self._base.identity != self._base_identity:
            raise ConstraintViolation('numeric_adapter_identity_changed')
        return require_literal_numeric_user_evidence(request, proposals)
