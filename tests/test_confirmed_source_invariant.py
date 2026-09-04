"""Regression tests for confirmed-memory provenance invariants."""
import unittest

from aml_retriever.governance import GovernanceError
from tests.test_governance import GovernanceCase


class TestConfirmedSourceInvariant(GovernanceCase):
    def test_unsourced_unknown_cannot_be_promoted_to_confirmed(self):
        record = self.service.propose_memory(
            user_id="u1",
            memory_key="profile.city",
            content="模型猜测可能在杭州",
            source_event_ids=[],
            status="unknown",
            authority="agent",
            created_by="agent",
            state_reason="insufficient evidence",
        )

        with self.assertRaisesRegex(
            GovernanceError,
            "confirmed memory requires source evidence",
        ):
            self.confirm(record)

        audit = self.service.search_governed(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
        )
        self.assertEqual(audit.records[0].status, "unknown")
        self.assertFalse(
            any(
                event.record_id == record.id and event.to_status == "confirmed"
                for event in audit.state_events
            )
        )

    def test_confirmation_revalidates_source_presence(self):
        event = self.add_event("来源随后失效")
        record = self.propose(event, "待确认", key="source.presence")
        self.service.db._write(
            lambda con: con.execute(
                "DELETE FROM raw_events WHERE id=?",
                (event.id,),
            )
        )

        with self.assertRaisesRegex(
            GovernanceError,
            "confirmed memory source evidence is missing",
        ):
            self.confirm(record)

        audit = self.service.search_governed(
            user_id="u1",
            memory_key="source.presence",
            mode="audit",
        )
        self.assertEqual(audit.records[0].status, "candidate")
        self.assertFalse(
            any(
                state_event.record_id == record.id
                and state_event.to_status == "confirmed"
                for state_event in audit.state_events
            )
        )

    def test_sourced_unknown_preserves_authorized_confirmation_path(self):
        event = self.add_event("证据存在，但当前结论暂时未知")
        record = self.service.propose_memory(
            user_id="u1",
            memory_key="project.status",
            content="等待负责人确认",
            source_event_ids=[event.id],
            status="unknown",
            authority="user",
            created_by="agent",
            state_reason="evidence recorded before review",
        )

        confirmed = self.confirm(record)
        self.assertEqual(confirmed.status, "confirmed")
        current = self.service.search_governed(
            user_id="u1",
            memory_key="project.status",
        )
        self.assertEqual([item.id for item in current.records], [record.id])
        self.assertFalse(current.abstain)


if __name__ == "__main__":
    unittest.main()
