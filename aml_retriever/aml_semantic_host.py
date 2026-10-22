"""Explicit opt-in OpenAI cue host, with private durable finite-budget hooks.

No credentials are read by imports or config validation. This module does not
provide deployment/spend authority: the operator supplies a fixed approved
runtime digest, scope, limits and private files. The enabled host offers every
message to the existing complete-coverage adapter. No provider fallback.
"""
import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import stat
import threading
import time
from collections import deque

from .aml_model_budget import BudgetPolicy, ModelBudget, MODEL

_REQUEST = threading.local()


@dataclass(frozen=True)
class SemanticHostConfig:
    model_key_file: str
    cue_db_path: str
    budget_db_path: str
    policy: BudgetPolicy
    enabled: bool = False
    queue_capacity: int = 16

    def validate(self, hosted):
        if self.enabled is not True or not isinstance(self.policy, BudgetPolicy):
            raise ValueError('semantic_host_disabled')
        paths = [self.model_key_file, self.cue_db_path, self.budget_db_path, hosted.db_path]
        for value in paths:
            p = Path(value)
            if not p.is_absolute() or p.is_symlink() or p != p.resolve():
                raise ValueError('semantic_host_absolute_real_paths')
        if len(set(paths)) != 4:
            raise ValueError('semantic_host_separate_files')
        if hosted.operation_timeout_seconds < 265:
            raise ValueError('semantic_host_worker_deadline')
        if (type(self.queue_capacity) is not int or not 16 <= self.queue_capacity <= 64 or
                hosted.max_inflight < self.queue_capacity or
                hosted.max_connections < self.queue_capacity + 2):
            raise ValueError('semantic_host_queue_capacity')
        return {'enabled': True, 'model': MODEL, 'budget_microusd': self.policy.budget_microusd,
                'max_attempts': self.policy.max_attempts, 'input_token_limit': 32768,
                'output_token_limit': 768, 'input_bytes_limit': 16384,
                'complete_add_coverage': True, 'network_timeout_seconds':120,
                'process_timeout_seconds':130, 'network_called': False,
                'credentials_read': False, 'queue_capacity': self.queue_capacity,
                'model_concurrency': 1, 'queue_deadline_included': True}


