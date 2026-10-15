"""Opt-in receipt TTL and durable revocation metadata, with no background job.

The trusted host must serialize EVERY receipt writer for the entire operation.
The private SQLite catalog and receipt files are not one atomic transaction:
orphan files fail closed and cleanup may need retry. Keep catalog tombstones
when restoring backups; deleting/rolling back the catalog loses revocation
history. This does not revoke core memories or authenticate host principals.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import os
import re
import sqlite3
import stat

from .access import (AccessContext, DisclosurePolicy, PERMISSION_READ,
    PERMISSION_AUDIT, PERMISSION_EVIDENCE, authorize_memory_read)
from .extraction import ExtractionValidationError
from .validity_review import (MAX_BYTES, ReviewReceipt, ValidityReceiptStore,
    ValidityReviewError, _authorize, _validate_purge_payload)


class ValidityRetentionError(ValidityReviewError):
    pass


def _identifier(value):
    return (isinstance(value,str) and bool(value.strip()) and value==value.strip()
        and len(value)<=256 and all(ord(c)>=32 and ord(c)!=127 for c in value))


def _erase_access(user_id, context, policy):
    required={PERMISSION_READ,PERMISSION_AUDIT,PERMISSION_EVIDENCE,'memory:erase'}
    if (not isinstance(context,AccessContext) or not isinstance(policy,DisclosurePolicy)
            or context.authority!='owner' or not required<=context.permissions
            or not _identifier(user_id)
            or any(k!='user' or v!=user_id for k,v in context.scopes.items())):
        raise ValidityRetentionError('validity_review_access_denied')
    if not authorize_memory_read(context,user_id=user_id,requested_scope={'user':user_id},
            mode='audit',disclosure_policy=policy).allowed:
        raise ValidityRetentionError('validity_review_access_denied')


class ManagedValidityReceiptStore:
    """Explicitly enabled bounded retention; no automatic file import or renewal.

    TTL begins at the first save using the injected aware wall clock. A reviewed
    child cannot outlive its indexed active pending parent. Expired/revoked
    digests cannot be restored via save, even if old bytes reappear on disk.
    User/source purges persist permanent tombstones before filesystem cleanup.
    """
    def __init__(self, root, *, retention_seconds, clock=None):
        if type(retention_seconds) is not int or retention_seconds<=0:
            raise ValidityRetentionError('positive_receipt_retention_required')
        if clock is not None and not callable(clock):
            raise ValidityRetentionError('aware_receipt_clock_required')
        self.base=ValidityReceiptStore(root)
        self.root=self.base.root
        self.retention_seconds=retention_seconds
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        self.catalog_path=self.root/'validity-retention.sqlite3'
        try:
            created=False
            try:
                fd=os.open(self.catalog_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:
                if (not stat.S_ISREG(self.catalog_path.lstat().st_mode)
                        or self.catalog_path.stat().st_mode&0o077):
                    raise ValidityRetentionError('invalid_retention_catalog')
            else:
                os.close(fd)
                created=True
            with self._connect() as db:
                if created:
                    db.executescript('''
                        CREATE TABLE receipts (
                            digest TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                            source_id TEXT NOT NULL, created_at TEXT NOT NULL,
                            expires_at TEXT NOT NULL, state TEXT NOT NULL
                                CHECK(state IN ('active','expired','revoked')));
                        CREATE TABLE revoked_users (user_id TEXT PRIMARY KEY);
                        CREATE TABLE revoked_sources (
                            user_id TEXT NOT NULL, source_id TEXT NOT NULL,
                            PRIMARY KEY(user_id,source_id));
                        PRAGMA user_version=1;
                    ''')
                if db.execute('PRAGMA user_version').fetchone()[0]!=1:
                    raise ValidityRetentionError('invalid_retention_catalog')
                db.execute('SELECT digest,user_id,source_id,created_at,expires_at,state FROM receipts LIMIT 0')
                db.execute('SELECT user_id FROM revoked_users LIMIT 0')
                db.execute('SELECT user_id,source_id FROM revoked_sources LIMIT 0')
        except (OSError,sqlite3.Error):
            raise ValidityRetentionError('invalid_retention_catalog') from None

    @contextmanager
    def _connect(self):
        if (self.root.is_symlink() or self.catalog_path.is_symlink()
                or not self.catalog_path.is_file()):
            raise ValidityRetentionError('invalid_retention_catalog')
        try:
            # mode=rw prevents an accidentally removed catalog from being recreated.
            db=sqlite3.connect(self.catalog_path.as_uri()+'?mode=rw',uri=True)
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
        except sqlite3.Error:
            raise ValidityRetentionError('invalid_retention_catalog') from None
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        try:
            now=self.clock()
            if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError()
            return now.astimezone(timezone.utc)
        except Exception:
            raise ValidityRetentionError('aware_receipt_clock_required') from None

    def _row(self, db, digest):
        row=db.execute('SELECT * FROM receipts WHERE digest=?',(digest,)).fetchone()
        if row is not None:
            try:
                created=datetime.fromisoformat(row['created_at'])
                expires=datetime.fromisoformat(row['expires_at'])
                if (not _identifier(row['user_id']) or not _identifier(row['source_id'])
                        or re.fullmatch(r'[0-9a-f]{64}',row['digest']) is None
                        or row['state'] not in {'active','expired','revoked'}
                        or created.tzinfo is None or expires.tzinfo is None
                        or expires<=created):
                    raise ValueError()
            except (TypeError,ValueError,KeyError):
                raise ValidityRetentionError('invalid_retention_catalog') from None
        return row

    def _blocked(self, db, user, source):
        return (db.execute('SELECT 1 FROM revoked_users WHERE user_id=?',(user,)).fetchone()
            or db.execute('SELECT 1 FROM revoked_sources WHERE user_id=? AND source_id=?',
                (user,source)).fetchone()) is not None

    def _live(self, db, row, now):
        if row is None:
            raise ValidityRetentionError('unindexed_validity_receipt')
        if row['state']!='active' or self._blocked(db,row['user_id'],row['source_id']):
            raise ValidityRetentionError('validity_receipt_unavailable')
        if now>=datetime.fromisoformat(row['expires_at']):
            db.execute("UPDATE receipts SET state='expired' WHERE digest=?",(row['digest'],))
            db.commit()
            raise ValidityRetentionError('validity_receipt_unavailable')

    def save(self, receipt, *, access_context, disclosure_policy):
        try:
            # Reconstruct immutable schema instead of trusting an arbitrary DTO.
            receipt=ReviewReceipt(receipt.raw)
            data=receipt.to_dict()
            _authorize(data,access_context,disclosure_policy)
            _validate_purge_payload(data)
            now=self._now()
            digest=receipt.digest
            with self._connect() as db:
                if self._blocked(db,data['user_id'],data['source']['id']):
                    raise ValidityRetentionError('validity_receipt_unavailable')
                row=self._row(db,digest)
                if row is not None:
                    self._live(db,row,now)
                    if (row['user_id'],row['source_id'])!=(data['user_id'],data['source']['id']):
                        raise ValidityRetentionError('invalid_retention_catalog')
                    # A missing indexed file is not silently repaired by a save.
                    self.base.read(digest,access_context=access_context,disclosure_policy=disclosure_policy)
                    return self.base.save(receipt,access_context=access_context,disclosure_policy=disclosure_policy)
                if (self.root/(digest+'.json')).exists() or (self.root/(digest+'.json')).is_symlink():
                    raise ValidityRetentionError('unindexed_validity_receipt')
                expires=now+timedelta(seconds=self.retention_seconds)
                if data['parent'] is not None:
                    parent=self._row(db,data['parent'])
                    self._live(db,parent,now)
                    parent_data=self.base.read(data['parent'],access_context=access_context,
                        disclosure_policy=disclosure_policy)
                    if (parent_data['status']!='pending' or parent_data['user_id']!=data['user_id']
                            or parent_data['source']!=data['source']):
                        raise ValidityRetentionError('invalid_receipt_parent')
                    expires=min(expires,datetime.fromisoformat(parent['expires_at']))
                self.base.save(receipt,access_context=access_context,disclosure_policy=disclosure_policy)
                db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',
                    (digest,data['user_id'],data['source']['id'],now.isoformat(),expires.isoformat(),'active'))
                return digest
        except ValidityRetentionError:
            raise
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def read(self, digest, *, access_context, disclosure_policy):
        if not isinstance(digest,str) or re.fullmatch(r'[0-9a-f]{64}',digest) is None:
            raise ValidityRetentionError('invalid_receipt_reference')
        try:
            with self._connect() as db:
                row=self._row(db,digest)
                if row is None:
                    raise ValidityRetentionError('unindexed_validity_receipt')
                decision=authorize_memory_read(access_context,user_id=row['user_id'],
                    requested_scope=None,mode='audit',disclosure_policy=disclosure_policy)
                if (not decision.allowed or PERMISSION_EVIDENCE not in access_context.permissions):
                    raise ValidityRetentionError('validity_review_access_denied')
                self._live(db,row,self._now())
                data=self.base.read(digest,access_context=access_context,disclosure_policy=disclosure_policy)
                _validate_purge_payload(data)
                if (row['user_id'],row['source_id'])!=(data['user_id'],data['source']['id']):
                    raise ValidityRetentionError('invalid_retention_catalog')
                return data
        except ValidityRetentionError:
            raise
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,AttributeError):
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def purge_user(self, user_id, *, access_context, disclosure_policy):
        self.revoke_user(user_id,access_context=access_context,disclosure_policy=disclosure_policy)
        try:
            return self.base.purge_user(user_id,access_context=access_context,
                disclosure_policy=disclosure_policy)
        except (sqlite3.Error,OSError):
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def purge_source(self, user_id, source_event_id, *, access_context, disclosure_policy):
        self.revoke_source(user_id,source_event_id,access_context=access_context,
            disclosure_policy=disclosure_policy)
        try:
            return self.base.purge_source(user_id,source_event_id,access_context=access_context,
                disclosure_policy=disclosure_policy)
        except (sqlite3.Error,OSError):
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def revoke_user(self, user_id, *, access_context, disclosure_policy):
        """Persist whole-user denial only; trusted host binds auth for callbacks."""
        _erase_access(user_id,access_context,disclosure_policy)
        try:
            with self._connect() as db:
                db.execute('INSERT OR IGNORE INTO revoked_users VALUES (?)',(user_id,))
                db.execute("UPDATE receipts SET state='revoked' WHERE user_id=?",(user_id,))
        except sqlite3.Error:
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def revoke_source(self, user_id, source_event_id, *, access_context, disclosure_policy):
        """Persist exact-source denial only; never derive authority from its ID."""
        _erase_access(user_id,access_context,disclosure_policy)
        if not _identifier(source_event_id):
            raise ValidityRetentionError('invalid_receipt_source')
        try:
            with self._connect() as db:
                db.execute('INSERT OR IGNORE INTO revoked_sources VALUES (?,?)',(user_id,source_event_id))
                db.execute("UPDATE receipts SET state='revoked' WHERE user_id=? AND source_id=?",
                    (user_id,source_event_id))
        except sqlite3.Error:
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def is_user_active(self, user_id):
        """Trusted-host tombstone status only; does not report receipt liveness."""
        if not _identifier(user_id):
            raise ValidityRetentionError('invalid_receipt_user')
        try:
            with self._connect() as db:
                return db.execute('SELECT 1 FROM revoked_users WHERE user_id=?',
                    (user_id,)).fetchone() is None
        except sqlite3.Error:
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def is_source_active(self, user_id, source_event_id):
        """Trusted-host status; user revocation also disables all its sources."""
        if not _identifier(user_id) or not _identifier(source_event_id):
            raise ValidityRetentionError('invalid_receipt_source')
        try:
            with self._connect() as db:
                return not self._blocked(db,user_id,source_event_id)
        except sqlite3.Error:
            raise ValidityRetentionError('validity_retention_operation_failed') from None

    def expire_user(self, user_id, *, access_context, disclosure_policy):
        """Explicit owner cleanup; failed unlink leaves durable denial for retry."""
        _erase_access(user_id,access_context,disclosure_policy)
        try:
            now=self._now()
            with self._connect() as db:
                rows=[self._row(db,r[0]) for r in db.execute(
                    'SELECT digest FROM receipts WHERE user_id=?',(user_id,)).fetchall()]
                expired={row['digest'] for row in rows
                    if now>=datetime.fromisoformat(row['expires_at'])}
                # Persist the expired digest tombstones before any file inspection
                # or unlink can fail. Whole-user metadata authorization is above.
                db.executemany("UPDATE receipts SET state='expired' WHERE digest=? AND state='active'",
                    [(digest,) for digest in expired])
                db.commit()
                targets=[]
                for path in sorted(self.root.glob('*.json')):
                    if (re.fullmatch(r'[0-9a-f]{64}\.json',path.name) is None
                            or not stat.S_ISREG(path.lstat().st_mode)):
                        raise ValidityRetentionError('invalid_receipt_file')
                    row=self._row(db,path.stem)
                    if row is None:
                        raise ValidityRetentionError('unindexed_validity_receipt')
                    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    with os.fdopen(fd,'rb') as stream:
                        raw=stream.read(MAX_BYTES+1)
                    if len(raw)>MAX_BYTES:
                        raise ValidityRetentionError('invalid_receipt_file')
                    receipt=ReviewReceipt(raw.decode('utf-8'))
                    data=receipt.to_dict()
                    _validate_purge_payload(data)
                    if (receipt.digest!=path.stem or
                            (row['user_id'],row['source_id'])!=(data['user_id'],data['source']['id'])):
                        raise ValidityRetentionError('invalid_receipt_file')
                    if path.stem in expired:
                        _authorize(data,access_context,disclosure_policy)
                        targets.append(path)
            for path in targets:
                path.unlink()
            return len(targets)
        except ValidityRetentionError:
            raise
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,AttributeError,ExtractionValidationError):
            raise ValidityRetentionError('validity_retention_operation_failed') from None
