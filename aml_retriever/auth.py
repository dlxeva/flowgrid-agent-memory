"""Trusted-principal binding for governed transport adapters.

This module is deliberately transport neutral.  HTTP, MCP, or another host
may authenticate in different ways, but an untrusted request body is never a
source of principal identity, authority, permissions, or user grants.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .access import AccessContext, DisclosurePolicy
from .governance import AUTHORITIES, GOVERNANCE_AUTHORITIES


PERMISSION_WRITE = "memory:write"
PERMISSION_EXTRACT = "memory:extract"
PERMISSION_READ = "memory:read"
PERMISSION_AUDIT = "memory:audit"
PERMISSION_TRANSITION = "memory:transition"
PERMISSION_ERASE = "memory:erase"

MEMORY_PERMISSIONS = frozenset(
    {
        PERMISSION_WRITE,
        PERMISSION_EXTRACT,
        PERMISSION_READ,
        PERMISSION_AUDIT,
        PERMISSION_TRANSITION,
        PERMISSION_ERASE,
    }
)

OPERATION_INGEST = "ingest"
OPERATION_EXTRACT = "extract"
OPERATION_READ_CURRENT = "read_current"
OPERATION_READ_AUDIT = "read_audit"
OPERATION_COMPILE_CURRENT = "compile_current"
OPERATION_COMPILE_AUDIT = "compile_audit"
OPERATION_TRANSITION = "transition"
OPERATION_ERASE = "erase"

_OPERATION_PERMISSION = MappingProxyType(
    {
        OPERATION_INGEST: frozenset({PERMISSION_WRITE}),
        OPERATION_EXTRACT: frozenset({PERMISSION_EXTRACT}),
        OPERATION_READ_CURRENT: frozenset({PERMISSION_READ}),
        OPERATION_READ_AUDIT: frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
        OPERATION_COMPILE_CURRENT: frozenset({PERMISSION_READ}),
        OPERATION_COMPILE_AUDIT: frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
        # REST uses an audit-facade metadata proof to bind an opaque record ID
        # to its immutable scope before mutation.  Transition is therefore a
        # deliberate compound grant rather than an implicit permission boost.
        OPERATION_TRANSITION: frozenset(
            {PERMISSION_TRANSITION, PERMISSION_READ, PERMISSION_AUDIT}
        ),
        OPERATION_ERASE: frozenset({PERMISSION_ERASE}),
    }
)

_AUDIT_OPERATIONS = frozenset(
    {OPERATION_READ_AUDIT, OPERATION_COMPILE_AUDIT, OPERATION_TRANSITION}
)
_SCOPE_FIELDS = frozenset(
    {"tenant", "user", "project", "agent", "session", "repository"}
)


class AuthorizationError(PermissionError):
    """A fixed, non-sensitive authorization failure."""

    def __init__(self, reason: str = "access_denied"):
        safe_reason = reason if reason in {
            "access_denied",
            "audit_purpose_required",
            "audit_purpose_not_allowed",
        } else "access_denied"
        self.reason = safe_reason
        super().__init__(safe_reason)


def _nonempty(value: object, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError(f"{field} must be a safe non-empty string")
    return normalized


def _string_set(
    value: Iterable[str],
    *,
    field: str,
    allow_empty: bool = True,
) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a collection of strings")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{field} must be a collection of strings") from exc
    normalized = frozenset(_nonempty(item, field=field) for item in items)
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _scope(value: Mapping[str, str], *, field: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _SCOPE_FIELDS:
            raise ValueError(f"{field} contains an unsupported field")
        normalized[key] = _nonempty(item, field=f"{field}.{key}")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class TrustedPrincipal:
    """Recursively immutable identity and policy supplied by a trusted host.

    Construct this object from local configuration or successful transport
    authentication.  Never construct it by unpacking an HTTP/MCP request.
    """

    principal_id: str
    authority: str
    allowed_users: frozenset[str]
    scopes: Mapping[str, str]
    permissions: frozenset[str]
    purpose: str | None = None
    allowed_audit_purposes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "principal_id", _nonempty(self.principal_id, field="principal_id")
        )
        object.__setattr__(
            self, "authority", _nonempty(self.authority, field="authority")
        )
        if self.authority not in AUTHORITIES:
            raise ValueError("authority is unsupported")
        object.__setattr__(
            self,
            "allowed_users",
            _string_set(self.allowed_users, field="allowed_users", allow_empty=False),
        )
        object.__setattr__(self, "scopes", _scope(self.scopes, field="scopes"))
        permissions = _string_set(self.permissions, field="permissions")
        if permissions - MEMORY_PERMISSIONS:
            raise ValueError("permissions contains an unsupported permission")
        object.__setattr__(self, "permissions", permissions)
        purpose = self.purpose
        if purpose is not None:
            purpose = _nonempty(purpose, field="purpose")
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(
            self,
            "allowed_audit_purposes",
            frozenset(
                item.casefold()
                for item in _string_set(
                    self.allowed_audit_purposes,
                    field="allowed_audit_purposes",
                )
            ),
        )

    def to_access_context(self) -> AccessContext:
        """Create a core access context using only trusted principal facts."""

        return AccessContext(
            principal_id=self.principal_id,
            authority=self.authority,
            scopes=self.scopes,
            permissions=self.permissions,
            purpose=self.purpose,
            allowed_users=self.allowed_users,
        )

    def to_disclosure_policy(self) -> DisclosurePolicy:
        """Create the fixed minimal disclosure policy for this principal."""

        return DisclosurePolicy(
            allowed_audit_purposes=self.allowed_audit_purposes,
        )


@dataclass(frozen=True)
class TrustedAccess:
    """Validated user/scope/operation binding passed to the product facade."""

    access_context: AccessContext
    disclosure_policy: DisclosurePolicy
    effective_scope: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.access_context, AccessContext):
            raise TypeError("access_context must be AccessContext")
        if not isinstance(self.disclosure_policy, DisclosurePolicy):
            raise TypeError("disclosure_policy must be DisclosurePolicy")
        object.__setattr__(
            self, "effective_scope", _scope(self.effective_scope, field="effective_scope")
        )


def authorize_operation(
    principal: TrustedPrincipal,
    *,
    user_id: str,
    requested_scope: Mapping[str, str] | None,
    operation: str,
) -> TrustedAccess:
    """Fail closed before a transport calls the facade.

    The returned scope is restricted by the principal and always contains the
    concrete user.  Audit additionally requires a concrete user grant and an
    exact trusted-purpose allowlist match.
    """

    if not isinstance(principal, TrustedPrincipal):
        raise AuthorizationError()
    if not isinstance(operation, str) or operation not in _OPERATION_PERMISSION:
        raise AuthorizationError()
    try:
        normalized_user = _nonempty(user_id, field="user_id")
        supplied_scope = _scope(requested_scope or {}, field="scope")
    except ValueError:
        raise AuthorizationError() from None

    if (
        normalized_user not in principal.allowed_users
        and "*" not in principal.allowed_users
    ):
        raise AuthorizationError()
    required = _OPERATION_PERMISSION[operation]
    if not required.issubset(principal.permissions):
        raise AuthorizationError()
    # A configured permission cannot turn an agent/system/external identity
    # into a human/policy owner gate. Reject at this transport-neutral trust
    # boundary so adapters expose one fixed authorization denial.
    if (
        operation in {OPERATION_TRANSITION, OPERATION_ERASE}
        and principal.authority not in GOVERNANCE_AUTHORITIES
    ):
        raise AuthorizationError()
    if operation in _AUDIT_OPERATIONS:
        if "*" in principal.allowed_users:
            raise AuthorizationError()
        if not principal.purpose:
            raise AuthorizationError("audit_purpose_required")
        if principal.purpose.casefold() not in principal.allowed_audit_purposes:
            raise AuthorizationError("audit_purpose_not_allowed")

    effective = dict(supplied_scope)
    if "user" in effective and effective["user"] != normalized_user:
        raise AuthorizationError()
    effective["user"] = normalized_user
    for key, value in principal.scopes.items():
        if key == "user" and value != normalized_user:
            raise AuthorizationError()
        if key in effective and effective[key] != value:
            raise AuthorizationError()
        effective[key] = value

    return TrustedAccess(
        access_context=principal.to_access_context(),
        disclosure_policy=principal.to_disclosure_policy(),
        effective_scope=effective,
    )


__all__ = [
    "PERMISSION_WRITE",
    "PERMISSION_EXTRACT",
    "PERMISSION_READ",
    "PERMISSION_AUDIT",
    "PERMISSION_TRANSITION",
    "PERMISSION_ERASE",
    "MEMORY_PERMISSIONS",
    "OPERATION_INGEST",
    "OPERATION_EXTRACT",
    "OPERATION_READ_CURRENT",
    "OPERATION_READ_AUDIT",
    "OPERATION_COMPILE_CURRENT",
    "OPERATION_COMPILE_AUDIT",
    "OPERATION_TRANSITION",
    "OPERATION_ERASE",
    "AuthorizationError",
    "TrustedPrincipal",
    "TrustedAccess",
    "authorize_operation",
]
