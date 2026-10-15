"""No network or deployment: invalid releases cannot touch a target venv."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/aml/install-wheel.sh'

class InstallerPreflightTests(unittest.TestCase):
    def test_accepted_synthetic_identity_uses_checked_copy_not_changed_source(self):
        # This exercises ordering/copy binding only, not a real release install.
        with tempfile.TemporaryDirectory(prefix='fg-installer-ordering-') as folder:
            root = Path(folder)
            wheel = root / 'flowgrid_agent_memory-0.1.1-py3-none-any.whl'
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('aml_retriever/release_identity.json', json.dumps({
                    'schema':'flowgrid.agent-memory.release-identity/v1',
                    'source_state':'committed', 'source_commit':'a' * 40}))
            checked = data.getvalue()
            wheel.write_bytes(checked)
            digest = hashlib.sha256(checked).hexdigest()
            calls = []
            def synthetic_run(command, *, check):
                self.assertTrue(check)
                calls.append(command)
                if len(calls) == 1:
                    wheel.write_bytes(b'changed after verification')
                elif len(calls) == 2:
                    copied = Path(command[-1])
                    self.assertNotEqual(copied, wheel)
                    self.assertEqual(copied.read_bytes(), checked)
            body = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
            with mock.patch.object(sys, 'argv', ['probe', str(wheel), digest, str(root/'target')]), \
                    mock.patch('subprocess.run', side_effect=synthetic_run):
                exec(compile(body, '<synthetic-installer-probe>', 'exec'), {})
            self.assertEqual(len(calls), 3)
            self.assertFalse((root/'target').exists())

    def run_invalid(self, identity, *, mismatch=False, duplicate=False):
        with tempfile.TemporaryDirectory(prefix='fg-installer-synthetic-') as folder:
            root = Path(folder)
            wheel = root / 'flowgrid_agent_memory-0.1.1-py3-none-any.whl'
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                text = json.dumps(identity) if not duplicate else '{"source_state":"committed","source_state":"uncommitted"}'
                archive.writestr('aml_retriever/release_identity.json', text)
            wheel.write_bytes(data.getvalue())
            target = root / 'target'
            digest = '0' * 64 if mismatch else hashlib.sha256(data.getvalue()).hexdigest()
            environment = dict(os.environ, AML_INSTALL_PYTHON=sys.executable)
            result = subprocess.run(['bash', str(SCRIPT), str(wheel), digest, str(target)],
                                    env=environment, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())
            self.assertNotIn(str(root), result.stderr.decode())

    def test_uncommitted_rejected_before_venv(self):
        self.run_invalid({'schema':'flowgrid.agent-memory.release-identity/v1',
                          'source_state':'uncommitted', 'source_commit':None})

    def test_hash_mismatch_rejected_before_venv(self):
        self.run_invalid({}, mismatch=True)

    def test_duplicate_identity_rejected_before_venv(self):
        self.run_invalid({}, duplicate=True)

    def test_fake_commit_format_rejected_before_venv(self):
        self.run_invalid({'schema':'flowgrid.agent-memory.release-identity/v1',
                          'source_state':'committed', 'source_commit':'not-a-commit'})

if __name__ == '__main__':
    unittest.main()
