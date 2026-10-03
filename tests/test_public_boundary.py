"""Actual staged-blob publication gates and deliberate contamination probes."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_public_boundary as gate


REPO = Path(__file__).resolve().parents[1]


class PublicBoundaryPolicyTests(unittest.TestCase):
    def test_source_templates_and_pinned_synthetic_assets_are_allowed(self):
        for path in (
            ".gitattributes", "aml_retriever/api.py", "flowgrid_memory/py.typed",
            "scripts/check_public_boundary.py", "tests/test_public_boundary.py",
            "deploy/aml/service.env.example", "deploy/aml/install-wheel.sh",
            "docs/AML_SUBMISSION.md", "config.example.json",
            *gate.FIXTURE_HASHES,
        ):
            with self.subTest(path=path):
                self.assertEqual(gate.check_blob(path, (REPO / path).read_bytes()), [])

    def test_reviewed_public_source_is_not_a_source_of_false_positives(self):
        # Current source checks are separate from the staged-blob tests below.
        paths = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout.split(b"\0")
        checked = 0
        for raw_path in paths:
            if raw_path:
                path = raw_path.decode("utf-8")
                checked += 1
                with self.subTest(path=path):
                    self.assertEqual(gate.check_blob(path, (REPO / path).read_bytes()), [])
        self.assertGreater(checked, 100)

    def test_database_ledgers_and_runtime_paths_are_rejected(self):
        cases = {
            "memory.sqlite3-wal": "database",
            "tests/sample.DB": "database",
            "PROJECT_MASTER.json": "private-control-ledger",
            "docs/SNAPSHOT.md": "private-control-ledger",
            ".flg/state.json": "private-runtime-directory",
            "aml_retriever/runtime/dump.py": "private-runtime-directory",
            "scripts/reports/real_session.py": "private-runtime-directory",
            ".env": "environment-secret-file",
            "tests/.env.production": "environment-secret-file",
            "deploy/credential.json": "credential-file",
            "aml_retriever/unreviewed.json": "unreviewed-public-path",
            "docs/private-audit.md": "unreviewed-public-path",
            "image.png": "unreviewed-public-path",
        }
        for path, category in cases.items():
            with self.subTest(path=path):
                self.assertIn(category, gate.check_blob(path, b"private contents"))

    def test_binary_special_modes_abnormal_paths_and_large_blobs_rejected(self):
        for data in (b"\x00SQLite", b"\xff\xfe", b"control\x01"):
            self.assertIn("unknown-binary", gate.check_blob("tests/probe.py", data))
        self.assertIn("symlink", gate.check_blob("tests/probe.py", b"target", "120000"))
        self.assertIn("unsupported-file-mode", gate.check_blob("tests/probe.py", b"", "160000"))
        self.assertIn("unexpected-executable", gate.check_blob("README.md", b"text", "100755"))
        for path in ("../README.md", "tests//probe.py", "tests\\probe.py", "tests/line\n.py"):
            self.assertIn("abnormal-path", gate.check_blob(path, b"text"))
        self.assertIn("oversized-blob", gate.check_blob("tests/probe.py", b"x" * (gate.MAX_BLOB_BYTES + 1)))

    def test_fixtures_are_pinned_against_same_filename_payload_replacement(self):
        for path in gate.FIXTURE_HASHES:
            self.assertIn("unreviewed-fixture-content", gate.check_blob(path, b'{"payload": "private"}'))

    def test_credentials_and_local_paths_rejected_without_values(self):
        secrets = (
            "gh" + "p_" + "q" * 36,
            "AK" + "IA" + "Q" * 16,
            "sk" + "-proj-" + "q" * 32,
            "-----BEGIN " + "PRIVATE KEY-----",
            'api_key="' + "random-live-value-123456789" + '"',
            'password="' + "random-live-password-987654321" + '"',
        )
        for value in secrets:
            with self.subTest(prefix=value[:3]):
                issues = gate.check_blob("tests/probe.py", value.encode())
                self.assertTrue(issues)
                self.assertNotIn(value, json.dumps(issues))
        paths = (
            "/" + "Users/" + "person/private.db",
            "/" + "Volumes/" + "disk/private.db",
            "/" + "home/" + "person/private.db",
            "C:" + "\\Users\\" + "Person\\private.db",
        )
        for value in paths:
            self.assertIn("local-machine-path", gate.check_blob("README.md", value.encode()))

    def test_empty_and_placeholder_credentials_are_allowed(self):
        for value in ("", "synthetic-test-credential-aml-only", "${FLOWGRID_CREDENTIAL}", "<replace-with-local-key>"):
            source = ('api_key="' + value + '"').encode()
            self.assertEqual(gate.check_blob("config.example.json", source), [])


class PublicBoundaryGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flowgrid-public-gate-")
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def stage(self, path: str, data: bytes):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.git("add", "--", path)

    def test_empty_index_and_non_repository_fail_closed(self):
        self.assertFalse(gate.inspect_index(self.root)["passed"])
        self.assertIn("empty-index", [row["category"] for row in gate.inspect_index(self.root)["violations"]])
        self.assertFalse(gate.inspect_index(self.root / "missing")["passed"])

    def test_clean_nonempty_source_passes(self):
        self.stage("README.md", b"Public source\n")
        result = gate.inspect_index(self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(result["checked_files"], 1)

    def test_autocrlf_checkout_preserves_pinned_fixture_raw_bytes(self):
        self.git("config", "core.autocrlf", "true")
        self.stage(".gitattributes", (REPO / ".gitattributes").read_bytes())
        for path in gate.FIXTURE_HASHES:
            with self.subTest(path=path):
                expected = (REPO / path).read_bytes()
                self.assertNotIn(b"\r\n", expected)
                self.stage(path, expected)
                target = self.root / path
                target.write_bytes(expected.replace(b"\n", b"\r\n"))
                self.git("checkout-index", "--force", "--", path)
                checked_out = target.read_bytes()
                self.assertEqual(checked_out, expected)
                self.assertEqual(gate.check_blob(path, checked_out), [])
        self.assertTrue(gate.inspect_index(self.root)["passed"])

    def test_published_index_not_clean_working_tree_controls_result(self):
        secret = "gh" + "p_" + "q" * 36
        self.stage("tests/probe.py", secret.encode())
        (self.root / "tests/probe.py").write_text("safe working tree\n")
        result = gate.inspect_index(self.root)
        self.assertFalse(result["passed"])
        self.assertNotIn(secret, json.dumps(result))
        self.git("add", "--", "tests/probe.py")
        (self.root / "tests/probe.py").write_text(secret)
        self.assertTrue(gate.inspect_index(self.root)["passed"])

    def test_multiple_staged_contaminants_and_cli_nonzero(self):
        self.stage("README.md", b"Public source\n")
        for path in ("memory.sqlite", "PROJECT_MASTER.json", ".env", ".flg/state.json"):
            self.stage(path, b"private probe\n")
        result = gate.inspect_index(self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["violations"]), 4)
        completed = subprocess.run(
            [sys.executable, str(REPO / "scripts/check_public_boundary.py"), "--repo", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("private probe", completed.stdout)
        self.assertFalse(json.loads(completed.stdout)["passed"])

    def test_staged_symlink_refused_without_following_target(self):
        # Directly construct an index entry; Windows needs no symlink permission.
        oid = subprocess.run(
            ["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
            input=b"nonexistent-external-private-target", capture_output=True, check=True,
        ).stdout.strip().decode()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{oid},tests/probe.py")
        result = gate.inspect_index(self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"], [{"path": "tests/probe.py", "category": "symlink"}])

    def test_midscan_index_change_is_rejected(self):
        self.stage("README.md", b"Public source\n")
        real_git = gate._git
        reads = 0

        def changing_git(repo, *args, data=None):
            nonlocal reads
            result = real_git(repo, *args, data=data)
            if args == ("ls-files", "--stage", "-z"):
                reads += 1
                if reads == 2:
                    return result + b"changed"
            return result

        with patch.object(gate, "_git", side_effect=changing_git):
            result = gate.inspect_index(self.root)
        self.assertFalse(result["passed"])
        self.assertIn("index-changed", [row["category"] for row in result["violations"]])


if __name__ == "__main__":
    unittest.main()
