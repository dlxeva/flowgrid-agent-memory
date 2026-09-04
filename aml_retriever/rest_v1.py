"""Authenticated, loopback-only governed REST v1 adapter.

This server is intentionally separate from ``aml_retriever.server``.  The
legacy AML Add/Search handler remains a compatibility adapter and is never
used as this product security boundary.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from ._version import PRODUCT_VERSION
from .auth import (
    OPERATION_COMPILE_AUDIT,
    OPERATION_COMPILE_CURRENT,
    OPERATION_ERASE,
    OPERATION_EXTRACT,
    OPERATION_INGEST,
    OPERATION_READ_AUDIT,
    OPERATION_READ_CURRENT,
    OPERATION_TRANSITION,
    AuthorizationError,
    TrustedPrincipal,
    authorize_operation,
)
from .extraction import ExtractionConflict, ExtractionError
from .facade import FlowGridMemory
from .governance import GovernanceConflict, GovernanceError
from .context import (
    MAX_CONTEXT_RECORDS,
    MAX_CONTEXT_REQUEST_CHARS,
    ContextCompiler,
)


REST_SCHEMA = "flowgrid.rest/v1"
DEFAULT_MAX_BODY_BYTES = 1_048_576
MAX_MAX_BODY_BYTES = 16 * 1_048_576
REQUEST_READ_TIMEOUT_SECONDS = 2.0
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENTIFIER = re.compile(r"[^\x00-\x1f\x7f]{1,256}")

_POST_PATHS = frozenset(
    {
        "/v1/events",
        "/v1/extractions",
        "/v1/memories/query",
        "/v1/context/compile",
        "/v1/memories/transition",
        "/v1/admin/erase-user",
    }
)
_ALL_PATHS = _POST_PATHS | {"/v1/health"}

_ERRORS = MappingProxyType(
    {
        "bad_request": (400, "bad_request", "request rejected"),
        "unauthorized": (401, "unauthorized", "authentication required"),
        "forbidden": (403, "forbidden", "operation not allowed"),
        "not_found": (404, "not_found", "route not found"),
        "method_not_allowed": (405, "method_not_allowed", "method not allowed"),
        "conflict": (409, "conflict", "operation conflicts with current state"),
        "body_too_large": (413, "body_too_large", "request body too large"),
        "unsupported_media_type": (415, "unsupported_media_type", "application/json required"),
        "invalid_request": (422, "invalid_request", "request validation failed"),
        "unsupported_token_budget": (422, "unsupported_token_budget", "exact token budget unavailable"),
        "expectation_failed": (417, "expectation_failed", "expectation not supported"),
        "uri_too_long": (414, "uri_too_long", "request target too long"),
        "headers_too_large": (431, "headers_too_large", "request headers too large"),
        "http_version_not_supported": (
            505,
            "http_version_not_supported",
            "HTTP version not supported",
        ),
        "service_unavailable": (503, "service_unavailable", "service unavailable"),
        "internal_error": (500, "internal_error", "internal operation failed"),
    }
)


class RestConfigurationError(ValueError):
    """Fixed startup configuration failure without secret/path values."""


class RequestValidationError(ValueError):
    """Untrusted request failed a strict schema check."""


def _config_error() -> RestConfigurationError:
    return RestConfigurationError("REST configuration rejected")


def _safe_string(value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise RequestValidationError
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise RequestValidationError
    return normalized


def _exact_object(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RequestValidationError
    if not required.issubset(value) or set(value) - required - optional:
        raise RequestValidationError
    return value


def _strict_json(raw: bytes) -> object:
    def object_pairs(pairs):
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RequestValidationError
            result[key] = value
        return result

    def reject_constant(_value):
        raise RequestValidationError

    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except RequestValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RequestValidationError from None


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _config_json(raw: str) -> object:
    try:
        return _strict_json(raw.encode("utf-8"))
    except (RequestValidationError, UnicodeEncodeError):
        raise _config_error() from None


def _config_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _config_error()
    return value


def _config_exact(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    data = _config_mapping(value)
    if not required.issubset(data) or set(data) - required - optional:
        raise _config_error()
    return data


def _config_string(value: object, *, allow_star: bool = False) -> str:
    try:
        normalized = _safe_string(value)
    except RequestValidationError:
        raise _config_error() from None
    if normalized == "*" and not allow_star:
        raise _config_error()
    return normalized


def _principal_from_mapping(value: object) -> TrustedPrincipal:
    data = _config_exact(
        value,
        required=frozenset(
            {"principal_id", "authority", "allowed_users", "scopes", "permissions"}
        ),
        optional=frozenset({"purpose", "allowed_audit_purposes"}),
    )
    allowed_users = data["allowed_users"]
    permissions = data["permissions"]
    audit_purposes = data.get("allowed_audit_purposes", [])
    if not isinstance(allowed_users, list) or not isinstance(permissions, list):
        raise _config_error()
    if not isinstance(audit_purposes, list):
        raise _config_error()
    try:
        return TrustedPrincipal(
            principal_id=data["principal_id"],
            authority=data["authority"],
            allowed_users=frozenset(allowed_users),
            scopes=_config_mapping(data["scopes"]),
            permissions=frozenset(permissions),
            purpose=data.get("purpose"),
            allowed_audit_purposes=frozenset(audit_purposes),
        )
    except (TypeError, ValueError):
        raise _config_error() from None


@dataclass(frozen=True)
class GovernedRestConfig:
    """Fully resolved startup configuration.

    ``bearer_token`` is injected directly or resolved from a named environment
    variable by :func:`load_rest_config`; it is never serialized or returned.
    """

    db_path: str
    principal: TrustedPrincipal
    auth_mode: str
    bind_host: str = "127.0.0.1"
    port: int = 8081
    bearer_token: str | None = field(default=None, repr=False, compare=False)
    erase_enabled: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise _config_error()
        db_path = self.db_path.strip()
        if db_path != ":memory:" and not Path(db_path).is_absolute():
            raise _config_error()
        object.__setattr__(self, "db_path", db_path)
        if not isinstance(self.principal, TrustedPrincipal):
            raise _config_error()
        if self.bind_host != "127.0.0.1":
            raise _config_error()
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise _config_error()
        if self.auth_mode not in {"local", "bearer"}:
            # In particular, ``none`` is never a legal product-server mode.
            raise _config_error()
        if not isinstance(self.erase_enabled, bool):
            raise _config_error()
        if (
            isinstance(self.max_body_bytes, bool)
            or not isinstance(self.max_body_bytes, int)
            or not 1 <= self.max_body_bytes <= MAX_MAX_BODY_BYTES
        ):
            raise _config_error()
        if self.auth_mode == "bearer":
            token = self.bearer_token
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 4096
                or any(not 33 <= ord(char) <= 126 for char in token)
            ):
                raise _config_error()
        elif self.bearer_token is not None:
            raise _config_error()

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "GovernedRestConfig":
        data = _config_exact(
            value,
            required=frozenset({"schema", "db_path", "bind", "auth"}),
            optional=frozenset({"erase", "limits"}),
        )
        if data["schema"] != REST_SCHEMA:
            raise _config_error()
        bind = _config_exact(data["bind"], required=frozenset({"host", "port"}))
        auth = _config_exact(
            data["auth"],
            required=frozenset({"mode", "principal"}),
            optional=frozenset({"token_env"}),
        )
        erase = _config_exact(
            data.get("erase", {"enabled": False}),
            required=frozenset({"enabled"}),
        )
        limits = _config_exact(
            data.get("limits", {"max_body_bytes": DEFAULT_MAX_BODY_BYTES}),
            required=frozenset({"max_body_bytes"}),
        )
        mode = auth["mode"]
        token: str | None = None
        if mode == "bearer":
            if "token_env" not in auth:
                raise _config_error()
            token_env = auth["token_env"]
            if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
                raise _config_error()
            source = os.environ if environ is None else environ
            token = source.get(token_env)
        elif "token_env" in auth:
            raise _config_error()
        try:
            return cls(
                db_path=data["db_path"],
                principal=_principal_from_mapping(auth["principal"]),
                auth_mode=mode,
                bind_host=bind["host"],
                port=bind["port"],
                bearer_token=token,
                erase_enabled=erase["enabled"],
                max_body_bytes=limits["max_body_bytes"],
            )
        except RestConfigurationError:
            raise
        except (TypeError, ValueError):
            raise _config_error() from None


def load_rest_config(
    path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> GovernedRestConfig:
    """Load one strict product config without echoing its path or secret."""

    if not isinstance(path, str) or not path.strip():
        raise _config_error()
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise _config_error() from None
    return GovernedRestConfig.from_mapping(_config_json(raw), environ=environ)


def _scope(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RequestValidationError
    allowed = {"tenant", "user", "project", "agent", "session", "repository"}
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in allowed:
            raise RequestValidationError
        result[key] = _safe_string(item)
    return result


def _optional_string(data: Mapping[str, object], key: str, *, maximum: int = 256) -> str | None:
    if key not in data or data[key] is None:
        return None
    return _safe_string(data[key], maximum=maximum)


def _positive_int(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise RequestValidationError
    return value


def _nonnegative_int(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RequestValidationError
    return value


class GovernedRestAdapter:
    """Own one governed facade and one independent loopback HTTP server."""

    def __init__(
        self,
        config: GovernedRestConfig,
        *,
        memory_factory=FlowGridMemory,
        server_class=None,
    ):
        if not isinstance(config, GovernedRestConfig):
            raise _config_error()
        # Re-run immutable config validation before the first writable DB open.
        config.__post_init__()
        self.config = config
        self._closed = False
        self._closing = False
        self._active_requests = 0
        self._lifecycle = threading.Condition(threading.RLock())
        self._serve_started = threading.Event()
        self._memory = None
        self._server = None
        memory = memory_factory(db_path=config.db_path)
        self._memory = memory
        server_type = server_class or _LoopbackThreadingHTTPServer
        server = None
        try:
            handler = _handler_for(self)
            server = server_type((config.bind_host, config.port), handler)
            if server.server_address[0] != "127.0.0.1":
                raise _config_error()
            self._server = server
        except BaseException:
            try:
                if server is not None:
                    server.server_close()
                memory.close()
            finally:
                self._memory = None
            raise

    @property
    def address(self) -> tuple[str, int]:
        server = self._server
        if server is None:
            raise RuntimeError("REST adapter is closed")
        host, port = server.server_address[:2]
        return str(host), int(port)

    @property
    def closed(self) -> bool:
        with self._lifecycle:
            return self._closed

    def serve_forever(self, *, poll_interval: float = 0.1) -> None:
        with self._lifecycle:
            server = self._server
            if self._closed or self._closing or server is None:
                raise RuntimeError("REST adapter is closed")
            self._serve_started.set()
        try:
            server.serve_forever(poll_interval=poll_interval)
        finally:
            with self._lifecycle:
                self._serve_started.clear()
                self._lifecycle.notify_all()

    def close(self) -> None:
        with self._lifecycle:
            if self._closed:
                return
            if self._closing:
                while not self._closed:
                    self._lifecycle.wait()
                return
            self._closing = True
            server = self._server
            memory = self._memory
        server_failure: BaseException | None = None
        try:
            if server is not None:
                try:
                    if self._serve_started.is_set():
                        server.shutdown()
                finally:
                    server.server_close()
        except BaseException as exc:
            server_failure = exc
        finally:
            # Even if an injected server's close hook fails, never close the
            # facade underneath a handler that already holds a request lease.
            with self._lifecycle:
                while self._active_requests:
                    self._lifecycle.wait()
            try:
                if memory is not None:
                    memory.close()
            finally:
                with self._lifecycle:
                    self._memory = None
                    self._server = None
                    self._closed = True
                    self._closing = False
                    self._lifecycle.notify_all()
        if server_failure is not None:
            raise server_failure

    def __enter__(self) -> "GovernedRestAdapter":
        if self._closed:
            raise RuntimeError("REST adapter is closed")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _authenticate(self, headers) -> TrustedPrincipal:
        principal = self.config.principal
        if self.config.auth_mode == "local":
            return principal
        values = headers.get_all("Authorization", [])
        provided = ""
        if len(values) == 1 and isinstance(values[0], str):
            value = values[0]
            if value.startswith("Bearer ") and value.count(" ") == 1:
                provided = value[7:]
        expected = self.config.bearer_token or ""
        # Always execute the constant-time comparator in bearer mode, including
        # missing/malformed headers, to keep one authentication path.
        try:
            provided_bytes = provided.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            provided_bytes = b""
        expected_bytes = expected.encode("ascii", errors="strict")
        if not hmac.compare_digest(provided_bytes, expected_bytes) or not provided_bytes:
            raise AuthorizationError()
        return principal

    def _memory_active(self) -> FlowGridMemory:
        memory = self._memory
        if self._closed or memory is None:
            raise RuntimeError("REST adapter is closed")
        return memory

    def _begin_request(self) -> bool:
        """Acquire a lifecycle lease before parsing an accepted connection."""

        with self._lifecycle:
            if self._closed or self._closing or self._memory is None:
                return False
            self._active_requests += 1
            self._lifecycle.notify_all()
            return True

    def _end_request(self) -> None:
        with self._lifecycle:
            if self._active_requests <= 0:  # pragma: no cover - invariant guard
                raise RuntimeError("REST request lease imbalance")
            self._active_requests -= 1
            if self._active_requests == 0:
                self._lifecycle.notify_all()

    def dispatch(self, path: str, principal: TrustedPrincipal, body: object) -> tuple[int, object]:
        if path == "/v1/events":
            return 200, self._events(principal, body)
        if path == "/v1/extractions":
            return 200, self._extractions(principal, body)
        if path == "/v1/memories/query":
            return 200, self._query(principal, body)
        if path == "/v1/context/compile":
            return 200, self._compile_context(principal, body)
        if path == "/v1/memories/transition":
            return 200, self._transition(principal, body)
        if path == "/v1/admin/erase-user":
            return 200, self._erase(principal, body)
        raise RequestValidationError

    def _events(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset({"request_id", "user_id", "messages"}),
            optional=frozenset({"session_id", "scope"}),
        )
        user_id = _safe_string(data["user_id"])
        scope = _scope(data.get("scope"))
        access = authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=scope,
            operation=OPERATION_INGEST,
        )
        messages = data["messages"]
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1_000:
            raise RequestValidationError
        normalized: list[dict[str, object]] = []
        for item in messages:
            message = _exact_object(
                item,
                required=frozenset({"role", "content"}),
                optional=frozenset({"timestamp"}),
            )
            role = _safe_string(message["role"], maximum=64).casefold()
            if role not in {"user", "assistant", "system", "tool", "external"}:
                raise RequestValidationError
            content = message["content"]
            if not isinstance(content, str) or not content.strip() or len(content) > 1_000_000:
                raise RequestValidationError
            normalized_item: dict[str, object] = {"role": role, "content": content}
            if "timestamp" in message:
                timestamp = message["timestamp"]
                if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                    raise RequestValidationError
                normalized_item["timestamp"] = timestamp
            normalized.append(normalized_item)
        receipt = self._memory_active().ingest_raw_events(
            request_id=_safe_string(data["request_id"]),
            user_id=user_id,
            session_id=_optional_string(data, "session_id"),
            messages=normalized,
            trusted_scope=access.effective_scope,
        )
        return {"receipt": receipt.to_dict()}

    def _extractions(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset({"user_id", "raw_event_ids", "idempotency_key"}),
            optional=frozenset({"scope"}),
        )
        user_id = _safe_string(data["user_id"])
        raw_event_ids = data["raw_event_ids"]
        if not isinstance(raw_event_ids, list) or not 1 <= len(raw_event_ids) <= 1_000:
            raise RequestValidationError
        normalized_ids = [_safe_string(item) for item in raw_event_ids]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise RequestValidationError
        access = authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=_scope(data.get("scope")),
            operation=OPERATION_EXTRACT,
        )
        receipt = self._memory_active().extract_candidates(
            user_id=user_id,
            raw_event_ids=normalized_ids,
            idempotency_key=_safe_string(data["idempotency_key"]),
            trusted_scope=access.effective_scope,
            # No request field can select or inject executable extraction code.
            extractor=None,
        )
        return {"receipt": receipt.to_dict()}

    def _query(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset({"user_id"}),
            optional=frozenset(
                {"memory_key", "query", "mode", "scope", "as_of", "max_records"}
            ),
        )
        user_id = _safe_string(data["user_id"])
        mode = data.get("mode", "current")
        if mode not in {"current", "audit"}:
            raise RequestValidationError
        if mode == "current" and data.get("as_of") is not None:
            # Historical lookup is audit evidence, never ordinary current
            # truth.  Allowing it here could resurrect a deleted, rejected, or
            # superseded record into a model-facing response.
            raise RequestValidationError
        access = authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=_scope(data.get("scope")),
            operation=OPERATION_READ_AUDIT if mode == "audit" else OPERATION_READ_CURRENT,
        )
        max_records = _positive_int(data.get("max_records", 100), maximum=MAX_CONTEXT_RECORDS)
        result = self._memory_active().query_memory(
            user_id=user_id,
            access_context=access.access_context,
            memory_key=_optional_string(data, "memory_key"),
            query=_optional_string(data, "query", maximum=10_000),
            mode=mode,
            scope=access.effective_scope,
            as_of=_optional_string(data, "as_of"),
            max_records=max_records,
            disclosure_policy=access.disclosure_policy,
        )
        if not result.allowed or result.state is None:
            raise AuthorizationError(result.reason)
        # Reuse the ContextCompiler's proven public allowlist and opaque source
        # locator contract.  Core CurrentStateResult contains internal IDs,
        # actors, state reasons, and raw audit bodies that are never a default
        # REST query shape.
        public = ContextCompiler(access.disclosure_policy).compile(
            result.state,
            max_chars=None,
            max_tokens=None,
        )
        return {"result": public.to_dict()}

    def _compile_context(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset({"user_id"}),
            optional=frozenset(
                {
                    "memory_key",
                    "query",
                    "mode",
                    "scope",
                    "as_of",
                    "max_records",
                    "max_chars",
                    "max_tokens",
                }
            ),
        )
        if data.get("max_tokens") is not None:
            raise _UnsupportedTokenBudget
        user_id = _safe_string(data["user_id"])
        mode = data.get("mode", "current")
        if mode not in {"current", "audit"}:
            raise RequestValidationError
        if mode == "current" and data.get("as_of") is not None:
            raise RequestValidationError
        access = authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=_scope(data.get("scope")),
            operation=(
                OPERATION_COMPILE_AUDIT if mode == "audit" else OPERATION_COMPILE_CURRENT
            ),
        )
        max_chars = data.get("max_chars")
        if max_chars is not None:
            max_chars = _nonnegative_int(max_chars, maximum=MAX_CONTEXT_REQUEST_CHARS)
        pack = self._memory_active().compile_context(
            user_id=user_id,
            access_context=access.access_context,
            memory_key=_optional_string(data, "memory_key"),
            query=_optional_string(data, "query", maximum=10_000),
            mode=mode,
            scope=access.effective_scope,
            as_of=_optional_string(data, "as_of"),
            max_records=_positive_int(data.get("max_records", 100), maximum=MAX_CONTEXT_RECORDS),
            max_chars=max_chars,
            max_tokens=None,
            disclosure_policy=access.disclosure_policy,
        )
        return {
            "context": pack.to_dict(),
            "injectable": mode == "current" and pack.status == "ready",
        }

    def _transition(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset(
                {"user_id", "record_id", "memory_key", "target_status", "reason"}
            ),
            optional=frozenset({"related_record_id", "scope"}),
        )
        user_id = _safe_string(data["user_id"])
        requested_scope = _scope(data.get("scope"))
        access = authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=requested_scope,
            operation=OPERATION_TRANSITION,
        )
        # Bind opaque record IDs through an audit-authorized, metadata-only
        # primary-key lookup. The request-declared scope is accepted only after
        # trusted principal restrictions are applied, and the record must match
        # that effective scope exactly. This avoids an unbounded or truncated
        # audit-result scan and loads no memory/evidence body.
        memory = self._memory_active()
        memory_key = _safe_string(data["memory_key"])
        record_id = _safe_string(data["record_id"])
        related_record_id = _optional_string(data, "related_record_id")
        if not memory.authorize_transition_target(
            user_id=user_id,
            record_id=record_id,
            memory_key=memory_key,
            access_context=access.access_context,
            scope=access.effective_scope,
            related_record_id=related_record_id,
            disclosure_policy=access.disclosure_policy,
        ):
            # Missing, cross-user, cross-scope, wrong-key, and wrong-slot
            # targets share one fixed denial and disclose no existence signal.
            raise AuthorizationError()
        record = memory.transition_memory(
            user_id=user_id,
            record_id=record_id,
            target_status=_safe_string(data["target_status"], maximum=32),
            actor=principal.principal_id,
            actor_authority=principal.authority,
            reason=_safe_string(data["reason"], maximum=2_000),
            related_record_id=related_record_id,
        )
        # A mutation receipt must not become a side channel around the public
        # query allowlist.  In particular, do not return user/source IDs,
        # provenance locators, actors, state reasons, confirmation metadata, or
        # related-record links from the internal MemoryRecord shape.
        return {
            "record": {
                "record_id": record.id,
                "current_status": record.current_status,
            }
        }

    def _erase(self, principal: TrustedPrincipal, body: object) -> dict[str, object]:
        data = _exact_object(
            body,
            required=frozenset({"user_id", "reason"}),
            optional=frozenset({"scope"}),
        )
        if not self.config.erase_enabled:
            raise AuthorizationError()
        user_id = _safe_string(data["user_id"])
        requested_scope = _scope(data.get("scope"))
        authorize_operation(
            principal,
            user_id=user_id,
            requested_scope=requested_scope,
            operation=OPERATION_ERASE,
        )
        # Erasure is user-wide.  A principal restricted to a project/session
        # must never use a narrow grant to erase data outside that grant.
        non_user_principal_scope = {key for key in principal.scopes if key != "user"}
        non_user_requested_scope = {key for key in requested_scope if key != "user"}
        if non_user_principal_scope or non_user_requested_scope:
            raise AuthorizationError()
        receipt = self._memory_active().erase_user(
            user_id=user_id,
            actor=principal.principal_id,
            actor_authority=principal.authority,
            reason=_safe_string(data["reason"], maximum=2_000),
        )
        return {"receipt": receipt.to_dict()}


class _UnsupportedTokenBudget(ValueError):
    pass


class _LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    # server_close joins every request handler before the facade/SQLite pool is
    # closed. A bounded socket read timeout prevents a partial request body
    # from holding shutdown indefinitely.
    daemon_threads = False
    block_on_close = True

    def handle_error(self, _request, _client_address) -> None:
        # BaseServer would print a traceback (and potentially request-derived
        # exception text) to stderr.  The adapter emits only fixed envelopes.
        return None


def _handler_for(adapter: GovernedRestAdapter):
    class GovernedRestRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FlowGridMemory"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)

        def log_message(self, _format, *_args) -> None:
            return None

        def version_string(self) -> str:
            return "FlowGridMemory"

        def parse_request(self) -> bool:
            # HTTP/0.9 has no response framing.  Reject both its two-word
            # request form and an explicit 0.9 version before stdlib accepts
            # them and routes a body-only response.
            requestline = self.raw_requestline.decode("iso-8859-1").rstrip("\r\n")
            words = requestline.split()
            legacy_request = len(words) == 2 or (
                len(words) == 3
                and re.fullmatch(r"HTTP/0+\.0*9", words[-1]) is not None
            )
            if not legacy_request:
                return super().parse_request()

            self.command = None
            self.path = ""
            self.requestline = requestline
            self.request_version = self.protocol_version
            self.close_connection = True
            self.send_error(400)
            return False

        def send_error(self, code, message=None, explain=None) -> None:
            del message, explain
            # ``BaseHTTPRequestHandler.parse_request`` leaves the request at
            # HTTP/0.9 until after a version has passed validation.  Its
            # response helpers intentionally suppress the status line and all
            # headers for HTTP/0.9, which otherwise turns malformed request
            # lines and unsupported versions into an ambiguous body-only
            # response.  Errors from this HTTP/1.1 adapter are always fully
            # framed, while the parser's status-code decision remains intact.
            if self.request_version == self.default_request_version:
                self.request_version = self.protocol_version
            if int(code) == 501:
                self._method_denied()
                return
            mapping = {
                400: "bad_request",
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                405: "method_not_allowed",
                413: "body_too_large",
                414: "uri_too_long",
                417: "expectation_failed",
                415: "unsupported_media_type",
                422: "invalid_request",
                431: "headers_too_large",
                505: "http_version_not_supported",
                500: "internal_error",
                503: "service_unavailable",
            }
            self._error(mapping.get(int(code), "internal_error"))

        def _route(self) -> str | None:
            if "?" in self.path or "#" in self.path:
                return None
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                return None
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                return None
            return parsed.path if parsed.path in _ALL_PATHS else None

        def handle_expect_100(self) -> bool:
            self._error("expectation_failed")
            return False

        def _headers(self, length: int, *, status: int, allow: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if allow is not None:
                self.send_header("Allow", allow)
            self.end_headers()

        def _respond(self, status: int, value: object, *, allow: str | None = None) -> None:
            # A one-request-per-connection contract closes every early-return
            # framing path.  Unread bytes can never be parsed as a pipelined
            # second request after a 401/404/405/health response.
            self.close_connection = True
            payload = _json_bytes(value)
            self._headers(len(payload), status=status, allow=allow)
            if self.command != "HEAD":
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        def _error(self, name: str, *, allow: str | None = None) -> None:
            status, code, message = _ERRORS[name]
            self._respond(
                status,
                {"error": {"code": code, "message": message}},
                allow=allow,
            )

        def _method_denied(self) -> None:
            route = self._route()
            if route is None:
                self._error("not_found")
                return
            allow = "GET" if route == "/v1/health" else "POST"
            self._error("method_not_allowed", allow=allow)

        def do_GET(self) -> None:
            route = self._route()
            if route is None:
                self._error("not_found")
            elif route != "/v1/health":
                self._error("method_not_allowed", allow="POST")
            else:
                self._respond(
                    200,
                    {"product_version": PRODUCT_VERSION, "ready": True},
                )

        def do_POST(self) -> None:
            route = self._route()
            if route is None:
                self._error("not_found")
                return
            if route == "/v1/health":
                self._error("method_not_allowed", allow="GET")
                return
            try:
                principal = adapter._authenticate(self.headers)
            except AuthorizationError:
                self._error("unauthorized")
                return
            try:
                body = self._read_json_body()
                status, result = adapter.dispatch(route, principal, body)
                self._respond(status, result)
            except _UnsupportedMediaType:
                self._error("unsupported_media_type")
            except _BodyTooLarge:
                self._error("body_too_large")
            except _BadRequest:
                self._error("bad_request")
            except _UnsupportedTokenBudget:
                self._error("unsupported_token_budget")
            except AuthorizationError:
                self._error("forbidden")
            except (ExtractionConflict, GovernanceConflict):
                self._error("conflict")
            except RequestValidationError:
                self._error("invalid_request")
            except (ExtractionError, GovernanceError, ValueError, TypeError):
                self._error("invalid_request")
            except OSError:
                self._error("service_unavailable")
            except BaseException:
                self._error("internal_error")

        def _read_json_body(self) -> object:
            if self.headers.get_all("Transfer-Encoding", []):
                raise _BadRequest
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1:
                raise _UnsupportedMediaType
            parts = [part.strip() for part in content_types[0].split(";")]
            if not parts or parts[0].casefold() != "application/json":
                raise _UnsupportedMediaType
            if len(parts) > 2 or (
                len(parts) == 2 and parts[1].casefold().replace(" ", "") != "charset=utf-8"
            ):
                raise _UnsupportedMediaType
            lengths = self.headers.get_all("Content-Length", [])
            if (
                len(lengths) != 1
                or len(lengths[0]) > 20
                or not re.fullmatch(r"[0-9]+", lengths[0])
            ):
                raise _BadRequest
            length = int(lengths[0])
            if length > adapter.config.max_body_bytes:
                raise _BodyTooLarge
            if length <= 0:
                raise _BadRequest
            try:
                raw = self.rfile.read(length)
            except TimeoutError:
                raise _BadRequest from None
            if len(raw) != length:
                raise _BadRequest
            try:
                parsed = _strict_json(raw)
            except RequestValidationError:
                raise _BadRequest from None
            if not isinstance(parsed, dict):
                raise RequestValidationError
            return parsed

        do_PUT = _method_denied
        do_PATCH = _method_denied
        do_DELETE = _method_denied
        do_OPTIONS = _method_denied
        do_HEAD = _method_denied
        do_TRACE = _method_denied
        do_CONNECT = _method_denied

        # Map framing exceptions after _read_json_body without exposing values.
        def handle_one_request(self) -> None:
            if not adapter._begin_request():
                self.close_connection = True
                return
            try:
                try:
                    super().handle_one_request()
                except _UnsupportedMediaType:
                    self._error("unsupported_media_type")
                except _BodyTooLarge:
                    self._error("body_too_large")
            finally:
                adapter._end_request()

    return GovernedRestRequestHandler


class _UnsupportedMediaType(ValueError):
    pass


class _BodyTooLarge(ValueError):
    pass


class _BadRequest(ValueError):
    pass


def build_rest_adapter(
    config_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> GovernedRestAdapter:
    """Load, validate, then open an adapter in that strict order."""

    config = load_rest_config(config_path, environ=environ)
    return GovernedRestAdapter(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowgrid-memory-rest")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    adapter: GovernedRestAdapter | None = None
    try:
        adapter = build_rest_adapter(args.config)
        adapter.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 130
    except RestConfigurationError:
        parser.exit(2, "REST configuration rejected\n")
    except Exception:
        parser.exit(1, "REST server failed\n")
    finally:
        if adapter is not None:
            adapter.close()


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = [
    "REST_SCHEMA",
    "DEFAULT_MAX_BODY_BYTES",
    "REQUEST_READ_TIMEOUT_SECONDS",
    "RestConfigurationError",
    "GovernedRestConfig",
    "GovernedRestAdapter",
    "load_rest_config",
    "build_rest_adapter",
    "main",
]
