"""Governed REST v1 contract, authorization, and real HTTP tests."""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aml_retriever.auth import AuthorizationError, MEMORY_PERMISSIONS, TrustedPrincipal
from aml_retriever.extraction import ExtractionValidationError
from aml_retriever.governance import GovernanceConflict
from aml_retriever.rest_v1 import (
    REST_SCHEMA,
    GovernedRestAdapter,
    GovernedRestConfig,
    RestConfigurationError,
)


def directive(content: str, *, memory_key: str = "profile.city") -> str:
    return "@flowgrid.memory/v1\n" + json.dumps(
        {
            "proposals": [
                {
                    "memory_key": memory_key,
                    "memory_type": "fact",
                    "subject": "$user",
                    "content": content,
                }
            ]
        },
        separators=(",", ":"),
    )


def make_principal(**overrides) -> TrustedPrincipal:
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


class _FakeServer:
    def __init__(self, address, _handler):
        self.server_address = (address[0], address[1] or 8123)
        self.closed = False

    def server_close(self):
        self.closed = True

    def shutdown(self):
        return None


class TestRestStartup(unittest.TestCase):
    def test_bad_config_is_rejected_before_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "must-not-exist.db"
            mapping = {
                "schema": REST_SCHEMA,
                "db_path": str(path),
                "bind": {"host": "127.0.0.1", "port": 0},
                "auth": {
                    "mode": "none",
                    "principal": {
                        "principal_id": "owner-1",
                        "authority": "owner",
                        "allowed_users": ["u1"],
                        "scopes": {},
                        "permissions": ["memory:read"],
                    },
                },
            }
            with self.assertRaises(RestConfigurationError):
                GovernedRestConfig.from_mapping(mapping, environ={})
            self.assertFalse(path.exists())

    def test_only_literal_ipv4_loopback_and_authenticated_modes_are_valid(self):
        for host in ("0.0.0.0", "localhost", "127.0.0.2", "::1", "example.com"):
            with self.subTest(host=host), self.assertRaises(RestConfigurationError):
                GovernedRestConfig(
                    db_path=":memory:",
                    principal=make_principal(),
                    auth_mode="local",
                    bind_host=host,
                    port=0,
                )
        with self.assertRaises(RestConfigurationError):
            GovernedRestConfig(
                db_path=":memory:",
                principal=make_principal(),
                auth_mode="none",
                port=0,
            )
        with self.assertRaises(RestConfigurationError):
            GovernedRestConfig(
                db_path=":memory:",
                principal=make_principal(),
                auth_mode="bearer",
                bearer_token="",
                port=0,
            )

    def test_bearer_config_resolves_only_named_environment_secret(self):
        mapping = {
            "schema": REST_SCHEMA,
            "db_path": ":memory:",
            "bind": {"host": "127.0.0.1", "port": 0},
            "auth": {
                "mode": "bearer",
                "token_env": "FLOWGRID_TEST_TOKEN",
                "principal": {
                    "principal_id": "owner-1",
                    "authority": "owner",
                    "allowed_users": ["u1"],
                    "scopes": {},
                    "permissions": ["memory:read"],
                },
            },
        }
        config = GovernedRestConfig.from_mapping(
            mapping, environ={"FLOWGRID_TEST_TOKEN": "injected-token"}
        )
        self.assertNotIn("injected-token", repr(config))
        self.assertEqual(config.auth_mode, "bearer")
        with self.assertRaises(RestConfigurationError):
            GovernedRestConfig.from_mapping(mapping, environ={})

    def test_server_constructor_failure_closes_opened_facade(self):
        instances = []

        class Memory:
            def __init__(self, *, db_path):
                del db_path
                self.closed = False
                instances.append(self)

            def close(self):
                self.closed = True

        class BrokenServer:
            def __init__(self, _address, _handler):
                raise OSError("bind secret")

        config = GovernedRestConfig(
            db_path=":memory:",
            principal=make_principal(),
            auth_mode="local",
            port=0,
        )
        with self.assertRaises(OSError):
            GovernedRestAdapter(
                config, memory_factory=Memory, server_class=BrokenServer
            )
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed)

    def test_post_constructor_activation_rejection_closes_server_and_facade(self):
        instances = []
        servers = []

        class Memory:
            def __init__(self, *, db_path):
                del db_path
                self.closed = False
                instances.append(self)

            def close(self):
                self.closed = True

        class WrongBoundServer(_FakeServer):
            def __init__(self, address, handler):
                super().__init__(("0.0.0.0", address[1]), handler)
                servers.append(self)

        with self.assertRaises(RestConfigurationError):
            GovernedRestAdapter(
                GovernedRestConfig(
                    db_path=":memory:",
                    principal=make_principal(),
                    auth_mode="local",
                    port=0,
                ),
                memory_factory=Memory,
                server_class=WrongBoundServer,
            )
        self.assertTrue(instances[0].closed)
        self.assertTrue(servers[0].closed)


class RestHttpCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "rest.db")
        self.config = GovernedRestConfig(
            db_path=self.db_path,
            principal=make_principal(),
            auth_mode="local",
            port=0,
            erase_enabled=False,
        )
        self.adapter = GovernedRestAdapter(self.config)
        self.thread = threading.Thread(target=self.adapter.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.adapter.close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def request(self, method, path, body=None, *, headers=None, adapter=None):
        target = self.adapter if adapter is None else adapter
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(*target.address, timeout=5)
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw)
        finally:
            connection.close()

    def cycle(
        self,
        *,
        user_id="u1",
        content="Paris",
        suffix="1",
        memory_key="profile.city",
    ):
        event_body = {
            "request_id": f"request-{suffix}",
            "user_id": user_id,
            "session_id": f"session-{suffix}",
            "scope": {"project": "alpha"},
            "messages": [
                {
                    "role": "user",
                    "content": directive(content, memory_key=memory_key),
                }
            ],
        }
        status, _headers, added = self.request("POST", "/v1/events", event_body)
        self.assertEqual(status, 200)
        raw_event_id = added["receipt"]["raw_event_ids"][0]
        status, _headers, extracted = self.request(
            "POST",
            "/v1/extractions",
            {
                "user_id": user_id,
                "raw_event_ids": [raw_event_id],
                "idempotency_key": f"extract-{suffix}",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        record_id = extracted["receipt"]["record_ids"][0]
        status, _headers, transitioned = self.request(
            "POST",
            "/v1/memories/transition",
            {
                "user_id": user_id,
                "record_id": record_id,
                "memory_key": memory_key,
                "target_status": "confirmed",
                "reason": "owner reviewed immutable source",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(transitioned["record"]["current_status"], "confirmed")
        self.assertEqual(
            set(transitioned["record"]), {"record_id", "current_status"}
        )
        rendered = json.dumps(transitioned)
        for private_key in (
            "user_id",
            "source_event_ids",
            "source_locator",
            "created_by",
            "state_reason",
            "confirmed_by",
            "confirmed_at",
            "related_record_id",
            "owner reviewed immutable source",
        ):
            self.assertNotIn(private_key, rendered)
        return record_id

    def test_health_is_minimal_anonymous_and_security_headers_are_fixed(self):
        status, headers, body = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"product_version", "ready"})
        self.assertTrue(body["ready"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(self.db_path, str(body))
        self.assertNotIn("owner-1", str(body))

    def test_full_ingest_candidate_owner_transition_current_and_context(self):
        with mock.patch("aml_retriever.server._Handler.do_POST") as legacy:
            record_id = self.cycle()
            status, _headers, current = self.request(
                "POST",
                "/v1/memories/query",
                {
                    "user_id": "u1",
                    "memory_key": "profile.city",
                    "scope": {"project": "alpha"},
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(current["result"]["status"], "ready")
            self.assertEqual(current["result"]["items"][0]["id"], record_id)
            self.assertEqual(current["result"]["items"][0]["content"], "Paris")
            self.assertEqual(
                set(current["result"]["items"][0]),
                {
                    "authority",
                    "content",
                    "current_status",
                    "id",
                    "memory_key",
                    "memory_type",
                    "observed_at",
                    "scope",
                    "source_locator",
                    "subject",
                    "valid_from",
                    "valid_until",
                    "why_selected",
                },
            )
            rendered = json.dumps(current)
            for private_key in (
                "user_id",
                "source_event_ids",
                "created_by",
                "state_reason",
                "confirmed_by",
                "confirmed_at",
                "withheld_record_ids",
            ):
                self.assertNotIn(private_key, rendered)

            status, _headers, context = self.request(
                "POST",
                "/v1/context/compile",
                {
                    "user_id": "u1",
                    "memory_key": "profile.city",
                    "scope": {"project": "alpha"},
                    "max_chars": 100_000,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(context["context"]["status"], "ready")
            self.assertTrue(context["injectable"])
            self.assertEqual(context["context"]["items"][0]["content"], "Paris")
            legacy.assert_not_called()

    def test_context_nonready_is_explicitly_noninjectable_and_tokens_are_rejected(self):
        status, _headers, context = self.request(
            "POST",
            "/v1/context/compile",
            {
                "user_id": "u1",
                "memory_key": "missing",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(context["context"]["status"], "unknown")
        self.assertFalse(context["injectable"])
        status, _headers, error = self.request(
            "POST",
            "/v1/context/compile",
            {
                "user_id": "u1",
                "scope": {"project": "alpha"},
                "max_tokens": 10,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "unsupported_token_budget")

    def test_user_scope_audit_and_erase_gates_do_not_leak_request_values(self):
        sentinel = "private-user-sentinel"
        cases = (
            {"user_id": sentinel, "scope": {"project": "alpha"}},
            {"user_id": "u1", "scope": {"project": "wrong-project-sentinel"}},
        )
        for body in cases:
            with self.subTest(body=body):
                status, _headers, error = self.request(
                    "POST", "/v1/memories/query", body
                )
                self.assertEqual(status, 403)
                rendered = json.dumps(error)
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn("wrong-project-sentinel", rendered)
        status, _headers, error = self.request(
            "POST",
            "/v1/admin/erase-user",
            {"user_id": "u1", "reason": "private erase body sentinel"},
        )
        self.assertEqual(status, 403)
        self.assertNotIn("u1", json.dumps(error))
        self.assertNotIn("private erase", json.dumps(error))

    def test_erase_enabled_authority_permission_scope_and_partition_matrix(self):
        denial_cases = [
            (
                make_principal(
                    scopes={},
                    permissions=frozenset(MEMORY_PERMISSIONS - {"memory:erase"}),
                ),
                {"user_id": "u1", "reason": "deny-sentinel"},
            ),
            *[
                (
                    make_principal(authority=authority, scopes={}),
                    {"user_id": "u1", "reason": "deny-sentinel"},
                )
                for authority in ("agent", "system", "external", "unknown")
            ],
            (
                make_principal(scopes={"project": "alpha"}),
                {"user_id": "u1", "reason": "deny-sentinel"},
            ),
            (
                make_principal(scopes={}),
                {
                    "user_id": "u1",
                    "reason": "deny-sentinel",
                    "scope": {"project": "alpha"},
                },
            ),
        ]
        for principal, body in denial_cases:
            with self.subTest(authority=principal.authority, scope=dict(principal.scopes)):
                denied = GovernedRestAdapter(
                    GovernedRestConfig(
                        db_path=":memory:",
                        principal=principal,
                        auth_mode="local",
                        port=0,
                        erase_enabled=True,
                    )
                )
                serve_thread = threading.Thread(target=denied.serve_forever)
                serve_thread.start()
                try:
                    status, _headers, error = self.request(
                        "POST", "/v1/admin/erase-user", body, adapter=denied
                    )
                    self.assertEqual(status, 403)
                    self.assertEqual(error["error"]["code"], "forbidden")
                    self.assertNotIn("u1", json.dumps(error))
                    self.assertNotIn("deny-sentinel", json.dumps(error))
                finally:
                    denied.close()
                    serve_thread.join(timeout=5)
                    self.assertFalse(serve_thread.is_alive())

        erase_db = str(Path(self.directory.name) / "erase-enabled.db")
        allowed = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=erase_db,
                principal=make_principal(
                    allowed_users=frozenset({"u1", "u2"}), scopes={}
                ),
                auth_mode="local",
                port=0,
                erase_enabled=True,
            )
        )
        serve_thread = threading.Thread(target=allowed.serve_forever)
        serve_thread.start()

        def seed(user_id, suffix):
            status, _headers, added = self.request(
                "POST",
                "/v1/events",
                {
                    "request_id": f"erase-{suffix}",
                    "user_id": user_id,
                    "session_id": f"erase-session-{suffix}",
                    "scope": {"project": "alpha"},
                    "messages": [
                        {"role": "user", "content": directive(f"erase-{suffix}")}
                    ],
                },
                adapter=allowed,
            )
            self.assertEqual(status, 200)
            status, _headers, extracted = self.request(
                "POST",
                "/v1/extractions",
                {
                    "user_id": user_id,
                    "raw_event_ids": added["receipt"]["raw_event_ids"],
                    "idempotency_key": f"erase-extract-{suffix}",
                    "scope": {"project": "alpha"},
                },
                adapter=allowed,
            )
            self.assertEqual(status, 200)
            status, _headers, transitioned = self.request(
                "POST",
                "/v1/memories/transition",
                {
                    "user_id": user_id,
                    "record_id": extracted["receipt"]["record_ids"][0],
                    "memory_key": "profile.city",
                    "target_status": "confirmed",
                    "reason": "erase setup owner review",
                    "scope": {"project": "alpha"},
                },
                adapter=allowed,
            )
            self.assertEqual(status, 200)

        try:
            seed("u1", "u1")
            seed("u2", "u2")

            concurrent_start = threading.Barrier(6)

            def read_other(index):
                concurrent_start.wait(timeout=5)
                return self.request(
                    "POST",
                    "/v1/memories/query",
                    {
                        "user_id": "u2",
                        "memory_key": "profile.city",
                        "scope": {"project": "alpha"},
                    },
                    adapter=allowed,
                )

            def write_other(index):
                concurrent_start.wait(timeout=5)
                return self.request(
                    "POST",
                    "/v1/events",
                    {
                        "request_id": f"other-write-{index}",
                        "user_id": "u2",
                        "scope": {"project": "alpha"},
                        "messages": [
                            {"role": "user", "content": f"other-{index}"}
                        ],
                    },
                    adapter=allowed,
                )

            def erase_target():
                concurrent_start.wait(timeout=5)
                return self.request(
                    "POST",
                    "/v1/admin/erase-user",
                    {"user_id": "u1", "reason": "privacy request"},
                    adapter=allowed,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(read_other, index) for index in range(4)]
                futures += [pool.submit(write_other, 0)]
                erase_future = pool.submit(erase_target)
                concurrent_results = [future.result(timeout=10) for future in futures]
                self.assertEqual(
                    [result[0] for result in concurrent_results],
                    [200] * len(futures),
                    concurrent_results,
                )
                status, _headers, erased = erase_future.result(timeout=10)
            self.assertEqual(status, 200)
            for key in (
                "deleted_messages",
                "deleted_views",
                "deleted_raw_events",
                "deleted_memory_records",
                "deleted_extraction_receipts",
                "deleted_proposal_origins",
            ):
                self.assertGreaterEqual(erased["receipt"][key], 1)

            tables = (
                "messages",
                "views",
                "fts",
                "requests",
                "sessions",
                "raw_events",
                "memory_state_events",
                "memory_records",
                "extraction_receipts",
                "proposal_origins",
            )
            con = sqlite3.connect(erase_db)
            try:
                for table in tables:
                    self.assertEqual(
                        con.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE user_id='u1'"
                        ).fetchone()[0],
                        0,
                        table,
                    )
                    self.assertGreater(
                        con.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE user_id='u2'"
                        ).fetchone()[0],
                        0,
                        table,
                    )
            finally:
                con.close()

            status, _headers, second = self.request(
                "POST",
                "/v1/admin/erase-user",
                {"user_id": "u1", "reason": "idempotent privacy retry"},
                adapter=allowed,
            )
            self.assertEqual(status, 200)
            self.assertTrue(
                all(
                    value == 0
                    for key, value in second["receipt"].items()
                    if key.startswith("deleted_")
                )
            )
        finally:
            allowed.close()
            serve_thread.join(timeout=5)
            self.assertFalse(serve_thread.is_alive())

    def test_audit_gate_uses_trusted_principal_purpose(self):
        self.cycle()
        status, _headers, audit = self.request(
            "POST",
            "/v1/memories/query",
            {
                "user_id": "u1",
                "mode": "audit",
                "memory_key": "profile.city",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(audit["result"]["status"], "ready")
        self.assertGreaterEqual(
            audit["result"]["omitted"]["by_reason"]["audit_evidence"], 1
        )
        self.assertNotIn("raw_events", audit["result"])
        self.assertNotIn("state_events", audit["result"])

    def test_candidate_and_withheld_internal_ids_never_escape_current_query(self):
        status, _headers, added = self.request(
            "POST",
            "/v1/events",
            {
                "request_id": "candidate-only",
                "user_id": "u1",
                "scope": {"project": "alpha"},
                "messages": [
                    {"role": "user", "content": directive("candidate-secret-sentinel")}
                ],
            },
        )
        self.assertEqual(status, 200)
        status, _headers, extracted = self.request(
            "POST",
            "/v1/extractions",
            {
                "user_id": "u1",
                "raw_event_ids": added["receipt"]["raw_event_ids"],
                "idempotency_key": "candidate-only",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        candidate_id = extracted["receipt"]["record_ids"][0]
        status, _headers, current = self.request(
            "POST",
            "/v1/memories/query",
            {
                "user_id": "u1",
                "memory_key": "profile.city",
                "scope": {"project": "alpha"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(current["result"]["status"], "unknown")
        self.assertTrue(current["result"]["owner_gate_required"])
        self.assertEqual(current["result"]["items"], [])
        rendered = json.dumps(current)
        self.assertNotIn(candidate_id, rendered)
        self.assertNotIn("candidate-secret-sentinel", rendered)
        self.assertNotIn("withheld_record_ids", rendered)

    def test_terminal_history_is_audit_only_and_never_injectable(self):
        for index, terminal in enumerate(("rejected", "superseded", "deleted"), start=1):
            with self.subTest(terminal=terminal):
                key = f"terminal.{terminal}"
                content = f"terminal-{terminal}-secret"
                record_id = self.cycle(
                    content=content,
                    suffix=f"terminal-{index}",
                    memory_key=key,
                )
                historical_cutoff = datetime.now(timezone.utc).isoformat()
                status, _headers, transitioned = self.request(
                    "POST",
                    "/v1/memories/transition",
                    {
                        "user_id": "u1",
                        "record_id": record_id,
                        "memory_key": key,
                        "target_status": terminal,
                        "reason": f"owner marked {terminal}",
                        "scope": {"project": "alpha"},
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(transitioned["record"]["current_status"], terminal)

                status, _headers, current = self.request(
                    "POST",
                    "/v1/memories/query",
                    {
                        "user_id": "u1",
                        "memory_key": key,
                        "scope": {"project": "alpha"},
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(current["result"]["status"], "unknown")
                self.assertNotIn(content, json.dumps(current))

                for path in ("/v1/memories/query", "/v1/context/compile"):
                    status, _headers, error = self.request(
                        "POST",
                        path,
                        {
                            "user_id": "u1",
                            "memory_key": key,
                            "scope": {"project": "alpha"},
                            "as_of": historical_cutoff,
                        },
                    )
                    self.assertEqual(status, 422)
                    self.assertNotIn(content, json.dumps(error))

                status, _headers, audit = self.request(
                    "POST",
                    "/v1/context/compile",
                    {
                        "user_id": "u1",
                        "memory_key": key,
                        "mode": "audit",
                        "scope": {"project": "alpha"},
                        "as_of": historical_cutoff,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(audit["context"]["status"], "ready")
                self.assertEqual(audit["context"]["items"][0]["content"], content)
                self.assertFalse(audit["injectable"])

    def test_principal_fields_and_executable_extractor_in_body_are_rejected(self):
        body = {
            "request_id": "forged",
            "user_id": "u1",
            "messages": [{"role": "user", "content": "do not persist"}],
            "principal_id": "forged-root",
            "authority": "owner",
            "permissions": ["memory:erase"],
        }
        status, _headers, error = self.request("POST", "/v1/events", body)
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "invalid_request")
        status, _headers, error = self.request(
            "POST",
            "/v1/extractions",
            {
                "user_id": "u1",
                "raw_event_ids": ["raw_missing"],
                "idempotency_key": "x",
                "extractor": {"callable": "danger"},
            },
        )
        self.assertEqual(status, 422)
        self.assertNotIn("danger", json.dumps(error))

    def test_events_exact_replay_and_divergent_payload_conflict(self):
        original = {
            "request_id": "exact-replay",
            "user_id": "u1",
            "session_id": "exact-session",
            "scope": {"project": "alpha"},
            "messages": [
                {"role": "user", "content": "exact-body", "timestamp": 1234567890000}
            ],
        }
        status, _headers, first = self.request("POST", "/v1/events", original)
        self.assertEqual(status, 200)
        self.assertFalse(first["receipt"]["idempotent"])
        status, _headers, replay = self.request("POST", "/v1/events", original)
        self.assertEqual(status, 200)
        self.assertTrue(replay["receipt"]["idempotent"])
        self.assertEqual(
            replay["receipt"]["raw_event_ids"], first["receipt"]["raw_event_ids"]
        )
        self.assertEqual(replay["receipt"]["session_id"], "exact-session")

        variants = []
        for field, value in (
            ("session_id", "different-session"),
            ("messages", [{"role": "user", "content": "different-body", "timestamp": 1234567890000}]),
            ("messages", [{"role": "assistant", "content": "exact-body", "timestamp": 1234567890000}]),
            ("messages", [{"role": "user", "content": "exact-body", "timestamp": 1234567890001}]),
        ):
            item = json.loads(json.dumps(original))
            item[field] = value
            variants.append(item)
        for variant in variants:
            status, _headers, error = self.request("POST", "/v1/events", variant)
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "conflict")
            self.assertNotIn("different", json.dumps(error))

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM messages WHERE request_id='exact-replay'"
                ).fetchone()[0],
                1,
            )
        finally:
            con.close()

    def test_content_type_body_limit_methods_and_errors_are_stable(self):
        connection = http.client.HTTPConnection(*self.adapter.address, timeout=5)
        try:
            connection.request(
                "POST",
                "/v1/events",
                body=b"{}",
                headers={"Content-Type": "text/plain"},
            )
            response = connection.getresponse()
            error = json.loads(response.read())
            self.assertEqual(response.status, 415)
            self.assertEqual(error["error"]["code"], "unsupported_media_type")
        finally:
            connection.close()

        status, headers, error = self.request("GET", "/v1/events")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")
        self.assertEqual(error["error"]["code"], "method_not_allowed")

        tiny_config = GovernedRestConfig(
            db_path=":memory:",
            principal=make_principal(),
            auth_mode="local",
            port=0,
            max_body_bytes=64,
        )
        tiny = GovernedRestAdapter(tiny_config)
        thread = threading.Thread(target=tiny.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(*tiny.address, timeout=5)
            conn.request(
                "POST",
                "/v1/events",
                body=b'{' + b'"x":"' + b'a' * 100 + b'"}',
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            error = json.loads(response.read())
            self.assertEqual(response.status, 413)
            self.assertEqual(error["error"]["code"], "body_too_large")
            conn.close()
        finally:
            tiny.close()
            thread.join(timeout=5)

    def test_handler_exception_is_sanitized_and_server_remains_healthy(self):
        secret = "database-path-and-body-secret"
        with mock.patch.object(self.adapter, "dispatch", side_effect=RuntimeError(secret)):
            status, _headers, error = self.request(
                "POST",
                "/v1/memories/query",
                {"user_id": "u1", "scope": {"project": "alpha"}},
            )
        self.assertEqual(status, 500)
        self.assertNotIn(secret, json.dumps(error))
        status, _headers, health = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ready"])

    def test_non_owner_authority_transition_is_fixed_403_without_mutation(self):
        for authority in ("agent", "system", "external", "unknown"):
            with self.subTest(authority=authority):
                path = str(Path(self.directory.name) / f"authority-{authority}.db")
                adapter = GovernedRestAdapter(
                    GovernedRestConfig(
                        db_path=path,
                        principal=make_principal(authority=authority),
                        auth_mode="local",
                        port=0,
                    )
                )
                principal = adapter.config.principal
                added = adapter._events(
                    principal,
                    {
                        "request_id": f"authority-{authority}",
                        "user_id": "u1",
                        "scope": {"project": "alpha"},
                        "messages": [
                            {"role": "user", "content": directive("authority-test")}
                        ],
                    },
                )
                extracted = adapter._extractions(
                    principal,
                    {
                        "user_id": "u1",
                        "raw_event_ids": added["receipt"]["raw_event_ids"],
                        "idempotency_key": f"authority-{authority}",
                        "scope": {"project": "alpha"},
                    },
                )
                record_id = extracted["receipt"]["record_ids"][0]
                serve_thread = threading.Thread(target=adapter.serve_forever)
                serve_thread.start()
                try:
                    status, _headers, error = self.request(
                        "POST",
                        "/v1/memories/transition",
                        {
                            "user_id": "u1",
                            "record_id": record_id,
                            "memory_key": "profile.city",
                            "target_status": "confirmed",
                            "reason": "forged owner gate sentinel",
                            "scope": {"project": "alpha"},
                        },
                        adapter=adapter,
                    )
                    self.assertEqual(status, 403)
                    self.assertEqual(error["error"]["code"], "forbidden")
                    self.assertNotIn(record_id, json.dumps(error))
                    self.assertNotIn("forged owner", json.dumps(error))
                    con = sqlite3.connect(path)
                    try:
                        self.assertEqual(
                            con.execute(
                                "SELECT status FROM memory_records WHERE id=?",
                                (record_id,),
                            ).fetchone()[0],
                            "candidate",
                        )
                    finally:
                        con.close()
                finally:
                    adapter.close()
                    serve_thread.join(timeout=5)
                    self.assertFalse(serve_thread.is_alive())

    def test_bearer_uses_constant_time_compare_for_wrong_and_right_tokens(self):
        bearer_config = GovernedRestConfig(
            db_path=":memory:",
            principal=make_principal(),
            auth_mode="bearer",
            bearer_token="expected-token",
            port=0,
        )
        bearer = GovernedRestAdapter(bearer_config)
        thread = threading.Thread(target=bearer.serve_forever, daemon=True)
        thread.start()
        try:
            def health_query(token):
                conn = http.client.HTTPConnection(*bearer.address, timeout=5)
                payload = json.dumps(
                    {"user_id": "u1", "scope": {"project": "alpha"}}
                ).encode()
                conn.request(
                    "POST",
                    "/v1/memories/query",
                    body=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
                response = conn.getresponse()
                response.read()
                conn.close()
                return response.status

            real_compare = __import__("hmac").compare_digest
            with mock.patch(
                "aml_retriever.rest_v1.hmac.compare_digest",
                wraps=real_compare,
            ) as compared:
                self.assertEqual(health_query("wrong-token"), 401)
                self.assertEqual(health_query("expected-token"), 200)
                self.assertEqual(health_query("é"), 401)
                self.assertEqual(compared.call_count, 3)
                self.assertTrue(
                    all(
                        isinstance(arg, bytes)
                        for call in compared.call_args_list
                        for arg in call.args
                    )
                )
            conn = http.client.HTTPConnection(*bearer.address, timeout=5)
            conn.request("GET", "/v1/health")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            conn.close()
        finally:
            bearer.close()
            thread.join(timeout=5)

    def test_close_then_reopen_same_database_and_port(self):
        host, port = self.adapter.address
        self.adapter.close()
        self.thread.join(timeout=5)
        replacement = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=self.db_path,
                principal=make_principal(),
                auth_mode="local",
                bind_host=host,
                port=port,
            )
        )
        thread = threading.Thread(target=replacement.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*replacement.address, timeout=5)
            connection.request("GET", "/v1/health")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            connection.close()
        finally:
            replacement.close()
            thread.join(timeout=5)

    def test_close_drains_inflight_request_and_concurrent_close_callers(self):
        host, port = self.adapter.address
        entered = threading.Event()
        release = threading.Event()
        result = []
        original_dispatch = self.adapter.dispatch

        def blocked_dispatch(path, principal, body):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original_dispatch(path, principal, body)

        with mock.patch.object(self.adapter, "dispatch", side_effect=blocked_dispatch):
            requester = threading.Thread(
                target=lambda: result.append(
                    self.request(
                        "POST",
                        "/v1/memories/query",
                        {
                            "user_id": "u1",
                            "memory_key": "missing",
                            "scope": {"project": "alpha"},
                        },
                    )
                )
            )
            requester.start()
            self.assertTrue(entered.wait(timeout=5))
            close_done = [threading.Event(), threading.Event()]

            def close(index):
                self.adapter.close()
                close_done[index].set()

            closers = [threading.Thread(target=close, args=(index,)) for index in range(2)]
            for closer in closers:
                closer.start()
            self.assertFalse(close_done[0].wait(timeout=0.1))
            self.assertFalse(close_done[1].is_set())
            release.set()
            requester.join(timeout=5)
            for closer in closers:
                closer.join(timeout=5)

        self.assertFalse(requester.is_alive())
        self.assertTrue(all(not closer.is_alive() for closer in closers))
        self.assertTrue(all(item.is_set() for item in close_done))
        self.assertEqual(result[0][0], 200)
        self.assertTrue(self.adapter.closed)

        replacement = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=self.db_path,
                principal=make_principal(),
                auth_mode="local",
                bind_host=host,
                port=port,
            )
        )
        serve_thread = threading.Thread(target=replacement.serve_forever)
        serve_thread.start()
        try:
            status, _headers, health = self.request(
                "GET", "/v1/health", adapter=replacement
            )
            self.assertEqual(status, 200)
            self.assertTrue(health["ready"])
        finally:
            replacement.close()
            serve_thread.join(timeout=5)
            self.assertFalse(serve_thread.is_alive())

    def test_authenticated_partial_body_times_out_and_shutdown_drains(self):
        adapter = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=str(Path(self.directory.name) / "partial.db"),
                principal=make_principal(),
                auth_mode="local",
                port=0,
            )
        )
        serve_thread = threading.Thread(target=adapter.serve_forever)
        serve_thread.start()
        client = socket.create_connection(adapter.address, timeout=5)
        close_thread = None
        try:
            client.sendall(
                b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
            )
            with adapter._lifecycle:
                self.assertTrue(
                    adapter._lifecycle.wait_for(
                        lambda: adapter._active_requests > 0, timeout=1.5
                    )
                )
            close_thread = threading.Thread(target=adapter.close)
            close_thread.start()
            client.settimeout(5)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
            self.assertEqual(response.count(b"HTTP/1.1"), 1)
            self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
            self.assertIn(b"Connection: close", response)
        finally:
            client.close()
            adapter.close()
            if close_thread is not None:
                close_thread.join(timeout=5)
                self.assertFalse(close_thread.is_alive())
            serve_thread.join(timeout=5)
            self.assertFalse(serve_thread.is_alive())

    def test_parser_errors_are_fully_framed_fixed_json(self):
        def exchange(request: bytes) -> bytes:
            client = socket.create_connection(self.adapter.address, timeout=5)
            try:
                client.sendall(request)
                client.shutdown(socket.SHUT_WR)
                chunks = []
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                client.close()

        unsupported_version = (
            b'{"error":{"code":"http_version_not_supported",'
            b'"message":"HTTP version not supported"}}'
        )
        bad_request = b'{"error":{"code":"bad_request","message":"request rejected"}}'
        cases = (
            (
                b"GET /v1/health HTTP/9.9\r\nHost: 127.0.0.1\r\n\r\n",
                b"HTTP/1.1 505 HTTP Version Not Supported",
                unsupported_version,
            ),
            (
                b"GET /v1/health HTTP/2.0\r\nHost: 127.0.0.1\r\n\r\n",
                b"HTTP/1.1 505 HTTP Version Not Supported",
                unsupported_version,
            ),
            (
                b"GET /v1/health HTTP/0.9\r\nHost: 127.0.0.1\r\n\r\n",
                b"HTTP/1.1 400 Bad Request",
                bad_request,
            ),
            (
                b"GET /v1/health\r\n",
                b"HTTP/1.1 400 Bad Request",
                bad_request,
            ),
            (
                b"GET /unknown\r\n\r\n",
                b"HTTP/1.1 400 Bad Request",
                bad_request,
            ),
            (
                b"GET /v1/health NOT-A-PROTOCOL\r\nHost: 127.0.0.1\r\n\r\n",
                b"HTTP/1.1 400 Bad Request",
                bad_request,
            ),
            (
                b"MALFORMED-REQUEST-LINE\r\n\r\n",
                b"HTTP/1.1 400 Bad Request",
                bad_request,
            ),
        )
        for request, expected_status_line, expected_body in cases:
            with self.subTest(request_line=request.split(b"\r\n", 1)[0]):
                response = exchange(request)
                self.assertEqual(response.count(b"HTTP/1.1"), 1, response)
                head, separator, body = response.partition(b"\r\n\r\n")
                self.assertEqual(separator, b"\r\n\r\n", response)
                lines = head.split(b"\r\n")
                self.assertEqual(lines[0], expected_status_line)
                headers = dict(line.split(b": ", 1) for line in lines[1:])
                self.assertEqual(
                    headers[b"Content-Type"], b"application/json; charset=utf-8"
                )
                self.assertEqual(
                    headers[b"Content-Length"], str(len(body)).encode()
                )
                self.assertEqual(headers[b"Cache-Control"], b"no-store")
                self.assertEqual(headers[b"Pragma"], b"no-cache")
                self.assertEqual(headers[b"X-Content-Type-Options"], b"nosniff")
                self.assertEqual(headers[b"Connection"], b"close")
                self.assertEqual(body, expected_body)

        for version in (b"HTTP/1.0", b"HTTP/1.1"):
            with self.subTest(version=version):
                response = exchange(
                    b"GET /v1/health "
                    + version
                    + b"\r\nHost: 127.0.0.1\r\n\r\n"
                )
                head, separator, body = response.partition(b"\r\n\r\n")
                self.assertEqual(separator, b"\r\n\r\n", response)
                self.assertEqual(head.split(b"\r\n", 1)[0], b"HTTP/1.1 200 OK")
                self.assertTrue(json.loads(body)["ready"])

    def test_unread_body_early_returns_cannot_execute_embedded_second_request(self):
        smuggle_db = str(Path(self.directory.name) / "smuggle.db")
        bearer = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=smuggle_db,
                principal=make_principal(),
                auth_mode="bearer",
                bearer_token="expected-token",
                port=0,
            )
        )
        thread = threading.Thread(target=bearer.serve_forever, daemon=True)
        thread.start()

        def exchange(request: bytes) -> bytes:
            client = socket.create_connection(bearer.address, timeout=5)
            try:
                client.sendall(request)
                client.shutdown(socket.SHUT_WR)
                chunks = []
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                client.close()

        embedded_body = json.dumps(
            {
                "request_id": "must-not-run",
                "user_id": "u1",
                "scope": {"project": "alpha"},
                "messages": [{"role": "user", "content": "must-not-persist"}],
            }
        ).encode()
        embedded = (
            b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer expected-token\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(embedded_body)}\r\n\r\n".encode()
            + embedded_body
        )
        cases = (
            (
                b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {len(embedded)}\r\n".encode()
                + b"Content-Type: application/json\r\n\r\n"
                + embedded,
                b" 401 ",
            ),
            (
                b"POST /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {len(embedded)}\r\n\r\n".encode()
                + embedded,
                b" 405 ",
            ),
            (
                b"POST /missing HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {len(embedded)}\r\n\r\n".encode()
                + embedded,
                b" 404 ",
            ),
            (
                b"PUT /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {len(embedded)}\r\n\r\n".encode()
                + embedded,
                b" 405 ",
            ),
            (
                b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {len(embedded)}\r\n\r\n".encode()
                + embedded,
                b" 200 ",
            ),
            (
                b"BREW /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                b" 405 ",
            ),
            (
                b"GET /v1/health? HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                b" 404 ",
            ),
            (
                b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer expected-token\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n"
                b"Expect: 100-continue\r\n\r\n{}",
                b" 417 ",
            ),
            (
                b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer expected-token\r\n"
                b"Content-Type: application/json\r\nContent-Length: "
                + b"9" * 5000
                + b"\r\n\r\n",
                b" 400 ",
            ),
            (
                b"GET /" + b"x" * 66000 + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                b" 414 ",
            ),
        )
        try:
            for request, expected in cases:
                with self.subTest(expected=expected):
                    response = exchange(request)
                    self.assertEqual(response.count(b"HTTP/1.1"), 1, response)
                    self.assertIn(expected, response.split(b"\r\n", 1)[0])
                    self.assertIn(b"Connection: close", response)
            con = sqlite3.connect(smuggle_db)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            finally:
                con.close()
            connection = http.client.HTTPConnection(*bearer.address, timeout=5)
            connection.request("GET", "/v1/health")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            connection.close()
        finally:
            bearer.close()
            thread.join(timeout=5)


class TestRestDirectAuthority(unittest.TestCase):
    def test_transition_related_record_must_resolve_in_same_scope_and_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "related.db")
            adapter = GovernedRestAdapter(
                GovernedRestConfig(
                    db_path=path,
                    principal=make_principal(scopes={}),
                    auth_mode="local",
                    port=0,
                ),
                server_class=_FakeServer,
            )
            principal = adapter.config.principal
            try:
                ids = {}
                for project in ("alpha", "beta"):
                    added = adapter._events(
                        principal,
                        {
                            "request_id": f"related-{project}",
                            "user_id": "u1",
                            "scope": {"project": project},
                            "messages": [
                                {
                                    "role": "user",
                                    "content": directive(f"related-{project}"),
                                }
                            ],
                        },
                    )
                    extracted = adapter._extractions(
                        principal,
                        {
                            "user_id": "u1",
                            "raw_event_ids": added["receipt"]["raw_event_ids"],
                            "idempotency_key": f"related-{project}",
                            "scope": {"project": project},
                        },
                    )
                    ids[project] = extracted["receipt"]["record_ids"][0]
                with self.assertRaises(AuthorizationError):
                    adapter._transition(
                        principal,
                        {
                            "user_id": "u1",
                            "record_id": ids["alpha"],
                            "memory_key": "profile.city",
                            "target_status": "deleted",
                            "reason": "must not link across scope",
                            "scope": {"project": "alpha"},
                            "related_record_id": ids["beta"],
                        },
                    )
                con = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        con.execute(
                            "SELECT status FROM memory_records WHERE id=?", (ids["alpha"],)
                        ).fetchone()[0],
                        "candidate",
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM memory_state_events WHERE record_id=?",
                            (ids["alpha"],),
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    con.close()
            finally:
                adapter.close()

    def test_ingest_scope_is_immutable_exact_and_cross_scope_extraction_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "scope.db")
            adapter = GovernedRestAdapter(
                GovernedRestConfig(
                    db_path=path,
                    principal=make_principal(scopes={}),
                    auth_mode="local",
                    port=0,
                ),
                server_class=_FakeServer,
            )
            principal = adapter.config.principal
            try:
                body = {
                    "request_id": "scope-bound",
                    "user_id": "u1",
                    "session_id": "scope-session",
                    "scope": {"project": "beta"},
                    "messages": [
                        {"role": "user", "content": directive("scope-secret")}
                    ],
                }
                first = adapter._events(principal, body)
                replay = adapter._events(principal, body)
                self.assertFalse(first["receipt"]["idempotent"])
                self.assertTrue(replay["receipt"]["idempotent"])
                self.assertEqual(
                    first["receipt"]["raw_event_ids"], replay["receipt"]["raw_event_ids"]
                )
                divergent = json.loads(json.dumps(body))
                divergent["scope"] = {"project": "alpha"}
                with self.assertRaises(GovernanceConflict):
                    adapter._events(principal, divergent)

                raw_id = first["receipt"]["raw_event_ids"][0]
                with self.assertRaises(ExtractionValidationError):
                    adapter._extractions(
                        principal,
                        {
                            "user_id": "u1",
                            "raw_event_ids": [raw_id],
                            "idempotency_key": "wrong-scope",
                            "scope": {"project": "alpha"},
                        },
                    )
                con = sqlite3.connect(path)
                try:
                    raw_scope = json.loads(
                        con.execute(
                            "SELECT scope_json FROM raw_events WHERE id=?", (raw_id,)
                        ).fetchone()[0]
                    )
                    self.assertEqual(
                        raw_scope,
                        {"project": "beta", "session": "scope-session", "user": "u1"},
                    )
                    for table in (
                        "memory_records",
                        "extraction_receipts",
                        "proposal_origins",
                    ):
                        self.assertEqual(
                            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                        )
                finally:
                    con.close()

                accepted = adapter._extractions(
                    principal,
                    {
                        "user_id": "u1",
                        "raw_event_ids": [raw_id],
                        "idempotency_key": "right-scope",
                        "scope": {"project": "beta"},
                    },
                )
                self.assertEqual(accepted["receipt"]["proposal_count"], 1)
            finally:
                adapter.close()

    def test_owner_gate_authority_matrix_comes_only_from_principal(self):
        for authority in ("user", "owner", "policy", "agent", "system", "external", "unknown"):
            with self.subTest(authority=authority):
                adapter = GovernedRestAdapter(
                    GovernedRestConfig(
                        db_path=":memory:",
                        principal=make_principal(authority=authority),
                        auth_mode="local",
                        port=0,
                    ),
                    server_class=_FakeServer,
                )
                principal = adapter.config.principal
                try:
                    added = adapter._events(
                        principal,
                        {
                            "request_id": "r",
                            "user_id": "u1",
                            "scope": {"project": "alpha"},
                            "messages": [
                                {
                                    "role": "user",
                                    "content": directive("Paris"),
                                }
                            ],
                        },
                    )
                    extracted = adapter._extractions(
                        principal,
                        {
                            "user_id": "u1",
                            "raw_event_ids": added["receipt"]["raw_event_ids"],
                            "idempotency_key": "e",
                            "scope": {"project": "alpha"},
                        },
                    )
                    body = {
                        "user_id": "u1",
                        "record_id": extracted["receipt"]["record_ids"][0],
                        "memory_key": "profile.city",
                        "target_status": "confirmed",
                        "reason": "trusted owner gate",
                        "scope": {"project": "alpha"},
                        # actor/authority are intentionally absent.
                    }
                    if authority in {"user", "owner", "policy"}:
                        record = adapter._transition(principal, body)["record"]
                        self.assertEqual(
                            record,
                            {
                                "record_id": extracted["receipt"]["record_ids"][0],
                                "current_status": "confirmed",
                            },
                        )
                    else:
                        with self.assertRaises(AuthorizationError):
                            adapter._transition(principal, body)
                finally:
                    adapter.close()

    def test_concurrent_requests_do_not_cross_user_results(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = GovernedRestAdapter(
                GovernedRestConfig(
                    db_path=str(Path(directory) / "concurrent.db"),
                    principal=make_principal(
                        allowed_users=frozenset({"u1", "u2"}), scopes={}
                    ),
                    auth_mode="local",
                    port=0,
                ),
                server_class=_FakeServer,
            )
            principal = adapter.config.principal
            try:
                for user, content in (("u1", "Paris-u1"), ("u2", "Rome-u2")):
                    added = adapter._events(
                        principal,
                        {
                            "request_id": f"r-{user}",
                            "user_id": user,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": directive(content),
                                }
                            ],
                        },
                    )
                    extracted = adapter._extractions(
                        principal,
                        {
                            "user_id": user,
                            "raw_event_ids": added["receipt"]["raw_event_ids"],
                            "idempotency_key": f"e-{user}",
                        },
                    )
                    adapter._transition(
                        principal,
                        {
                            "user_id": user,
                            "record_id": extracted["receipt"]["record_ids"][0],
                            "memory_key": "profile.city",
                            "target_status": "confirmed",
                            "reason": "isolated owner confirmation",
                        },
                    )

                def query(user):
                    return adapter._query(
                        principal,
                        {"user_id": user, "memory_key": "profile.city"},
                    )["result"]["items"][0]["content"]

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    outputs = list(pool.map(query, ["u1", "u2"] * 20))
                self.assertEqual(outputs[0::2], ["Paris-u1"] * 20)
                self.assertEqual(outputs[1::2], ["Rome-u2"] * 20)
            finally:
                adapter.close()


if __name__ == "__main__":
    unittest.main()
