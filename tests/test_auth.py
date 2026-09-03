"""Trusted principal and operation binding tests."""
from __future__ import annotations

import unittest

from aml_retriever.auth import (
    MEMORY_PERMISSIONS,
    OPERATION_COMPILE_AUDIT,
    OPERATION_ERASE,
    OPERATION_INGEST,
    OPERATION_READ_CURRENT,
    OPERATION_TRANSITION,
    PERMISSION_AUDIT,
    PERMISSION_ERASE,
    PERMISSION_READ,
    PERMISSION_WRITE,
    PERMISSION_TRANSITION,
    AuthorizationError,
    TrustedPrincipal,
    authorize_operation,
)


def principal(**overrides) -> TrustedPrincipal:
    values = {
        "principal_id": "owner-1",
        "authority": "owner",
        "allowed_users": frozenset({"u1"}),
        "scopes": {"project": "alpha"},
        "permissions": frozenset(MEMORY_PERMISSIONS),
        "purpose": "incident review",
        "allowed_audit_purposes": frozenset({"incident review"}),
    }
    values.update(overrides)
    return TrustedPrincipal(**values)


class TestTrustedPrincipal(unittest.TestCase):
    def test_principal_is_recursively_immutable_and_detached(self):
        source_scope = {"project": "alpha"}
        value = principal(scopes=source_scope)
        source_scope["project"] = "mutated"
        self.assertEqual(value.scopes["project"], "alpha")
        with self.assertRaises(TypeError):
            value.scopes["project"] = "mutated"  # type: ignore[index]
        with self.assertRaises(Exception):
            value.permissions.add(PERMISSION_WRITE)  # type: ignore[attr-defined]
        with self.assertRaises(Exception):
            value.authority = "agent"  # type: ignore[misc]

    def test_authority_and_permissions_are_closed_sets(self):
        with self.assertRaises(ValueError):
            principal(authority="root")
        with self.assertRaises(ValueError):
            principal(permissions=frozenset({"memory:god-mode"}))
        for authority in ("user", "owner", "policy", "agent", "system", "external", "unknown"):
            with self.subTest(authority=authority):
                self.assertEqual(principal(authority=authority).authority, authority)

    def test_operation_requires_explicit_permission_and_allowed_user(self):
        restricted = principal(permissions=frozenset({PERMISSION_READ}))
        allowed = authorize_operation(
            restricted,
            user_id="u1",
            requested_scope={"project": "alpha"},
            operation=OPERATION_READ_CURRENT,
        )
        self.assertEqual(dict(allowed.effective_scope), {"project": "alpha", "user": "u1"})
        for operation in (OPERATION_INGEST, OPERATION_ERASE):
            with self.subTest(operation=operation), self.assertRaises(AuthorizationError):
                authorize_operation(
                    restricted,
                    user_id="u1",
                    requested_scope={"project": "alpha"},
                    operation=operation,
                )
        with self.assertRaises(AuthorizationError):
            authorize_operation(
                principal(),
                user_id="u2",
                requested_scope={"project": "alpha"},
                operation=OPERATION_READ_CURRENT,
            )

    def test_scope_mismatch_fails_closed_without_echoing_values(self):
        sentinel = "secret-project-sentinel"
        with self.assertRaises(AuthorizationError) as raised:
            authorize_operation(
                principal(),
                user_id="u1",
                requested_scope={"project": sentinel},
                operation=OPERATION_READ_CURRENT,
            )
        self.assertEqual(str(raised.exception), "access_denied")
        self.assertNotIn(sentinel, str(raised.exception))

    def test_audit_requires_read_concrete_user_and_trusted_purpose(self):
        base = principal(
            permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
        )
        grant = authorize_operation(
            base,
            user_id="u1",
            requested_scope={"project": "alpha"},
            operation=OPERATION_COMPILE_AUDIT,
        )
        self.assertEqual(grant.access_context.purpose, "incident review")
        self.assertTrue(grant.disclosure_policy.allows_audit_purpose("INCIDENT REVIEW"))

        cases = (
            principal(purpose=None),
            principal(allowed_audit_purposes=frozenset()),
            principal(allowed_users=frozenset({"*"})),
            principal(permissions=frozenset({PERMISSION_AUDIT})),
        )
        for item in cases:
            with self.subTest(item=item), self.assertRaises(AuthorizationError):
                authorize_operation(
                    item,
                    user_id="u1",
                    requested_scope={"project": "alpha"},
                    operation=OPERATION_COMPILE_AUDIT,
                )

    def test_erase_is_a_distinct_permission(self):
        item = principal(permissions=frozenset({PERMISSION_WRITE, PERMISSION_ERASE}))
        grant = authorize_operation(
            item,
            user_id="u1",
            requested_scope={"project": "alpha"},
            operation=OPERATION_ERASE,
        )
        self.assertEqual(grant.access_context.principal_id, "owner-1")
        for authority in ("agent", "system", "external", "unknown"):
            with self.subTest(authority=authority), self.assertRaises(AuthorizationError):
                authorize_operation(
                    principal(authority=authority),
                    user_id="u1",
                    requested_scope={},
                    operation=OPERATION_ERASE,
                )

    def test_transition_is_an_explicit_compound_scope_proof_grant(self):
        for permissions in (
            frozenset({PERMISSION_TRANSITION}),
            frozenset({PERMISSION_TRANSITION, PERMISSION_READ}),
            frozenset({PERMISSION_TRANSITION, PERMISSION_AUDIT}),
        ):
            with self.subTest(permissions=permissions), self.assertRaises(AuthorizationError):
                authorize_operation(
                    principal(permissions=permissions),
                    user_id="u1",
                    requested_scope={"project": "alpha"},
                    operation=OPERATION_TRANSITION,
                )
        grant = authorize_operation(
            principal(
                permissions=frozenset(
                    {PERMISSION_TRANSITION, PERMISSION_READ, PERMISSION_AUDIT}
                )
            ),
            user_id="u1",
            requested_scope={"project": "alpha"},
            operation=OPERATION_TRANSITION,
        )
        self.assertEqual(dict(grant.effective_scope)["project"], "alpha")
        for authority in ("agent", "system", "external", "unknown"):
            with self.subTest(authority=authority), self.assertRaises(AuthorizationError):
                authorize_operation(
                    principal(authority=authority),
                    user_id="u1",
                    requested_scope={"project": "alpha"},
                    operation=OPERATION_TRANSITION,
                )


if __name__ == "__main__":
    unittest.main()
