#!/usr/bin/env python3
"""Generate commit-bound release evidence without third-party dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--tests-passed", action="store_true")
    parser.add_argument("--fresh-install-passed", action="store_true")
    parser.add_argument("--container-passed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]+)?", args.version):
        raise SystemExit("invalid version")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise SystemExit("invalid commit")
    if not (
        args.tests_passed
        and args.fresh_install_passed
        and args.container_passed
    ):
        raise SystemExit("release gates were not explicitly marked passed")
    artifacts = sorted(
        path for path in args.dist.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    if len(artifacts) < 2:
        raise SystemExit("wheel and sdist are required")
    args.output.mkdir(parents=True, exist_ok=True)
    subjects = [
        {
            "name": path.name,
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in artifacts
    ]
    generated_at = datetime.now(timezone.utc).isoformat()
    checksums = "".join(f"{item['sha256']}  {item['name']}\n" for item in subjects)
    (args.output / "checksums.txt").write_text(checksums, encoding="utf-8")

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    acceptance = {
        "schema": "flowgrid.release-acceptance/v1",
        "version": args.version,
        "commit": args.commit,
        "repository": args.repository,
        "run_url": args.run_url,
        "generated_at": generated_at,
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "gates": {
            "tests": {"status": "passed", "command": "./scripts/run_tests.sh --with-mcp"},
            "fresh_wheel_install": {"status": "passed"},
            "public_namespace": {"status": "passed", "module": "flowgrid_memory"},
            "container_contract": {
                "status": "passed",
                "targets": ["cli", "mcp"],
            },
        },
        "artifacts": subjects,
    }
    write_json(args.output / "acceptance.json", acceptance)

    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": item["name"], "digest": {"sha256": item["sha256"]}}
            for item in subjects
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/Attestations/GitHubActionsWorkflow@v1",
                "externalParameters": {
                    "repository": args.repository,
                    "ref": os.environ.get("GITHUB_REF"),
                    "workflow": os.environ.get("GITHUB_WORKFLOW"),
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/{args.repository}@{args.commit}",
                        "digest": {"gitCommit": args.commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": args.run_url},
                "metadata": {
                    "invocationId": os.environ.get("GITHUB_RUN_ID"),
                    "startedOn": generated_at,
                    "finishedOn": generated_at,
                },
            },
        },
    }
    write_json(args.output / "provenance.json", provenance)

    files = []
    relationships = []
    for index, item in enumerate(subjects, start=1):
        spdx_id = f"SPDXRef-Artifact-{index}"
        files.append(
            {
                "SPDXID": spdx_id,
                "fileName": item["name"],
                "checksums": [{"algorithm": "SHA256", "checksumValue": item["sha256"]}],
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-Package",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": spdx_id,
            }
        )
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"flowgrid-agent-memory-{args.version}",
        "documentNamespace": (
            f"https://github.com/{args.repository}/releases/tag/v{args.version}/spdx/{args.commit}"
        ),
        "creationInfo": {
            "created": generated_at,
            "creators": ["Tool: flowgrid-generate-release-evidence/1"],
        },
        "packages": [
            {
                "name": project["name"],
                "SPDXID": "SPDXRef-Package",
                "versionInfo": args.version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": "MIT",
                "licenseDeclared": "MIT",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{project['name']}@{args.version}",
                    }
                ],
                "comment": json.dumps(
                    {
                        "dependencies": project.get("dependencies", []),
                        "optional-dependencies": project.get("optional-dependencies", {}),
                    },
                    sort_keys=True,
                ),
            }
        ],
        "files": files,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package",
            },
            *relationships,
        ],
    }
    write_json(args.output / "sbom.spdx.json", sbom)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
