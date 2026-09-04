"""Local owner-review service and product CLI invariants."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aml_retriever.access import PERMISSION_READ, AccessContext
from aml_retriever.facade import FlowGridMemory
from aml_retriever.owner_review import OwnerReviewError, OwnerReviewSession
from aml_retriever.product_cli import EXIT_OPERATIONAL, main


class OwnerReviewCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="flowgrid-owner-review-")
        self.path = str(Path(self.directory.name) / "memory.db")
        self.memory = FlowGridMemory(db_path=self.path)
        self.serial = 0

    def tearDown(self) -> None:
        self.memory.close()
        self.directory.cleanup()

    def candidate(
        self,
        content: str,
        *,
        key: str,
        scope: dict[str, str] | None = None,
        status: str = "candidate",
    ):
        self.serial += 1
        trusted_scope = dict(scope or {})
        ingested = self.memory.ingest_raw_events(
            request_id=f"review-ingest-{self.serial}",
            user_id="u1",
            session_id=f"review-session-{self.serial}",
            messages=({"role": "user", "content": f"source: {content}"},),
            trusted_scope=trusted_scope,
        )
        return self.memory.propose_memory(
            user_id="u1",
            memory_key=key,
            content=content,
            source_event_ids=ingested.raw_event_ids,
            status=status,
            authority="user",
            created_by="review-test-extractor",
            scope=trusted_scope,
            state_reason="candidate awaiting local owner review",
        )

    def confirm(self, record) -> None:
        self.memory.transition_memory(
            user_id="u1",
            record_id=record.id,
            target_status="confirmed",
            actor="setup-owner",
            actor_authority="owner",
            reason="setup confirmation",
        )

    def session(
        self,
        *,
        scope: dict[str, str] | None = None,
        actor: str = "owner-1",
    ) -> OwnerReviewSession:
        return OwnerReviewSession(
            memory=self.memory,
            user_id="u1",
            actor=actor,
            scope=scope,
        )


class TestOwnerReviewSession(OwnerReviewCase):
    def test_queue_contains_only_exact_scope_pending_records_and_evidence(self):
        alpha = self.candidate(
            "Alpha candidate",
            key="project.alpha.decision",
            scope={"project": "alpha"},
        )
        self.candidate(
            "Beta candidate",
            key="project.beta.decision",
            scope={"project": "beta"},
        )
        self.candidate("Global candidate", key="profile.global")
        already_confirmed = self.candidate(
            "Confirmed value",
            key="project.alpha.confirmed",
            scope={"project": "alpha"},
        )
        self.confirm(already_confirmed)

        queue = self.session(scope={"project": "alpha"}).list_pending()
        self.assertEqual(queue.total_pending, 1)
        self.assertEqual(queue.returned_count, 1)
        self.assertFalse(queue.has_more)
        item = queue.items[0]
        self.assertEqual(item.record_id, alpha.id)
        self.assertEqual(item.current_status, "candidate")
        self.assertEqual(item.content, "Alpha candidate")
        self.assertTrue(item.evidence_complete)
        self.assertEqual(len(item.evidence), 1)
        self.assertEqual(item.evidence[0].content, "source: Alpha candidate")
        self.assertTrue(item.evidence[0].compatible)
        self.assertEqual(
            item.scope,
            {"project": "alpha", "user": "u1"},
        )

    def test_confirm_and_reject_apply_explicit_owner_transitions(self):
        accepted = self.candidate(
            "Accepted value",
            key="project.accepted",
            scope={"project": "alpha"},
        )
        rejected = self.candidate(
            "Rejected value",
            key="project.rejected",
            scope={"project": "alpha"},
            status="inferred",
        )
        session = self.session(scope={"project": "alpha"}, actor="review-owner")

        confirmed_receipt = session.decide(
            record_id=accepted.id,
            decision="confirm",
            reason="source evidence verified",
        )
        rejected_receipt = session.decide(
            record_id=rejected.id,
            decision="reject",
            reason="inference is not sufficiently supported",
        )
        self.assertEqual(confirmed_receipt.previous_status, "candidate")
        self.assertEqual(confirmed_receipt.current_status, "confirmed")
        self.assertTrue(confirmed_receipt.evidence_verified)
        self.assertEqual(rejected_receipt.previous_status, "inferred")
        self.assertEqual(rejected_receipt.current_status, "rejected")
        self.assertEqual(session.list_pending().items, ())

        receipt_text = json.dumps(
            confirmed_receipt.to_dict(), ensure_ascii=False, sort_keys=True
        )
        self.assertNotIn("Accepted value", receipt_text)
        self.assertNotIn("source evidence verified", receipt_text)

        access = AccessContext(
            principal_id="trusted-agent",
            authority="service",
            scopes={"project": "alpha"},
            permissions=frozenset({PERMISSION_READ}),
            purpose="current context",
            allowed_users=frozenset({"u1"}),
        )
        current = self.memory.query_current(
            user_id="u1",
            access_context=access,
            memory_key="project.accepted",
            scope={"project": "alpha"},
        )
        self.assertEqual([item.id for item in current.state.records], [accepted.id])

    def test_missing_evidence_blocks_confirmation_but_allows_rejection(self):
        record = self.candidate(
            "Evidence will be removed",
            key="project.evidence",
            scope={"project": "alpha"},
        )
        with sqlite3.connect(self.path) as con:
            con.execute(
                "DELETE FROM raw_events WHERE id=?",
                (record.source_event_ids[0],),
            )

        session = self.session(scope={"project": "alpha"})
        queue = session.list_pending(record_id=record.id)
        self.assertEqual(queue.total_pending, 1)
        self.assertFalse(queue.items[0].evidence_complete)
        self.assertEqual(queue.items[0].evidence, ())
        with self.assertRaisesRegex(OwnerReviewError, "confirmation evidence is incomplete"):
            session.decide(
                record_id=record.id,
                decision="confirm",
                reason="must fail closed",
            )
        receipt = session.decide(
            record_id=record.id,
            decision="reject",
            reason="source evidence is no longer available",
        )
        self.assertEqual(receipt.current_status, "rejected")
        self.assertFalse(receipt.evidence_verified)

    def test_cross_scope_target_is_unavailable(self):
        beta = self.candidate(
            "Beta-only candidate",
            key="project.beta",
            scope={"project": "beta"},
        )
        alpha_session = self.session(scope={"project": "alpha"})
        with self.assertRaisesRegex(OwnerReviewError, "review target is unavailable"):
            alpha_session.decide(
                record_id=beta.id,
                decision="confirm",
                reason="attempted cross-scope decision",
            )
        beta_queue = self.session(scope={"project": "beta"}).list_pending()
        self.assertEqual([item.record_id for item in beta_queue.items], [beta.id])

    def test_limit_and_record_filter_are_deterministic(self):
        records = [
            self.candidate(
                f"Candidate {index}",
                key=f"project.queue.{index}",
                scope={"project": "alpha"},
            )
            for index in range(3)
        ]
        session = self.session(scope={"project": "alpha"})
        limited = session.list_pending(limit=2)
        self.assertEqual(limited.total_pending, 3)
        self.assertEqual(limited.returned_count, 2)
        self.assertTrue(limited.has_more)
        one = session.list_pending(limit=1, record_id=records[2].id)
        self.assertEqual(one.total_pending, 1)
        self.assertEqual([item.record_id for item in one.items], [records[2].id])
        with self.assertRaises(OwnerReviewError):
            session.list_pending(limit=101)


class TestOwnerReviewCLI(OwnerReviewCase):
    def test_cli_lists_evidence_then_emits_minimal_decision_receipt(self):
        record = self.candidate(
            "CLI candidate body",
            key="project.cli",
            scope={"project": "alpha"},
        )
        self.memory.close()

        common = [
            "review",
            "--db",
            self.path,
            "--user",
            "u1",
            "--actor",
            "cli-owner",
            "--scope",
            "project=alpha",
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(common)
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        queue = json.loads(stdout.getvalue())
        self.assertEqual(queue["mode"], "review_queue")
        self.assertEqual(queue["items"][0]["record_id"], record.id)
        self.assertEqual(queue["items"][0]["content"], "CLI candidate body")
        self.assertEqual(
            queue["items"][0]["evidence"][0]["content"],
            "source: CLI candidate body",
        )

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                common
                + [
                    "--record",
                    record.id,
                    "--decision",
                    "confirm",
                    "--reason",
                    "CLI owner verified evidence",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["mode"], "review_decision")
        self.assertEqual(receipt["current_status"], "confirmed")
        rendered = stdout.getvalue()
        self.assertNotIn("CLI candidate body", rendered)
        self.assertNotIn("CLI owner verified evidence", rendered)
        self.assertNotIn("source:", rendered)

    def test_cli_missing_database_and_invalid_decision_shape_fail_closed(self):
        missing = str(Path(self.directory.name) / "missing-private-name.db")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "review",
                    "--db",
                    missing,
                    "--user",
                    "u1",
                    "--actor",
                    "cli-owner",
                ]
            )
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertFalse(Path(missing).exists())
        self.assertNotIn(missing, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

        record = self.candidate(
            "Invalid CLI shape",
            key="project.invalid-cli",
            scope={"project": "alpha"},
        )
        self.memory.close()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "review",
                    "--db",
                    self.path,
                    "--user",
                    "u1",
                    "--actor",
                    "cli-owner",
                    "--scope",
                    "project=alpha",
                    "--record",
                    record.id,
                    "--decision",
                    "confirm",
                ]
            )
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertNotIn(record.id, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
