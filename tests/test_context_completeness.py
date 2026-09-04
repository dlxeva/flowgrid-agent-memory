"""Resolver-limit completeness across core, REST, and MCP surfaces."""
from __future__ import annotations

import unittest

from aml_retriever.facade import FlowGridMemory
from tests.test_context_compiler import ContextCase
from tests.test_mcp_tools import MCPToolsCase
from tests.test_rest_v1 import RestHttpCase


class TestCoreCompleteness(ContextCase):
    def _seed(self, count: int, *, size: int = 1) -> None:
        for index in range(count):
            content = chr(65 + index) * size
            self.confirm(
                self.propose(content, key=f"completeness.key.{index}")
            )

    def test_current_and_audit_report_resolver_truncation(self):
        self._seed(3)

        current = self.service.search_governed(user_id="u1", max_records=2)
        self.assertEqual(current.matched_count, 3)
        self.assertEqual(current.returned_count, 2)
        self.assertTrue(current.truncated)
        self.assertEqual(
            current.to_dict()["completeness"],
            {"matched_count": 3, "returned_count": 2, "truncated": True},
        )

        audit = self.service.search_governed(
            user_id="u1", mode="audit", max_records=2
        )
        self.assertEqual(audit.matched_count, 3)
        self.assertEqual(audit.returned_count, 2)
        self.assertTrue(audit.truncated)

    def test_context_pack_reports_resolver_and_budget_omissions(self):
        self._seed(3, size=1_000)
        generous = self.service.compile_context(
            user_id="u1",
            access_context=self.access(),
            max_records=2,
            max_chars=100_000,
        )
        self.assertEqual(generous.status, "ready")
        self.assertEqual(len(generous.items), 2)
        self.assertEqual(
            generous.completeness,
            {
                "complete": False,
                "matched_count": 3,
                "returned_count": 2,
                "reason": "resolver_limit",
            },
        )
        self.assertEqual(
            generous.omitted["by_reason"]["resolver_limit"], 1
        )

        single = self.service.compile_context(
            user_id="u1",
            memory_key="completeness.key.0",
            access_context=self.access(),
            max_records=2,
            max_chars=100_000,
        )
        limited = self.service.compile_context(
            user_id="u1",
            access_context=self.access(),
            max_records=2,
            max_chars=single.budget["used_chars"] + 500,
        )
        self.assertEqual(limited.status, "ready")
        self.assertEqual(len(limited.items), 1)
        self.assertEqual(
            limited.completeness,
            {
                "complete": False,
                "matched_count": 3,
                "returned_count": 1,
                "reason": "resolver_limit_and_budget",
            },
        )
        self.assertEqual(limited.omitted["by_reason"]["resolver_limit"], 1)
        self.assertEqual(limited.omitted["by_reason"]["budget"], 1)


class TestRestCompleteness(unittest.TestCase):
    # Reuse only the transport fixture helpers. Subclassing RestHttpCase would
    # rerun its complete test suite in this module.
    setUp = RestHttpCase.setUp
    tearDown = RestHttpCase.tearDown
    request = RestHttpCase.request
    cycle = RestHttpCase.cycle

    def test_query_and_context_surface_resolver_truncation(self):
        for index in range(3):
            self.cycle(
                suffix=f"completeness-{index}",
                content=f"value-{index}",
                memory_key=f"completeness.key.{index}",
            )

        request = {
            "user_id": "u1",
            "scope": {"project": "alpha"},
            "max_records": 2,
        }
        status, _headers, current = self.request(
            "POST", "/v1/memories/query", request
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            current["result"]["completeness"],
            {
                "complete": False,
                "matched_count": 3,
                "returned_count": 2,
                "reason": "resolver_limit",
            },
        )
        self.assertEqual(
            current["result"]["omitted"]["by_reason"]["resolver_limit"], 1
        )

        status, _headers, compiled = self.request(
            "POST",
            "/v1/context/compile",
            {**request, "max_chars": 100_000},
        )
        self.assertEqual(status, 200)
        self.assertTrue(compiled["injectable"])
        self.assertEqual(
            compiled["context"]["completeness"],
            current["result"]["completeness"],
        )


class TestMCPCompleteness(unittest.TestCase):
    # Reuse the fixture lifecycle without inheriting unrelated MCP test cases.
    setUp = MCPToolsCase.setUp
    tearDown = MCPToolsCase.tearDown

    def _seed(self) -> None:
        with FlowGridMemory(db_path=self.db_path) as memory:
            for index in range(3):
                receipt = memory.ingest_raw_events(
                    request_id=f"completeness-{index}",
                    user_id="u1",
                    session_id=f"session-{index}",
                    messages=[
                        {"role": "user", "content": f"value-{index}"}
                    ],
                    trusted_scope={"project": "mcp-test"},
                )
                record = memory.propose_memory(
                    user_id="u1",
                    memory_key=f"completeness.key.{index}",
                    content=f"value-{index}",
                    source_event_ids=receipt.raw_event_ids,
                    status="candidate",
                    authority="user",
                    created_by="test-seed",
                    scope={"project": "mcp-test"},
                )
                memory.transition_memory(
                    user_id="u1",
                    record_id=record.id,
                    target_status="confirmed",
                    actor="owner-test",
                    actor_authority="owner",
                    reason="test confirmation",
                )

    def test_query_and_compile_surface_resolver_truncation(self):
        self._seed()
        kwargs = {
            "user_id": "u1",
            "scope": {"project": "mcp-test"},
            "max_records": 2,
            "max_chars": 100_000,
        }
        current = self.tools.memory_query_current(**kwargs)
        compiled = self.tools.memory_compile_context(**kwargs)
        for result in (current, compiled):
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                result["completeness"],
                {
                    "complete": False,
                    "matched_count": 3,
                    "returned_count": 2,
                    "reason": "resolver_limit",
                },
            )
            self.assertEqual(
                result["omitted"]["by_reason"]["resolver_limit"], 1
            )


if __name__ == "__main__":
    unittest.main()
