"""Generated release evidence is complete and commit-bound."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestReleaseEvidence(unittest.TestCase):
    def test_generator_writes_checksums_acceptance_sbom_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            output = root / "out"
            dist.mkdir()
            (dist / "flowgrid_agent_memory-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
            (dist / "flowgrid_agent_memory-0.1.0.tar.gz").write_bytes(b"sdist")
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
            sbom = json.loads((output / "sbom.spdx.json").read_text())
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(len(sbom["files"]), 2)


if __name__ == "__main__":
    unittest.main()
