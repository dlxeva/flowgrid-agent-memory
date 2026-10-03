#!/usr/bin/env python3
"""Freeze public sources, rebuild twice, and install wheel/sdist offline.

Only the explicit source allowlist is copied. Existing host memory, project
state, credentials, databases, build output and unlisted files are never read.
The JSON report contains artifact names and hashes, never local source paths
or subprocess output. This base-package gate supplements the existing MCP,
container, SBOM and GitHub attestation release gates.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aml_retriever.release_integrity import RUNTIME_SOURCE_FILES, runtime_digest_from_bytes
from scripts.check_public_boundary import check_blob, check_text
sys.path.pop(0)

IDENTITY_SCHEMA = "flowgrid.agent-memory.release-identity/v1"
REPORT_SCHEMA = "flowgrid.agent-memory.release-verification/v1"
IDENTITY_PATH = "aml_retriever/release_identity.json"
DEFAULT_EPOCH = 1788307200

# Deliberately enumerated, rather than a recursive package/data wildcard.
# New runtime modules must be explicitly reviewed into this list.
SOURCE_ALLOWLIST = frozenset(
    {
        ".gitattributes", "pyproject.toml", "MANIFEST.in", "LICENSE", "NOTICE", "README.md", "README.zh-CN.md",
        "docs/INSTALL.md", "docs/LOCAL_SECURITY.md", "docs/DATA_LIFECYCLE.md",
        "docs/REST_V1.md", "docs/MCP.md", "docs/EVAL.md", "docs/API_CONTRACT.md",
        "docs/EXTRACTOR_CONTRACT.md", "docs/ACCEPTANCE_CRITERIA.md",
        "docs/ACCEPTANCE_V0_1.md", "docs/RELEASE_NOTES.md", "docs/RELEASE.md",
        "scripts/verify_release.py", "scripts/generate_release_evidence.py",
        "scripts/check_public_boundary.py",
        "scripts/smoke_wheel.py", "scripts/smoke_mcp.py", "scripts/run_tests.sh",
        "tests/__init__.py", "tests/test_release.py",
        "tests/test_aml_hosted.py", "tests/test_aml_cycle2_rehearsal.py",
        "aml_retriever/__init__.py", "aml_retriever/_version.py",
        "aml_retriever/access.py", "aml_retriever/aml_hosted.py", "aml_retriever/api.py",
        "aml_retriever/auth.py", "aml_retriever/cli.py", "aml_retriever/compiler.py",
        "aml_retriever/config.py", "aml_retriever/context.py",
        "aml_retriever/extraction.py", "aml_retriever/facade.py", "aml_retriever/features.py",
        "aml_retriever/governance.py", "aml_retriever/mcp_adapter.py",
        "aml_retriever/mcp_tools.py", "aml_retriever/migrations.py",
        "aml_retriever/model_extraction.py", "aml_retriever/owner_review.py",
        "aml_retriever/product_cli.py", "aml_retriever/rest_v1.py",
        "aml_retriever/release_integrity.py",
        "aml_retriever/retriever.py", "aml_retriever/server.py",
        "aml_retriever/store.py", "aml_retriever/views.py",
        "aml_retriever/evaluation/__init__.py", "aml_retriever/evaluation/dataset.py",
        "aml_retriever/evaluation/governance_suite.py", "aml_retriever/evaluation/harness.py",
        "aml_retriever/evaluation/host_validation.py", "aml_retriever/evaluation/metrics.py",
        "aml_retriever/evaluation/fixtures/governance_v1.json",
        "aml_retriever/evaluation/baselines/legacy_v11_small.json",
        "flowgrid_memory/__init__.py", "flowgrid_memory/cli.py",
        "flowgrid_memory/conformance.py", "flowgrid_memory/mcp.py",
        "flowgrid_memory/rest.py", "flowgrid_memory/py.typed",
        "docs/AML_DEPLOYMENT.md", "docs/AML_SUBMISSION.md",
        "deploy/aml/nginx.conf.example", "deploy/aml/flowgrid-memory-aml.service",
        "deploy/aml/service.env.example", "deploy/aml/install-wheel.sh", "scripts/smoke_aml_hosted.py",
        "scripts/rehearse_aml_cycle2.py",
    }
)
PACKAGE_ALLOWLIST = frozenset(
    name for name in SOURCE_ALLOWLIST
    if name.startswith(("aml_retriever/", "flowgrid_memory/"))
) | {IDENTITY_PATH}
EGG_INFO_FILES = frozenset(
    {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
     "requires.txt", "top_level.txt"}
)
DIST_INFO_FILES = frozenset(
    {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE", "licenses/NOTICE"}
)


class VerificationError(ValueError):
    """Safe error codes only; raw subprocess diagnostics stay out of reports."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def source_manifest(files: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": name, "sha256": sha256_bytes(value), "size_bytes": len(value)}
        for name, value in sorted(files.items())
    ]


