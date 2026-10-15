"""Optional fixed synthetic local review demo, with no model or extraction.

This demo is not human authentication: its configured owner and recorded local
action are synthetic. The host's human_review origin marker is only an adapter
contract here, never evidence that a human identity was verified. Only a NEW,
empty, existing private directory is the default. Explicit synthetic-only resume
validates the original sample and receipts; it never recaptures or renews them.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
import json
import re
from pathlib import Path
import sqlite3
import stat

from . import FlowGridMemory
from .auth import TrustedPrincipal
from .extraction import ExtractorIdentity, ExtractionRequest, canonical_json, sha256_canonical, _strict_json_loads
from .validity_host import TrustedReviewDecision, ValidityReviewHost
from .validity_local_lock import LocalValidityUserLock
from .validity_retention import ManagedValidityReceiptStore, ValidityRetentionError
from .date_expression import resolve_date_expression
from .date_meaning_adapter import pending_quote_date_reviews
from .governance import raw_event_from_row, raw_event_id_for_message
from .validity_review import ReviewReceipt, review_validity

SOURCE='从明天起，每次发布前做测试。'
EVIDENCE_TIME='2026-10-04T09:00:00+08:00'
USER='synthetic-local-review-demo'
ACTOR='synthetic-local-demo-owner'
EVENT_SCHEMA='flowgrid.synthetic-local-review-action/v1'
CORRECTION_SCHEMA='flowgrid.synthetic-local-correction/v1'


class LocalReviewDemoError(ValueError):
    pass


class LocalReviewDemo:
    """Single pending review in a new synthetic sandbox; never confirms memory."""
    def __init__(self, root, *, retention_seconds, resume_synthetic=False):
        self.memory=None
        self.closed=False
        try:
            self.root=Path(root)
            info=self.root.lstat()
            if (not self.root.is_absolute() or not stat.S_ISDIR(info.st_mode)
                    or info.st_mode&0o777!=0o700
                    or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid())):
                raise LocalReviewDemoError('new_private_demo_directory_required')
            if type(resume_synthetic) is not bool:
                raise LocalReviewDemoError('explicit_synthetic_resume_required')
            if not resume_synthetic and any(self.root.iterdir()):
                raise LocalReviewDemoError('new_empty_demo_directory_required')
            self.root_identity=info.st_dev,info.st_ino
            if type(retention_seconds) is not int or retention_seconds<=0:
                raise LocalReviewDemoError('positive_demo_retention_required')
            self.retention_seconds=retention_seconds
            self.principal=TrustedPrincipal(principal_id=ACTOR,authority='owner',
                allowed_users={USER},scopes={'user':USER},
                permissions={'memory:read','memory:audit','memory:evidence','memory:transition','memory:erase'},
                purpose='synthetic-local-review-demo',allowed_audit_purposes={'synthetic-local-review-demo'})
            self.access_context=self.principal.to_access_context()
            self.disclosure_policy=self.principal.to_disclosure_policy()
            if resume_synthetic:
                self._resume_paths()
            else:
                (self.root/'receipts').mkdir(mode=0o700)
                (self.root/'locks').mkdir(mode=0o700)
            self.receipts=ManagedValidityReceiptStore(self.root/'receipts',retention_seconds=retention_seconds)
            self.serialize_user=LocalValidityUserLock(self.root/'locks')
            self.actions_path=self.root/'local-actions.sqlite3'
            if not resume_synthetic:
                self._create_private(self.actions_path)
            self.actions_identity=self.actions_path.stat().st_dev,self.actions_path.stat().st_ino
            if not resume_synthetic:
                with self._actions() as db:
                    db.execute('''CREATE TABLE actions (
                    pending_digest TEXT PRIMARY KEY, actor TEXT NOT NULL, user_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL,
                    event_digest TEXT NOT NULL UNIQUE, review_digest TEXT)''')
            memory_path=self.root/'memory.sqlite3'
            if resume_synthetic:
                events=self._resume_events(memory_path)
            else:
                self._create_private(memory_path)
                self.memory=FlowGridMemory(db_path=str(memory_path))
                ingested=self.memory.ingest_raw_events(user_id=USER,request_id='synthetic-demo-source',
                    session_id='synthetic-demo-session',trusted_scope={'user':USER},
                    messages=[{'role':'user','content':SOURCE,
                        'timestamp':int(datetime.fromisoformat(EVIDENCE_TIME).timestamp()*1000)}])
                events=self.memory._active().db.raw_events_for_extraction(user_id=USER,
                    event_ids=ingested.raw_event_ids)
            self.request=ExtractionRequest(USER,'synthetic-demo-review',tuple(events),{'user':USER},
                ExtractorIdentity('synthetic-local-demo','1','fixed-local-no-model',deterministic=True))
            item={'memory_key':'workflow.deploy_check','field':'valid_from',
                'source_event_id':events[0].id,'expression':'明天','evidence_quote':SOURCE}
            response=canonical_json({'proposals':[item]})
            if resume_synthetic:
                pending=pending_quote_date_reviews(self.request,response,
                    memory_keys=('workflow.deploy_check',),utc_offsets={events[0].id:'+08:00'},
                    **self._authorization())[0]
                self.pending_digest=pending.digest
                with self.serialize_user(USER):
                    self._resume_receipts(pending)
                self.memory=FlowGridMemory(db_path=str(memory_path))
            self.host=ValidityReviewHost(memory=self.memory,receipts=self.receipts,
                resolve_decision=self._resolve_decision,serialize_user=self.serialize_user,
                is_user_active=self.receipts.is_user_active,
                revoke_user=lambda user:self.receipts.revoke_user(user,**self._authorization()),
                is_source_active=self.receipts.is_source_active,
                revoke_source=lambda user,source:self.receipts.revoke_source(user,source,**self._authorization()),
                enabled=True)
            if not resume_synthetic:
                self.pending_digest=self.host.capture(self.request,response,
                    principal=self.principal,memory_keys=('workflow.deploy_check',),
                    utc_offsets={events[0].id:'+08:00'})[0]
            with self.serialize_user(USER),self._actions() as db:
                db.execute('CREATE TABLE IF NOT EXISTS corrections (proposal_digest TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS correction_decisions (proposal_digest TEXT PRIMARY KEY,outcome TEXT NOT NULL,decision_digest TEXT NOT NULL)')
                self._correction(db)
        except LocalReviewDemoError:
            self.close()
            raise
        except Exception:
            self.close()
            raise LocalReviewDemoError('local_demo_initialization_failed') from None

    @staticmethod
    def _create_private(path):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(fd)

    def _private_resume_path(self,path,directory=False):
        info=path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or info.st_mode&0o777!=(0o700 if directory else 0o600)
                or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid())):
            raise LocalReviewDemoError('invalid_synthetic_demo_resume')

    def _resume_paths(self):
        required={'receipts','locks','memory.sqlite3','local-actions.sqlite3'}
        names={p.name for p in self.root.iterdir()}
        if not required<=names:
            raise LocalReviewDemoError('invalid_synthetic_demo_resume')
        for path in self.root.iterdir():
            if path.name in {'receipts','locks'}:
                self._private_resume_path(path,True)
            elif (path.name in {'memory.sqlite3','local-actions.sqlite3','launch.json'}
                    or re.fullmatch(r'launch-[0-9a-f]{16}\.json',path.name)
                    or path.name in {base+suffix for base in ('memory.sqlite3','local-actions.sqlite3')
                        for suffix in ('-wal','-shm','-journal')}):
                self._private_resume_path(path)
            else:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
        for relative in ('receipts/validity-retention.sqlite3','locks/validity-lock-registry.sqlite3',
                         'locks/'+hashlib.sha256(USER.encode()).hexdigest()+'.sqlite3'):
            self._private_resume_path(self.root/relative)
        for directory in ('receipts','locks'):
            for path in (self.root/directory).iterdir():
                if directory=='receipts':
                    allowed=(re.fullmatch(r'[0-9a-f]{64}\.json',path.name)
                        or path.name in {'validity-retention.sqlite3'+s for s in ('','-wal','-shm','-journal')})
                else:
                    bases={'validity-lock-registry.sqlite3',hashlib.sha256(USER.encode()).hexdigest()+'.sqlite3'}
                    allowed=path.name in {b+s for b in bases for s in ('','-wal','-shm','-journal')}
                if not allowed:
                    raise LocalReviewDemoError('invalid_synthetic_demo_resume')
                self._private_resume_path(path)

    @contextmanager
    def _readonly(self,path):
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            yield db
        finally:
            db.close()

    def _resume_events(self,path):
        with self._readonly(path) as db:
            allowed={'messages','views','requests','sessions','fts','fts_data','fts_idx','fts_content',
                'fts_docsize','fts_config','raw_events','governance_meta','memory_records',
                'memory_state_events','extraction_meta','extraction_receipts','proposal_origins'}
            if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}!=allowed:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            rows=db.execute('SELECT re.*,m.role,m.content,m.session_id,m.ts_ms,m.request_id,m.user_id AS message_user FROM raw_events re '
                'JOIN messages m ON m.id=re.source_message_id').fetchall()
            if (len(rows)!=1 or db.execute('SELECT count(*) FROM messages').fetchone()[0]!=1
                    or db.execute('SELECT count(*) FROM memory_records').fetchone()[0]!=0
                    or db.execute('SELECT count(*) FROM requests').fetchone()[0]!=1):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            row=rows[0]
            event=raw_event_from_row(row)
            if (event.user_id!=USER or event.content!=SOURCE or event.role!='user'
                    or event.event_type!='message' or event.authority!='user'
                    or event.scope!={'user':USER,'session':'synthetic-demo-session'}
                    or event.observed_at!=datetime.fromisoformat(EVIDENCE_TIME).astimezone(timezone.utc).isoformat()
                    or event.id!=raw_event_id_for_message(event.source_message_id)
                    or event.source_locator!='messages:'+event.source_message_id
                    or row['session_id']!='synthetic-demo-session' or row['request_id']!='synthetic-demo-source'
                    or row['message_user']!=USER
                    or row['ts_ms']!=int(datetime.fromisoformat(EVIDENCE_TIME).timestamp()*1000)):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            request=db.execute('SELECT * FROM requests').fetchone()
            if (request['request_id']!='synthetic-demo-source' or request['user_id']!=USER
                    or request['session_id']!='synthetic-demo-session'
                    or json.loads(request['message_ids'])!=[event.source_message_id]
                    or any(db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                        for table in ('memory_state_events','extraction_receipts','proposal_origins'))):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            views=db.execute('SELECT * FROM views').fetchall()
            if (len(views)!=2 or {v['view_type'] for v in views}!={'window','session-segment'}
                    or any(v['user_id']!=USER or v['session_id']!='synthetic-demo-session'
                        or v['content']!='user: '+SOURCE
                        or json.loads(v['source_ids'])!=[event.source_message_id]
                        or v['created_at']!=event.observed_at or v['start_seq']!=0 or v['end_seq']!=0
                        for v in views)):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            return (event,)

    def _resume_receipts(self,pending):
        with self._actions() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'actions'}<=tables or tables-{'actions','corrections','correction_decisions'}:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            if bool('corrections' in tables)!=bool('correction_decisions' in tables):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            row=self._event(db)
            if 'corrections' in tables:
                self._correction(db)
        expected={pending.digest:pending}
        if row is not None:
            child=review_validity(pending,self.request,**self._authorization(),outcome=row['outcome'],
                review_source='synthetic-local-action:'+row['event_digest'],reason=row['reason']).receipt
            expected[child.digest]=child
            if row['review_digest'] not in {None,child.digest}:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
        files={p.stem for p in self.receipts.root.glob('*.json')}
        if (pending.digest not in files or files-set(expected)
                or (row is not None and row['review_digest'] is not None and row['review_digest'] not in files)):
            raise LocalReviewDemoError('invalid_synthetic_demo_resume')
        for digest in files:
            if self.receipts.base.read(digest,**self._authorization())!=expected[digest].to_dict():
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
        with self._readonly(self.receipts.catalog_path) as db:
            if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}!=\
                    {'receipts','revoked_users','revoked_sources'}:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            rows=db.execute('SELECT * FROM receipts').fetchall()
            if {r['digest'] for r in rows}!=files:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            parent=next(r for r in rows if r['digest']==pending.digest)
            created=datetime.fromisoformat(parent['created_at'])
            expires=datetime.fromisoformat(parent['expires_at'])
            if created.tzinfo is None or expires.tzinfo is None or (expires-created).total_seconds()!=self.retention_seconds:
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            for item in rows:
                start=datetime.fromisoformat(item['created_at'])
                end=datetime.fromisoformat(item['expires_at'])
                if (item['user_id']!=USER or item['source_id']!=self.request.raw_events[0].id
                        or item['state'] not in {'active','expired','revoked'}
                        or start.tzinfo is None or end.tzinfo is None or not start<end<=expires):
                    raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            if any(r[0]!=USER for r in db.execute('SELECT user_id FROM revoked_users')):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')
            if any(tuple(r)!=(USER,self.request.raw_events[0].id)
                    for r in db.execute('SELECT user_id,source_id FROM revoked_sources')):
                raise LocalReviewDemoError('invalid_synthetic_demo_resume')

    def _authorization(self):
        return {'access_context':self.access_context,'disclosure_policy':self.disclosure_policy}

    @contextmanager
    def _actions(self):
        root_info=self.root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_mode&0o777!=0o700
                or (root_info.st_dev,root_info.st_ino)!=self.root_identity):
            raise LocalReviewDemoError('invalid_local_demo_event')
        info=self.actions_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode&0o077
                or (info.st_dev,info.st_ino)!=self.actions_identity):
            raise LocalReviewDemoError('invalid_local_demo_event')
        db=sqlite3.connect(self.actions_path.as_uri()+'?mode=rw',uri=True)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def _event_payload(self, row):
        return {'schema':EVENT_SCHEMA,'actor':row['actor'],'user_id':row['user_id'],
            'request_digest':row['request_digest'],'pending_digest':row['pending_digest'],
            'outcome':row['outcome'],'reason':row['reason']}

    def _event(self, db):
        rows=db.execute('SELECT * FROM actions').fetchall()
        if not rows:
            return None
        if len(rows)!=1:
            raise LocalReviewDemoError('invalid_local_demo_event')
        row=rows[0]
        if (row['actor']!=ACTOR or row['user_id']!=USER
                or row['request_digest']!=self.request.digest or row['pending_digest']!=self.pending_digest
                or row['outcome'] not in {'supported','rejected','unknown'}
                or not isinstance(row['reason'],str) or not row['reason'].strip()
                or sha256_canonical(self._event_payload(row))!=row['event_digest']):
            raise LocalReviewDemoError('invalid_local_demo_event')
        return row

    def _resolve_decision(self, locator, principal):
        with self._actions() as db:
            row=self._event(db)
        if (row is None or principal!=self.principal
                or locator!='synthetic-local-action:'+row['event_digest']):
            raise LocalReviewDemoError('invalid_local_demo_event')
        return TrustedReviewDecision(source_locator=locator,principal_id=ACTOR,user_id=USER,
            request_digest=row['request_digest'],pending_digest=row['pending_digest'],
            outcome=row['outcome'],reason=row['reason'],origin='human_review')

    def _open(self):
        if self.closed:
            raise LocalReviewDemoError('local_demo_closed')

    def _live_supported(self,pending_digest,review_digest):
        if pending_digest!=self.pending_digest:
            raise LocalReviewDemoError('invalid_local_demo_correction')
        self.host._active(USER)
        self.host._sources(self.request)
        self.receipts.read(self.pending_digest,**self._authorization())
        with self._actions() as db:
            row=self._event(db)
        if (row is None or row['outcome']!='supported' or not row['review_digest']
                or row['review_digest']!=review_digest):
            raise LocalReviewDemoError('supported_local_demo_review_required')
        data=self.receipts.read(review_digest,**self._authorization())
        if (data['status']!='supported' or data['parent']!=self.pending_digest
                or data['request_digest']!=self.request.digest
                or data['scope']!=dict(self.request.trusted_scope)
                or data['review']['principal_id']!=ACTOR or data['review']['reason']!=row['reason']
                or data['review']['source_locator']!='synthetic-local-action:'+row['event_digest']):
            raise LocalReviewDemoError('invalid_local_demo_correction')
        return data

    def _correction_payload(self,target,value,reason,review_digest):
        if (not isinstance(target,str) or target not in {'reference_time','execution_time'}
                or not isinstance(value,str) or not isinstance(reason,str)
                or not reason.strip() or len(reason)>2048
                or any(ord(c)<32 and c not in '\n\t' for c in reason)):
            raise LocalReviewDemoError('invalid_local_demo_correction')
        if target=='reference_time':
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00',value) is None:
                raise LocalReviewDemoError('invalid_local_demo_correction')
            datetime.fromisoformat(value)
            before=EVIDENCE_TIME
            resolution=resolve_date_expression('明天',anchor=value,utc_offset='+08:00').to_dict()
        else:
            if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value) is None:
                raise LocalReviewDemoError('invalid_local_demo_correction')
            before=None
            resolution=None
        return {'schema':CORRECTION_SCHEMA,'actor':ACTOR,'user_id':USER,
            'request_digest':self.request.digest,'pending_digest':self.pending_digest,
            'review_digest':review_digest,'scope':dict(self.request.trusted_scope),
            'target':target,'before':before,'after':value,'reason':reason,
            'resolution':resolution,'candidate_only':True,'confirmed':False}

    def _decision_payload(self,proposal_digest,outcome):
        return {'schema':CORRECTION_SCHEMA+'/decision','actor':ACTOR,'user_id':USER,
            'scope':dict(self.request.trusted_scope),'proposal_digest':proposal_digest,'outcome':outcome}

    def _correction(self,db):
        rows=db.execute('SELECT * FROM corrections').fetchall()
        decisions=db.execute('SELECT * FROM correction_decisions').fetchall()
        if not rows and not decisions:
            return None
        if len(rows)!=1 or len(decisions)>1:
            raise LocalReviewDemoError('invalid_local_demo_correction')
        row=rows[0]
        payload=_strict_json_loads(row['payload'],field='local correction')
        action=self._event(db)
        if (not isinstance(payload,dict) or action is None or action['outcome']!='supported'
                or not action['review_digest']
                or canonical_json(payload)!=row['payload']
                or sha256_canonical(payload)!=row['proposal_digest']
                or payload!=self._correction_payload(payload.get('target'),payload.get('after'),
                    payload.get('reason'),action['review_digest'])):
            raise LocalReviewDemoError('invalid_local_demo_correction')
        status='pending'
        if decisions:
            decision=decisions[0]
            if (decision['proposal_digest']!=row['proposal_digest']
                    or decision['outcome'] not in {'accept','reject'}
                    or decision['decision_digest']!=sha256_canonical(
                        self._decision_payload(row['proposal_digest'],decision['outcome']))):
                raise LocalReviewDemoError('invalid_local_demo_correction')
            status='accepted_for_demo' if decision['outcome']=='accept' else 'rejected_for_demo'
        return {**payload,'proposal_digest':row['proposal_digest'],'status':status,
            'derived_resolved_value':payload['resolution']['value'] if payload['resolution'] else None}

    def propose_correction(self, *, target, proposed_value, reason, pending_digest, review_digest):
        self._open()
        try:
            with self.serialize_user(USER):
                self._live_supported(pending_digest,review_digest)
                payload=self._correction_payload(target,proposed_value,reason,review_digest)
                digest=sha256_canonical(payload)
                with self._actions() as db:
                    previous=self._correction(db)
                    if previous is not None and previous['proposal_digest']!=digest:
                        raise LocalReviewDemoError('local_demo_correction_conflict')
                    if previous is None:
                        db.execute('INSERT INTO corrections VALUES (?,?)',(digest,canonical_json(payload)))
                return self.state()
        except LocalReviewDemoError:
            raise
        except Exception:
            raise LocalReviewDemoError('local_demo_correction_failed') from None

    def decide_correction(self, *, proposal_digest, outcome):
        self._open()
        if not isinstance(outcome,str) or outcome not in {'accept','reject'}:
            raise LocalReviewDemoError('invalid_local_demo_correction')
        try:
            with self.serialize_user(USER):
                with self._actions() as db:
                    proposal=self._correction(db)
                    if proposal is None or proposal['proposal_digest']!=proposal_digest:
                        raise LocalReviewDemoError('invalid_local_demo_correction')
                    self._live_supported(proposal['pending_digest'],proposal['review_digest'])
                    decisions=db.execute('SELECT outcome FROM correction_decisions').fetchall()
                    if decisions and decisions[0][0]!=outcome:
                        raise LocalReviewDemoError('local_demo_correction_conflict')
                    if not decisions:
                        db.execute('INSERT INTO correction_decisions VALUES (?,?,?)',
                            (proposal_digest,outcome,sha256_canonical(self._decision_payload(proposal_digest,outcome))))
                return self.state()
        except LocalReviewDemoError:
            raise
        except Exception:
            raise LocalReviewDemoError('local_demo_correction_failed') from None

    def state(self):
        self._open()
        try:
            with self.serialize_user(USER):
                with self._actions() as db:
                    row=self._event(db)
                review_digest=row['review_digest'] if row is not None else None
                reason=None
                correction=None
                source=expression=resolved_value=observed_at=None
                if (not self.receipts.is_user_active(USER) or
                        not self.receipts.is_source_active(USER,self.request.raw_events[0].id)):
                    status='revoked'
                else:
                    try:
                        data=self.receipts.read(review_digest or self.pending_digest,**self._authorization())
                        status=data['status']
                        source=data['source']['content']
                        expression=data['binding']['evidence']['quote']
                        resolved_value=data['binding']['value']
                        observed_at=EVIDENCE_TIME
                        if review_digest is not None:
                            if (data['parent']!=self.pending_digest or data['status']!=row['outcome']
                                    or data['review']['reason']!=row['reason']
                                    or data['review']['source_locator']!='synthetic-local-action:'+row['event_digest']):
                                raise LocalReviewDemoError('invalid_local_demo_event')
                            reason=row['reason']
                        with self._actions() as db:
                            correction=self._correction(db)
                    except ValidityRetentionError as error:
                        if str(error)!='validity_receipt_unavailable':
                            raise
                        status='expired'
                return {'source':source,'expression':expression,'resolved_value':resolved_value,
                    'observed_at':observed_at,
                    'status':status,'pending_digest':self.pending_digest,'review_digest':review_digest,
                    'reason':reason,'retention_seconds':self.retention_seconds,
                    'correction':correction,
                    'synthetic':True,'confirmed':False}
        except LocalReviewDemoError:
            raise
        except Exception:
            raise LocalReviewDemoError('local_demo_state_failed') from None

    def review(self, *, outcome, reason, pending_digest):
        self._open()
        if (not isinstance(outcome,str) or outcome not in {'supported','rejected','unknown'}
                or not isinstance(reason,str) or not reason.strip() or len(reason)>2048
                or any(ord(c)<32 and c not in '\n\t' for c in reason)
                or pending_digest!=self.pending_digest):
            raise LocalReviewDemoError('invalid_local_demo_review')
        try:
            with self.serialize_user(USER):
                self.host._active(USER)
                self.host._sources(self.request)
                self.receipts.read(self.pending_digest,**self._authorization())
                with self._actions() as db:
                    row=self._event(db)
                    if row is not None and (row['outcome'],row['reason'])!=(outcome,reason):
                        raise LocalReviewDemoError('local_demo_review_conflict')
                    if row is None:
                        payload={'schema':EVENT_SCHEMA,'actor':ACTOR,'user_id':USER,
                            'request_digest':self.request.digest,'pending_digest':self.pending_digest,
                            'outcome':outcome,'reason':reason}
                        event_digest=sha256_canonical(payload)
                        db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?,?,NULL)',
                            (self.pending_digest,ACTOR,USER,self.request.digest,outcome,reason,event_digest))
                    else:
                        event_digest=row['event_digest']
                        if row['review_digest'] is not None:
                            self.receipts.read(row['review_digest'],**self._authorization())
                            return self.state()
                # Local action is committed first. Retrying the same action uses
                # its exact event and the create-only receipt's idempotent save.
                reviewed=self.host.review(self.request,self.pending_digest,principal=self.principal,
                    decision_locator='synthetic-local-action:'+event_digest)
                with self._actions() as db:
                    row=self._event(db)
                    if row['review_digest'] not in {None,reviewed.receipt.digest}:
                        raise LocalReviewDemoError('invalid_local_demo_event')
                    db.execute('UPDATE actions SET review_digest=? WHERE pending_digest=?',
                        (reviewed.receipt.digest,self.pending_digest))
                return self.state()
        except LocalReviewDemoError:
            raise
        except Exception:
            raise LocalReviewDemoError('local_demo_review_failed') from None

    def close(self):
        if not self.closed:
            self.closed=True
            if self.memory is not None:
                self.memory.close()
