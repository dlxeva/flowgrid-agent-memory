"""Opt-in host read gate. No authentication backend, default grants or transport.

All host writers/revocations must share serialize_user. Callbacks are trusted
Python and must query current identity/lifecycle. Returned values are snapshots;
call again immediately before disclosure/use. Unguarded SDK/SQL is not protected.
"""
from contextlib import contextmanager
import json
from .auth import TrustedPrincipal, authorize_operation, OPERATION_READ_CURRENT, OPERATION_READ_AUDIT
from .context import ContextCompiler


class HostReadError(PermissionError):
    pass


class DeferredReadMemory:
    """Allow trusted read gates to authorize before opening storage."""
    def __init__(self, load):
        self._load = load

    def query_memory(self, **kwargs):
        return self._load().query_memory(**kwargs)

    def _active(self):
        return self._load()._active()


class LifecycleMemoryReader:
    def __init__(self, *, memory, refresh_principal, serialize_user, is_user_active,
                 is_source_active, enabled=False):
        if type(enabled) is not bool or any(not callable(c) for c in
                (refresh_principal, serialize_user, is_user_active, is_source_active)):
            raise HostReadError('explicit_host_read_callbacks_required')
        self.memory = memory
        self.refresh_principal = refresh_principal
        self.serialize_user = serialize_user
        self.is_user_active = is_user_active
        self.is_source_active = is_source_active
        self.enabled = enabled

    def _refresh(self, principal, user_id, scope, mode):
        if not self.enabled:
            raise HostReadError('host_reader_disabled')
        if not isinstance(principal, TrustedPrincipal):
            raise HostReadError('host_identity_required')
        try:
            current = self.refresh_principal(principal)
            if not isinstance(current, TrustedPrincipal) or current.principal_id != principal.principal_id:
                raise ValueError()
            access = authorize_operation(current, user_id=user_id, requested_scope=scope,
                operation=OPERATION_READ_AUDIT if mode == 'audit' else OPERATION_READ_CURRENT)
        except Exception:
            raise HostReadError('host_read_access_denied') from None
        return current, access

    def _live(self, user_id, sources):
        try:
            if self.is_user_active(user_id) is not True:
                raise HostReadError('host_user_revoked')
            if any(self.is_source_active(user_id, source) is not True for source in sources):
                raise HostReadError('host_source_revoked')
        except HostReadError:
            raise
        except Exception:
            raise HostReadError('host_lifecycle_unavailable') from None

    @contextmanager
    def _session(self, principal, user_id, scope, mode):
        if mode not in {'current', 'audit'}:
            raise HostReadError('invalid_read_mode')
        # Check permission before any lock/lifecycle callback or DB access.
        self._refresh(principal, user_id, scope, mode)
        try:
            with self.serialize_user(user_id):
                current, access = self._refresh(principal, user_id, scope, mode)
                self._live(user_id, ())
                yield current, access
        except HostReadError:
            raise
        except Exception:
            raise HostReadError('host_read_unavailable') from None

    def _sources(self, user_id, state):
        sources = {source for record in state.records for source in record.source_event_ids}
        sources.update(event.id for event in state.raw_events)
        ids = set(state.withheld_record_ids) | set(state.conflicts)
        ids.update(event.record_id for event in state.state_events)
        if ids:
            # Metadata for IDs already returned by an authorized scoped query;
            # no raw text or additional audit grant is added to the caller.
            with self.memory._active().db.connection() as db:
                placeholders = ','.join('?' for _ in ids)
                rows = db.execute('SELECT id,source_event_ids FROM memory_records WHERE user_id=? AND id IN ('+
                    placeholders+')', (user_id, *sorted(ids))).fetchall()
            if len(rows) != len(ids):
                raise HostReadError('host_source_binding_unavailable')
            for row in rows:
                values = json.loads(row['source_event_ids'])
                if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                    raise HostReadError('host_source_binding_unavailable')
                sources.update(values)
        return sources

    def _finish(self, principal, initial, user_id, scope, mode, sources):
        current, _ = self._refresh(principal, user_id, scope, mode)
        if current != initial:
            raise HostReadError('host_read_grant_changed')
        self._live(user_id, sources)

    def _query(self, user_id, access, mode, scope, options):
        if set(options) - {'memory_key', 'query', 'as_of', 'max_records'}:
            raise HostReadError('invalid_read_options')
        result = self.memory.query_memory(user_id=user_id, access_context=access.access_context,
            disclosure_policy=access.disclosure_policy, mode=mode,
            scope=dict(access.effective_scope), **options)
        if not result.allowed or result.state is None:
            raise HostReadError('host_read_access_denied')
        sources = self._sources(user_id, result.state)
        self._live(user_id, sources)
        return result, sources

    def query_memory(self, *, user_id, principal, scope=None, mode='current', **options):
        with self._session(principal, user_id, scope, mode) as (current, access):
            result, sources = self._query(user_id, access, mode, scope, options)
            self._finish(principal, current, user_id, scope, mode, sources)
            return result

    def query_current(self, **kwargs):
        return self.query_memory(mode='current', **kwargs)

    def query_audit(self, **kwargs):
        return self.query_memory(mode='audit', **kwargs)

    def read_source_events(self, *, user_id, principal, source_event_ids, scope=None):
        """Read exact raw evidence with explicit audit-purpose authorization.

        Works without derived records. Missing, out-of-scope or revoked sources
        refuse the whole result; this never creates current/confirmed state.
        """
        if (type(source_event_ids) not in (list, tuple) or not 1 <= len(source_event_ids) <= 32
                or any(type(value) is not str or not value or len(value) > 256 for value in source_event_ids)
                or len(set(source_event_ids)) != len(source_event_ids)):
            raise HostReadError('bounded_unique_source_ids_required')
        sources = set(source_event_ids)
        _, initial_access = self._refresh(principal, user_id, scope, 'audit')
        canonical = initial_access.effective_scope['user']
        with self._session(principal, canonical, scope, 'audit') as (current, access):
            canonical_user = access.effective_scope['user']
            self._live(canonical_user, sources)
            events = self.memory._active().db.raw_events_for_extraction(
                user_id=canonical_user, event_ids=list(source_event_ids))
            if len(events) != len(sources) or {event.id for event in events} != sources:
                raise HostReadError('host_source_unavailable')
            if any(any(event.scope.get(key) != value for key, value in access.effective_scope.items()) for event in events):
                raise HostReadError('host_source_access_denied')
            self._finish(principal, current, canonical_user, scope, 'audit', sources)
            by_id = {event.id: event for event in events}
            return tuple(by_id[value] for value in source_event_ids)

    def compile_context(self, *, user_id, principal, scope=None, mode='current',
                        max_chars=None, max_tokens=None, token_counter=None, **options):
        with self._session(principal, user_id, scope, mode) as (current, access):
            result, sources = self._query(user_id, access, mode, scope, options)
            pack = ContextCompiler(access.disclosure_policy, token_counter).compile(result.state,
                max_chars=max_chars, max_tokens=max_tokens)
            self._finish(principal, current, user_id, scope, mode, sources)
            return pack

    def compose_numeric_evidence(self, *, user_id, principal, spans, requested_scope=None, **options):
        from .extraction import EvidenceSpan
        if type(spans) not in (list, tuple) or not 2 <= len(spans) <= 32 or any(type(s) is not EvidenceSpan for s in spans):
            raise HostReadError('bounded_typed_spans_required')
        sources = {span.source_event_id for span in spans}
        with self._session(principal, user_id, requested_scope, 'audit') as (current, access):
            if 'memory:evidence' not in current.permissions:
                raise HostReadError('host_read_access_denied')
            if set(options) - {'unit', 'operation', 'enabled', 'allow_cross_session'}:
                raise HostReadError('invalid_read_options')
            self._live(user_id, sources)
            result = self.memory.compose_numeric_evidence(user_id=user_id, principal=current,
                spans=spans, requested_scope=dict(access.effective_scope), **options)
            self._finish(principal, current, user_id, requested_scope, 'audit', sources)
            return result
