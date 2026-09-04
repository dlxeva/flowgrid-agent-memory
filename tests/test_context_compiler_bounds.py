"""Complexity and hard resource ceilings for ContextCompiler."""
from __future__ import annotations

import math
import unittest
from unittest import mock

import aml_retriever.context as context_module
from aml_retriever.access import AccessContext, PERMISSION_READ
from aml_retriever.api import MemoryService
from aml_retriever.auth import MEMORY_PERMISSIONS, TrustedPrincipal
from aml_retriever.context import (
    MAX_CONTEXT_RECORDS,
    MAX_CONTEXT_REQUEST_CHARS,
    ContextCompiler,
    ContextPack,
)
from aml_retriever.governance import CurrentStateResult, MemoryRecord
from aml_retriever.rest_v1 import (
    GovernedRestAdapter,
    GovernedRestConfig,
    RequestValidationError,
)


_TIMESTAMP = "2026-09-04T00:00:00+00:00"


def record(index: int, *, content: str = "x") -> MemoryRecord:
    raw_id = f"raw_{index:024x}"
    return MemoryRecord(
        id=f"mem_{index:08d}",
        user_id="u1",
        memory_key=f"key.{index:08d}",
        memory_type="fact",
        subject="u1",
        content=content,
        status="confirmed",
        source_event_ids=[raw_id],
        source_locator=[f"raw_events:{raw_id}"],
        observed_at=_TIMESTAMP,
        valid_from=None,
        valid_until=None,
        authority="user",
        scope={"user": "u1"},
        confidence=1.0,
        created_by="test",
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        confirmed_by="owner",
        confirmed_at=_TIMESTAMP,
        supersedes_record_id=None,
        state_reason="confirmed for test",
    )


def current_result(count: int, *, content: str = "x") -> CurrentStateResult:
    return CurrentStateResult(
        mode="current",
        current_status="confirmed",
        abstain=False,
        reason="confirmed_current_memory",
        records=[record(index, content=content) for index in range(count)],
    )


def access() -> AccessContext:
    return AccessContext(
        principal_id="trusted-test",
        authority="owner",
        scopes={},
        permissions=frozenset({PERMISSION_READ}),
        purpose="context test",
        allowed_users=frozenset({"u1"}),
    )


class TestCompilerComplexity(unittest.TestCase):
    def test_character_budget_uses_logarithmic_prefix_probes(self):
        result = current_result(MAX_CONTEXT_RECORDS)
        compiler = ContextCompiler()
        full = compiler.compile(result)
        self.assertEqual(full.status, "ready")
        budget = full.budget["used_chars"] // 2

        with mock.patch.object(
            compiler,
            "_finalize",
            wraps=compiler._finalize,
        ) as finalized:
            limited = compiler.compile(result, max_chars=budget)

        self.assertEqual(limited.status, "ready")
        self.assertGreater(len(limited.items), 0)
        self.assertLess(len(limited.items), MAX_CONTEXT_RECORDS)
        self.assertLessEqual(limited.budget["used_chars"], budget)
        self.assertEqual(
            limited.omitted["by_reason"]["budget"],
            MAX_CONTEXT_RECORDS - len(limited.items),
        )
        maximum_probes = 2 + math.ceil(math.log2(MAX_CONTEXT_RECORDS + 1))
        self.assertLessEqual(finalized.call_count, maximum_probes)

    def test_no_budget_full_fit_uses_one_finalize_probe(self):
        compiler = ContextCompiler()
        with mock.patch.object(
            compiler,
            "_finalize",
            wraps=compiler._finalize,
        ) as finalized:
            pack = compiler.compile(current_result(100))
        self.assertEqual(pack.status, "ready")
        self.assertEqual(len(pack.items), 100)
        self.assertEqual(finalized.call_count, 1)

    def test_context_pack_caches_canonical_json(self):
        pack = ContextPack(
            status="unknown",
            abstain=True,
            reason="no_confirmed_memory",
        )
        with mock.patch.object(
            context_module,
            "canonical_json",
            wraps=context_module.canonical_json,
        ) as rendered:
            first = pack.to_json()
            second = pack.to_json()
        self.assertEqual(first, second)
        self.assertEqual(rendered.call_count, 1)


