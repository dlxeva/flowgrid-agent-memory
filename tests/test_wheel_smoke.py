"""The wheel gate must not import the checkout or inherit Python path overrides."""
import contextlib
import io
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class TestWheelSmoke(unittest.TestCase):
    def setUp(self):
        self.script = runpy.run_path("scripts/smoke_wheel.py")

    def test_all_commands_use_external_cwd_and_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "synthetic.whl"
            wheel.touch()
            with patch.dict(os.environ, {"PYTHONPATH": "/unsafe-source", "PYTHONHOME": "/unsafe-home"}), patch("subprocess.run") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.script["main"]([str(wheel)]), 0)
            calls = run.call_args_list
            self.assertEqual(len(calls), 8)
            roots = {call.kwargs["cwd"] for call in calls}
            self.assertEqual(len(roots), 1)
            root = roots.pop()
            self.assertNotEqual(root, Path.cwd())
            self.assertFalse(root.exists())
            for call in calls:
                self.assertNotIn("PYTHONPATH", call.kwargs["env"])
                self.assertNotIn("PYTHONHOME", call.kwargs["env"])
                self.assertTrue(call.kwargs["check"])
            self.assertIn(str(wheel.resolve()) + "[mcp]", calls[1].args[0])
            self.assertEqual(calls[2].args[0][1:3], ("-I", "-c"))
            self.assertIn("--server-executable", calls[-1].args[0])
            self.assertIn("--server-cwd", calls[-1].args[0])

    def test_failed_probe_is_not_reported_as_success_and_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "synthetic.whl"
            wheel.touch()
            with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "probe")) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    self.script["main"]([str(wheel)])
            self.assertFalse(run.call_args.kwargs["cwd"].exists())

    def test_source_import_origin_is_rejected(self):
        # The repository imports here are intentional negative-test inputs.
        with self.assertRaisesRegex(AssertionError, "non-wheel import"):
            exec(self.script["IMPORT_PROBE"], {})


if __name__ == "__main__":
    unittest.main()
