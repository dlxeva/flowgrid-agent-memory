"""Source demo entrypoint tests from outside the repository."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "demo_governed_memory.py"


class TestGovernedDemoScript(unittest.TestCase):
    def _run(self, *args: str, cwd: Path) -> dict[str, object]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("Traceback", completed.stdout)
        self.assertNotIn("concise and evidence-first", completed.stdout)
        return json.loads(completed.stdout)

    def test_default_is_ephemeral_and_content_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            before = set(cwd.iterdir())
            result = self._run(cwd=cwd)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["ephemeral_cleanup"], "automatic")
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(set(cwd.iterdir()), before)

    def test_explicit_database_persists_real_audit_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            path = cwd / "explicit.db"
            result = self._run("--db", str(path), cwd=cwd)
            self.assertEqual(result["status"], "ok")
            self.assertNotIn(str(path), str(result))
            self.assertTrue(path.exists())
            con = sqlite3.connect(str(path))
            try:
                records = con.execute(
                    "SELECT id,status FROM memory_records ORDER BY created_at,id"
                ).fetchall()
                chains = [
                    [
                        row[0]
                        for row in con.execute(
                            "SELECT to_status FROM memory_state_events WHERE record_id=? "
                            "ORDER BY transitioned_at,id",
                            (record_id,),
                        ).fetchall()
                    ]
                    for record_id, _status in records
                ]
            finally:
                con.close()
            self.assertEqual({status for _record_id, status in records}, {"superseded", "confirmed"})
            self.assertEqual(
                sorted(chains),
                sorted([["candidate", "confirmed", "superseded"], ["candidate", "confirmed"]]),
            )


if __name__ == "__main__":
    unittest.main()
