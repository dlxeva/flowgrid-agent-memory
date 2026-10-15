"""Generated release evidence is complete and commit-bound."""
import json
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
import tarfile
import zipfile
from pathlib import Path


class TestReleaseEvidence(unittest.TestCase):
    def test_generator_writes_checksums_acceptance_sbom_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            output = root / "out"
            dist.mkdir()
            # The generator now validates archive identity and verification
            # hashes. Use actual archives instead of the obsolete byte stubs.
            identity = {
                "source_state": "committed", "source_commit": "a" * 40,
                "product_version": "0.1.0",
            }
            payload = json.dumps(identity).encode()
            wheel = dist / "flowgrid_agent_memory-0.1.0-py3-none-any.whl"
            sdist = dist / "flowgrid_agent_memory-0.1.0.tar.gz"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("aml_retriever/release_identity.json", payload)
            with tarfile.open(sdist, "w:gz") as archive:
                member = tarfile.TarInfo(
                    "flowgrid_agent_memory-0.1.0/aml_retriever/release_identity.json"
                )
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            report = {
                "schema": "flowgrid.agent-memory.release-verification/v1",
                "status": "passed", "source_identity": identity,
                "artifacts": [
                    {"name": path.name,
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "size_bytes": path.stat().st_size}
                    for path in (wheel, sdist)
                ],
            }
            (dist / "release-verification.json").write_text(json.dumps(report))
            subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_release_evidence.py",
                    "--dist",
                    str(dist),
                    "--output",
                    str(output),
                    "--version",
                    "0.1.0",
                    "--commit",
                    "a" * 40,
                    "--repository",
                    "dlxeva/flowgrid-agent-memory",
                    "--run-url",
                    "https://github.com/dlxeva/flowgrid-agent-memory/actions/runs/1",
                    "--tests-passed",
                    "--fresh-install-passed",
                    "--container-passed",
                ],
                check=True,
            )
            expected = {
                "checksums.txt",
                "acceptance.json",
                "provenance.json",
                "sbom.spdx.json",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            acceptance = json.loads((output / "acceptance.json").read_text())
            self.assertEqual(acceptance["commit"], "a" * 40)
            self.assertEqual(len(acceptance["artifacts"]), 2)
            self.assertEqual(
                acceptance["gates"]["container_contract"]["status"],
                "passed",
            )
            self.assertEqual(
                acceptance["gates"]["container_contract"]["targets"],
                ["cli", "mcp"],
            )
            sbom = json.loads((output / "sbom.spdx.json").read_text())
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(len(sbom["files"]), 2)


if __name__ == "__main__":
    unittest.main()
