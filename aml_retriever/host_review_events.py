"""Opt-in parsing of authenticated host review-action readbacks.

No network, credentials, free-text approval inference, or default installation.
The trusted provider must authenticate BOTH reader and original human actor,
read current event/revocation state, and serialize event writers with the host.
Python types are contracts, not signatures or protection from trusted code.
"""
from dataclasses import dataclass
import json
import re
from types import MappingProxyType

from .auth import TrustedPrincipal, authorize_operation, OPERATION_READ_AUDIT, OPERATION_TRANSITION
from .validity_host import HostReviewError, TrustedReviewDecision


ACTION_SCHEMA = "flowgrid.host-review-action/v1"
MAX_ACTION_BYTES = 16384
_KEY = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SCOPES = frozenset({"tenant", "user", "project", "agent", "session", "repository"})


@dataclass(frozen=True)
class HostEventReadback:
    """Provider-owned identity metadata, never unpacked from request/model JSON.

    ``reviewer`` comes from actual host authentication; ``actor_kind`` is from
    host event metadata, not an origin field in action text. ``active`` must
    reflect current revocation/deletion status, never a permanent local cache.
    """
    host_id: str
    channel_id: str
    locator: str
    reviewer: TrustedPrincipal
    actor_kind: str
    active: bool
    action_bytes: bytes


def _identifier(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 256 and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def _decode_action(raw):
    if type(raw) is not bytes or len(raw) > MAX_ACTION_BYTES:
        raise HostReviewError("host_action_invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise HostReviewError("host_action_invalid") from None
    fields = {"schema", "user_id", "scope", "request_digest", "pending_digest", "outcome", "reason"}
    if (type(data) is not dict or set(data) != fields or data["schema"] != ACTION_SCHEMA
            or type(data["outcome"]) is not str
            or data["outcome"] not in {"supported", "rejected", "unknown"}
            or any(not isinstance(data[key], str) or _DIGEST.fullmatch(data[key]) is None
                   for key in ("request_digest", "pending_digest"))
            or not isinstance(data["reason"], str) or not data["reason"].strip()
            or len(data["reason"]) > 2048
            or any(ord(c) < 32 and c not in "\n\t" for c in data["reason"])):
        raise HostReviewError("host_action_invalid")
    return data


class HostReviewEventResolver:
    """A scoped ``ValidityReviewHost.resolve_decision`` implementation.

    Authorizes the bound user's audit scope before provider lookup. Provider
    callback receives the actual caller and must perform live authenticated
    lookup; this class cannot create human-auth evidence for it.
    """
    def __init__(self, *, host_id, channel_id, user_id, scope, read_event, enabled=False):
        if (not isinstance(host_id, str) or _KEY.fullmatch(host_id) is None
                or not _identifier(channel_id) or not _identifier(user_id)
                or not isinstance(scope, dict) or scope.get("user") != user_id
                or set(scope) - _SCOPES or not all(_identifier(v) for v in scope.values())
                or not callable(read_event) or type(enabled) is not bool):
            raise HostReviewError("explicit_host_event_configuration_required")
        self.host_id = host_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.scope = MappingProxyType(dict(scope))
        self.read_event = read_event
        self.enabled = enabled

    def _access(self, principal, operation):
        access = authorize_operation(principal, user_id=self.user_id,
                                     requested_scope=self.scope, operation=operation)
        # A configured restriction must already be bound to this event scope;
        # injecting it now would not prove that the event belonged to it.
        if dict(access.effective_scope) != dict(self.scope):
            raise HostReviewError("host_event_scope_mismatch")
        if "memory:evidence" not in access.access_context.permissions:
            raise HostReviewError("evidence_permission_required")

    def __call__(self, locator, caller):
        if not self.enabled:
            raise HostReviewError("host_event_resolver_disabled")
        prefix = "host-review:" + self.host_id + ":"
        if (not isinstance(locator, str) or not locator.startswith(prefix)
                or _KEY.fullmatch(locator[len(prefix):]) is None):
            raise HostReviewError("host_event_reference_invalid")
        self._access(caller, OPERATION_READ_AUDIT)
        try:
            event = self.read_event(locator, caller)
        except Exception:
            raise HostReviewError("host_event_unavailable") from None
        if (not isinstance(event, HostEventReadback) or event.host_id != self.host_id
                or event.channel_id != self.channel_id or event.locator != locator
                or event.active is not True or event.actor_kind != "human"
                or not isinstance(event.reviewer, TrustedPrincipal)
                or event.reviewer.authority != "owner"):
            raise HostReviewError("host_event_provenance_mismatch")
        self._access(event.reviewer, OPERATION_TRANSITION)
        data = _decode_action(event.action_bytes)
        if data["user_id"] != self.user_id or data["scope"] != dict(self.scope):
            raise HostReviewError("host_event_scope_mismatch")
        return TrustedReviewDecision(
            source_locator=locator, principal_id=event.reviewer.principal_id,
            user_id=self.user_id, request_digest=data["request_digest"],
            pending_digest=data["pending_digest"], outcome=data["outcome"],
            reason=data["reason"], origin="human_review",
        )
