#!/usr/bin/env bash
# Operator-run offline install of a reviewed hash-pinned release. No deployment.
# This script does not verify signatures; the operator must review provenance.
set -euo pipefail
if [ "$#" -ne 3 ]; then
  echo "Usage: install-wheel.sh ABSOLUTE_WHEEL_PATH EXPECTED_SHA256 ABSOLUTE_VENV_PATH" >&2
  exit 2
fi
wheel_path="$1"
expected_digest="$2"
venv_path="$3"
case "$wheel_path" in /*.whl) ;; *) echo "Wheel path rejected" >&2; exit 2 ;; esac
case "$venv_path" in /*) ;; *) echo "Virtual environment path rejected" >&2; exit 2 ;; esac
case "$venv_path" in /|/opt|/usr|/usr/local|/var|/var/lib|/home|/root) echo "Virtual environment target rejected" >&2; exit 2 ;; esac
if [ -e "$venv_path" ] && [ ! -f "$venv_path/pyvenv.cfg" ]; then
  echo "Existing non-virtual-environment target rejected" >&2
  exit 2
fi
if [[ ! "$expected_digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Wheel digest rejected" >&2
  exit 2
fi
python_bin="${AML_INSTALL_PYTHON:-python3.11}"
# Read and bind the bytes before creating or changing the target environment.
# Keep a private copy for pip so later source-path replacement cannot substitute
# an unchecked artifact. TemporaryDirectory owns only its generated copy.
"$python_bin" - "$wheel_path" "$expected_digest" "$venv_path" <<'PY'
import hashlib
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result

def rejected():
    print('Reviewed committed wheel required', file=sys.stderr)
    raise SystemExit(2)

if sys.version_info < (3, 11):
    rejected()
source, expected, target = sys.argv[1:]
try:
    path = pathlib.Path(source)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        rejected()
    data = path.read_bytes()
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        rejected()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [item for item in archive.infolist()
                   if item.filename == 'aml_retriever/release_identity.json']
        if len(entries) != 1 or entries[0].file_size > 2 * 1024 * 1024:
            rejected()
        identity = json.loads(archive.read(entries[0]), object_pairs_hook=unique)
    if (not isinstance(identity, dict)
            or identity.get('schema') != 'flowgrid.agent-memory.release-identity/v1'
            or identity.get('source_state') != 'committed'
            or not re.fullmatch('[0-9a-f]{40}', identity.get('source_commit') or '')):
        rejected()
except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError):
    rejected()

with tempfile.TemporaryDirectory(prefix='flowgrid-reviewed-install-') as private:
    copied = pathlib.Path(private) / path.name
    copied.write_bytes(data)
    subprocess.run([sys.executable, '-m', 'venv', target], check=True)
    python = str(pathlib.Path(target) / 'bin/python')
    subprocess.run([python, '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--force-reinstall', str(copied)], check=True)
    subprocess.run([python, '-c',
        'from aml_retriever.aml_hosted import runtime_identity; '
        'i=runtime_identity(); '
        'assert i["identity_source"] == "build_manifest" and '
        'i["source_state"] == "committed" and i["source_commit"]; '
        'print("Pinned AML release installed; credentials and service are not configured")'],
        check=True)
PY