def _private_key(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (os.name == 'posix' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
            raise ValueError('semantic_host_private_key_file')
        raw = os.read(fd, 4098)
        if len(raw) > 4097:
            raise ValueError('semantic_host_key_size')
        key = raw.decode('ascii').rstrip('\r\n')
        if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError('semantic_host_key_format')
        return key
    finally:
        os.close(fd)


class _GuardedTransport:
    provider = 'openai'

    def __init__(self, transport, ledger, cues):
        self.transport, self.ledger, self.cues = transport, ledger, cues
        self.lock = threading.Lock()

    def __call__(self, wire):
        # Reject busy admission immediately: queuing a paid worker behind
        # another request could start it after the HTTP operation timed out.
        if not self.lock.acquire(blocking=False):
            raise ValueError('semantic_host_busy')
        try:
            try:
                context = getattr(_REQUEST, 'context', None)
                raw = (self.transport(wire) if context is None else
                       self.transport(wire, deadline=context[0], cancelled=context[1]))
                result, _, _ = self.cues.completion(raw, provider='openai')
                data = self.cues.decode(wire['messages'][1]['content'])
                if set(data) == {'messages'}:
                    # Validate each complete-coverage chunk against its own
                    # original indices before allowing any raw Add commit.
                    selected = data['messages']
                    if (not isinstance(result, dict) or set(result) != {'cues'} or
                            not isinstance(result['cues'], list) or len(result['cues']) > len(selected)):
                        raise ValueError('cue_shape')
                    allowed = {m['index'] for m in selected}; seen = set()
                    for cue in result['cues']:
                        if (not isinstance(cue, dict) or set(cue) != {'index', 'keywords'} or
                                type(cue['index']) is not int or cue['index'] not in allowed or cue['index'] in seen):
                            raise ValueError('cue_source_index')
                        seen.add(cue['index'])
                        self.cues.strings(cue['keywords'], self.cues.MAX_KEYWORDS)
                else:
                    if not isinstance(result, dict) or set(result) != {'queries'}:
                        raise ValueError('cue_shape')
                    self.cues.strings(result['queries'], self.cues.MAX_QUERIES)
                return raw
            except Exception:
                self.ledger.halt()
                raise
        finally:
            self.lock.release()


class _AdmissionService:
    """Bounded FIFO; waiting counts against the original HTTP deadline.

    No background worker owns queued work. Its HTTP executor thread must still
    be live, uncancelled and within its deadline before entering the backend.
    """
    def __init__(self, service, capacity=16, operation_seconds=270):
        if type(capacity) is not int or not 1 <= capacity <= 128:
            raise ValueError('semantic_host_queue_capacity')
        self._service = service
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = False
        self._closing = False
        self._active_cancelled = None
        self._capacity = capacity
        self._operation_seconds = operation_seconds

    def __getattr__(self, name):
        return getattr(self._service, name)

    def _call(self, name, payload, deadline=None, cancelled=None):
        from .api import ApiError
        deadline = time.monotonic() + self._operation_seconds if deadline is None else deadline
        cancelled = threading.Event() if cancelled is None else cancelled
        ticket = object()
        def expired():
            return self._closing or cancelled.is_set() or time.monotonic() >= deadline
        with self._condition:
            if expired() or len(self._waiting) + int(self._active) >= self._capacity:
                raise ApiError(429, 'semantic queue unavailable')
            self._waiting.append(ticket)
            try:
                while self._active or self._waiting[0] is not ticket:
                    if expired():
                        raise ApiError(429, 'semantic queue expired')
                    self._condition.wait(min(.05, max(0, deadline - time.monotonic())))
                if expired():
                    raise ApiError(429, 'semantic queue expired')
                self._waiting.popleft()
                self._active = True
                self._active_cancelled = cancelled
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
        previous = getattr(_REQUEST, 'context', None)
        _REQUEST.context = (deadline, cancelled)
        try:
            if expired():
                raise ApiError(429, 'semantic queue expired')
            return getattr(self._service, name)(payload)
        finally:
            _REQUEST.context = previous
            with self._condition:
                self._active = False
                self._active_cancelled = None
                self._condition.notify_all()

    def cancel_pending(self):
        with self._condition:
            self._closing = True
            if self._active_cancelled is not None:
                self._active_cancelled.set()
            self._condition.notify_all()

    def dispatch(self, route, payload, deadline, cancelled):
        from .api import ApiError
        names = {'/add': 'official_add', '/search': 'official_search'}
        if route not in names:
            raise ApiError(404, 'route not found')
        return self._call(names[route], payload, deadline, cancelled)

    def official_add(self, payload): return self._call('official_add', payload)
    def official_search(self, payload): return self._call('official_search', payload)
    def delete_user(self, user_id): return self._call('delete_user', user_id)


def create_server(hosted, semantic):
    """Construct loopback listener; caller owns serve/shutdown/close lifecycle."""
    semantic.validate(hosted)
    from . import aml_semantic_cues as cues
    from .aml_hosted import HostedAMLServer, hosted_retrieval_config, retrieval_config_digest, runtime_identity
    from .api import MemoryService
    identity = runtime_identity()
    if identity.get('runtime_digest_sha256') != semantic.policy.implementation_sha256:
        raise ValueError('semantic_host_runtime_drift')
    if hosted.require_build_identity and (identity.get('identity_source') != 'build_manifest' or
                                          identity.get('source_state') != 'committed'):
        raise ValueError('semantic_host_fixed_build_required')
    retrieval = hosted_retrieval_config(hosted)
    if retrieval_config_digest(retrieval) != semantic.policy.profile_sha256:
        raise ValueError('semantic_host_profile_drift')
    ledger = ModelBudget(semantic.budget_db_path, semantic.policy, enabled=True)
    key = _private_key(semantic.model_key_file)
    transport = cues.OpenAICueTransport(key, reserve=ledger.reserve, settle=ledger.settle,
                                      unknown=ledger.unknown, enabled=True,
                                      network_seconds=120, process_seconds=130)
    core = MemoryService(retrieval)
    service = None
    try:
        service = cues.SemanticCueService(core, _GuardedTransport(transport, ledger, cues),
                    enabled=True, cue_db_path=semantic.cue_db_path, provider='openai',
                    complete_add_coverage=True)
        admission = _AdmissionService(service, semantic.queue_capacity, hosted.operation_timeout_seconds)
        server = HostedAMLServer(replace(hosted, retrieval_json='{}'), service=admission,
                                 quiet=True, deadline_dispatch=admission.dispatch,
                                 deadline_shutdown=admission.cancel_pending)
    except BaseException:
        if service is not None: service.close()
        else: core.close()
        raise
    server.identity.update(semantic_budget_enforced=True, semantic_cues_add_input_coverage='complete',
        semantic_cues_protocol=cues.COMPLETE_PROTOCOL, semantic_cues_model=MODEL, semantic_cues_provider='openai',
        semantic_cues_default_service=False, semantic_cues_gain_verified=False,
        semantic_http_queue_capacity=semantic.queue_capacity, semantic_model_concurrency=1,
        semantic_queue_deadline_included=True)
    # Local trusted-operator inspection only; not included in public health.
    server.model_budget = ledger
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit finite-budget OpenAI cue service')
    parser.add_argument('--config', required=True, help='Absolute local JSON config; contains paths, never keys')
    parser.add_argument('--check', action='store_true', help='Validate without reading keys or opening a listener')
    args = parser.parse_args(argv)
    server = None
    try:
        from .aml_hosted import HostedConfig
        path = Path(args.config)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('semantic_host_config_path')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise ValueError('semantic_host_duplicate_config')
                value[key] = item
            return value
        data = json.loads(path.read_text(), object_pairs_hook=unique)
        # API credential is read only for actual startup. --check uses a dummy
        # token purely for HostedConfig's shape checks, never as service auth.
        if set(data) != {'hosted', 'api_key_file', 'semantic', 'policy'}:
            raise ValueError('semantic_host_config_shape')
        if 'credential' in data['hosted']:
            raise ValueError('semantic_host_no_inline_key')
        if data['hosted'].get('require_build_identity', True) is not True:
            raise ValueError('semantic_host_fixed_build_required')
        data['hosted']['require_build_identity'] = True
        api_path = Path(data['api_key_file'])
        if not api_path.is_absolute() or api_path != api_path.resolve():
            raise ValueError('semantic_host_api_key_path')
        if str(api_path) in {data['semantic'].get(k) for k in ('model_key_file', 'cue_db_path', 'budget_db_path')} | {data['hosted'].get('db_path')}:
            raise ValueError('semantic_host_separate_auth')
        hosted = HostedConfig(credential='synthetic-config-check' if args.check else _private_key(api_path), **data['hosted'])
        semantic = SemanticHostConfig(policy=BudgetPolicy(**data['policy']), **data['semantic'])
        report = semantic.validate(hosted)
        if args.check:
            print(json.dumps(report, sort_keys=True))
            return 0
        server = create_server(hosted, semantic)
        print('[aml-semantic-host] ready', flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('[aml-semantic-host] configuration or runtime failure', flush=True)
        return 1
    finally:
        if server is not None:
            server.server_close()
            server.service.close()


if __name__ == '__main__':
    raise SystemExit(main())
