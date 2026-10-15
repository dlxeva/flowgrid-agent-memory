"""Default-disabled candidate writer; trusted callbacks, no auth/model backend.

All writers and grant/revocation changes must share serialize_user. This is
not a malicious Python plugin sandbox or a distributed transaction. Its named
operations never forward confirmation, transition, erase or SQL; trusted code
holding the underlying memory object can bypass this adapter.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from .auth import TrustedPrincipal, authorize_operation, OPERATION_INGEST, OPERATION_EXTRACT
from .host_lifecycle_reads import DeferredReadMemory
from .extraction import (DirectiveMemoryExtractor, CallableMemoryExtractor,
    ExtractorIdentity, MAX_PROPOSALS, validate_proposals, canonical_json)


class HostWriteError(PermissionError):
    pass


class DeferredCandidateMemory(DeferredReadMemory):
    """Named candidate operations only; no management forwarding."""
    def ingest_raw_events(self, **kwargs):
        return self._load().ingest_raw_events(**kwargs)

    def extract_candidates(self, **kwargs):
        return self._load().extract_candidates(**kwargs)


def _plain(value, depth=0):
    if depth>32:raise HostWriteError('plain_message_array_required')
    if type(value) in (str,int,float,bool,type(None)):return
    if type(value) in (list,tuple):
        for item in value:_plain(item,depth+1)
    elif type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():_plain(item,depth+1)
    else:raise HostWriteError('plain_message_array_required')


class _GuardedExtractor:
    def __init__(self, base, check, effective_scope):
        identity=base.identity
        if not isinstance(identity,ExtractorIdentity):
            raise HostWriteError('extractor_identity_required')
        # Stable host-wrapper identity keeps idempotency separate from unguarded
        # extraction. No caller privileges or credentials enter the digest.
        base_fields={key:getattr(identity,key) for key in ('name','version','implementation','config_digest','deterministic')}
        self.base_identity=ExtractorIdentity(**base_fields)
        self.base_fields=base_fields
        config=hashlib.sha256(canonical_json({'base':base_fields,'guard':'candidate-writer/v2'}).encode()).hexdigest()
        self.identity=ExtractorIdentity('host-live-candidate','2','core.host-lifecycle-writer',config,identity.deterministic)
        self.base=base;self.check=check;self.scope=dict(effective_scope)

    def extract(self, request):
        if any(getattr(self.base.identity,key)!=value for key,value in self.base_fields.items()):
            raise HostWriteError('extractor_identity_changed')
        if any(any(e.scope.get(k)!=v for k,v in self.scope.items()) for e in request.raw_events):
            raise HostWriteError('source_scope_mismatch')
        self.check()
        # Standard extractors receive their own identity contract. The core
        # still stores the outer guard-specific request/fingerprint, preserving
        # distinct guarded idempotency without breaking composed constraints.
        inner=replace(request,extractor=self.base_identity)
        if type(self.base) is DirectiveMemoryExtractor:
            output=DirectiveMemoryExtractor.extract(self.base,inner)
        elif type(self.base) is CallableMemoryExtractor:
            output=CallableMemoryExtractor.extract(self.base,inner)
        else:
            output=self.base.extract(inner)
        if any(getattr(self.base.identity,key)!=value for key,value in self.base_fields.items()):
            raise HostWriteError('extractor_identity_changed')
        if type(output) not in (list,tuple) or len(output)>MAX_PROPOSALS:
            raise HostWriteError('bounded_proposal_array_required')
        # Materialize/validate before the last host check. Do not return lazy
        # plugin output whose iteration could occur after that check.
        proposals=validate_proposals(request,output)
        self.check()
        return proposals


class LifecycleCandidateWriter:
    def __init__(self, *, memory, refresh_principal, serialize_user,
                 is_user_active, is_source_active, enabled=False):
        if type(enabled) is not bool or any(not callable(c) for c in
                (refresh_principal,serialize_user,is_user_active,is_source_active)):
            raise HostWriteError('explicit_host_write_callbacks_required')
        self.memory=memory;self.refresh_principal=refresh_principal
        self.serialize_user=serialize_user;self.is_user_active=is_user_active
        self.is_source_active=is_source_active;self.enabled=enabled

    def _refresh(self,principal,user_id,scope,operation):
        if not self.enabled:raise HostWriteError('host_writer_disabled')
        if not isinstance(principal,TrustedPrincipal):raise HostWriteError('host_identity_required')
        try:
            current=self.refresh_principal(principal)
            if not isinstance(current,TrustedPrincipal) or current.principal_id!=principal.principal_id:
                raise ValueError()
            access=authorize_operation(current,user_id=user_id,requested_scope=scope,operation=operation)
        except Exception:
            raise HostWriteError('host_write_access_denied') from None
        return current,access

    def _live(self,user_id,sources):
        try:
            if self.is_user_active(user_id) is not True:raise HostWriteError('host_user_revoked')
            if any(self.is_source_active(user_id,source) is not True for source in sources):
                raise HostWriteError('host_source_revoked')
        except HostWriteError:raise
        except Exception:raise HostWriteError('host_lifecycle_unavailable') from None

    @contextmanager
    def _session(self,principal,user_id,scope,operation,sources):
        _,preaccess=self._refresh(principal,user_id,scope,operation)
        canonical_user=preaccess.effective_scope['user']
        try:
            with self.serialize_user(canonical_user):
                current,access=self._refresh(principal,user_id,scope,operation)
                canonical_user=access.effective_scope['user']
                self._live(canonical_user,sources)
                def check():
                    latest,_=self._refresh(principal,user_id,scope,operation)
                    if latest!=current:raise HostWriteError('host_write_grant_changed')
                    self._live(canonical_user,sources)
                yield canonical_user,access,check
        except HostWriteError:raise
        except Exception:raise HostWriteError('host_candidate_write_failed') from None

    def ingest_raw_events(self, *, principal, user_id, request_id, session_id,
                          messages, requested_scope=None):
        # No user code or lazy container may run during message materialization.
        if type(messages) not in (list,tuple) or not 1<=len(messages)<=1000 or any(type(m) is not dict for m in messages):
            raise HostWriteError('plain_message_array_required')
        _plain(messages)
        try:messages=json.loads(canonical_json(messages))
        except (ValueError,TypeError):raise HostWriteError('plain_message_array_required') from None
        with self._session(principal,user_id,requested_scope,OPERATION_INGEST,()) as (canonical_user,access,check):
            check()
            receipt=self.memory.ingest_raw_events(user_id=canonical_user,request_id=request_id,
                session_id=session_id,messages=messages,trusted_scope=dict(access.effective_scope))
            self._live(canonical_user,receipt.raw_event_ids)
            check()
            return receipt

    def extract_candidates(self, *, principal, user_id, raw_event_ids,
                           idempotency_key, requested_scope=None, extractor=None):
        if type(raw_event_ids) not in (list,tuple) or not raw_event_ids or len(raw_event_ids)>1000:
            raise HostWriteError('bounded_source_array_required')
        if any(not isinstance(i,str) or not i for i in raw_event_ids):
            raise HostWriteError('bounded_source_array_required')
        sources=tuple(raw_event_ids)
        with self._session(principal,user_id,requested_scope,OPERATION_EXTRACT,sources) as (canonical_user,access,check):
            guarded=_GuardedExtractor(extractor if extractor is not None else DirectiveMemoryExtractor(),
                check,access.effective_scope)
            check()
            receipt=self.memory.extract_candidates(user_id=canonical_user,raw_event_ids=sources,
                idempotency_key=idempotency_key,trusted_scope=dict(access.effective_scope),extractor=guarded)
            # Also covers idempotent replay where the extractor is not invoked.
            check()
            return receipt
