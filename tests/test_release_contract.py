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

    def test_release_workflow_requires_acceptance_evidence_and_attestation(self):
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        for required in (
            "./scripts/run_tests.sh --with-mcp",
            "python -m build",
            "generate_release_evidence.py",
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
