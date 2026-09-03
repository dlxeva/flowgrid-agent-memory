"""Zero-dependency acceptance tests for the safe MCP tool core."""
from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aml_retriever.auth import (
    PERMISSION_EXTRACT,
    PERMISSION_READ,
    PERMISSION_WRITE,
    TrustedPrincipal,
)
from aml_retriever.extraction import DIRECTIVE_PREFIX
from aml_retriever.facade import FlowGridMemory
from aml_retriever.mcp_tools import GovernedMCPTools, TOOL_NAMES


def _principal(
    *,
    users=frozenset({"u1"}),
    permissions=frozenset({PERMISSION_WRITE, PERMISSION_EXTRACT, PERMISSION_READ}),
) -> TrustedPrincipal:
    return TrustedPrincipal(
        principal_id="local-mcp-principal",
        authority="owner",
        allowed_users=users,
        scopes={"project": "mcp-test"},
        permissions=permissions,
    )


def _directive(content: str = "private-sentinel-content") -> str:
    return DIRECTIVE_PREFIX + "\n" + json.dumps(
        {
            "proposals": [
                {
                    "memory_key": "profile.response_style",
                    "memory_type": "preference",
                    "subject": "$user",
                    "content": content,
                    "confidence": 1.0,
                }
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class MCPToolsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flowgrid-mcp-tools-")
        self.db_path = str(Path(self._tmp.name) / "memory.db")
        self.tools = GovernedMCPTools(db_path=self.db_path, principal=_principal())

    def tearDown(self):
        self.tools.close()
        self._tmp.cleanup()

    def ingest(self, *, body: str | None = None) -> dict[str, object]:
        return self.tools.memory_ingest_events(
            request_id="ingest-1",
            user_id="u1",
            session_id="session-1",
            messages=[{"role": "user", "content": body or _directive()}],
            scope={"project": "mcp-test"},
        )


class TestSafeSurface(MCPToolsCase):
    def test_exact_four_methods_and_no_governance_admin_escape_hatch(self):
        self.assertEqual(
            TOOL_NAMES,
            (
                "memory_ingest_events",
                "memory_extract_candidates",
                "memory_query_current",
                "memory_compile_context",
            ),
        )
        for forbidden in (
            "transition_memory",
            "query_audit",
            "erase_user",
            "raw_evidence",
            "memory_transition",
        ):
            self.assertFalse(hasattr(self.tools, forbidden))
        signature = inspect.signature(self.tools.memory_extract_candidates)
        self.assertNotIn("extractor", signature.parameters)
        for method_name in TOOL_NAMES:
            parameters = inspect.signature(getattr(self.tools, method_name)).parameters
            for forbidden in ("principal", "authority", "permissions", "allowed_users"):
                self.assertNotIn(forbidden, parameters)
        self.assertNotIn(
            "as_of", inspect.signature(self.tools.memory_query_current).parameters
        )
        self.assertNotIn(
            "as_of", inspect.signature(self.tools.memory_compile_context).parameters
        )

    def test_denial_happens_before_database_open(self):
        self.tools.close()
        denied_path = str(Path(self._tmp.name) / "must-not-exist.db")
        denied = GovernedMCPTools(
            db_path=denied_path,
            principal=_principal(permissions=frozenset()),
        )
        try:
            result = denied.memory_ingest_events(
                request_id="r",
                user_id="u1",
                messages=[{"role": "user", "content": "secret"}],
                scope={"project": "mcp-test"},
            )
            self.assertEqual(result, {"status": "error", "error": {"code": "access_denied"}})
            self.assertFalse(Path(denied_path).exists())
        finally:
            denied.close()

    def test_forged_user_scope_and_message_authority_are_rejected(self):
        wrong_user = self.tools.memory_query_current(
            user_id="attacker",
            memory_key="profile.response_style",
            scope={"project": "mcp-test"},
        )
        wrong_scope = self.tools.memory_query_current(
            user_id="u1",
            memory_key="profile.response_style",
            scope={"project": "other"},
        )
        forged_message = self.tools.memory_ingest_events(
            request_id="r",
            user_id="u1",
            messages=[
                {
                    "role": "user",
                    "content": "secret",
                    "authority": "owner",
                }
            ],
            scope={"project": "mcp-test"},
        )
        self.assertEqual(wrong_user["error"]["code"], "access_denied")
        self.assertEqual(wrong_scope["error"]["code"], "access_denied")
        self.assertEqual(forged_message["error"]["code"], "invalid_request")

    def test_fixed_errors_never_echo_body_path_or_identifier(self):
        sentinel = "SENTINEL-BODY-PATH-ID-/private/secret.db"
        result = self.tools.memory_ingest_events(
            request_id=sentinel,
            user_id="u1",
            messages=[{"role": "user", "content": sentinel, "extra": sentinel}],
            scope={"project": "mcp-test"},
        )
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(self.db_path, rendered)


class TestGovernedFlow(MCPToolsCase):
    def test_ingest_idempotency_is_bound_to_exact_payload_session_and_scope(self):
        first = self.ingest(body="exact replay payload")
        replay = self.ingest(body="exact replay payload")
        changed_body = self.ingest(body="different payload")
        changed_session = self.tools.memory_ingest_events(
            request_id="ingest-1",
            user_id="u1",
            session_id="different-session",
            messages=[{"role": "user", "content": "exact replay payload"}],
            scope={"project": "mcp-test"},
        )
        open_scope_principal = TrustedPrincipal(
            principal_id="scope-replay-check",
            authority="owner",
            allowed_users=frozenset({"u1"}),
            scopes={},
            permissions=frozenset(
                {PERMISSION_WRITE, PERMISSION_EXTRACT, PERMISSION_READ}
            ),
        )
        other_scope = GovernedMCPTools(
            db_path=self.db_path,
            principal=open_scope_principal,
        )
        try:
            changed_scope = other_scope.memory_ingest_events(
                request_id="ingest-1",
                user_id="u1",
                session_id="session-1",
                messages=[{"role": "user", "content": "exact replay payload"}],
                scope={"project": "different-project"},
            )
        finally:
            other_scope.close()

        self.assertEqual(first["status"], "success")
        self.assertFalse(first["idempotent"])
        self.assertEqual(replay["status"], "success")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["raw_event_ids"], first["raw_event_ids"])
        for rejected in (changed_body, changed_session, changed_scope):
            self.assertEqual(
                rejected,
                {"status": "error", "error": {"code": "invalid_request"}},
            )
        with sqlite3.connect(self.db_path) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_extraction_cannot_rebind_raw_event_to_another_scope(self):
        broad = TrustedPrincipal(
            principal_id="scope-isolation-check",
            authority="owner",
            allowed_users=frozenset({"u1"}),
            scopes={},
            permissions=frozenset(
                {PERMISSION_WRITE, PERMISSION_EXTRACT, PERMISSION_READ}
            ),
        )
        scoped_tools = GovernedMCPTools(db_path=self.db_path, principal=broad)
        try:
            ingested = scoped_tools.memory_ingest_events(
                request_id="beta-source",
                user_id="u1",
                messages=[{"role": "user", "content": _directive("beta secret")}],
                scope={"project": "beta"},
            )
            denied = scoped_tools.memory_extract_candidates(
                user_id="u1",
                raw_event_ids=ingested["raw_event_ids"],
                idempotency_key="alpha-rebind",
                scope={"project": "alpha"},
            )
        finally:
            scoped_tools.close()
        self.assertEqual(
            denied,
            {"status": "error", "error": {"code": "invalid_request"}},
        )
        with sqlite3.connect(self.db_path) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM extraction_receipts").fetchone()[0], 0
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM proposal_origins").fetchone()[0], 0
            )

    def test_ingest_extract_candidate_stays_unknown_with_owner_gate(self):
        ingested = self.ingest()
        self.assertEqual(ingested["status"], "success")
        self.assertEqual(ingested["event_count"], 1)
        self.assertNotIn("private-sentinel-content", json.dumps(ingested))

        extracted = self.tools.memory_extract_candidates(
            user_id="u1",
            raw_event_ids=ingested["raw_event_ids"],
            idempotency_key="extract-1",
            scope={"project": "mcp-test"},
        )
        self.assertEqual(extracted["status"], "success")
        self.assertEqual(extracted["proposal_count"], 1)
        self.assertEqual(len(extracted["record_ids"]), 1)
        self.assertNotIn("private-sentinel-content", json.dumps(extracted))

        current = self.tools.memory_query_current(
            user_id="u1",
            memory_key="profile.response_style",
            scope={"project": "mcp-test"},
        )
        context = self.tools.memory_compile_context(
            user_id="u1",
            memory_key="profile.response_style",
            scope={"project": "mcp-test"},
            max_chars=4096,
        )
        for result in (current, context):
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["abstain"])
            self.assertTrue(result["owner_gate_required"])
            self.assertEqual(result["items"], [])
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("private-sentinel-content", rendered)
            self.assertNotIn("state_events", rendered)
            self.assertNotIn("withheld_record_ids", rendered)

    def test_confirmed_current_uses_context_pack_static_allowlist(self):
        ingested = self.ingest(body=_directive("confirmed public memory"))
        extracted = self.tools.memory_extract_candidates(
            user_id="u1",
            raw_event_ids=ingested["raw_event_ids"],
            idempotency_key="extract-confirm",
            scope={"project": "mcp-test"},
        )
        with FlowGridMemory(db_path=self.db_path) as memory:
            memory.transition_memory(
                user_id="u1",
                record_id=extracted["record_ids"][0],
                target_status="confirmed",
                actor="owner-test",
                actor_authority="owner",
                reason="owner confirmed direct evidence",
            )
        result = self.tools.memory_query_current(
            user_id="u1",
            memory_key="profile.response_style",
            scope={"project": "mcp-test"},
            max_chars=4096,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["items"][0]["content"], "confirmed public memory")
        rendered = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            '"state_reason"',
            '"created_by"',
            '"state_events"',
            '"raw_events"',
            '"source_event_ids"',
            '"actor"',
            '"path"',
        ):
            self.assertNotIn(forbidden, rendered)

    def test_ordinary_text_is_zero_proposal_and_callable_is_unavailable(self):
        ingested = self.ingest(body="ordinary natural language")
        result = self.tools.memory_extract_candidates(
            user_id="u1",
            raw_event_ids=ingested["raw_event_ids"],
            idempotency_key="extract-zero",
            scope={"project": "mcp-test"},
        )
        self.assertEqual(result["proposal_count"], 0)
        self.assertEqual(result["record_ids"], [])

    def test_character_budget_supported_and_token_budget_refused(self):
        chars = self.tools.memory_compile_context(
            user_id="u1",
            memory_key="missing",
            scope={"project": "mcp-test"},
            max_chars=4096,
        )
        tokens = self.tools.memory_compile_context(
            user_id="u1",
            memory_key="missing",
            scope={"project": "mcp-test"},
            max_chars=4096,
            max_tokens=100,
        )
        self.assertEqual(chars["budget"]["max_chars"], 4096)
        self.assertEqual(
            tokens,
            {"status": "error", "error": {"code": "token_budget_unsupported"}},
        )

    def test_close_is_idempotent_and_calls_fail_closed(self):
        self.tools.close()
        self.tools.close()
        result = self.tools.memory_query_current(
            user_id="u1",
            memory_key="anything",
            scope={"project": "mcp-test"},
        )
        self.assertEqual(result, {"status": "error", "error": {"code": "tool_closed"}})

    def test_concurrent_same_request_and_extraction_are_idempotent(self):
        # Two separately constructed adapter cores model two local MCP client
        # sessions sharing one explicit SQLite database.
        second = GovernedMCPTools(db_path=self.db_path, principal=_principal())
        body = _directive("concurrent-private-content")

        def ingest(tools):
            return tools.memory_ingest_events(
                request_id="concurrent-request",
                user_id="u1",
                messages=[{"role": "user", "content": body}],
                scope={"project": "mcp-test"},
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ingested = list(pool.map(ingest, (self.tools, second)))
            self.assertTrue(all(result["status"] == "success" for result in ingested))
            self.assertEqual(ingested[0]["raw_event_ids"], ingested[1]["raw_event_ids"])
            self.assertEqual(sorted(result["idempotent"] for result in ingested), [False, True])

            def extract(tools):
                return tools.memory_extract_candidates(
                    user_id="u1",
                    raw_event_ids=ingested[0]["raw_event_ids"],
                    idempotency_key="concurrent-extraction",
                    scope={"project": "mcp-test"},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                extracted = list(pool.map(extract, (self.tools, second)))
            self.assertTrue(all(result["status"] == "success" for result in extracted))
            self.assertEqual(extracted[0]["record_ids"], extracted[1]["record_ids"])
            self.assertEqual(
                sorted(result["idempotent"] for result in extracted), [False, True]
            )
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
