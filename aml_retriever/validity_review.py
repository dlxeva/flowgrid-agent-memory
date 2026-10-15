"""Optional local host review receipts, not a memory/authorization ledger.

The host supplies authenticated AccessContext and decision provenance. Model
payloads cannot supply those. Loaded files are audit data only: no activation.
Hashes detect accidental changes, not malicious re-signing by filesystem owners.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

from .access import (AccessContext, DisclosurePolicy, PERMISSION_EVIDENCE,
                     PERMISSION_READ, PERMISSION_AUDIT, authorize_memory_read)
from .date_expression import DateResolution
from .extraction import EvidenceSpan, ExtractionValidationError, _aware_iso, _strict_json_loads, canonical_json, sha256_canonical
from .extraction_constraints import ValidityBinding, check_constraints

SCHEMA='flowgrid.validity-review/v1'
MAX_BYTES=65536

class ValidityReviewError(ExtractionValidationError):
    pass

def _authorize(payload, context, policy):
    decision=authorize_memory_read(context,user_id=payload['user_id'],requested_scope=payload['scope'],
                                   mode='audit',disclosure_policy=policy)
    if not decision.allowed or PERMISSION_EVIDENCE not in context.permissions:
        raise ValidityReviewError('validity_review_access_denied')
    # Unlike an ordinary query, injecting a missing restriction cannot prove
    # that this already-captured source belongs to that scope.
    source_scope=payload['source']['scope']
    if any(payload['scope'].get(k)!=v or source_scope.get(k)!=v for k,v in context.scopes.items()):
        raise ValidityReviewError('validity_review_access_denied')

def _text(value):
    if not isinstance(value,str) or not value.strip() or len(value)>2048:
        raise ValidityReviewError('invalid_review_provenance')
    return value

@dataclass(frozen=True)
class ReviewReceipt:
    """Immutable bytes; a stored outcome alone is never approval authority."""
    raw: str

    def __post_init__(self):
        if not isinstance(self.raw,str) or len(self.raw.encode())>MAX_BYTES:
            raise ValidityReviewError('invalid_review_receipt')
        data=_strict_json_loads(self.raw,field='review receipt')
        if not isinstance(data,dict) or data.get('schema')!=SCHEMA or data.get('status') not in {'pending','supported','rejected','unknown'}:
            raise ValidityReviewError('invalid_review_receipt')
        required={'schema','status','parent','user_id','scope','request_digest','source','binding','interpretation','review'}
        if set(data)!=required or not isinstance(data['user_id'],str) or not data['user_id'].strip():
            raise ValidityReviewError('invalid_review_receipt')
        if not isinstance(data['scope'],dict) or not isinstance(data['source'],dict) or not isinstance(data['binding'],dict):
            raise ValidityReviewError('invalid_review_receipt')
        if set(data['source'])!={'id','content','scope','observed_at','recorded_at','source_locator','role'} or not isinstance(data['source']['scope'],dict):
            raise ValidityReviewError('invalid_review_receipt')
        if set(data['binding'])!={'memory_key','field','value','evidence','resolution'} or not isinstance(data['binding']['evidence'],dict):
            raise ValidityReviewError('invalid_review_receipt')
        if not isinstance(data['request_digest'],str) or re.fullmatch(r'[0-9a-f]{64}',data['request_digest']) is None:
            raise ValidityReviewError('invalid_review_receipt')
        _text(data['interpretation'])
        if data['status']=='pending':
            if data['parent'] is not None or data['review'] is not None:
                raise ValidityReviewError('invalid_review_receipt')
        else:
            if not isinstance(data['parent'],str) or re.fullmatch(r'[0-9a-f]{64}',data['parent']) is None:
                raise ValidityReviewError('invalid_review_receipt')
            review=data['review']
            if not isinstance(review,dict) or set(review)!={'principal_id','authority','source_locator','reason'} or review['authority']!='owner':
                raise ValidityReviewError('invalid_review_receipt')
            for key in ('principal_id','source_locator','reason'):
                _text(review[key])
        if canonical_json(data)!=self.raw:
            raise ValidityReviewError('noncanonical_review_receipt')

    @property
    def digest(self):
        return sha256_canonical(self.to_dict())

    def to_dict(self):
        return json.loads(self.raw)

@dataclass(frozen=True)
class ReviewedValidity:
    receipt: ReviewReceipt
    binding: ValidityBinding | None

def prepare_validity_review(request, binding, *, access_context, disclosure_policy, interpretation):
    """Capture host proposal; always pending, never silently approved."""
    request.assert_integrity()
    check_constraints(request,[],validity_bindings=[binding])
    event=next(e for e in request.raw_events if e.id==binding.evidence.source_event_id)
    if event.role!='user':
        raise ValidityReviewError('user_source_required')
    payload={'schema':SCHEMA,'status':'pending','parent':None,'user_id':request.user_id,
        'scope':dict(request.trusted_scope),'request_digest':request.digest,
        'source':{'id':event.id,'content':event.content,'scope':dict(event.scope),
                  'observed_at':event.observed_at,'recorded_at':event.recorded_at,
                  'source_locator':event.source_locator,'role':event.role},
        'binding':{'memory_key':binding.memory_key,'field':binding.field,'value':binding.value,
                   'evidence':binding.evidence.to_dict(),
                   'resolution':binding.resolution.to_dict() if binding.resolution is not None else None},
        'interpretation':_text(interpretation),'review':None}
    _authorize(payload,access_context,disclosure_policy)
    return ReviewReceipt(canonical_json(payload))

def _binding(payload):
    value=payload['binding']
    resolution=DateResolution(**value['resolution']) if value['resolution'] is not None else None
    return ValidityBinding(value['memory_key'],value['field'],value['value'],
                           EvidenceSpan(**value['evidence']),resolution)

def review_validity(receipt, request, *, access_context, disclosure_policy,
                    outcome, review_source, reason):
    """Host-reviewed decision: binding only for explicitly supported outcome.

    Scope and owner checks are runtime authorization. The host must separately
    verify the source locator is a genuine decision, not model self-approval.
    This function neither reads external conversations nor confirms memory.
    """
    data=receipt.to_dict()
    _authorize(data,access_context,disclosure_policy)
    if access_context.authority!='owner':
        raise ValidityReviewError('trusted_owner_review_required')
    if data['status']!='pending' or outcome not in {'supported','rejected','unknown'}:
        raise ValidityReviewError('invalid_review_transition')
    _text(review_source)
    _text(reason)
    request.assert_integrity()
    if request.digest!=data['request_digest']:
        raise ValidityReviewError('review_request_changed')
    binding=_binding(data)
    event=next(e for e in request.raw_events if e.id==binding.evidence.source_event_id)
    if (event.content,event.observed_at,event.recorded_at,dict(event.scope),event.source_locator,event.role)!=(
        data['source']['content'],data['source']['observed_at'],data['source']['recorded_at'],
        data['source']['scope'],data['source']['source_locator'],data['source']['role']):
        raise ValidityReviewError('review_source_changed')
    check_constraints(request,[],validity_bindings=[binding])
    data.update(status=outcome,parent=receipt.digest,review={
        'principal_id':access_context.principal_id,'authority':access_context.authority,
        'source_locator':review_source,'reason':reason})
    reviewed=ReviewReceipt(canonical_json(data))
    return ReviewedValidity(reviewed,binding if outcome=='supported' else None)

class ValidityReceiptStore:
    """Explicit private artifact directory, content-addressed create-only files.

    Read returns plain audit data, never a binding or executable review object.
    The host is responsible for deletion/retention alongside raw-source deletion.
    This adds no background process, service endpoint, or memory state machine.
    """
    def __init__(self, root):
        root=Path(root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValidityReviewError('explicit_existing_receipt_directory_required')
        self.root=root

    def save(self, receipt, *, access_context, disclosure_policy):
        data=receipt.to_dict()
        _authorize(data,access_context,disclosure_policy)
        if data['status']!='pending':
            if access_context.authority!='owner' or not isinstance(data.get('review'),dict) or data['review'].get('principal_id')!=access_context.principal_id or data['review'].get('authority')!='owner':
                raise ValidityReviewError('trusted_owner_review_required')
        raw=receipt.raw.encode()
        path=self.root/(receipt.digest+'.json')
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            existing=self.read(receipt.digest,access_context=access_context,disclosure_policy=disclosure_policy)
            if existing!=data:
                raise ValidityReviewError('receipt_collision') from None
            return receipt.digest
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return receipt.digest

    def read(self, digest, *, access_context, disclosure_policy):
        if not isinstance(access_context,AccessContext) or not isinstance(disclosure_policy,DisclosurePolicy) or not {PERMISSION_READ,PERMISSION_AUDIT,PERMISSION_EVIDENCE}<=access_context.permissions or not disclosure_policy.allows_audit_purpose(access_context.purpose):
            raise ValidityReviewError('validity_review_access_denied')
        if not isinstance(digest,str) or re.fullmatch(r'[0-9a-f]{64}',digest) is None:
            raise ValidityReviewError('invalid_receipt_reference')
        path=self.root/(digest+'.json')
        if path.is_symlink():
            raise ValidityReviewError('invalid_receipt_file')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            raise ValidityReviewError('receipt_too_large')
        try:
            receipt=ReviewReceipt(raw.decode('utf-8'))
            data=receipt.to_dict()
        except (KeyError,TypeError,ValueError,UnicodeError):
            raise ValidityReviewError('invalid_receipt_file') from None
        if receipt.digest!=digest:
            raise ValidityReviewError('receipt_changed')
        _authorize(data,access_context,disclosure_policy)
        return data

    def purge_user(self, user_id, *, access_context, disclosure_policy):
        """Explicit optional whole-user receipt removal; return a count only.

        The authenticated host MUST quiesce every writer to this trusted private
        directory with its lifecycle lock for the entire call. This preflight
        provides no cross-process atomicity or revocation guarantee. Filesystem
        failures during unlink can leave a partially completed purge; retry is
        harmless. Core memories and non-JSON files are outside this operation.
        """
        return self._purge(user_id,source_event_id=None,access_context=access_context,
            disclosure_policy=disclosure_policy)

    def purge_source(self, user_id, source_event_id, *, access_context, disclosure_policy):
        """Remove this exact user's source receipt lineage; return a count only.

        Uses the same whole-user authorization gate and all-file preflight as
        purge_user. The host MUST quiesce all directory writers with its lifecycle
        lock. No cross-process atomicity or revocation guarantee is provided.
        Other sources, other users, and core memories are outside this operation.
        """
        if (not isinstance(source_event_id,str) or not source_event_id.strip()
                or source_event_id!=source_event_id.strip() or len(source_event_id)>256
                or any(ord(c)<32 or ord(c)==127 for c in source_event_id)):
            raise ValidityReviewError('invalid_receipt_source')
        return self._purge(user_id,source_event_id=source_event_id,
            access_context=access_context,disclosure_policy=disclosure_policy)

    def _purge(self, user_id, *, source_event_id, access_context, disclosure_policy):
        required={PERMISSION_READ,PERMISSION_AUDIT,PERMISSION_EVIDENCE,'memory:erase'}
        if (not isinstance(access_context,AccessContext)
                or not isinstance(disclosure_policy,DisclosurePolicy)
                or access_context.authority!='owner'
                or not required<=access_context.permissions
                or not isinstance(user_id,str) or not user_id.strip()
                or user_id!=user_id.strip()
                or any(k!='user' or v!=user_id for k,v in access_context.scopes.items())):
            raise ValidityReviewError('validity_review_access_denied')
        decision=authorize_memory_read(access_context,user_id=user_id,
            requested_scope={'user':user_id},mode='audit',disclosure_policy=disclosure_policy)
        if not decision.allowed:
            raise ValidityReviewError('validity_review_access_denied')
        targets=[]
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValidityReviewError('invalid_receipt_file')
            # Every receipt is checked, including other users, before the first
            # unlink. A corrupt/unknown JSON file cannot silently hide ownership.
            for path in sorted(self.root.glob('*.json')):
                if (re.fullmatch(r'[0-9a-f]{64}\.json',path.name) is None
                        or not stat.S_ISREG(path.lstat().st_mode)):
                    raise ValidityReviewError('invalid_receipt_file')
                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                with os.fdopen(fd,'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValidityReviewError('invalid_receipt_file')
                    raw=stream.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:
                    raise ValidityReviewError('invalid_receipt_file')
                receipt=ReviewReceipt(raw.decode('utf-8'))
                data=receipt.to_dict()
                if receipt.digest!=path.stem:
                    raise ValidityReviewError('invalid_receipt_file')
                _validate_purge_payload(data)
                if (data['user_id']==user_id and
                        (source_event_id is None or data['source']['id']==source_event_id)):
                    _authorize(data,access_context,disclosure_policy)
                    targets.append(path)
        except (OSError,ValueError,TypeError,KeyError,AttributeError,ExtractionValidationError):
            raise ValidityReviewError('invalid_receipt_file') from None
        try:
            for path in targets:
                path.unlink()
        except OSError:
            raise ValidityReviewError('receipt_purge_failed') from None
        return len(targets)


def _validate_purge_payload(data):
    """Validate nested schema and evidence before using on-disk ownership."""
    fields={'tenant','user','project','agent','session','repository'}
    for scope in (data['scope'],data['source']['scope']):
        if any(k not in fields or not isinstance(v,str) or not v.strip()
               for k,v in scope.items()):
            raise ValidityReviewError('invalid_receipt_file')
        if 'user' in scope and scope['user']!=data['user_id']:
            raise ValidityReviewError('invalid_receipt_file')
    source=data['source']
    if (source['role']!='user' or not isinstance(source['id'],str) or not source['id'].strip()
            or not isinstance(source['content'],str) or not source['content'].strip()
            or not isinstance(source['source_locator'],str) or not source['source_locator'].strip()):
        raise ValidityReviewError('invalid_receipt_file')
    for key in ('observed_at','recorded_at'):
        if _aware_iso(source[key],field=key) is None:
            raise ValidityReviewError('invalid_receipt_file')
    if set(data['binding']['evidence'])!={'source_event_id','start','end','quote'}:
        raise ValidityReviewError('invalid_receipt_file')
    resolution=data['binding']['resolution']
    if resolution is not None and (not isinstance(resolution,dict) or set(resolution)!=
            {'expression','anchor','utc_offset','value','granularity','version'}):
        raise ValidityReviewError('invalid_receipt_file')
    binding=_binding(data)
    span=binding.evidence
    if (span.source_event_id!=source['id'] or span.end>len(source['content'])
            or source['content'][span.start:span.end]!=span.quote):
        raise ValidityReviewError('invalid_receipt_file')
