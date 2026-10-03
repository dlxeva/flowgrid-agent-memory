#!/usr/bin/env python3
"""Fail closed on private material in the Git index, without printing its values.

This gate scans index blobs, including unchanged tracked files. It never treats
an ignored working-tree file or an empty index as proof of a safe publication.
The allowlist and fixture hashes require review when publication scope changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


MAX_BLOB_BYTES = 2 * 1024 * 1024
CONTROL_NAMES = frozenset(name.casefold() for name in (
    "PROJECT_MASTER.json", "ORCHESTRATION.json", "PROJECT.md", "FRAMING.md",
    "DECISIONS.md", "SNAPSHOT.md", "CONSTRAINTS.md", "PROGRESS.md",
    "GOAL_EVOLUTION.md", "ANCHORS.md", "RATIONALE_TRAIL.md", "LESSONS_LEARNED.md",
    "AGENTS.md", "MEMORY.md",
))
PRIVATE_DIRS = frozenset((
    ".git", ".flg", ".agents", ".codex", ".hermes", ".zcode", ".ssh", ".aws",
    ".venv", "venv", "__pycache__", "data", "runtime", "cache", "caches",
    "report", "reports", "eval_out", "backups", "scratch", "sessions",
    "conversations", "transcripts", "memory", "memories", "build", "dist",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
ROOT_FILES = frozenset((
    ".gitignore", ".dockerignore", ".gitattributes", "Dockerfile", "LICENSE", "NOTICE",
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.md",
    "pyproject.toml", "MANIFEST.in", "config.example.json",
    "config.product.example.json", ".env.example",
))
REVIEWED_DOCS = frozenset((
    "ACCEPTANCE_CRITERIA.md", "ACCEPTANCE_V0_1.md", "AML_DEPLOYMENT.md",
    "AML_SUBMISSION.md", "API_CONTRACT.md", "CONTAINER.md",
    "CONTEXT_COMPLETENESS.md", "CONTEXT_LIMITS.md", "DATA_LIFECYCLE.md",
    "EVAL.md", "EXTRACTOR_CONFORMANCE.md", "EXTRACTOR_CONTRACT.md", "INSTALL.md",
    "LOCAL_SECURITY.md", "MCP.md", "OWNER_REVIEW.md", "PRODUCT_BOUNDARY.md",
    "PUBLIC_API.md", "RELEASE.md", "RELEASE_NOTES.md", "REPOSITORY_GOVERNANCE.md", "REST_V1.md",
))
FIXTURE_HASHES = {
    "aml_retriever/evaluation/fixtures/governance_v1.json":
        "bb40dcc8a2acd4581e949fade95160b4efab8c3bc71e6bfdf833fa6fb05013a7",
    "aml_retriever/evaluation/baselines/legacy_v11_small.json":
        "f05708f8b6b1c4adbf9418540ef5f4580467aeafb912be57b927b608c15ef295",
}
DEPLOY_FILES = frozenset((
    "deploy/aml/flowgrid-memory-aml.service", "deploy/aml/install-wheel.sh",
    "deploy/aml/nginx.conf.example", "deploy/aml/service.env.example",
))
_DATABASE = re.compile(r"\.(?:db|sqlite|sqlite3|mdb|rdb)(?:[-.](?:wal|shm|journal|bak))?$", re.I)
_CREDENTIAL_FILE = re.compile(
    r"(?:\.(?:pem|key|p12|pfx|token|keystore)$|(?:^|[._-])(?:credentials?|secrets?)(?:[._-]|$))",
    re.I,
)
_LOCAL_PATH = re.compile(
    r"(?:/(?:Users|Volumes|home|root)/[^\s\"'<>\\/]+/|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"'<>\\/]+[\\/])"
)
_STRONG_SECRETS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:]+:[^\s/@]{8,}@"),
)
_LITERAL_CREDENTIAL = re.compile(
    r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|"
    r"memory[_-]?system[_-]?key|client[_-]?secret|password|credential)[\"']?"
    r"\s*(?:=|:)\s*[\"']([^\"'\r\n]{16,})[\"']"
)
_PLACEHOLDER_PREFIXES = (
    "synthetic-", "test-", "local-smoke-", "expected-", "wrong-", "example-",
    "your-", "change-me", "${", "<",
)


def path_issue(path: str) -> str | None:
    """Return a category for a disallowed source path, or None."""
    parts = path.split("/")
    if (not path or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        return "abnormal-path"
    folded = [part.casefold() for part in parts]
    basename = folded[-1]
    if basename in CONTROL_NAMES:
        return "private-control-ledger"
    if any(part in PRIVATE_DIRS or part.endswith(".egg-info") for part in folded[:-1]):
        return "private-runtime-directory"
    if _DATABASE.search(basename):
        return "database"
    if (basename == ".env" or basename.startswith(".env.")) and not basename.endswith(".example"):
        return "environment-secret-file"
    if _CREDENTIAL_FILE.search(basename):
        return "credential-file"
    if path in ROOT_FILES or path in FIXTURE_HASHES or path in DEPLOY_FILES:
        return None
    if len(parts) == 2 and parts[0] == "docs" and parts[1] in REVIEWED_DOCS:
        return None
    if parts[0] in {"aml_retriever", "flowgrid_memory", "tests", "scripts"}:
        if PurePosixPath(path).suffix == ".py":
            return None
        if parts[0] == "scripts" and len(parts) == 2 and PurePosixPath(path).suffix == ".sh":
            return None
        if path == "flowgrid_memory/py.typed":
            return None
    if path == ".github/CODEOWNERS":
        return None
    if len(parts) == 3 and parts[:2] == [".github", "workflows"] and PurePosixPath(path).suffix in {".yml", ".yaml"}:
        return None
    return "unreviewed-public-path"


def check_text(data: bytes) -> list[str]:
    """Scan portable text content; no matches or sensitive values are returned."""
    if len(data) > MAX_BLOB_BYTES:
        return ["oversized-blob"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["unknown-binary"]
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        return ["unknown-binary"]
    issues = []
    if _LOCAL_PATH.search(text):
        issues.append("local-machine-path")
    if any(pattern.search(text) for pattern in _STRONG_SECRETS):
        issues.append("strong-credential-pattern")
    for match in _LITERAL_CREDENTIAL.finditer(text):
        value = match.group(1)
        if not value.casefold().startswith(_PLACEHOLDER_PREFIXES):
            issues.append("literal-credential")
            break
    return issues


def check_blob(path: str, data: bytes, mode: str = "100644") -> list[str]:
    """Reusable source member policy, also used by archive publication gates."""
    issues = []
    if mode not in {"100644", "100755"}:
        issues.append("symlink" if mode == "120000" else "unsupported-file-mode")
    elif mode == "100755" and PurePosixPath(path).suffix not in {".py", ".sh"}:
        issues.append("unexpected-executable")
    issue = path_issue(path)
    if issue:
        issues.append(issue)
    issues.extend(check_text(data))
    if path in FIXTURE_HASHES and hashlib.sha256(data).hexdigest() != FIXTURE_HASHES[path]:
        issues.append("unreviewed-fixture-content")
    return sorted(set(issues))


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], input=data, capture_output=True, check=False,
    )
    if completed.returncode:
        raise RuntimeError("git-read-failed")
    return completed.stdout


def inspect_index(repo: Path) -> dict:
    """Inspect staged blobs and reject changes to the index during inspection."""
    violations = []
    checked = 0
    try:
        initial = _git(repo, "ls-files", "--stage", "-z")
        if not initial:
            violations.append({"path": "<index>", "category": "empty-index"})
        entries = []
        for entry in initial.split(b"\0"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = header.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="replace")
            if stage != "0":
                violations.append({"path": path, "category": "unmerged-index"})
                continue
            if mode not in {"100644", "100755"}:
                category = "symlink" if mode == "120000" else "unsupported-file-mode"
                violations.append({"path": path, "category": category})
                continue
            entries.append((mode, oid, path))
        if entries:
            sizes = _git(repo, "cat-file", "--batch-check", data=("\n".join(entry[1] for entry in entries) + "\n").encode("ascii"))
            lines = sizes.splitlines()
            if len(lines) != len(entries):
                raise RuntimeError("git-read-failed")
            for (mode, oid, path), line in zip(entries, lines):
                fields = line.split()
                if len(fields) != 3 or fields[0].decode("ascii") != oid or fields[1] != b"blob":
                    raise RuntimeError("git-read-failed")
                checked += 1
                if int(fields[2]) > MAX_BLOB_BYTES:
                    violations.append({"path": path, "category": "oversized-blob"})
                    continue
                blob = _git(repo, "cat-file", "blob", oid)
                for category in check_blob(path, blob, mode):
                    violations.append({"path": path, "category": category})
        if _git(repo, "ls-files", "--stage", "-z") != initial:
            violations.append({"path": "<index>", "category": "index-changed"})
    except (OSError, RuntimeError, ValueError):
        violations.append({"path": "<index>", "category": "git-read-failed"})
    return {
        "schema": "flowgrid.public-source-boundary/v1",
        "surface": "git-index",
        "checked_files": checked,
        "passed": checked > 0 and not violations,
        "violations": sorted(violations, key=lambda item: (item["path"], item["category"])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="JSON output is always enabled")
    args = parser.parse_args(argv)
    result = inspect_index(args.repo)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
