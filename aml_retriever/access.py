"""Explicit read authorization and disclosure policy for governed memory.

The core deliberately does not construct :class:`AccessContext` from request
payloads.  A transport adapter must derive it from an authenticated principal
and trusted policy data.  This keeps caller supplied ``authority`` fields out
of the authorization boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


PERMISSION_READ = "memory:read"
PERMISSION_AUDIT = "memory:audit"
PERMISSION_EVIDENCE = "memory:evidence"

_SCOPE_FIELDS = frozenset(
    {"tenant", "user", "project", "agent", "session", "repository"}
)
_SAFE_RECORD_FIELDS = frozenset(
    {
        "id",
        "memory_key",
        "memory_type",
        "subject",
        "content",
        "authority",
        "scope",
        "observed_at",
        "valid_from",
        "valid_until",
    }
)
_DEFAULT_RECORD_FIELDS = _SAFE_RECORD_FIELDS


def _normalize_string_set(value: Iterable[str], *, field: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a collection of strings")
    try:
        items = list(value)
    except TypeError as exc:
        raise ValueError(f"{field} must be a collection of strings") from exc
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValueError(f"{field} must contain only non-empty strings")
    return frozenset(item.strip() for item in items)


def _normalize_scope(value: Mapping[str, str], *, field: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _SCOPE_FIELDS:
            raise ValueError(f"{field} contains an unsupported scope field")
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field}.{key} must be a non-empty string")
        result[key] = item.strip()
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class AccessContext:
    """Trusted authorization facts for one compile request.

    ``allowed_users`` is explicit; cross-user access is never inferred from a
    principal name.  ``scopes`` are restrictions, not defaults.  If a caller
    omits a restricted project/repository scope, authorization injects the
    restriction so omission cannot widen the query.
    """

    principal_id: str
    authority: str
    scopes: Mapping[str, str]
    permissions: frozenset[str]
    purpose: str | None
    allowed_users: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise ValueError("principal_id must be a non-empty string")
        if not isinstance(self.authority, str) or not self.authority.strip():
            raise ValueError("authority must be a non-empty trusted value")
        object.__setattr__(self, "principal_id", self.principal_id.strip())
        object.__setattr__(self, "authority", self.authority.strip())
        object.__setattr__(self, "scopes", _normalize_scope(self.scopes, field="scopes"))
        object.__setattr__(
            self,
            "permissions",
            _normalize_string_set(self.permissions, field="permissions"),
        )
        object.__setattr__(
            self,
            "allowed_users",
            _normalize_string_set(self.allowed_users, field="allowed_users"),
        )
        if self.purpose is not None:
            if not isinstance(self.purpose, str):
                raise ValueError("purpose must be a string or None")
            if len(self.purpose) > 256 or any(
                ord(char) < 32 or ord(char) == 127 for char in self.purpose
            ):
                raise ValueError("purpose must be at most 256 characters without control characters")
            purpose = self.purpose.strip()
            object.__setattr__(self, "purpose", purpose or None)


@dataclass(frozen=True)
class DisclosurePolicy:
    """Static field allowlist for a context pack.

    The allowlist cannot opt into raw event bodies, transition actors,
    internal state reasons, or withheld record identifiers.  This slice
    provides field-level minimization only; it does not claim semantic PII
    detection inside an otherwise allowed memory ``content`` value.

    Audit is fail-closed by default: ``allowed_audit_purposes=None`` and an
    empty set both disable audit compilation.  A non-empty set creates an
    exact, case-insensitive allowlist.
    """

    record_fields: frozenset[str] = _DEFAULT_RECORD_FIELDS
    allowed_audit_purposes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        fields = _normalize_string_set(self.record_fields, field="record_fields")
        unsupported = fields - _SAFE_RECORD_FIELDS
        if unsupported:
            raise ValueError(
                "record_fields contains fields outside the disclosure allowlist: "
                + ", ".join(sorted(unsupported))
            )
        object.__setattr__(self, "record_fields", fields)
        if self.allowed_audit_purposes is not None:
            purposes = _normalize_string_set(
                self.allowed_audit_purposes,
                field="allowed_audit_purposes",
            )
            object.__setattr__(
                self,
                "allowed_audit_purposes",
                frozenset(item.casefold() for item in purposes),
            )

    def allows_audit_purpose(self, purpose: str | None) -> bool:
        if not isinstance(purpose, str) or not purpose.strip():
            return False
        allowed = self.allowed_audit_purposes
        return bool(allowed) and purpose.strip().casefold() in allowed


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    effective_scope: Mapping[str, str]


def authorize_memory_read(
    access_context: AccessContext,
    *,
    user_id: str,
    requested_scope: Mapping[str, str] | None,
    mode: str,
    disclosure_policy: DisclosurePolicy,
) -> AccessDecision:
    """Authorize and bind a governed read before retrieval or budgeting."""

    if not isinstance(access_context, AccessContext):
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    if not isinstance(disclosure_policy, DisclosurePolicy):
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    if not isinstance(user_id, str) or not user_id.strip():
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    user_id = user_id.strip()
    if user_id not in access_context.allowed_users and "*" not in access_context.allowed_users:
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    if PERMISSION_READ not in access_context.permissions:
        return AccessDecision(False, "access_denied", MappingProxyType({}))

    if mode == "ordinary":
        mode = "current"
    if mode not in {"current", "audit"}:
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    if mode == "audit":
        # A wildcard is useful for explicitly trusted bulk current-state jobs,
        # but audit history always requires a concrete user grant.
        if "*" in access_context.allowed_users:
            return AccessDecision(False, "access_denied", MappingProxyType({}))
        if PERMISSION_AUDIT not in access_context.permissions:
            return AccessDecision(False, "access_denied", MappingProxyType({}))
        if not access_context.purpose:
            return AccessDecision(False, "audit_purpose_required", MappingProxyType({}))
        if not disclosure_policy.allows_audit_purpose(access_context.purpose):
            return AccessDecision(False, "audit_purpose_not_allowed", MappingProxyType({}))

    if requested_scope is None:
        requested: Mapping[str, str] = {}
    else:
        try:
            requested = _normalize_scope(requested_scope, field="scope")
        except ValueError:
            return AccessDecision(False, "access_denied", MappingProxyType({}))

    effective = dict(requested)
    requested_user = effective.get("user")
    if requested_user is not None and requested_user != user_id:
        return AccessDecision(False, "access_denied", MappingProxyType({}))
    effective["user"] = user_id

    for key, value in access_context.scopes.items():
        if key == "user" and value != user_id:
            return AccessDecision(False, "access_denied", MappingProxyType({}))
        if key in effective and effective[key] != value:
            return AccessDecision(False, "access_denied", MappingProxyType({}))
        effective[key] = value

    return AccessDecision(
        True,
        "authorized",
        MappingProxyType(dict(sorted(effective.items()))),
    )


__all__ = [
    "PERMISSION_READ",
    "PERMISSION_AUDIT",
    "PERMISSION_EVIDENCE",
    "AccessContext",
    "DisclosurePolicy",
    "AccessDecision",
    "authorize_memory_read",
]