def source_digest(files: dict[str, bytes]) -> str:
    return sha256_bytes(json_bytes(source_manifest(files)))


def _git(source: Path, *arguments: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments], capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def committed_source(source: Path, files: dict[str, bytes]) -> str | None:
    """A HEAD label is valid only when every frozen public input matches it."""
    revision = _git(source, "rev-parse", "HEAD")
    if revision is None:
        return None
    commit = revision.decode("ascii").strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        return None
    for name, value in files.items():
        if _git(source, "show", f"{commit}:{name}") != value:
            return None
    return commit


def _versions(files: dict[str, bytes]) -> tuple[str, str]:
    parsed = ast.parse(files["aml_retriever/_version.py"].decode("utf-8"))
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in parsed.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    return str(constants["PRODUCT_VERSION"]), str(constants["AML_ADAPTER_VERSION"])


def freeze_source(source: Path) -> tuple[dict[str, bytes], dict[str, object]]:
    source = source.resolve(strict=True)
    for package in ("aml_retriever", "flowgrid_memory"):
        unexpected = {
            path.relative_to(source).as_posix()
            for path in (source / package).rglob("*.py")
        } - SOURCE_ALLOWLIST
        if unexpected:
            raise VerificationError("unreviewed_runtime_module")
    files: dict[str, bytes] = {}
    for name in sorted(SOURCE_ALLOWLIST):
        path = source / name
        if path.is_symlink() or source not in path.resolve().parents:
            raise VerificationError("source_symlink_or_escape")
        if not path.is_file():
            raise VerificationError("required_public_source_missing")
        files[name] = path.read_bytes()
        if check_blob(name, files[name]):
            raise VerificationError("source_public_boundary_failed")
    product_version, adapter_version = _versions(files)
    commit = committed_source(source, files)
    identity: dict[str, object] = {
        "schema": IDENTITY_SCHEMA,
        "product_version": product_version,
        "adapter_version": adapter_version,
        "source_digest_sha256": source_digest(files),
        "source_commit": commit,
        "source_state": "committed" if commit else "uncommitted",
        "runtime_files": sorted(RUNTIME_SOURCE_FILES),
        "runtime_digest_sha256": runtime_digest_from_bytes({name: files[name] for name in RUNTIME_SOURCE_FILES}),
    }
    if check_text(json_bytes(identity)):
        raise VerificationError("identity_public_boundary_failed")
    return files, identity


def stage_source(files: dict[str, bytes], identity: dict[str, object], target: Path, epoch: int) -> None:
    for name, value in sorted({**files, IDENTITY_PATH: json_bytes(identity)}.items()):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o755 if path.suffix == ".sh" else 0o644)
        os.utime(path, (epoch, epoch))


