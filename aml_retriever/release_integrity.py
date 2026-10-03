"""Deterministic integrity checks for reviewed runtime source/resources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


RUNTIME_SOURCE_FILES = frozenset({
    "aml_retriever/__init__.py", "aml_retriever/_version.py", "aml_retriever/access.py",
    "aml_retriever/aml_hosted.py", "aml_retriever/api.py", "aml_retriever/auth.py",
    "aml_retriever/cli.py", "aml_retriever/compiler.py", "aml_retriever/config.py",
    "aml_retriever/context.py", "aml_retriever/extraction.py", "aml_retriever/facade.py",
    "aml_retriever/features.py", "aml_retriever/governance.py", "aml_retriever/mcp_adapter.py",
    "aml_retriever/mcp_tools.py", "aml_retriever/migrations.py", "aml_retriever/model_extraction.py",
    "aml_retriever/owner_review.py", "aml_retriever/product_cli.py",
    "aml_retriever/release_integrity.py", "aml_retriever/rest_v1.py", "aml_retriever/retriever.py",
    "aml_retriever/server.py", "aml_retriever/store.py", "aml_retriever/views.py",
    "aml_retriever/evaluation/__init__.py", "aml_retriever/evaluation/dataset.py",
    "aml_retriever/evaluation/governance_suite.py", "aml_retriever/evaluation/harness.py",
    "aml_retriever/evaluation/host_validation.py", "aml_retriever/evaluation/metrics.py",
    "aml_retriever/evaluation/fixtures/governance_v1.json",
    "aml_retriever/evaluation/baselines/legacy_v11_small.json",
    "flowgrid_memory/__init__.py", "flowgrid_memory/cli.py", "flowgrid_memory/conformance.py",
    "flowgrid_memory/mcp.py", "flowgrid_memory/rest.py", "flowgrid_memory/py.typed",
})


def runtime_digest_from_bytes(files: dict[str, bytes]) -> str:
    """Hash sorted relative paths, SHA256 content hashes and byte lengths."""
    if set(files) != RUNTIME_SOURCE_FILES:
        raise ValueError("runtime_file_set_mismatch")
    manifest = [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        for path, data in sorted(files.items())
    ]
    encoded = (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_source_digest(root: Path | None = None) -> tuple[list[str], str]:
    """Reject drift, links or unreviewed runtime inputs before returning a hash."""
    base = (root or Path(__file__).resolve().parent.parent).resolve(strict=True)
    discovered: set[str] = set()
    for package in ("aml_retriever", "flowgrid_memory"):
        directory = base / package
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("runtime_package_missing_or_link")
        for path in directory.rglob("*"):
            relative = path.relative_to(base).as_posix()
            if "__pycache__" in path.parts or relative == "aml_retriever/release_identity.json":
                continue
            if path.suffix not in {".py", ".json"} and path.name != "py.typed":
                continue
            if path.is_symlink() or base not in path.resolve().parents or not path.is_file():
                raise ValueError("runtime_file_missing_or_link")
            discovered.add(relative)
    if discovered != RUNTIME_SOURCE_FILES:
        raise ValueError("runtime_file_set_mismatch")
    files = {name: (base / name).read_bytes() for name in sorted(discovered)}
    return sorted(discovered), runtime_digest_from_bytes(files)
