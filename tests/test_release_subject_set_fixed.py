"""Portable exact-subject regression; synthetic archives do not prove CI."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_subject_fixed", ROOT / "scripts/generate_release_evidence.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class ReleaseSubjectSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release-subject-set-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.output = self.root / "out"
        identity = {"schema": "flowgrid.agent-memory.release-identity/v1",
            "source_state": "committed", "source_commit": "a" * 40, "product_version": "0.1.0"}
        raw = json.dumps(identity).encode()
        self.wheel = self.dist / "flowgrid_agent_memory-0.1.0-py3-none-any.whl"
        self.sdist = self.dist / "flowgrid_agent_memory-0.1.0.tar.gz"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("aml_retriever/release_identity.json", raw)
        with tarfile.open(self.sdist, "w:gz") as archive:
            info = tarfile.TarInfo("flowgrid_agent_memory-0.1.0/aml_retriever/release_identity.json")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        report = {"schema": "flowgrid.agent-memory.release-verification/v1", "status": "passed",
            "source_identity": identity, "artifacts": [{"name": p.name,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size_bytes": p.stat().st_size}
                for p in (self.wheel, self.sdist)]}
        (self.dist / "release-verification.json").write_text(json.dumps(report))

    def validate(self):
        return generator.validate_directory(self.dist, version="0.1.0", commit="a" * 40)

    def test_exact_bound_pair_retains_honest_limitations(self):
        binding = self.validate()
        self.assertEqual(binding["binding"], "matched")
        self.assertFalse(binding["test_execution_verified"])
        self.assertFalse(binding["signed_attestation_verified"])

    def test_extra_archive_types_and_versions_rejected(self):
        for name in ("unbound.whl", "other.tar.gz", "flowgrid_agent_memory-0.0.9-py3-none-any.whl"):
            with self.subTest(name=name):
                path = self.dist / name
                path.write_bytes(b"SYNTHETIC_UNBOUND")
                with self.assertRaisesRegex(ValueError, "^artifact_set_mismatch$"):
                    self.validate()
                path.unlink()

    def test_unbound_archive_symlink_rejected(self):
        (self.dist / "unbound.whl").symlink_to(self.wheel)
        with self.assertRaisesRegex(ValueError, "^artifact_set_mismatch$"):
            self.validate()

    def test_main_rejects_before_creating_output(self):
        (self.dist / "unbound.whl").write_bytes(b"SYNTHETIC_UNBOUND")
        args = argparse.Namespace(dist=self.dist, output=self.output, version="0.1.0",
            commit="a" * 40, repository="synthetic/test", run_url="https://example.invalid/synthetic",
            tests_passed=True, fresh_install_passed=True, container_passed=True)
        with patch.object(generator, "parse_args", return_value=args):
            with self.assertRaisesRegex(SystemExit, "^release_artifact_binding_failed$"):
                generator.main()
        self.assertFalse(self.output.exists())

    def test_missing_archive_refused(self):
        self.sdist.unlink()
        with self.assertRaisesRegex(ValueError, "^artifact_set_mismatch$"):
            self.validate()


