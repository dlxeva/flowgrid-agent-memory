#!/usr/bin/env bash
# Operator-run offline install of a reviewed, hash-pinned wheel. No deployment.
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
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
"$python_bin" -c 'import hashlib, pathlib, sys; p=pathlib.Path(sys.argv[1]); d=hashlib.sha256(p.read_bytes()).hexdigest(); sys.exit(0 if d == sys.argv[2] else 1)' "$wheel_path" "$expected_digest"
"$python_bin" -m venv "$venv_path"
"$venv_path/bin/python" -m pip install --no-index --no-deps --force-reinstall "$wheel_path"
"$venv_path/bin/python" -c 'from aml_retriever.aml_hosted import runtime_identity; i=runtime_identity(); assert i["identity_source"] == "build_manifest" and i["source_state"] == "committed" and i["source_commit"]; print("Pinned AML release installed; credentials and service are not configured")'
