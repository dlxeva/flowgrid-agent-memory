"""Provenance, archive privacy, deterministic sdist and real offline release."""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import verify_release as release
from aml_retriever.release_integrity import runtime_source_digest


REPO = Path(__file__).resolve().parents[1]


class TestReleaseBoundary(unittest.TestCase):
    def test_committed_label_requires_every_frozen_input_to_match(self):
        commit = "a" * 40
        files = {"public.py": b"reviewed"}
        with patch.object(release, "_git", side_effect=[commit.encode(), b"reviewed"]):
            self.assertEqual(release.committed_source(REPO, files), commit)
        with patch.object(release, "_git", side_effect=[commit.encode(), b"old contents"]):
            self.assertIsNone(release.committed_source(REPO, files))

    def test_freeze_excludes_private_state_and_fails_unreviewed_runtime_module(self):
        files, identity = release.freeze_source(REPO)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release.stage_source(files, identity, root, release.DEFAULT_EPOCH)
            for name in (".flg/state.json", "data/real-host.sqlite3", "PROJECT_MASTER.json", "reports/real.txt", ".env"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private sentinel")
            copied, metadata = release.freeze_source(root)
            self.assertEqual(copied, files)
            self.assertEqual(metadata["source_commit"], None)
            self.assertEqual(metadata["source_state"], "uncommitted")
            (root / "aml_retriever/private.py").write_bytes(b"private sentinel")
            with self.assertRaisesRegex(release.VerificationError, "unreviewed_runtime_module"):
                release.freeze_source(root)

    def test_archive_rejects_private_unknown_and_traversal_members(self):
        for name in ("data/private.sqlite3", ".flg/state.json", "../escape", "PROJECT_MASTER.json", "aml_retriever/evaluation/fixtures/real.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "invalid.whl"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(name, b"private")
                with self.assertRaises(release.VerificationError):
                    release.verify_archive(path, {}, {})

    def test_sdist_normalization_removes_clock_and_owner_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = []
            for index in range(2):
                raw = root / f"raw-{index}.tar.gz"
                with raw.open("wb") as handle:
                    with gzip.GzipFile(fileobj=handle, mode="wb", mtime=500 + index) as stream:
                        with tarfile.open(fileobj=stream, mode="w") as archive:
                            info = tarfile.TarInfo("package/source.py")
                            info.size = 4
                            info.mtime = 1000 + index
                            info.uid = 10 + index
                            info.uname = f"local-user-{index}"
                            archive.addfile(info, io.BytesIO(b"code"))
                target = root / f"normalized-{index}.tar.gz"
                release.normalize_sdist(raw, target, release.DEFAULT_EPOCH)
                outputs.append(target.read_bytes())
            self.assertEqual(*outputs)

    def test_runtime_digest_detects_modified_resource_and_added_code(self):
        files, identity = release.freeze_source(REPO)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release.stage_source(files, identity, root, release.DEFAULT_EPOCH)
            paths, digest = runtime_source_digest(root)
            self.assertEqual(paths, identity["runtime_files"])
            self.assertEqual(digest, identity["runtime_digest_sha256"])
            fixture = root / "aml_retriever/evaluation/fixtures/governance_v1.json"
            fixture.write_bytes(fixture.read_bytes() + b" ")
            self.assertNotEqual(runtime_source_digest(root)[1], digest)
            (root / "aml_retriever/unreviewed.py").write_bytes(b"pass")
            with self.assertRaisesRegex(ValueError, "runtime_file_set_mismatch"):
                runtime_source_digest(root)

    def test_manifest_and_python_allowlists_match(self):
        lines = (REPO / "MANIFEST.in").read_text().splitlines()
        declared = {name for line in lines if line.startswith("include ") for name in line.split()[1:]}
        self.assertEqual(declared, release.SOURCE_ALLOWLIST | {release.IDENTITY_PATH})


class TestRealOfflineRelease(unittest.TestCase):
    def test_two_rebuilds_sdist_to_wheel_and_fresh_installs(self):
        with tempfile.TemporaryDirectory(prefix="flowgrid-release-test-") as directory:
            parent = Path(directory)
            output = parent / "artifacts"
            report = release.verify_release(REPO, output, parent, release.DEFAULT_EPOCH)
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["network_used"])
            self.assertEqual(report["official_aml"], "unverified")
            for kind in ("fresh_wheel", "fresh_sdist"):
                installed = report["gates"][kind]
                self.assertTrue(installed["isolated_imports"])
                self.assertEqual(installed["base_runtime_dependencies"], [])
                self.assertTrue(installed["governance_fixture_attested"])
                self.assertTrue(all(installed["governed_demo_checks"].values()))
            published = json.loads((output / "release-verification.json").read_text())
            self.assertEqual(published, report)
            serialized = json.dumps(report)
            self.assertNotIn(str(REPO), serialized)
            self.assertNotIn(str(parent), serialized)
            for artifact in report["artifacts"]:
                self.assertEqual(release.sha256(output / artifact["name"]), artifact["sha256"])


if __name__ == "__main__":
    unittest.main()
