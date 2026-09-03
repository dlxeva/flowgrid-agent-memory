from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aml_retriever.access import PERMISSION_READ, AccessContext
from aml_retriever.evaluation.host_validation import (
    OUTPUT_FIELDS,
    advance_host_trial,
    assess_host_answer,
    create_host_trial,
)
from aml_retriever.facade import FlowGridMemory


class HostValidationFixtureCase(unittest.TestCase):
    def test_two_session_fixture_hides_invalidated_state(self):
        with tempfile.TemporaryDirectory(prefix="host-validation-") as directory:
            db_path = str(Path(directory) / "memory.db")
            state = create_host_trial(db_path=db_path, nonce="fixed-nonce")
            access = AccessContext(
                principal_id="fixture-reader",
                authority="owner",
                scopes=state.scope,
                permissions=frozenset({PERMISSION_READ}),
                purpose="synthetic host validation",
                allowed_users=frozenset({state.user_id}),
            )
            with FlowGridMemory(db_path=db_path) as memory:
                first = memory.query_current(
                    user_id=state.user_id,
                    access_context=access,
                    memory_key="project.release_channel",
                    scope=state.scope,
                )
                self.assertTrue(first.allowed)
                self.assertEqual(
                    first.state.records[0].content,
                    state.expected_session_one["release_channel"],
                )

            expected_two, forbidden_two = advance_host_trial(state)
            with FlowGridMemory(db_path=db_path) as memory:
                second = memory.query_current(
                    user_id=state.user_id,
                    access_context=access,
                    memory_key="project.release_channel",
                    scope=state.scope,
                )
                retired = memory.query_current(
                    user_id=state.user_id,
                    access_context=access,
                    memory_key="project.retired_token",
                    scope=state.scope,
                )
                self.assertEqual(second.state.records[0].content, expected_two["release_channel"])
                self.assertTrue(retired.state.abstain)
                rendered = json.dumps(
                    {
                        "second": [record.content for record in second.state.records],
                        "retired": [record.content for record in retired.state.records],
                    }
                )
                for forbidden in forbidden_two:
                    self.assertNotIn(forbidden, rendered)


class HostAnswerAssessmentCase(unittest.TestCase):
    def setUp(self):
        self.expected = {field: "unknown" for field in OUTPUT_FIELDS}
        self.expected["release_channel"] = "current-value"
        self.expected["stable_owner"] = "stable-value"

    def test_exact_answer_with_tool_call_passes(self):
        result = assess_host_answer(
            dict(self.expected),
            expected=self.expected,
            forbidden_values=("old-value",),
            raw_output=json.dumps(self.expected),
            memory_tool_calls=1,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["forbidden_leak_count"], 0)

    def test_leak_or_missing_tool_is_non_compensating_failure(self):
        result = assess_host_answer(
            dict(self.expected),
            expected=self.expected,
            forbidden_values=("old-value",),
            raw_output=json.dumps(self.expected) + " old-value",
            memory_tool_calls=0,
        )
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["used_memory_tool"])
        self.assertFalse(result["checks"]["forbidden_values_absent"])


if __name__ == "__main__":
    unittest.main()
