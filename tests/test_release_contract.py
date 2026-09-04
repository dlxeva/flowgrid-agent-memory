"""Repository-level release and container contracts."""
import unittest
from pathlib import Path


class TestReleaseContract(unittest.TestCase):
    def test_default_container_is_non_networked_cli_and_mcp_is_separate(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM runtime AS mcp", dockerfile)
        self.assertIn("FROM runtime AS cli", dockerfile)
        self.assertTrue(dockerfile.rstrip().endswith('CMD ["doctor", "--ephemeral"]'))
        self.assertNotIn('ENTRYPOINT ["flowgrid-memory-rest"]', dockerfile)
        self.assertNotIn("EXPOSE ", dockerfile)

    def test_ci_builds_and_runs_both_supported_container_targets(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        for required in (
            "name: Container contract",
            "docker build --target cli",
            "docker run --rm flowgrid-agent-memory:ci",
            "docker build --target mcp",
            "python scripts/smoke_mcp.py --container-image flowgrid-agent-memory:mcp-ci",
            "python scripts/smoke_wheel.py dist/*.whl",
        ):
            self.assertIn(required, workflow)

    def test_release_workflow_requires_all_evidence_and_attestation_gates(self):
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        for required in (
            "./scripts/run_tests.sh --with-mcp",
            "python -m build",
            "python scripts/smoke_wheel.py dist/*.whl",
            "python scripts/smoke_mcp.py --container-image flowgrid-agent-memory:mcp-release",
            "docker build --target cli",
            "docker build --target mcp",
            "generate_release_evidence.py",
            "--container-passed",
            "actions/attest@v4.2.2",
            "artifact-metadata: write",
            "gh release create",
        ):
            self.assertIn(required, workflow)

    def test_mutable_acceptance_hashes_are_not_committed(self):
        acceptance = Path("docs/ACCEPTANCE_V0_1.md").read_text(encoding="utf-8")
        self.assertNotIn("fresh wheel SHA-256", acceptance)
        self.assertIn("ACCEPTANCE_CRITERIA.md", acceptance)


if __name__ == "__main__":
    unittest.main()
