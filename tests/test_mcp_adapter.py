"""Official MCP SDK v2 adapter tests (skipped when the optional SDK is absent)."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aml_retriever.auth import (
    PERMISSION_EXTRACT,
    PERMISSION_READ,
    PERMISSION_WRITE,
    TrustedPrincipal,
)
from aml_retriever.extraction import DIRECTIVE_PREFIX
from aml_retriever.mcp_adapter import MCPDependencyError, create_mcp_server, main
from aml_retriever.mcp_tools import GovernedMCPTools, TOOL_NAMES


MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def _principal() -> TrustedPrincipal:
    return TrustedPrincipal(
        principal_id="official-sdk-client",
        authority="owner",
        allowed_users=frozenset({"u1"}),
        scopes={"project": "mcp-sdk-test"},
        permissions=frozenset({PERMISSION_WRITE, PERMISSION_EXTRACT, PERMISSION_READ}),
    )


def _directive() -> str:
    return DIRECTIVE_PREFIX + "\n" + json.dumps(
        {
            "proposals": [
                {
                    "memory_key": "profile.language",
                    "memory_type": "preference",
                    "subject": "$user",
                    "content": "SDK-SENTINEL-CONTENT",
                }
            ]
        },
        separators=(",", ":"),
    )


class TestOptionalDependency(unittest.TestCase):
    @unittest.skipIf(MCP_AVAILABLE, "base dependency-absence check")
    def test_base_import_succeeds_and_factory_fails_with_fixed_error(self):
        with self.assertRaisesRegex(MCPDependencyError, "^mcp_dependency_unavailable$"):
            create_mcp_server(db_path=":memory:", principal=_principal())

    def test_cli_configuration_failure_is_fixed_and_stdout_stays_empty(self):
        sentinel = "SECRET-CONFIG-PATH"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "--db",
                    f"/tmp/{sentinel}.db",
                    "--principal-config",
                    f"/tmp/{sentinel}.json",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "flowgrid-memory-mcp: configuration_error\n")
        self.assertNotIn(sentinel, stderr.getvalue())


@unittest.skipUnless(MCP_AVAILABLE, "official MCP SDK v2 optional dependency")
class TestOfficialSDKClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from mcp import Client

        self.Client = Client
        self._tmp = tempfile.TemporaryDirectory(prefix="flowgrid-mcp-sdk-")
        self.db_path = str(Path(self._tmp.name) / "memory.db")
        self.server = create_mcp_server(db_path=self.db_path, principal=_principal())

    def tearDown(self):
        self._tmp.cleanup()

    async def test_list_tools_is_exact_and_schemas_have_no_identity_override(self):
        async with self.Client(self.server) as client:
            listed = await client.list_tools()
            self.assertEqual([tool.name for tool in listed.tools], list(TOOL_NAMES))
            for tool in listed.tools:
                properties = tool.input_schema["properties"]
                for forbidden in (
                    "principal",
                    "principal_id",
                    "authority",
                    "permissions",
                    "allowed_users",
                    "actor",
                    "transition",
                    "as_of",
                ):
                    self.assertNotIn(forbidden, properties)
            self.assertEqual(
                set(TOOL_NAMES),
                {
                    "memory_ingest_events",
                    "memory_extract_candidates",
                    "memory_query_current",
                    "memory_compile_context",
                },
            )

            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            prompts = await client.list_prompts()
            self.assertEqual(resources.resources, [])
            self.assertEqual(templates.resource_templates, [])
            self.assertEqual(prompts.prompts, [])

    async def test_real_client_ingest_extract_current_and_context(self):
        async with self.Client(self.server) as client:
            ingest = await client.call_tool(
                "memory_ingest_events",
                {
                    "request_id": "sdk-ingest-1",
                    "user_id": "u1",
                    "messages": [{"role": "user", "content": _directive()}],
                    "scope": {"project": "mcp-sdk-test"},
                },
            )
            self.assertFalse(ingest.is_error)
            self.assertEqual(ingest.structured_content["event_count"], 1)
            self.assertNotIn("SDK-SENTINEL-CONTENT", json.dumps(ingest.structured_content))

            extract = await client.call_tool(
                "memory_extract_candidates",
                {
                    "user_id": "u1",
                    "raw_event_ids": ingest.structured_content["raw_event_ids"],
                    "idempotency_key": "sdk-extract-1",
                    "scope": {"project": "mcp-sdk-test"},
                },
            )
            self.assertFalse(extract.is_error)
            self.assertEqual(extract.structured_content["proposal_count"], 1)

            for name in ("memory_query_current", "memory_compile_context"):
                result = await client.call_tool(
                    name,
                    {
                        "user_id": "u1",
                        "memory_key": "profile.language",
                        "scope": {"project": "mcp-sdk-test"},
                        "max_chars": 4096,
                    },
                )
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["status"], "unknown")
                self.assertTrue(result.structured_content["abstain"])
                self.assertTrue(result.structured_content["owner_gate_required"])
                self.assertEqual(result.structured_content["items"], [])
                rendered = json.dumps(result.structured_content)
                self.assertNotIn("SDK-SENTINEL-CONTENT", rendered)
                for forbidden in (
                    '"raw_events"',
                    '"state_events"',
                    '"actor"',
                    '"state_reason"',
                    '"created_by"',
                    '"source_event_ids"',
                    '"path"',
                ):
                    self.assertNotIn(forbidden, rendered)

    async def test_forged_identity_user_scope_unknown_and_bad_args_are_fixed(self):
        cases = (
            (
                "memory_query_current",
                {
                    "user_id": "u1",
                    "memory_key": "x",
                    "scope": {"project": "mcp-sdk-test"},
                    "principal": "SECRET-FORGED-PRINCIPAL",
                },
                "invalid_request",
            ),
            (
                "memory_query_current",
                {
                    "user_id": "SECRET-FORGED-USER",
                    "memory_key": "x",
                    "scope": {"project": "mcp-sdk-test"},
                },
                "access_denied",
            ),
            (
                "memory_query_current",
                {
                    "user_id": "u1",
                    "memory_key": "x",
                    "scope": {"project": "SECRET-FORGED-SCOPE"},
                },
                "access_denied",
            ),
            (
                "memory_query_current",
                {
                    "user_id": "u1",
                    "memory_key": "x",
                    "scope": {"project": "mcp-sdk-test"},
                    "as_of": "2020-01-01T00:00:00+00:00",
                },
                "invalid_request",
            ),
            ("SECRET-UNKNOWN-TOOL", {}, "tool_not_available"),
        )
        async with self.Client(self.server) as client:
            for name, arguments, code in cases:
                with self.subTest(code=code):
                    result = await client.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], code)
                    rendered = json.dumps(result.model_dump(by_alias=True), ensure_ascii=False)
                    self.assertNotIn("SECRET-", rendered)
                    self.assertNotIn(self.db_path, rendered)

    async def test_token_budget_is_explicitly_unsupported(self):
        async with self.Client(self.server) as client:
            result = await client.call_tool(
                "memory_compile_context",
                {
                    "user_id": "u1",
                    "memory_key": "missing",
                    "scope": {"project": "mcp-sdk-test"},
                    "max_tokens": 10,
                },
            )
            self.assertTrue(result.is_error)
            self.assertEqual(
                result.structured_content,
                {"status": "error", "error": {"code": "token_budget_unsupported"}},
            )

    async def test_forbidden_tools_do_not_open_or_mutate_database(self):
        untouched = str(Path(self._tmp.name) / "forbidden-must-not-exist.db")
        server = create_mcp_server(db_path=untouched, principal=_principal())
        async with self.Client(server) as client:
            for name in (
                "memory_transition",
                "memory_query_audit",
                "memory_erase_user",
                "memory_raw_evidence",
            ):
                result = await client.call_tool(name, {})
                self.assertTrue(result.is_error)
                self.assertEqual(
                    result.structured_content["error"]["code"],
                    "tool_not_available",
                )
                self.assertFalse(Path(untouched).exists())
        self.assertFalse(Path(untouched).exists())

    async def test_all_tool_exceptions_are_sanitized_everywhere(self):
        cases = {
            "memory_ingest_events": {
                "request_id": "r",
                "user_id": "u1",
                "messages": [{"role": "user", "content": "body"}],
                "scope": {"project": "mcp-sdk-test"},
            },
            "memory_extract_candidates": {
                "user_id": "u1",
                "raw_event_ids": ["raw_000000000000000000000000"],
                "idempotency_key": "k",
                "scope": {"project": "mcp-sdk-test"},
            },
            "memory_query_current": {
                "user_id": "u1",
                "memory_key": "x",
                "scope": {"project": "mcp-sdk-test"},
            },
            "memory_compile_context": {
                "user_id": "u1",
                "memory_key": "x",
                "scope": {"project": "mcp-sdk-test"},
            },
        }
        sentinel = "SECRET-TOOL-EXCEPTION-/private/memory.db"
        for method_name, arguments in cases.items():
            with self.subTest(method_name=method_name):
                server = create_mcp_server(
                    db_path=str(Path(self._tmp.name) / f"{method_name}.db"),
                    principal=_principal(),
                )
                stderr = io.StringIO()
                with mock.patch.object(
                    GovernedMCPTools,
                    method_name,
                    side_effect=RuntimeError(sentinel),
                ), contextlib.redirect_stderr(stderr):
                    async with self.Client(server) as client:
                        result = await client.call_tool(method_name, arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(
                    result.structured_content,
                    {"status": "error", "error": {"code": "operation_failed"}},
                )
                rendered = (
                    json.dumps(result.model_dump(by_alias=True), ensure_ascii=False)
                    + repr(result)
                    + stderr.getvalue()
                )
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn(self.db_path, rendered)

    async def test_two_trusted_principals_are_user_isolated(self):
        shared_db = str(Path(self._tmp.name) / "principal-isolation.db")
        u1_server = create_mcp_server(db_path=shared_db, principal=_principal())
        u2 = TrustedPrincipal(
            principal_id="second-official-client",
            authority="owner",
            allowed_users=frozenset({"u2"}),
            scopes={"project": "mcp-sdk-test"},
            permissions=frozenset({PERMISSION_READ}),
        )
        u2_server = create_mcp_server(db_path=shared_db, principal=u2)
        async with self.Client(u1_server) as client:
            created = await client.call_tool(
                "memory_ingest_events",
                {
                    "request_id": "principal-u1",
                    "user_id": "u1",
                    "messages": [{"role": "user", "content": "u1-private-body"}],
                    "scope": {"project": "mcp-sdk-test"},
                },
            )
            self.assertFalse(created.is_error)
        with sqlite3.connect(shared_db) as connection:
            before = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        async with self.Client(u2_server) as client:
            denied = await client.call_tool(
                "memory_query_current",
                {
                    "user_id": "u1",
                    "memory_key": "x",
                    "scope": {"project": "mcp-sdk-test"},
                },
            )
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content["error"]["code"], "access_denied")
        with sqlite3.connect(shared_db) as connection:
            after = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(before, after)

    async def test_server_lifespan_closes_core_after_disconnect(self):
        async with self.Client(self.server) as client:
            await client.list_tools()
        # Reconnect to the same server. Its captured core was closed by the
        # first official Client lifespan.
        async with self.Client(self.server) as client:
            result = await client.call_tool(
                "memory_query_current",
                {
                    "user_id": "u1",
                    "memory_key": "x",
                    "scope": {"project": "mcp-sdk-test"},
                },
            )
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error"]["code"], "tool_closed")


if __name__ == "__main__":
    unittest.main()
