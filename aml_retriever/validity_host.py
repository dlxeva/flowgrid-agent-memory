"""Opt-in trusted host bridge; no transport, model, default grants or UI.

Host callbacks must implement genuine authenticated human-decision lookup,
durable revocation and serialization across EVERY writer for a user. The core
cannot authenticate a human from a JSON label or a process/OS owner. This bridge
never turns a supported date interpretation into confirmed memory.
"""
from dataclasses import dataclass

from .access import PERMISSION_EVIDENCE
from .auth import (authorize_operation, OPERATION_READ_AUDIT,
                   OPERATION_TRANSITION, OPERATION_ERASE, OPERATION_EXTRACT)
from .date_meaning_adapter import pending_quote_date_reviews
from .extraction import canonical_json
from .validity_review import ReviewReceipt, ReviewedValidity, review_validity, _binding
from .extraction_constraints import ConstrainedExtractor


class HostReviewError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedReviewDecision:
    """Return value of a host-owned verifier, never unpacked from model JSON."""
    source_locator: str
    principal_id: str
    user_id: str
    request_digest: str
    pending_digest: str
    outcome: str
    reason: str
    origin: str


class ValidityReviewHost:
    def __init__(self, *, memory, receipts, resolve_decision, serialize_user,
                 is_user_active, revoke_user, is_source_active=None, revoke_source=None,
                 enabled=False):
        if type(enabled) is not bool or any(not callable(c) for c in
                (resolve_decision,serialize_user,is_user_active,revoke_user)):
            raise HostReviewError('explicit_trusted_host_callbacks_required')
        self.memory=memory
        self.receipts=receipts
        self.resolve_decision=resolve_decision
        self.serialize_user=serialize_user
        self.is_user_active=is_user_active
        self.revoke_user=revoke_user
        if any(c is not None and not callable(c) for c in (is_source_active,revoke_source)):
            raise HostReviewError('explicit_trusted_source_callbacks_required')
        self.is_source_active=is_source_active
        self.revoke_source=revoke_source
        self.enabled=enabled

    def _access(self, principal, user_id, scope, operation):
        if not self.enabled:
            raise HostReviewError('validity_host_disabled')
        access=authorize_operation(principal,user_id=user_id,requested_scope=scope,operation=operation)
        if PERMISSION_EVIDENCE not in access.access_context.permissions:
            raise HostReviewError('evidence_permission_required')
        return access

    def _active(self, user_id):
        try:
            active=self.is_user_active(user_id)
        except Exception:
            raise HostReviewError('host_lifecycle_unavailable') from None
        if active is not True:
            raise HostReviewError('host_user_revoked')

    def capture(self, request, response, *, principal, memory_keys, utc_offsets):
        access=self._access(principal,request.user_id,request.trusted_scope,OPERATION_READ_AUDIT)
        with self.serialize_user(request.user_id):
            self._active(request.user_id)
            self._sources(request)
            receipts=pending_quote_date_reviews(request,response,memory_keys=memory_keys,
                utc_offsets=utc_offsets,access_context=access.access_context,
                disclosure_policy=access.disclosure_policy)
            return tuple(self.receipts.save(r,access_context=access.access_context,
                disclosure_policy=access.disclosure_policy) for r in receipts)

    def review(self, request, pending_digest, *, principal, decision_locator):
        access=self._access(principal,request.user_id,request.trusted_scope,OPERATION_TRANSITION)
        if principal.authority!='owner':
            raise HostReviewError('trusted_owner_required')
        with self.serialize_user(request.user_id):
            self._active(request.user_id)
            self._sources(request)
            data=self.receipts.read(pending_digest,access_context=access.access_context,
                disclosure_policy=access.disclosure_policy)
            event=self._decision(request,pending_digest,principal,decision_locator,
                reviewer_id=principal.principal_id)
            reviewed=review_validity(ReviewReceipt(canonical_json(data)),request,
                access_context=access.access_context,disclosure_policy=access.disclosure_policy,
                outcome=event.outcome,review_source=event.source_locator,reason=event.reason)
            self.receipts.save(reviewed.receipt,access_context=access.access_context,
                disclosure_policy=access.disclosure_policy)
            return reviewed

    def _decision(self, request, pending_digest, principal, locator, *, reviewer_id):
        """Read live host provenance, never infer approval from a cached receipt.

        ``principal`` is the authenticated caller of this operation. At use time
        it may differ from the original reviewer; the resolver must authorize
        access without changing the original actor. Its current readback must
        exclude revoked events and share the host serialization domain.
        """
        try:
            event=self.resolve_decision(locator,principal)
        except Exception:
            raise HostReviewError('review_provenance_unavailable') from None
        if (not isinstance(event,TrustedReviewDecision) or event.origin!='human_review'
            or event.source_locator!=locator or event.principal_id!=reviewer_id
            or event.user_id!=request.user_id or event.request_digest!=request.digest
            or event.pending_digest!=pending_digest
            or event.outcome not in {'supported','rejected','unknown'}):
            raise HostReviewError('review_provenance_mismatch')
        return event

    def _sources(self, request):
        if self.is_source_active is None:
            return  # Legacy bridge has no source-erasure feature configured.
        try:
            active=all(self.is_source_active(request.user_id,e.id) is True for e in request.raw_events)
        except Exception:
            raise HostReviewError('host_lifecycle_unavailable') from None
        if not active:
            raise HostReviewError('host_source_revoked')

    def extract_reviewed(self, request, reviews, *, principal, extractor):
        """Recheck live receipts AND host approval; never activate cached bindings."""
        access=self._access(principal,request.user_id,request.trusted_scope,OPERATION_READ_AUDIT)
        authorize_operation(principal,user_id=request.user_id,requested_scope=request.trusted_scope,
            operation=OPERATION_EXTRACT)
        with self.serialize_user(request.user_id):
            self._active(request.user_id)
            self._sources(request)
            bindings=[]
            for reviewed in reviews:
                if not isinstance(reviewed,ReviewedValidity) or reviewed.binding is None:
                    raise HostReviewError('supported_live_review_required')
                data=self.receipts.read(reviewed.receipt.digest,access_context=access.access_context,
                    disclosure_policy=access.disclosure_policy)
                if (data!=reviewed.receipt.to_dict() or data['status']!='supported'
                    or data['request_digest']!=request.digest or data['user_id']!=request.user_id
                    or reviewed.binding!=_binding(data)):
                    raise HostReviewError('supported_live_review_required')
                event=self._decision(request,data['parent'],principal,
                    data['review']['source_locator'],reviewer_id=data['review']['principal_id'])
                if event.outcome!='supported' or event.reason!=data['review']['reason']:
                    raise HostReviewError('review_provenance_mismatch')
                bindings.append(reviewed.binding)
            if not bindings:
                raise HostReviewError('supported_live_review_required')
            return self.memory.extract_candidates(user_id=request.user_id,
                raw_event_ids=[e.id for e in request.raw_events],idempotency_key=request.idempotency_key,
                trusted_scope=dict(request.trusted_scope),
                extractor=ConstrainedExtractor(extractor,validity_bindings=bindings))

    def _erase_access(self, user_id, principal, reason):
        access=self._access(principal,user_id,{},OPERATION_TRANSITION)
        user_id=access.effective_scope['user']
        authorize_operation(principal,user_id=user_id,requested_scope={},operation=OPERATION_ERASE)
        if principal.authority!='owner' or set(principal.scopes)-{'user'}:
            raise HostReviewError('whole_user_owner_erase_required')
        if not isinstance(reason,str) or not reason.strip():
            raise HostReviewError('erase_reason_required')
        return user_id,access

    def erase_source(self, user_id, source_event_id, *, principal, reason):
        user_id,access=self._erase_access(user_id,principal,reason)
        if (not isinstance(source_event_id,str) or not source_event_id.strip()
            or source_event_id!=source_event_id.strip() or len(source_event_id)>256
            or any(ord(c)<32 or ord(c)==127 for c in source_event_id)):
            raise HostReviewError('invalid_source_reference')
        if self.revoke_source is None or self.is_source_active is None:
            raise HostReviewError('source_lifecycle_callbacks_required')
        with self.serialize_user(user_id):
            try:
                self.revoke_source(user_id,source_event_id)
                if self.is_source_active(user_id,source_event_id) is not False:
                    raise HostReviewError('durable_source_revocation_required')
                count=self.receipts.purge_source(user_id,source_event_id,
                    access_context=access.access_context,disclosure_policy=access.disclosure_policy)
                receipt=self.memory.erase_source(user_id=user_id,source_event_id=source_event_id,
                    actor=principal.principal_id,actor_authority=principal.authority,reason=reason)
            except Exception:
                raise HostReviewError('host_source_erase_incomplete') from None
            return {'memory':receipt,'deleted_validity_receipts':count}

    def expire_reviews(self, user_id, *, principal):
        user_id,access=self._erase_access(user_id,principal,'explicit receipt retention cleanup')
        expire=getattr(self.receipts,'expire_user',None)
        if not callable(expire):
            raise HostReviewError('explicit_retention_store_required')
        with self.serialize_user(user_id):
            return expire(user_id,access_context=access.access_context,
                disclosure_policy=access.disclosure_policy)

    def erase_user(self, user_id, *, principal, reason):
        # Deliberate compound grant: private artifact inspection + whole-user erase.
        user_id,access=self._erase_access(user_id,principal,reason)
        with self.serialize_user(user_id):
            try:
                self.revoke_user(user_id)
                if self.is_user_active(user_id) is not False:
                    raise HostReviewError('durable_revocation_required')
                count=self.receipts.purge_user(user_id,access_context=access.access_context,
                    disclosure_policy=access.disclosure_policy)
                receipt=self.memory.erase_user(user_id=user_id,actor=principal.principal_id,
                    actor_authority=principal.authority,reason=reason)
            except Exception:
                # Revocation MUST survive failure; retry cleanup, never reactivate.
                raise HostReviewError('host_erase_incomplete') from None
            return {'memory':receipt.to_dict(),'deleted_validity_receipts':count}
