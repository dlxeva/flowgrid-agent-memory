"""Opt-in user-memory adapter composition, disabled by default.

The host attests validity meaning. This adapter checks exact user provenance,
not entailment, modality, slot equivalence or authorization to change state.
No transport, credential, storage, retry or automatic proposal repair.
"""
from copy import deepcopy
from dataclasses import replace

from .extraction import ExtractorIdentity, sha256_canonical
from .extraction_constraints import (
    ConstraintViolation, ConstrainedExtractor, check_constraints,
)
from .user_evidence_policy import require_user_authored_evidence


class UserEvidenceConstrainedExtractor:
    """Explicit host composition; enablement does not authorize API spending.

    Each draft needs a current user quote. Every supplied validity binding must
    itself point to current user text; unrelated user quotes cannot authorize
    an assistant's date. Invalid output rejects the entire batch unchanged.
    """

    def __init__(self, extractor, *, enabled=False, verbatim_keys=(),
                 validity_bindings=()):
        if type(enabled) is not bool:
            raise ConstraintViolation('invalid_user_adapter_configuration')
        self._bindings = deepcopy(tuple(validity_bindings))
        self._constrained = ConstrainedExtractor(
            extractor, verbatim_keys=verbatim_keys,
            validity_bindings=self._bindings,
        )
        self._inner_identity = self._constrained.identity
        self._enabled = enabled
        self._identity = ExtractorIdentity(
            name='flowgrid.user-evidence-constrained', version='1',
            implementation='flowgrid.user-evidence-constrained/v1',
            config_digest=sha256_canonical({
                'constrained_fingerprint': self._inner_identity.fingerprint,
                'require_user_quote_per_draft': True,
                'require_user_validity_binding': True,
                'enabled': enabled,
            }),
            deterministic=self._inner_identity.deterministic,
        )

    @property
    def identity(self):
        return self._identity

    def extract(self, request):
        request.assert_integrity()
        if not self._enabled:
            raise ConstraintViolation('user_adapter_disabled')
        if (request.extractor != self.identity
                or self._constrained.identity != self._inner_identity):
            raise ConstraintViolation('user_adapter_identity_changed')
        # Validate host binding boundaries before invoking an injected runner.
        # This does not decide whether a date actually means effective-from.
        check_constraints(request, (), validity_bindings=self._bindings)
        events = {event.id: event for event in request.raw_events}
        if any(events[b.evidence.source_event_id].role != 'user'
               for b in self._bindings):
            raise ConstraintViolation('user_validity_evidence_required')
        inner_request = replace(request, extractor=self._inner_identity)
        proposals = self._constrained.extract(inner_request)
        request.assert_integrity()
        if self._constrained.identity != self._inner_identity:
            raise ConstraintViolation('user_adapter_identity_changed')
        return require_user_authored_evidence(request, proposals)
