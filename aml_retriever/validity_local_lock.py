"""Opt-in local SQLite serialization for trusted validity-host operations.

All participating local processes must share this exact private directory and
use this adapter for EVERY writer. No remote/distributed locking is provided.
Deleting or restoring the directory/catalogs during use is unsupported and
fails closed when detectable. This lock does not authenticate principals; the
host must authorize operations before entering it. No default wiring exists.
"""
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time


class ValidityLocalLockError(ValueError):
    """Adapter failures use fixed codes; caller exceptions propagate unchanged."""


def _safe_user(value):
    return (isinstance(value,str) and bool(value.strip()) and value==value.strip()
        and len(value)<=256 and all(ord(c)>=32 and ord(c)!=127 for c in value))


class LocalValidityUserLock:
    """Callable ``serialize_user`` adapter with same-instance/thread reentrancy.

    BEGIN IMMEDIATE holds each user's database write reservation until exit.
    Connection close/process exit releases that reservation. A minimal registry
    records lock-file identities so missing/replaced files are not recreated.
    SQLite and filesystem publication are not one transaction: a crash during
    initial registration can leave an orphan which requires trusted recovery.
    Separate instances nested on one thread may time out; they do not share a
    reentrancy cache. Forked children must construct a new adapter; inherited
    adapters are rejected rather than reusing copied thread-local lock state.
    """
    def __init__(self, root, *, timeout_seconds=5.0):
        if (isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float))
                or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=60):
            raise ValidityLocalLockError('finite_local_lock_timeout_required')
        self.root=Path(root)
        self.timeout_seconds=float(timeout_seconds)
        self.process_id=os.getpid()
        self.local=threading.local()
        self.registry=self.root/'validity-lock-registry.sqlite3'
        try:
            if not self.root.is_absolute():
                raise ValidityLocalLockError('private_existing_lock_directory_required')
            self.root_identity=self._private_stat(self.root,directory=True)
            if not self.registry.exists() and not self.registry.is_symlink():
                # Existing lock databases without their registry must not become
                # a fresh locking domain after accidental metadata loss.
                if any(re.fullmatch(r'[0-9a-f]{64}\.sqlite3',p.name)
                        for p in self.root.iterdir()):
                    raise ValidityLocalLockError('invalid_local_lock_catalog')
                self._publish(self.registry,registry=True)
            self.registry_identity=self._private_stat(self.registry)
            with self._db(self.registry,time.monotonic()+self.timeout_seconds) as db:
                self._registry_schema(db)
        except (OSError,sqlite3.Error,TypeError,ValueError) as error:
            if isinstance(error,ValidityLocalLockError):
                raise
            raise ValidityLocalLockError('invalid_local_lock_catalog') from None

    def _private_stat(self, path, *, directory=False):
        info=path.lstat()
        expected=stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_mode&0o077
                or (directory and info.st_mode&0o700!=0o700)
                or (not directory and info.st_mode&0o600!=0o600)
                or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid())):
            raise ValidityLocalLockError('private_local_lock_path_required')
        return info.st_dev,info.st_ino

    def _verify_root(self):
        if (self._private_stat(self.root,directory=True)!=self.root_identity
                or self._private_stat(self.registry)!=self.registry_identity):
            raise ValidityLocalLockError('local_lock_path_changed')

    @contextmanager
    def _db(self, path, deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise ValidityLocalLockError('local_lock_timeout')
        db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=remaining)
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    def _publish(self, target, *, registry=False, digest=None):
        fd,temporary=tempfile.mkstemp(prefix='.validity-lock-init-',suffix='.sqlite3',dir=self.root)
        os.close(fd)
        temporary=Path(temporary)
        try:
            db=sqlite3.connect(temporary)
            try:
                with db:
                    if registry:
                        db.execute('CREATE TABLE locks (digest TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL)')
                        db.execute('CREATE TABLE directory (device INTEGER NOT NULL,inode INTEGER NOT NULL)')
                        db.execute('INSERT INTO directory VALUES (?,?)',self.root_identity)
                    else:
                        db.execute('CREATE TABLE lock_identity (digest TEXT PRIMARY KEY)')
                        db.execute('INSERT INTO lock_identity VALUES (?)',(digest,))
                    db.execute('PRAGMA user_version=1')
            finally:
                db.close()
            # Publish only initialized bytes and never overwrite another creator.
            try:
                os.link(temporary,target,follow_symlinks=False)
            except FileExistsError:
                if not registry:
                    raise ValidityLocalLockError('local_lock_path_changed') from None
        finally:
            temporary.unlink(missing_ok=True)

    def _registry_schema(self, db):
        if (db.execute('PRAGMA user_version').fetchone()[0]!=1
                or db.execute('SELECT device,inode FROM directory').fetchall()!=[self.root_identity]):
            raise ValidityLocalLockError('invalid_local_lock_catalog')
        db.execute('SELECT digest,device,inode FROM locks LIMIT 0')

    def _identity(self, db, digest):
        if (db.execute('PRAGMA user_version').fetchone()[0]!=1
                or db.execute('SELECT digest FROM lock_identity').fetchall()!=[(digest,)]):
            raise ValidityLocalLockError('invalid_local_lock_catalog')

    def _registered_path(self, digest, deadline):
        path=self.root/(digest+'.sqlite3')
        with self._db(self.registry,deadline) as db:
            db.execute('BEGIN IMMEDIATE')
            self._registry_schema(db)
            row=db.execute('SELECT device,inode FROM locks WHERE digest=?',(digest,)).fetchone()
            if row is None:
                if path.exists() or path.is_symlink():
                    raise ValidityLocalLockError('invalid_local_lock_catalog')
                self._publish(path,digest=digest)
                row=self._private_stat(path)
                db.execute('INSERT INTO locks VALUES (?,?,?)',(digest,*row))
            if self._private_stat(path)!=row:
                raise ValidityLocalLockError('local_lock_path_changed')
            db.commit()
        return path,row

    @contextmanager
    def __call__(self, user_id):
        if os.getpid()!=self.process_id:
            raise ValidityLocalLockError('local_lock_process_changed')
        if not _safe_user(user_id):
            raise ValidityLocalLockError('invalid_local_lock_user')
        digest=hashlib.sha256(user_id.encode('utf-8')).hexdigest()
        held=getattr(self.local,'held',None)
        if held is None:
            held=self.local.held={}
        entered=False
        try:
            self._verify_root()
            if digest in held:
                path,identity=held[digest]
                if self._private_stat(path)!=identity:
                    raise ValidityLocalLockError('local_lock_path_changed')
                entered=True
                yield
                return
            deadline=time.monotonic()+self.timeout_seconds
            path,identity=self._registered_path(digest,deadline)
            with self._db(path,deadline) as db:
                db.execute('BEGIN IMMEDIATE')
                self._verify_root()
                if self._private_stat(path)!=identity:
                    raise ValidityLocalLockError('local_lock_path_changed')
                self._identity(db,digest)
                held[digest]=(path,identity)
                try:
                    entered=True
                    yield
                finally:
                    held.pop(digest,None)
                    # A transaction with no data writes only releases the lock.
                    db.rollback()
        except sqlite3.OperationalError as error:
            if entered:
                raise
            code=('local_lock_timeout' if getattr(error,'sqlite_errorcode',None)
                in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else 'invalid_local_lock_catalog')
            raise ValidityLocalLockError(code) from None
        except (OSError,sqlite3.Error):
            if entered:
                raise
            raise ValidityLocalLockError('invalid_local_lock_catalog') from None
