#!/usr/bin/env python3
"""Run the governed local-memory proof without emitting memory content."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aml_retriever.product_cli import run_governed_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FlowGrid Agent Memory governed demo")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--db", metavar="PATH", help="explicit persistent SQLite path")
    group.add_argument("--ephemeral", action="store_true", help="explicit temporary mode")
    args = parser.parse_args(argv)

    if args.db:
        result = run_governed_demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="flowgrid-memory-demo-") as directory:
            result = run_governed_demo(str(Path(directory) / "demo.db"))
            result["ephemeral_cleanup"] = "automatic"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(
            '{"reason":"local_memory_operation_failed","status":"error"}',
            file=sys.stderr,
        )
        raise SystemExit(4)