class TestCompilerHardLimits(unittest.TestCase):
    def test_direct_result_record_count_is_bounded_before_item_rendering(self):
        result = current_result(MAX_CONTEXT_RECORDS + 1)
        with mock.patch.object(
            context_module,
            "_record_item",
            side_effect=AssertionError("oversized result reached item rendering"),
        ):
            pack = ContextCompiler().compile(result)
        self.assertEqual(pack.status, "budget_exceeded")
        self.assertEqual(pack.reason, "context_input_limit_exceeded")
        self.assertEqual(pack.items, [])

    def test_single_item_and_aggregate_utf8_limits_fail_closed(self):
        secret = "private-oversized-memory"
        oversized = current_result(1, content=secret * 20)
        with mock.patch.object(context_module, "MAX_CONTEXT_ITEM_BYTES", 64):
            item_failure = ContextCompiler().compile(oversized)
        self.assertEqual(item_failure.reason, "context_input_limit_exceeded")
        self.assertNotIn(secret, item_failure.to_json())

        with mock.patch.object(context_module, "MAX_CONTEXT_ITEM_BYTES", 100_000), mock.patch.object(
            context_module,
            "MAX_CONTEXT_TOTAL_ITEM_BYTES",
            64,
        ):
            aggregate_failure = ContextCompiler().compile(current_result(2))
        self.assertEqual(aggregate_failure.reason, "context_input_limit_exceeded")
        self.assertEqual(aggregate_failure.items, [])

    def test_unexpected_final_pack_byte_overflow_fails_closed(self):
        secret = "output-limit-secret"
        with mock.patch.object(context_module, "MAX_CONTEXT_PACK_BYTES", 32):
            pack = ContextCompiler().compile(current_result(1, content=secret))
        self.assertEqual(pack.status, "budget_exceeded")
        self.assertEqual(pack.reason, "context_output_limit_exceeded")
        self.assertEqual(pack.items, [])
        self.assertNotIn(secret, pack.to_json())

    def test_service_rejects_excessive_max_records_before_retrieval(self):
        service = MemoryService(db=mock.Mock())
        with mock.patch.object(
            service,
            "search_governed",
            side_effect=AssertionError("oversized request reached retrieval"),
        ) as search:
            pack = service.compile_context(
                user_id="u1",
                access_context=access(),
                max_records=MAX_CONTEXT_RECORDS + 1,
            )
        self.assertEqual(pack.status, "budget_exceeded")
        self.assertEqual(pack.reason, "invalid_max_records")
        search.assert_not_called()

    def test_rest_rejects_limits_above_product_ceilings(self):
        principal = TrustedPrincipal(
            principal_id="owner-test",
            authority="owner",
            allowed_users=frozenset({"u1"}),
            scopes={"project": "alpha"},
            permissions=frozenset(MEMORY_PERMISSIONS),
            purpose="context limit test",
            allowed_audit_purposes=frozenset({"context limit test"}),
        )
        adapter = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=":memory:",
                principal=principal,
                auth_mode="local",
                port=0,
            )
        )
        try:
            with self.assertRaises(RequestValidationError):
                adapter._query(
                    principal,
                    {
                        "user_id": "u1",
                        "scope": {"project": "alpha"},
                        "max_records": MAX_CONTEXT_RECORDS + 1,
                    },
                )
            with self.assertRaises(RequestValidationError):
                adapter._compile_context(
                    principal,
                    {
                        "user_id": "u1",
                        "scope": {"project": "alpha"},
                        "max_chars": MAX_CONTEXT_REQUEST_CHARS + 1,
                    },
                )
        finally:
            adapter.close()


if __name__ == "__main__":
    unittest.main()
