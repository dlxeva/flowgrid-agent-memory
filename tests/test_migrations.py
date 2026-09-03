"""Fail-closed, read-only, and real-baseline migration tests."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aml_retriever.facade import FlowGridMemory
from aml_retriever.extraction import ExtractionValidationError
from aml_retriever.governance import GovernanceError
from aml_retriever.migrations import inspect_schema
from tests.legacy_fixture import create_legacy_database


REPO = Path(__file__).resolve().parents[1]
LEGACY_COMMIT = "cdae7dbd38d73eda33793b30017559bdfb75eff5"


def _dump(path: Path) -> str:
    con = sqlite3.connect(str(path))
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


class TestReadOnlySchemaInspection(unittest.TestCase):
    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "never-created.db"
            report = inspect_schema(str(path))
            self.assertEqual(report.status, "missing")
            self.assertTrue(report.compatible)
            self.assertTrue(report.read_only)
            self.assertFalse(path.exists())

    def test_current_schema_is_ready_and_path_is_not_disclosed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with FlowGridMemory(db_path=str(path)):
                pass
            report = inspect_schema(str(path))
            self.assertEqual(report.status, "ready")
            self.assertEqual(report.current_version, report.supported_version)
            self.assertNotIn(str(path), str(report.to_dict()))

    def test_existing_empty_sqlite_file_can_be_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            sqlite3.connect(str(path)).close()
            before = inspect_schema(str(path))
            self.assertEqual(before.status, "uninitialized")
            self.assertTrue(before.compatible)
            with FlowGridMemory(db_path=str(path)):
                pass
            self.assertEqual(inspect_schema(str(path)).status, "ready")

    def test_ingest_request_receipt_binding_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with FlowGridMemory(db_path=str(path)) as memory:
                memory.ingest_raw_events(
                    request_id="immutable-request",
                    user_id="u1",
                    session_id="s1",
                    messages=[{"role": "user", "content": "immutable payload"}],
                    trusted_scope={"project": "alpha"},
                )
            con = sqlite3.connect(str(path))
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    con.execute(
                        "UPDATE requests SET message_ids='[]' "
                        "WHERE request_id='immutable-request' AND user_id='u1'"
                    )
            finally:
                con.close()
            self.assertEqual(inspect_schema(str(path)).status, "ready")

    def test_eight_processes_can_initialize_one_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "shared.db"
            start = root / "start"
            child = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from aml_retriever.facade import FlowGridMemory\n"
                "Path(sys.argv[3]).touch()\n"
                "while not Path(sys.argv[2]).exists(): time.sleep(0.005)\n"
                "try:\n"
                "    with FlowGridMemory(db_path=sys.argv[1]): pass\n"
                "except BaseException:\n"
                "    print('ERR')\n"
                "    raise SystemExit(1)\n"
                "print('OK')\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO)
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child,
                        str(path),
                        str(start),
                        str(root / f"ready-{index}"),
                    ],
                    cwd=str(REPO),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(8)
            ]
            try:
                deadline = time.monotonic() + 15
                while (
                    sum((root / f"ready-{index}").exists() for index in range(8)) < 8
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(
                    sum((root / f"ready-{index}").exists() for index in range(8)),
                    8,
                )
                start.touch()
                results = [process.communicate(timeout=30) for process in processes]
                self.assertEqual([process.returncode for process in processes], [0] * 8)
                self.assertEqual(
                    [stdout.strip() for stdout, _stderr in results], ["OK"] * 8
                )
                self.assertEqual(inspect_schema(str(path)).status, "ready")
            finally:
                start.touch(exist_ok=True)
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)

    def test_process_exit_before_extraction_rolls_back_entire_empty_bootstrap(self):
        """A creator crash cannot persist legacy/governance half-schema."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            sqlite3.connect(str(path)).close()
            before = _dump(path)
            child = (
                "import os,sys\n"
                "from unittest import mock\n"
                "from aml_retriever.facade import FlowGridMemory\n"
                "def die(_con): os._exit(91)\n"
                "with mock.patch('aml_retriever.retriever.extracted.install_schema', "
                "side_effect=die): FlowGridMemory(db_path=sys.argv[1])\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO)
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=str(REPO),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 91)
            self.assertEqual(_dump(path), before)
            report = inspect_schema(str(path))
            self.assertEqual(report.status, "uninitialized")
            self.assertTrue(report.compatible)
            with FlowGridMemory(db_path=str(path)):
                pass
            self.assertEqual(inspect_schema(str(path)).status, "ready")

    def test_process_exit_after_extraction_meta_create_rolls_back_component(self):
        """A crash inside the optional-component DDL leaves no partial trace."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governance-only.db"
            with FlowGridMemory(db_path=str(path)):
                pass
            con = sqlite3.connect(str(path))
            try:
                con.executescript(
                    """
                    DROP TRIGGER extraction_receipts_no_update;
                    DROP TRIGGER proposal_origins_no_update;
                    DROP INDEX idx_extraction_receipt_digest;
                    DROP INDEX idx_proposal_origins_receipt;
                    DROP TABLE proposal_origins;
                    DROP TABLE extraction_receipts;
                    DROP TABLE extraction_meta;
                    """
                )
                con.commit()
            finally:
                con.close()
            self.assertEqual(inspect_schema(str(path)).status, "migration_required")
            before = _dump(path)
            child = (
                "import os,sys\n"
                "from unittest import mock\n"
                "from aml_retriever.facade import FlowGridMemory\n"
                "def die(*_args,**_kwargs): os._exit(92)\n"
                "with mock.patch('aml_retriever.compiler._table_layout', "
                "side_effect=die): FlowGridMemory(db_path=sys.argv[1])\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO)
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=str(REPO),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 92)
            self.assertEqual(_dump(path), before)
            report = inspect_schema(str(path))
            self.assertEqual(report.status, "migration_required")
            self.assertEqual(report.extraction_status, "uninitialized")
            with FlowGridMemory(db_path=str(path)):
                pass
            self.assertEqual(inspect_schema(str(path)).status, "ready")


class TestFailClosedSchemaGate(unittest.TestCase):
    def _ready_db(self, directory: str) -> Path:
        path = Path(directory) / "memory.db"
        with FlowGridMemory(db_path=str(path)):
            pass
        return path

    def _replace_version(self, path: Path, value: str) -> None:
        con = sqlite3.connect(str(path))
        try:
            con.execute(
                "UPDATE governance_meta SET value=? WHERE key='schema_version'",
                (value,),
            )
            con.commit()
        finally:
            con.close()

    def _assert_refused_without_mutation(
        self,
        path: Path,
        expected_status: str,
        error_type=GovernanceError,
    ) -> None:
        before = _dump(path)
        report = inspect_schema(str(path))
        self.assertEqual(report.status, expected_status)
        self.assertFalse(report.compatible)
        with self.assertRaises(error_type):
            FlowGridMemory(db_path=str(path))
        self.assertEqual(_dump(path), before, "refusal must happen before any DDL or data write")

    def test_future_schema_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            self._replace_version(path, "2")
            self._assert_refused_without_mutation(path, "future")

    def test_future_extraction_schema_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute(
                "UPDATE extraction_meta SET value='2' WHERE key='schema_version'"
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "future", ExtractionValidationError
            )

    def test_malformed_extraction_schema_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute(
                "UPDATE extraction_meta SET value='bogus' WHERE key='schema_version'"
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_non_integer_schema_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            self._replace_version(path, "one")
            self._assert_refused_without_mutation(path, "corrupt")

    def test_noncanonical_integer_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            self._replace_version(path, "01")
            self._assert_refused_without_mutation(path, "corrupt")

    def test_malformed_meta_layout_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE governance_meta(key TEXT,value TEXT)")
            con.execute("INSERT INTO governance_meta VALUES('schema_version','1')")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_same_name_noop_immutability_trigger_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.executescript(
                """
                DROP TRIGGER raw_events_no_update;
                CREATE TRIGGER raw_events_no_update
                BEFORE UPDATE ON raw_events BEGIN SELECT 1; END;
                """
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_same_name_noop_extraction_trigger_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.executescript(
                """
                DROP TRIGGER extraction_receipts_no_update;
                CREATE TRIGGER extraction_receipts_no_update
                BEFORE UPDATE ON extraction_receipts BEGIN SELECT 1; END;
                """
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_ready_schema_missing_legacy_requests_table_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE requests")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_ready_schema_rejects_extra_trigger_and_view(self):
        for kind in ("trigger", "view"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = self._ready_db(directory)
                con = sqlite3.connect(str(path))
                if kind == "trigger":
                    con.execute(
                        "CREATE TRIGGER hostile_copy AFTER INSERT ON messages "
                        "BEGIN SELECT 1; END"
                    )
                else:
                    con.execute("CREATE VIEW hostile_view AS SELECT content FROM messages")
                con.commit()
                con.close()
                self._assert_refused_without_mutation(path, "corrupt")

    def test_governance_v1_missing_key_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("ALTER TABLE raw_events DROP COLUMN authority")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_missing_governance_version_row_is_corrupt_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute(
                "DELETE FROM governance_meta WHERE key='schema_version'"
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_missing_governance_meta_table_is_corrupt_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE governance_meta")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_missing_extraction_version_row_is_corrupt_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute(
                "DELETE FROM extraction_meta WHERE key='schema_version'"
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_missing_extraction_meta_table_is_partial_and_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE extraction_meta")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_malformed_raw_events_with_missing_meta_fails_before_writable_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE governance_meta")
            con.execute("ALTER TABLE raw_events DROP COLUMN authority")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_standalone_governance_v1_trace_is_not_treated_as_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial-governance.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE raw_events(id TEXT PRIMARY KEY)")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_messages_only_lookalike_is_not_exact_aml_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages-only.db"
            con = sqlite3.connect(str(path))
            con.execute(
                """CREATE TABLE messages(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT,
                seq INTEGER NOT NULL,
                role TEXT,
                content TEXT NOT NULL,
                ts_ms INTEGER,
                created_at TEXT NOT NULL,
                request_id TEXT,
                added_at TEXT NOT NULL
            )"""
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_standalone_extraction_subset_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial-extraction.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE extraction_receipts(user_id TEXT)")
            con.execute(
                "CREATE INDEX idx_extraction_receipt_digest "
                "ON extraction_receipts(user_id)"
            )
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_missing_governance_index_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP INDEX idx_memory_current")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt")

    def test_missing_extraction_index_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.execute("DROP INDEX idx_extraction_receipt_digest")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )

    def test_complete_governance_only_database_can_add_extraction_component(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._ready_db(directory)
            con = sqlite3.connect(str(path))
            con.executescript(
                """
                DROP TRIGGER extraction_receipts_no_update;
                DROP TRIGGER proposal_origins_no_update;
                DROP INDEX idx_extraction_receipt_digest;
                DROP INDEX idx_proposal_origins_receipt;
                DROP TABLE proposal_origins;
                DROP TABLE extraction_receipts;
                DROP TABLE extraction_meta;
                """
            )
            con.commit()
            con.close()

            before = inspect_schema(str(path))
            self.assertEqual(before.status, "migration_required")
            self.assertTrue(before.compatible)
            self.assertEqual(before.extraction_status, "uninitialized")
            with FlowGridMemory(db_path=str(path)):
                pass
            self.assertEqual(inspect_schema(str(path)).status, "ready")

    def test_confirmed_history_with_missing_governance_tables_fails_pre_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "confirmed-partial.db"
            with FlowGridMemory(db_path=str(path)) as memory:
                ingest = memory.ingest_raw_events(
                    request_id="confirmed-source",
                    user_id="u1",
                    session_id="s1",
                    messages=[{"role": "user", "content": "我住在杭州"}],
                )
                candidate = memory.propose_memory(
                    user_id="u1",
                    memory_key="profile.city",
                    content="杭州",
                    source_event_ids=ingest.raw_event_ids,
                    status="candidate",
                    authority="user",
                    created_by="migration-test",
                )
                memory.transition_memory(
                    user_id="u1",
                    record_id=candidate.id,
                    target_status="confirmed",
                    actor="owner-1",
                    actor_authority="owner",
                    reason="owner confirmed",
                )
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE governance_meta")
            con.execute("DROP TABLE memory_state_events")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(path, "corrupt", GovernanceError)

    def test_candidate_receipt_with_missing_extraction_tables_fails_pre_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate-partial.db"
            directive = (
                '@flowgrid.memory/v1\n{"proposals":[{'
                '"memory_key":"profile.city","memory_type":"fact",'
                '"subject":"$user","content":"杭州"}]}'
            )
            with FlowGridMemory(db_path=str(path)) as memory:
                ingest = memory.ingest_raw_events(
                    request_id="candidate-source",
                    user_id="u1",
                    session_id="s1",
                    messages=[{"role": "user", "content": directive}],
                )
                receipt = memory.extract_candidates(
                    user_id="u1",
                    raw_event_ids=ingest.raw_event_ids,
                    idempotency_key="candidate-receipt",
                )
                self.assertEqual(receipt.proposal_count, 1)
            con = sqlite3.connect(str(path))
            con.execute("DROP TABLE extraction_meta")
            con.execute("DROP TABLE proposal_origins")
            con.commit()
            con.close()
            self._assert_refused_without_mutation(
                path, "corrupt", ExtractionValidationError
            )


class TestExactLegacyBaselineMigration(unittest.TestCase):
    def test_real_cdae_database_migrates_and_preserves_source(self):
        """Migrate a DB with the exact accepted AML base schema."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "legacy.db"
            create_legacy_database(
                db_path,
                content="legacy-source-sentinel",
                request_id="legacy-request",
            )
            if not db_path.exists():  # pragma: no cover - defensive diagnostic
                self.fail("exact legacy baseline failed to create its database")

            before = inspect_schema(str(db_path))
            self.assertEqual(before.status, "uninitialized")
            with FlowGridMemory(db_path=str(db_path)):
                pass
            after = inspect_schema(str(db_path))
            self.assertEqual(after.status, "ready")
            con = sqlite3.connect(str(db_path))
            try:
                source = con.execute("SELECT content FROM messages").fetchone()[0]
                raw_count = con.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(source, "legacy-source-sentinel")
            self.assertEqual(raw_count, 1)


if __name__ == "__main__":
    unittest.main()
