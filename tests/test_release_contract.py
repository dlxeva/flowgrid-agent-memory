"""Repository-level release and container contracts."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def release_creation_script() -> str:
    """Run the exact workflow shell body rather than a mirrored implementation."""
    workflow = (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
    section = workflow.split("      - name: Create annotated tag and GitHub Release\n", 1)[1]
    script = section.split("        run: |\n", 1)[1].split("        env:\n", 1)[0]
    return textwrap.dedent(script).replace("${{ steps.version.outputs.tag }}", "v-contract-recovery")


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
            "python scripts/verify_release.py",
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


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("git"),
                     "release workflow shell is supported on its Linux/POSIX runner")
class TestReleaseTagRecovery(unittest.TestCase):
    """Local bare remotes and fake gh; these tests never contact GitHub."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flowgrid-tag-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.checkout = self.root / "checkout"
        self.git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        source = self.root / "synthetic-source"
        source.mkdir()
        template = self.root / "empty-template"
        template.mkdir()
        self._git(source, "init", "--quiet", "--initial-branch=main", f"--template={template}")
        self._git(source, "config", "user.name", "Synthetic release fixture")
        self._git(source, "config", "user.email", "fixture@example.invalid")
        tree = self._git(source, "mktree", input="").stdout.strip()
        self.other_commit = self._git(source, "-c", "commit.gpgsign=false", "commit-tree", tree,
                                      "-m", "synthetic initial").stdout.strip()
        self.commit = self._git(source, "-c", "commit.gpgsign=false", "commit-tree", tree,
                                "-p", self.other_commit, "-m", "synthetic candidate").stdout.strip()
        self._git(source, "update-ref", "refs/heads/main", self.commit)
        self._git(self.root, "clone", "--quiet", "--bare", str(source), str(self.remote))
        self._git(self.root, "clone", "--quiet", "--no-hardlinks", str(self.remote), str(self.checkout))
        self.tag = "v-contract-recovery"
        self.tag_ref = f"refs/tags/{self.tag}"
        self._git(self.checkout, "config", "user.name", "Synthetic release fixture")
        self._git(self.checkout, "config", "user.email", "fixture@example.invalid")
        (self.checkout / "dist").mkdir()
        (self.checkout / "dist" / "synthetic.whl").write_bytes(b"synthetic artifact")
        (self.checkout / "release-evidence").mkdir()
        (self.checkout / "release-evidence" / "synthetic.json").write_text("{}")
        binary = self.root / "bin"
        binary.mkdir()
        fake_gh = binary / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "if [ \"$1 $2\" = 'release view' ]; then\n"
            "  [ \"${SYNTHETIC_RELEASE_EXISTS:-0}\" = 1 ]; exit $?\n"
            "fi\n"
            "if [ \"$1 $2\" = 'release create' ]; then\n"
            "  printf '%s\\n' create >> \"$SYNTHETIC_GH_LOG\"\n"
            "  [ \"${SYNTHETIC_CREATE_FAIL:-0}\" = 0 ]; exit $?\n"
            "fi\n"
            "exit 90\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.gh_log = self.root / "gh.log"
        self.env = {**self.git_env, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "GITHUB_SHA": self.commit, "GITHUB_REPOSITORY": "synthetic/release-fixture",
                    "SYNTHETIC_GH_LOG": str(self.gh_log)}

    def _git(self, cwd, *arguments, check=True, input=None):
        return subprocess.run(["git", *arguments], cwd=cwd, check=check, input=input,
                              env=self.git_env, capture_output=True, text=True, timeout=30)

    def _run_release(self, **overrides):
        return subprocess.run(["bash", "-c", release_creation_script()], cwd=self.checkout,
                              env={**self.env, **overrides}, capture_output=True,
                              text=True, timeout=30)

    def _remote_object(self):
        result = self._git(self.root, "--git-dir", str(self.remote), "rev-parse", "--verify", self.tag_ref, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def test_release_failure_after_push_recovers_same_tag_without_recreating_it(self):
        failed = self._run_release(SYNTHETIC_CREATE_FAIL="1")
        self.assertNotEqual(failed.returncode, 0)
        original = self._remote_object()
        self.assertIsNotNone(original)
        self.assertEqual(self._git(self.checkout, "rev-parse", f"{self.tag_ref}^{{commit}}").stdout.strip(), self.commit)
        recovered = self._run_release(SYNTHETIC_CREATE_FAIL="0")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self._remote_object(), original)
        self.assertEqual(self._git(self.checkout, "rev-parse", self.tag_ref).stdout.strip(), original)
        self.assertEqual(self.gh_log.read_text().splitlines(), ["create", "create"])

    def test_matching_remote_annotated_tag_is_fetched_and_preserved(self):
        self._git(self.checkout, "tag", "-a", self.tag, self.commit, "-m", "synthetic existing tag")
        self._git(self.checkout, "push", "origin", self.tag_ref)
        original = self._remote_object()
        self._git(self.checkout, "tag", "-d", self.tag)
        result = self._run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._remote_object(), original)
        self.assertEqual(self._git(self.checkout, "rev-parse", self.tag_ref).stdout.strip(), original)

    def test_matching_remote_lightweight_tag_is_reused(self):
        self._git(self.root, "--git-dir", str(self.remote), "update-ref", self.tag_ref, self.commit)
        result = self._run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._remote_object(), self.commit)
        self.assertEqual(self._git(self.checkout, "rev-parse", self.tag_ref).stdout.strip(), self.commit)

    def test_conflicting_local_tag_fails_without_push_or_release(self):
        self._git(self.checkout, "tag", "-a", self.tag, self.other_commit, "-m", "synthetic conflicting tag")
        original = self._git(self.checkout, "rev-parse", self.tag_ref).stdout.strip()
        result = self._run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local tag", result.stderr)
        self.assertIsNone(self._remote_object())
        self.assertEqual(self._git(self.checkout, "rev-parse", self.tag_ref).stdout.strip(), original)
        self.assertFalse(self.gh_log.exists())

    def test_conflicting_remote_tag_fails_without_overwrite_or_release(self):
        self._git(self.root, "--git-dir", str(self.remote), "update-ref", self.tag_ref, self.other_commit)
        result = self._run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote tag", result.stderr)
        self.assertEqual(self._remote_object(), self.other_commit)
        self.assertNotEqual(self._git(self.checkout, "show-ref", "--verify", "--quiet", self.tag_ref, check=False).returncode, 0)
        self.assertFalse(self.gh_log.exists())

    def test_existing_release_is_rejected_before_tag_mutation(self):
        result = self._run_release(SYNTHETIC_RELEASE_EXISTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertIsNone(self._remote_object())
        self.assertFalse(self.gh_log.exists())

    def test_tag_updates_never_use_force(self):
        script = release_creation_script()
        self.assertNotIn("--force", script)
        self.assertNotIn("git tag -f", script)
        self.assertNotIn('"+refs/tags/', script)


if __name__ == "__main__":
    unittest.main()
