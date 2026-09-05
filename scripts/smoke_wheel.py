#!/usr/bin/env python3
"""Verify the built wheel outside the checkout in a fresh, isolated venv."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


IMPORT_PROBE = """
import importlib.metadata
import pathlib
import sys
import aml_retriever
import flowgrid_memory
prefix = pathlib.Path(sys.prefix).resolve()
for module in (aml_retriever, flowgrid_memory):
    origin = pathlib.Path(module.__file__).resolve()
    assert prefix in origin.parents, f'non-wheel import: {origin}'
assert flowgrid_memory.__version__ == importlib.metadata.version('flowgrid-agent-memory')
assert aml_retriever.PRODUCT_VERSION == flowgrid_memory.__version__
print('wheel import origins and versions verified')
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        parser.error("expected a built .whl file")
    smoke = Path(__file__).resolve().with_name("smoke_mcp.py")
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    with tempfile.TemporaryDirectory(prefix="flowgrid-wheel-") as temporary:
        root = Path(temporary)

        def run(*command: str) -> None:
            subprocess.run(command, cwd=root, env=env, check=True, timeout=300)

        run(sys.executable, "-I", "-m", "venv", str(root / "venv"))
        binary = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = str(binary / ("python.exe" if os.name == "nt" else "python"))
        run(python, "-I", "-m", "pip", "install", str(wheel) + "[mcp]")
        run(python, "-I", "-c", IMPORT_PROBE)
        run(str(binary / "flowgrid-memory"), "--version")
        run(str(binary / "flowgrid-memory"), "doctor", "--ephemeral")
        run(str(binary / "flowgrid-memory"), "demo", "--ephemeral")
        run(str(binary / "flowgrid-memory-rest"), "--help")
        run(python, "-I", str(smoke), "--server-executable", str(binary / "flowgrid-memory-mcp"), "--server-cwd", str(root))
    print("fresh wheel isolation and MCP stdio smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
