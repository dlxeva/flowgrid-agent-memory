"""Safety and failure-path checks for the synthetic container probe."""
import importlib.util
from pathlib import Path
import runpy
import subprocess
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK not installed")
class TestMCPSmoke(unittest.TestCase):
    def setUp(self):
        self.script = runpy.run_path("scripts/smoke_mcp.py")

    def test_container_is_scoped_and_cleaned_even_when_transport_fails(self):
        import anyio

        run = self.script["_run"]
        transport = Mock(side_effect=RuntimeError("synthetic transport failure"))
        cleanup = Mock()
        with patch.dict(run.__globals__, {"stdio_client": transport, "_remove_container": cleanup}):
            with self.assertRaisesRegex(RuntimeError, "synthetic transport failure"):
                anyio.run(run, None, str(Path.cwd()), "synthetic-image")
        params = transport.call_args.args[0]
        self.assertEqual(params.command, "docker")
        self.assertEqual(params.args[:5], ["run", "--rm", "-i", "--network", "none"])
        self.assertEqual(params.args.count("--mount"), 1)
        mount = params.args[params.args.index("--mount") + 1]
        self.assertTrue(mount.endswith("dst=/run/principal.json,readonly"))
        self.assertNotIn(str(Path.cwd()), mount)
        self.assertEqual(params.args[-4:], ["--db", ":memory:", "--principal-config", "/run/principal.json"])
        cleanup.assert_called_once_with(params.args[params.args.index("--name") + 1])

    def test_cleanup_accepts_auto_removed_container(self):
        for code, error in ((0, ""), (1, "Error response from daemon: No such container: synthetic")):
            with self.subTest(code=code), patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, "", error)) as run:
                self.script["_remove_container"]("synthetic")
                self.assertEqual(run.call_args.args[0], ["docker", "rm", "--force", "synthetic"])

    def test_cleanup_failure_fails_the_probe(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                self.script["_remove_container"]("synthetic")


if __name__ == "__main__":
    unittest.main()
