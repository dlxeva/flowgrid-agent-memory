"""Stable facade and safe product CLI contract tests."""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aml_retriever import (
    PERMISSION_AUDIT,
    PERMISSION_READ,
    AccessContext,
    DisclosurePolicy,
    FlowGridMemory,
    GovernanceError,
)
from aml_retriever.product_cli import (
    EXIT_INCOMPATIBLE,
    EXIT_OPERATIONAL,
    main,
    run_governed_demo,
)


class TestStableGovernedFacade(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "memory.db")
        self.memory = FlowGridMemory(db_path=self.path)
        self.access = AccessContext(
            principal_id="owner-1",
            authority="owner",
            scopes={"project": "facade-test"},
            permissions=frozenset({PERMISSION_READ}),
            purpose="current context",
            allowed_users=frozenset({"u1"}),
        )

    def tearDown(self):
        self.memory.close()
        self.directory.cleanup()

    def _candidate(self):
        ingested = self.memory.ingest_raw_events(
            request_id="r1",
            user_id="u1",
            session_id="s1",
            messages=({"role": "user", "content": "source sentinel"},),
        )
        return self.memory.propose_memory(
            user_id="u1",
            memory_key="profile.city",
            content="candidate sentinel",
            source_event_ids=ingested.raw_event_ids,
            status="candidate",
            authority="user",
            created_by="extractor-1",
            scope={"project": "facade-test"},
        )

    def test_facade_does_not_expose_database_and_requires_explicit_path(self):
        self.assertFalse(hasattr(self.memory, "db"))
        with self.assertRaises(TypeError):
            FlowGridMemory()  # type: ignore[call-arg]

    def test_query_requires_trusted_access_context_and_candidate_is_not_current(self):
        record = self._candidate()
        with self.assertRaises(TypeError):
            self.memory.query_current(user_id="u1", memory_key=record.memory_key)
        denied = self.memory.query_current(
            user_id="u1",
            memory_key=record.memory_key,
            access_context={"principal_id": "forged"},  # type: ignore[arg-type]
        )
        self.assertFalse(denied.allowed)
        self.assertIsNone(denied.state)

        current = self.memory.query_current(
            user_id="u1", memory_key=record.memory_key, access_context=self.access
        )
        self.assertTrue(current.allowed)
        self.assertTrue(current.state.abstain)
        self.assertTrue(current.state.owner_gate_required)
        self.assertEqual(current.state.records, [])

    def test_user_scope_and_audit_purpose_fail_closed_without_body(self):
        record = self._candidate()
        wrong_user = AccessContext(
            principal_id="owner-1",
            authority="owner",
            scopes={"project": "facade-test"},
            permissions=frozenset({PERMISSION_READ}),
            purpose="current context",
            allowed_users=frozenset({"someone-else"}),
        )
        denied = self.memory.query_current(
            user_id="u1", memory_key=record.memory_key, access_context=wrong_user
        )
        self.assertFalse(denied.allowed)
        self.assertIsNone(denied.state)
        self.assertNotIn("candidate sentinel", str(denied.to_dict()))
        self.assertNotIn(record.id, str(denied.to_dict()))

        wrong_scope = AccessContext(
            principal_id="owner-1",
            authority="owner",
            scopes={"project": "different-project"},
            permissions=frozenset({PERMISSION_READ}),
            purpose="current context",
            allowed_users=frozenset({"u1"}),
        )
        denied = self.memory.query_current(
            user_id="u1",
            memory_key=record.memory_key,
            access_context=wrong_scope,
            scope={"project": "facade-test"},
        )
        self.assertFalse(denied.allowed)
        self.assertIsNone(denied.state)

        audit_access = AccessContext(
            principal_id="auditor-1",
            authority="owner",
            scopes={"project": "facade-test"},
            permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
            purpose="incident review",
            allowed_users=frozenset({"u1"}),
        )
        denied = self.memory.query_audit(
            user_id="u1", memory_key=record.memory_key, access_context=audit_access
        )
        self.assertFalse(denied.allowed)
        self.assertIsNone(denied.state)
        allowed = self.memory.query_audit(
            user_id="u1",
            memory_key=record.memory_key,
            access_context=audit_access,
            disclosure_policy=DisclosurePolicy(
                allowed_audit_purposes=frozenset({"incident review"})
            ),
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual([item.id for item in allowed.state.records], [record.id])

    def test_no_default_confirmation_and_agent_cannot_cross_owner_gate(self):
        with self.assertRaises(TypeError):
            self.memory.propose_memory(  # type: ignore[call-arg]
                user_id="u1",
                memory_key="missing.status",
                content="candidate",
                source_event_ids=(),
                authority="agent",
                created_by="agent",
            )
        record = self._candidate()
        with self.assertRaises(GovernanceError):
            self.memory.transition_memory(
                user_id="u1",
                record_id=record.id,
                target_status="confirmed",
                actor="agent-1",
                actor_authority="agent",
                reason="self promotion is forbidden",
            )
        confirmed = self.memory.transition_memory(
            user_id="u1",
            record_id=record.id,
            target_status="confirmed",
            actor="owner-1",
            actor_authority="owner",
            reason="owner reviewed source evidence",
        )
        self.assertEqual(confirmed.current_status, "confirmed")

    def test_facade_rejects_confirmed_or_unknown_proposal_start(self):
        for status in ("confirmed", "unknown", "rejected"):
            with self.assertRaises(GovernanceError):
                self.memory.propose_memory(
                    user_id="u1",
                    memory_key="bad.start",
                    content="not persisted",
                    source_event_ids=(),
                    status=status,
                    authority="agent",
                    created_by="agent",
                )

    def test_privacy_erase_has_explicit_authority_gate(self):
        self._candidate()
        with self.assertRaises(GovernanceError):
            self.memory.erase_user(
                user_id="u1",
                actor="agent-1",
                actor_authority="agent",
                reason="not authorized",
            )
        receipt = self.memory.erase_user(
            user_id="u1",
            actor="owner-1",
            actor_authority="owner",
            reason="user requested erasure",
        )
        self.assertEqual(receipt.deleted_messages, 1)
        current = self.memory.query_current(
            user_id="u1", memory_key="profile.city", access_context=self.access
        )
        self.assertTrue(current.state.abstain)

    def test_authorized_supersession_never_resurrects_old_memory(self):
        old = self._candidate()
        self.memory.transition_memory(
            user_id="u1",
            record_id=old.id,
            target_status="confirmed",
            actor="owner-1",
            actor_authority="owner",
            reason="owner confirmed old evidence",
        )
        ingested = self.memory.ingest_raw_events(
            request_id="r2",
            user_id="u1",
            session_id="s2",
            messages=({"role": "user", "content": "replacement source"},),
        )
        replacement = self.memory.propose_memory(
            user_id="u1",
            memory_key=old.memory_key,
            content="replacement current value",
            source_event_ids=ingested.raw_event_ids,
            status="candidate",
            authority="user",
            created_by="extractor-1",
            scope={"project": "facade-test"},
            supersedes_record_id=old.id,
        )
        self.memory.transition_memory(
            user_id="u1",
            record_id=replacement.id,
            target_status="confirmed",
            actor="owner-1",
            actor_authority="owner",
            reason="owner confirmed replacement evidence",
        )
        current = self.memory.query_current(
            user_id="u1", memory_key=old.memory_key, access_context=self.access
        )
        self.assertEqual([item.id for item in current.state.records], [replacement.id])
        self.assertNotIn(old.content, str(current.to_dict()))
        pack = self.memory.compile_context(
            user_id="u1", access_context=self.access, memory_key=old.memory_key
        )
        self.assertEqual(pack.status, "ready")
        self.assertEqual([item["id"] for item in pack.items], [replacement.id])
        self.assertNotIn(old.content, pack.to_json())

        self.memory.transition_memory(
            user_id="u1",
            record_id=replacement.id,
            target_status="deleted",
            actor="owner-1",
            actor_authority="owner",
            reason="owner deleted current memory",
        )
        after_delete = self.memory.query_current(
            user_id="u1", memory_key=old.memory_key, access_context=self.access
        )
        self.assertTrue(after_delete.state.abstain)
        self.assertEqual(after_delete.state.records, [])
        self.assertNotIn(old.content, str(after_delete.to_dict()))
        self.assertNotIn(replacement.content, str(after_delete.to_dict()))

    def test_close_is_idempotent_and_blocks_reuse(self):
        self.memory.close()
        self.memory.close()
        self.assertTrue(self.memory.closed)
        with self.assertRaises(RuntimeError):
            self.memory.ingest_raw_events(
                request_id="after-close",
                user_id="u1",
                session_id="s1",
                messages=({"role": "user", "content": "must not write"},),
            )


class TestProductCLI(unittest.TestCase):
    def test_all_stateful_commands_require_explicit_database_choice(self):
        for command in ("doctor", "demo"):
            with self.subTest(command=command):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main([command])
                self.assertEqual(raised.exception.code, 2)
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as both:
                        main([command, "--db", "x.db", "--ephemeral"])
                self.assertEqual(both.exception.code, 2)

    def test_cli_memory_database_spelling_must_use_ephemeral_flag(self):
        for command in ("doctor", "demo"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main([command, "--db", ":memory:"])
            self.assertEqual(code, EXIT_OPERATIONAL)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(":memory:", stderr.getvalue())

    def test_doctor_missing_path_is_read_only_and_does_not_disclose_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-secret-name.db"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["doctor", "--db", str(path)])
            self.assertEqual(code, EXIT_INCOMPATIBLE)
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), output.getvalue())
            body = json.loads(output.getvalue())
            self.assertEqual(body["schema"]["status"], "missing")
            self.assertFalse(body["database_opened_writable"])

    def test_demo_uses_facade_and_real_database_state_chain(self):
        source = inspect.getsource(run_governed_demo)
        self.assertNotIn("MemoryService", source)
        self.assertNotIn("RetrieverDB", source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.db"
            result = run_governed_demo(str(path))
            self.assertTrue(all(result["checks"].values()))
            self.assertFalse(result["memory_content_emitted"])
            self.assertNotIn("concise and evidence-first", str(result))
            self.assertNotIn(str(path), str(result))

            con = sqlite3.connect(str(path))
            try:
                records = con.execute(
                    "SELECT id,status FROM memory_records ORDER BY created_at,id"
                ).fetchall()
                chains = {
                    record_id: [
                        row[0]
                        for row in con.execute(
                            "SELECT to_status FROM memory_state_events "
                            "WHERE record_id=? ORDER BY transitioned_at,id",
                            (record_id,),
                        ).fetchall()
                    ]
                    for record_id, _status in records
                }
                raw_count = con.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            finally:
                con.close()
            self.assertEqual({status for _record_id, status in records}, {"superseded", "confirmed"})
            self.assertEqual(
                sorted(chains.values()),
                sorted([["candidate", "confirmed", "superseded"], ["candidate", "confirmed"]]),
            )
            self.assertEqual(raw_count, 2)

    def test_ephemeral_failure_cleans_main_wal_and_shm_and_hides_error(self):
        real_temporary_directory = tempfile.TemporaryDirectory
        created: list[Path] = []

        class RecordingTemporaryDirectory:
            def __init__(self, *args, **kwargs):
                self._inner = real_temporary_directory(*args, **kwargs)

            def __enter__(self):
                value = self._inner.__enter__()
                created.append(Path(value))
                return value

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        secret = "raw-memory-secret-that-must-not-leak"

        def failing_demo(path: str):
            base = Path(path)
            for suffix in ("", "-wal", "-shm"):
                Path(str(base) + suffix).write_text(secret, encoding="utf-8")
            raise RuntimeError(secret)

        stderr = io.StringIO()
        with mock.patch(
            "aml_retriever.product_cli.tempfile.TemporaryDirectory",
            RecordingTemporaryDirectory,
        ), mock.patch(
            "aml_retriever.product_cli.run_governed_demo", side_effect=failing_demo
        ), contextlib.redirect_stderr(stderr):
            code = main(["demo", "--ephemeral"])
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertTrue(created)
        self.assertFalse(created[0].exists())
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
