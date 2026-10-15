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


import tarfile
import zipfile

def decode(data):
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('metadata_too_large')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_metadata_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)

def validate_claims(report, identities, artifacts, *, version, commit):
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('invalid_commit')
    if not re.fullmatch('[0-9]+\\.[0-9]+\\.[0-9]+', version):
        raise ValueError('invalid_version')
    if report.get('schema') != 'flowgrid.agent-memory.release-verification/v1' or report.get('status') != 'passed':
        raise ValueError('verification_not_passed')
    identity = report.get('source_identity')
    if not isinstance(identity, dict) or identity.get('source_state') != 'committed':
        raise ValueError('committed_identity_required')
    if identity.get('source_commit') != commit or identity.get('product_version') != version:
        raise ValueError('identity_label_mismatch')
    if len(identities) != 2 or any((item != identity for item in identities)):
        raise ValueError('archive_identity_mismatch')
    rows = report.get('artifacts')
    if not isinstance(rows, list) or len(rows) != 2 or any((not isinstance(row, dict) for row in rows)):
        raise ValueError('artifact_manifest_invalid')
    if len({row.get('name') for row in rows}) != 2:
        raise ValueError('artifact_manifest_invalid')
    expected = {row['name']: (row.get('sha256'), row.get('size_bytes')) for row in rows}
    if artifacts != expected:
        raise ValueError('artifact_hash_mismatch')
    return {'binding': 'matched', 'signed_attestation_verified': False, 'test_execution_verified': False}

def validate_directory(directory, *, version, commit):
    directory = Path(directory)
    if not re.fullmatch('[0-9]+\\.[0-9]+\\.[0-9]+', version):
        raise ValueError('invalid_version')
    names = [f'flowgrid_agent_memory-{version}-py3-none-any.whl', f'flowgrid_agent_memory-{version}.tar.gz']
    archive_names = {p.name for p in directory.iterdir()
                     if p.suffix == '.whl' or p.name.endswith('.tar.gz')}
    if archive_names != set(names):
        raise ValueError('artifact_set_mismatch')
    artifacts, identities = ({}, [])
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('unexpected_artifact')
        data = path.read_bytes()
        artifacts[name] = (hashlib.sha256(data).hexdigest(), len(data))
        if name.endswith('.whl'):
            with zipfile.ZipFile(path) as archive:
                matches = [i for i in archive.infolist() if i.filename == 'aml_retriever/release_identity.json']
                if len(matches) != 1 or matches[0].file_size > 2 * 1024 * 1024:
                    raise ValueError('archive_identity_invalid')
                identities.append(decode(archive.read(matches[0])))
        else:
            with tarfile.open(path) as archive:
                matches = [i for i in archive.getmembers() if i.name == f'flowgrid_agent_memory-{version}/aml_retriever/release_identity.json']
                if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 2 * 1024 * 1024:
                    raise ValueError('archive_identity_invalid')
                identities.append(decode(archive.extractfile(matches[0]).read()))
    path = directory / 'release-verification.json'
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('verification_report_invalid')
    return validate_claims(decode(path.read_bytes()), identities, artifacts, version=version, commit=commit)

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
    try:
        binding = validate_directory(args.dist, version=args.version, commit=args.commit)
    except (ValueError, OSError, zipfile.BadZipFile, tarfile.TarError):
        raise SystemExit("release_artifact_binding_failed")
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
    acceptance["artifact_binding"] = binding
    acceptance["gate_claim_origin"] = "caller_asserted; CI execution evidence required separately"
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
