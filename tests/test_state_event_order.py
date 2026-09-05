"""State event order is deterministic when a host clock repeats a timestamp."""
from __future__ import annotations

import unittest
from unittest import mock

from aml_retriever.access import (
    AccessContext,
    DisclosurePolicy,
    PERMISSION_AUDIT,
    PERMISSION_READ,
)
from aml_retriever.facade import FlowGridMemory


class TestRepeatedClockStateOrder(unittest.TestCase):
    def test_latest_inserted_transition_is_authoritative(self):
        instant = "2026-09-06T00:00:00+00:00"
        with mock.patch("aml_retriever.governance.utc_now", return_value=instant):
            with FlowGridMemory(db_path=":memory:") as memory:
                source = memory.ingest_raw_events(
                    request_id="clock-source",
                    user_id="u1",
                    session_id="s1",
                    messages=[{"role": "user", "content": "I prefer concise replies."}],
                )
                candidate = memory.propose_memory(
                    user_id="u1",
                    memory_key="profile.reply_style",
                    memory_type="preference",
                    subject="u1",
                    content="concise replies",
                    status="candidate",
                    source_event_ids=source.raw_event_ids,
                    authority="user",
                    created_by="u1",
                )
                memory.transition_memory(
                    record_id=candidate.id,
                    target_status="confirmed",
                    actor="u1",
                    actor_authority="user",
                    reason="owner confirmed",
                    user_id="u1",
                )
                memory.transition_memory(
                    record_id=candidate.id,
                    target_status="deleted",
                    actor="u1",
                    actor_authority="user",
                    reason="owner deleted",
                    user_id="u1",
                )
                audit = memory.query_audit(
                    user_id="u1",
                    memory_key="profile.reply_style",
                    access_context=AccessContext(
                        principal_id="owner",
                        authority="owner",
                        scopes={},
                        permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
                        purpose="audit repeated clock",
                        allowed_users=frozenset({"u1"}),
                    ),
                    disclosure_policy=DisclosurePolicy(
                        allowed_audit_purposes=frozenset({"audit repeated clock"})
                    ),
                )
        self.assertTrue(audit.allowed)
        self.assertEqual(audit.state.records[0].status, "deleted")
        self.assertEqual(
            [event.to_status for event in audit.state.state_events],
            ["candidate", "confirmed", "deleted"],
        )


if __name__ == "__main__":
    unittest.main()
