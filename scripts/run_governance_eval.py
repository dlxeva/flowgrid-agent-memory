#!/usr/bin/env python3
"""Run the non-compensable governance / local-proxy / official-claim gates."""
from __future__ import annotations

import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aml_retriever.evaluation.governance_suite import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    run_governance_suite,
    write_result,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run FlowGrid Agent Memory governance hard gates and synthetic AML proxies. "
            "Safety gates and retrieval metrics are reported separately."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("quick", "product"),
        default="quick",
        help="quick checks implemented local surfaces; product also requires governed REST and MCP",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="path to the governance evaluation manifest",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="write the structured JSON artifact to this file; '-' writes stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_governance_suite(profile=args.profile, manifest_path=args.manifest)
    if args.output == "-":
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        )
    else:
        write_result(args.output, result)
    return 0 if result.get("verdict", {}).get("profile") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