def offline_env(epoch: int) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    result.update({
        "SOURCE_DATE_EPOCH": str(epoch), "PYTHONHASHSEED": "0",
        "PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return result


def run(command: list[str], *, cwd: Path, env: dict[str, str], code: str) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise VerificationError(code)
    return result.stdout


def backend_build(source: Path, output: Path, kind: str, env: dict[str, str]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    code = (
        "import setuptools.build_meta as backend; "
        f"backend.build_{kind}({str(output)!r})"
    )
    run([sys.executable, "-I", "-c", code], cwd=source, env=env, code=f"{kind}_build_failed")
    suffix = "*.whl" if kind == "wheel" else "*.tar.gz"
    artifacts = list(output.glob(suffix))
    if len(artifacts) != 1:
        raise VerificationError("unexpected_artifact_count")
    return artifacts[0]


def normalize_sdist(source: Path, target: Path, epoch: int) -> None:
    """Normalize gzip and all tar headers; setuptools does not fix gzip mtime."""
    entries: list[tuple[str, bytes | None, bool]] = []
    with tarfile.open(source, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                entries.append((member.name, None, False))
            elif member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise VerificationError("invalid_sdist_member")
                entries.append((member.name, stream.read(), bool(member.mode & 0o111)))
            else:
                raise VerificationError("sdist_link_or_special_file")
    with target.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=epoch, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, value, executable in sorted(entries):
                    info = tarfile.TarInfo(name)
                    info.mtime = epoch
                    info.mode = 0o755 if value is None or (executable and name.endswith(".sh")) else 0o644
                    info.type = tarfile.DIRTYPE if value is None else tarfile.REGTYPE
                    info.size = 0 if value is None else len(value)
                    archive.addfile(info, None if value is None else io.BytesIO(value))


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise VerificationError("archive_path_escape")
    return path


def verify_archive(artifact: Path, files: dict[str, bytes], identity: dict[str, object]) -> None:
    expected = {**files, IDENTITY_PATH: json_bytes(identity)}
    members: dict[str, bytes] = {}
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise VerificationError("duplicate_archive_member")
            dist_prefix = f"flowgrid_agent_memory-{identity.get('product_version')}.dist-info/"
            for member in archive.infolist():
                name = member.filename
                _safe_member(name)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise VerificationError("wheel_symlink")
                if name in PACKAGE_ALLOWLIST:
                    members[name] = archive.read(name)
                elif name.startswith(dist_prefix):
                    info_file = name[len(dist_prefix):]
                    if info_file not in DIST_INFO_FILES:
                        raise VerificationError("unreviewed_wheel_metadata")
                else:
                    raise VerificationError("unreviewed_wheel_member")
                if check_text(archive.read(name)):
                    raise VerificationError("wheel_content_boundary_failed")
        required = PACKAGE_ALLOWLIST
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            names = [member.name for member in archive.getmembers()]
            if len(names) != len(set(names)):
                raise VerificationError("duplicate_archive_member")
            roots = {_safe_member(member.name).parts[0] for member in archive.getmembers()}
            if len(roots) != 1:
                raise VerificationError("invalid_sdist_root")
            for member in archive.getmembers():
                path = _safe_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise VerificationError("sdist_link_or_special_file")
                if check_text(archive.extractfile(member).read()):
                    raise VerificationError("sdist_content_boundary_failed")
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                if relative in expected:
                    members[relative] = archive.extractfile(member).read()
                elif relative in {"PKG-INFO", "setup.cfg"}:
                    continue
                elif relative.startswith("flowgrid_agent_memory.egg-info/") and relative.split(".egg-info/", 1)[1] in EGG_INFO_FILES:
                    continue
                else:
                    raise VerificationError("unreviewed_sdist_member")
        required = expected.keys()
    if set(members) != set(required):
        raise VerificationError("archive_source_missing")
    if any(members[name] != expected[name] for name in members):
        raise VerificationError("archive_source_changed")


def extract_sdist(artifact: Path, target: Path) -> Path:
    target.mkdir()
    with tarfile.open(artifact, "r:gz") as archive:
        for member in archive.getmembers():
            path = _safe_member(member.name)
            destination = target.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
            else:
                raise VerificationError("sdist_link_or_special_file")
    roots = list(target.iterdir())
    if len(roots) != 1:
        raise VerificationError("invalid_sdist_root")
    return roots[0]


def provision_local_backend(python: Path, *, cwd: Path, env: dict[str, str]) -> str:
    """Provision the already-installed build tool without an index/download.

    A base wheel needs no build tool. A fresh sdist venv receives an exact copy
    of the caller's installed setuptools distribution, not system-site-packages.
    """
    site = Path(run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=cwd, env=env, code="venv_site_unavailable",
    ).strip())
    distribution = importlib.metadata.distribution("setuptools")
    # Some Python 3.11 ensurepip bundles seed an older setuptools. This venv
    # was just created by verify_install; remove that seed before provisioning
    # the exact reviewed local build distribution, including its metadata.
    for path in site.iterdir():
        if path.name in {"setuptools", "_distutils_hack", "pkg_resources", "distutils-precedence.pth"} or (
            path.name.startswith("setuptools-") and path.name.endswith(".dist-info")
        ):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    for entry in distribution.files or ():
        name = PurePosixPath(str(entry))
        if name.is_absolute() or ".." in name.parts or "__pycache__" in name.parts or name.suffix == ".pyc":
            continue
        if name.parts[0] not in {"setuptools", "_distutils_hack", "distutils-precedence.pth", "pkg_resources", f"setuptools-{distribution.version}.dist-info"}:
            continue
        origin = Path(distribution.locate_file(entry))
        if origin.is_file():
            destination = site.joinpath(*name.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
    observed = json.loads(run(
        [str(python), "-I", "-c", "import json,setuptools,importlib.metadata; print(json.dumps([setuptools.__version__,importlib.metadata.version('setuptools')]))"],
        cwd=cwd, env=env, code="local_build_backend_probe_failed",
    ))
    if observed != [distribution.version, distribution.version]:
        raise VerificationError("local_build_backend_version_mismatch")
    return distribution.version


INSTALL_PROBE = '''
import importlib.metadata as metadata, importlib.util, json, pathlib, sys
import aml_retriever, flowgrid_memory
from importlib.resources import files
from aml_retriever.evaluation.governance_suite import load_manifest, CANONICAL_MANIFEST_SHA256, DEFAULT_BASELINE_PATH
from aml_retriever.aml_hosted import runtime_identity
prefix = pathlib.Path(sys.prefix).resolve()
assert all(prefix in pathlib.Path(module.__file__).resolve().parents for module in (aml_retriever, flowgrid_memory))
distribution = metadata.distribution('flowgrid-agent-memory')
assert distribution.version == flowgrid_memory.__version__ == aml_retriever.PRODUCT_VERSION
requirements = distribution.requires or []
assert all('extra ==' in requirement and 'mcp' in requirement for requirement in requirements)
assert importlib.util.find_spec('mcp') is None
identity = json.loads(files('aml_retriever').joinpath('release_identity.json').read_text())
assert identity['schema'] == 'flowgrid.agent-memory.release-identity/v1'
assert identity['product_version'] == distribution.version
assert identity['adapter_version'] == aml_retriever.AML_ADAPTER_VERSION
assert runtime_identity()['source_digest_sha256'] == identity['source_digest_sha256']
assert load_manifest().sha256 == CANONICAL_MANIFEST_SHA256 and DEFAULT_BASELINE_PATH.is_file()
entries = {entry.name for entry in distribution.entry_points if entry.group == 'console_scripts'}
assert {'flowgrid-memory', 'flowgrid-memory-rest', 'flowgrid-memory-mcp', 'flowgrid-memory-aml'} <= entries
print(json.dumps({'version': distribution.version, 'adapter_version': aml_retriever.AML_ADAPTER_VERSION,
    'isolated_imports': True, 'base_runtime_dependencies': [], 'mcp_optional_and_absent': True,
    'governance_fixture_attested': True, 'release_identity': identity, 'console_entries': sorted(entries)}))
'''


def verify_install(artifact: Path, target: Path, env: dict[str, str], *, from_sdist: bool) -> dict[str, object]:
    outside = target / "outside"
    outside.mkdir(parents=True)
    venv = target / "venv"
    run([sys.executable, "-I", "-m", "venv", str(venv)], cwd=outside, env=env, code="fresh_venv_failed")
    binary = venv / ("Scripts" if os.name == "nt" else "bin")
    python = binary / ("python.exe" if os.name == "nt" else "python")
    backend = provision_local_backend(python, cwd=outside, env=env) if from_sdist else None
    run(
        [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", "--no-build-isolation", str(artifact)],
        cwd=outside, env=env, code="offline_install_failed",
    )
    info = json.loads(run([str(python), "-I", "-c", INSTALL_PROBE], cwd=outside, env=env, code="installed_probe_failed"))
    suffix = ".exe" if os.name == "nt" else ""
    cli = str(binary / f"flowgrid-memory{suffix}")
    version = run([cli, "--version"], cwd=outside, env=env, code="installed_cli_version_failed")
    if str(info["version"]) not in version:
        raise VerificationError("installed_cli_version_mismatch")
    doctor = json.loads(run([cli, "doctor", "--ephemeral"], cwd=outside, env=env, code="installed_doctor_failed"))
    demo = json.loads(run([cli, "demo", "--ephemeral"], cwd=outside, env=env, code="installed_governed_demo_failed"))
    if doctor.get("status") != "ok" or demo.get("status") != "ok" or not all(demo.get("checks", {}).values()):
        raise VerificationError("installed_governance_failed")
    for entry in ("flowgrid-memory-rest", "flowgrid-memory-aml"):
        run([str(binary / f"{entry}{suffix}"), "--help"], cwd=outside, env=env, code="installed_entrypoint_failed")
    run([str(python), "-I", "-m", "pip", "check"], cwd=outside, env=env, code="installed_pip_check_failed")
    info.update({"status": "passed", "doctor": "passed", "governed_demo_checks": demo["checks"], "offline": True})
    if backend is not None:
        info["build_backend_provision"] = {"method": "exact_local_distribution_copy", "setuptools": backend}
    return info


def verify_release(source: Path, output: Path, work_parent: Path, epoch: int, *, require_committed: bool = False) -> dict[str, object]:
    if epoch < 315532800:
        raise VerificationError("epoch_before_zip_1980")
    source = source.resolve(strict=True)
    output = output.resolve()
    work_parent = work_parent.resolve(strict=True)
    if output == source or source in output.parents or work_parent == source or source in work_parent.parents:
        raise VerificationError("release_work_must_be_outside_source")
    if output.exists() and any(output.iterdir()):
        raise VerificationError("output_directory_must_be_empty")
    backend_version = importlib.metadata.version("setuptools")
    if int(backend_version.split(".", 1)[0]) < 77:
        raise VerificationError("setuptools_77_required")
    files, identity = freeze_source(source)
    if require_committed and identity["source_state"] != "committed":
        raise VerificationError("committed_public_source_required")
    env = offline_env(epoch)
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA, "status": "failed", "source_identity": identity,
        "source_date_epoch": epoch,
        "build_tools": {"python": platform.python_version(), "setuptools": backend_version},
        "evidence_scope": "local_offline_base_package",
        "official_aml": "unverified", "network_used": False,
    }
    with tempfile.TemporaryDirectory(prefix="flowgrid-release-", dir=work_parent) as directory:
        work = Path(directory)
        built: list[tuple[Path, Path]] = []
        for index in range(2):
            staged = work / f"source-{index}"
            stage_source(files, identity, staged, epoch)
            wheel = backend_build(staged, work / f"wheel-{index}", "wheel", env)
            raw_sdist = backend_build(staged, work / f"sdist-raw-{index}", "sdist", env)
            normalized_dir = work / f"sdist-{index}"
            normalized_dir.mkdir()
            sdist = normalized_dir / raw_sdist.name
            normalize_sdist(raw_sdist, sdist, epoch)
            verify_archive(wheel, files, identity)
            verify_archive(sdist, files, identity)
            built.append((wheel, sdist))
        wheel, sdist = built[0]
        if sha256(wheel) != sha256(built[1][0]) or sha256(sdist) != sha256(built[1][1]):
            raise VerificationError("rebuild_not_byte_identical")
        unpacked = extract_sdist(sdist, work / "unpacked")
        rebuilt = backend_build(unpacked, work / "wheel-from-sdist", "wheel", env)
        if sha256(rebuilt) != sha256(wheel):
            raise VerificationError("sdist_wheel_not_byte_identical")
        report["gates"] = {
            "source_allowlist": "passed", "wheel_rebuild_byte_identical": "passed",
            "source_public_boundary": "passed",
            "sdist_rebuild_byte_identical": "passed", "sdist_to_wheel_byte_identical": "passed",
            "fresh_wheel": verify_install(wheel, work / "install-wheel", env, from_sdist=False),
            "fresh_sdist": verify_install(sdist, work / "install-sdist", env, from_sdist=True),
        }
        report["artifacts"] = []
        for artifact in (wheel, sdist):
            shutil.copyfile(artifact, output / artifact.name)
            report["artifacts"].append({"name": artifact.name, "sha256": sha256(artifact), "size_bytes": artifact.stat().st_size})
    report["status"] = "passed"
    if check_text(json_bytes(report)):
        raise VerificationError("report_public_boundary_failed")
    (output / "source-manifest.json").write_bytes(json_bytes({"source_identity": identity, "files": source_manifest(files)}))
    (output / "release-build-tools.json").write_bytes(json_bytes({"source_date_epoch": epoch, **report["build_tools"]}))
    (output / "release-verification.json").write_bytes(json_bytes(report))
    (output / "checksums.txt").write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in report["artifacts"]), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", "--output-dir", type=Path, required=True, help="empty artifact directory outside the source")
    parser.add_argument("--work-parent", type=Path, required=True, help="existing disposable-work parent outside the source")
    parser.add_argument("--source-date-epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH)))
    parser.add_argument("--require-committed", action="store_true", help="require every frozen public input to match HEAD")
    args = parser.parse_args(argv)
    try:
        result = verify_release(args.source, args.output, args.work_parent, args.source_date_epoch, require_committed=args.require_committed)
    except (VerificationError, OSError, ValueError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError) as exc:
        error = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "failed", "error": error}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
