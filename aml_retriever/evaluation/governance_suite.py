"""Manifest-driven governance and AML proxy evaluation.

The suite deliberately keeps three evidence classes separate:

``core_invariant``
    Non-compensable FlowGrid memory semantics.  Every declared gate must pass.
``local_e2e_proxy``
    Deterministic, synthetic, local proxies.  They are useful regression
    evidence, but are not official AML scores and are never averaged with the
    core gates.
``official_aml``
    An independent claim gate.  In v1 it is unverified unless a separately
    verifiable official platform artifact exists.  Local green tests cannot
    open it.

The implementation uses only the Python standard library and public product
service methods.  Temporary SQLite databases are disposable evaluation input;
no product source file or persistent project state is mutated.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "flowgrid.agent-memory.governance-eval.manifest/v1"
RESULT_SCHEMA = "flowgrid.agent-memory.governance-eval.result/v1"
BASELINE_SCHEMA = "flowgrid.agent-memory.legacy-baseline/v1"
SUITE_NAME = "governance-v1"
CANONICAL_MANIFEST_SHA256 = (
    "714b2ded85115df473ff51a5318c1b8116b5354951e802a283b3189aa4ff445a"
)

LEGACY_BASELINE_COMMIT = "cdae7dbd38d73eda33793b30017559bdfb75eff5"
LEGACY_BASELINE_TREE = "b893323a1e5f8e0860923b48a87d365a6100fe08"
LEGACY_BASELINE_DATASET_SHA256 = (
    "245752a7f0f076207de85ef17b5c6af9fb07937893b9c073968c55876f523098"
)
LEGACY_BASELINE_CANONICAL_SHA256 = (
    "ef6e515648d29eee3dc047fa70598c7f558c7d8d1661d84f027ea01381ba4dbf"
)
LEGACY_BASELINE_STAGE = "L9_guarded_supersession"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
DEFAULT_MANIFEST_PATH = PACKAGE_DIR / "fixtures" / "governance_v1.json"
DEFAULT_BASELINE_PATH = PACKAGE_DIR / "baselines" / "legacy_v11_small.json"

LAYERS = ("core_invariant", "local_e2e_proxy", "official_aml")
CORE_CAPABILITIES = frozenset(
    {
        "FLG.raw_immutable",
        "FLG.authority_gate",
        "FLG.traceability",
        "D1",
        "D3",
        "E1",
        "E2",
        "H1",
        "H2",
    }
)
LOCAL_PROXY_CAPABILITIES = frozenset(
    {
        "proxy.A2",
        "proxy.B2",
        "proxy.B3",
        "proxy.G1",
        "proxy.G3",
        "proxy.G5",
        "legacy.v11_floor",
    }
)
OFFICIAL_CAPABILITIES = frozenset({"official.artifact"})
MANDATORY_CAPABILITIES = CORE_CAPABILITIES | LOCAL_PROXY_CAPABILITIES

MANDATORY_OPERATOR_BY_CAPABILITY = {
    "FLG.raw_immutable": "raw_immutable",
    "FLG.authority_gate": "authority_gate",
    "FLG.traceability": "traceability",
    "D1": "current_replacement",
    "D3": "deletion_suppression",
    "E1": "direct_preference",
    "E2": "anti_stereotype",
    "H1": "unknown_and_owner_gate",
    "H2": "minimal_disclosure",
    "proxy.A2": "proxy_attribution",
    "proxy.B2": "proxy_three_segment_chain",
    "proxy.B3": "proxy_evidence_path",
    "proxy.G1": "proxy_rule_evidence",
    "proxy.G3": "proxy_workflow_evidence",
    "proxy.G5": "proxy_wire_contract",
    "legacy.v11_floor": "legacy_floor",
    "official.artifact": "official_unverified",
}

MANDATORY_FIXTURE_BY_CAPABILITY = {
    "FLG.raw_immutable": "raw_immutable",
    "FLG.authority_gate": "authority_gate",
    "FLG.traceability": "traceability",
    "D1": "current_replacement",
    "D3": "deletion_suppression",
    "E1": "direct_preference",
    "E2": "anti_stereotype",
    "H1": "unknown_and_owner_gate",
    "H2": "minimal_disclosure",
    "proxy.A2": "proxy_attribution",
    "proxy.B2": "proxy_three_segment_chain",
    "proxy.B3": "proxy_evidence_path",
    "proxy.G1": "proxy_rule_evidence",
    "proxy.G3": "proxy_workflow_evidence",
    "proxy.G5": "proxy_wire_contract",
    "legacy.v11_floor": "legacy_floor",
    "official.artifact": "official_artifact",
}

MANDATORY_SURFACE_CONTRACTS = {
    "governed_core": {
        "operator": "python_surface",
        "module": "aml_retriever.api",
        "attributes": [
            "MemoryService.search_governed",
            "MemoryService.propose_memory",
            "MemoryService.transition_memory",
        ],
    },
    "context_compiler": {
        "operator": "python_surface",
        "module": "aml_retriever.api",
        "attributes": [
            "MemoryService.compile_context",
            "AccessContext",
            "DisclosurePolicy",
            "ContextPack",
        ],
    },
    "official_add_search": {
        "operator": "python_surface",
        "module": "aml_retriever.api",
        "attributes": [
            "MemoryService.official_add",
            "MemoryService.official_search",
        ],
    },
    "governed_rest": {
        "operator": "python_surface",
        "module": "aml_retriever.rest_v1",
        "attributes": ["GovernedRestAdapter"],
    },
    "mcp": {
        "operator": "python_surface",
        "module": "aml_retriever.mcp_adapter",
        "attributes": ["create_mcp_server"],
    },
}

PROFILE_MINIMUM_SURFACES = {
    "quick": frozenset({"governed_core", "context_compiler", "official_add_search"}),
    "product": frozenset(
        {
            "governed_core",
            "context_compiler",
            "official_add_search",
            "governed_rest",
            "mcp",
        }
    ),
}

CRITICAL_FAILURE_CODES = frozenset(
    {
        "terminal_leak",
        "unauthorized_leak",
        "unknown_missing",
        "owner_gate_missing",
        "source_chain_missing",
        "budget_overrun",
        "legacy_floor_regression",
    }
)


class EvaluationError(ValueError):
    """Fail-closed manifest, probe, or baseline error."""


@dataclass(frozen=True)
class LoadedManifest:
    data: dict[str, Any]
    sha256: str
    label: str
    attested: bool


@dataclass(frozen=True)
class EvaluationRuntime:
    repo_root: Path
    manifest_path: Path


class _ExactCharacterCounter:
    is_exact = True

    @staticmethod
    def count_tokens(text: str) -> int:
        return len(text)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON artifact must be an object: {path.name}")
    return value


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EvaluationError(f"{field} must be an array of non-empty strings")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise EvaluationError(f"{field} must not contain duplicates")
    return normalized


def _validate_manifest_structure(manifest: Mapping[str, Any]) -> None:
    """Validate manifest structure and mandatory bindings.

    This structural pass is deliberately separate from canonical attestation
    so ``run_governance_suite`` can identify a well-formed custom manifest and
    return an explicit non-attestable artifact instead of mislabelling it as a
    standard governance-v1 result.
    """

    if not isinstance(manifest, Mapping):
        raise EvaluationError("manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise EvaluationError("unsupported manifest schema")
    if manifest.get("suite") != SUITE_NAME:
        raise EvaluationError("unsupported evaluation suite")
    profiles = manifest.get("profiles")
    surfaces = manifest.get("surfaces")
    capabilities = manifest.get("capabilities")
    fixtures = manifest.get("fixtures")
    for name, value in (
        ("profiles", profiles),
        ("surfaces", surfaces),
        ("capabilities", capabilities),
        ("fixtures", fixtures),
    ):
        if not isinstance(value, Mapping) or not value:
            raise EvaluationError(f"manifest.{name} must be a non-empty object")

    declared_capabilities = set(capabilities)
    missing = (MANDATORY_CAPABILITIES | OFFICIAL_CAPABILITIES) - declared_capabilities
    if missing:
        raise EvaluationError(
            "manifest is missing mandatory capabilities: " + ", ".join(sorted(missing))
        )

    for capability_id, specification in capabilities.items():
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise EvaluationError("capability ids must be non-empty strings")
        if not isinstance(specification, Mapping):
            raise EvaluationError(f"capability {capability_id} must be an object")
        layer = specification.get("layer")
        if layer not in LAYERS:
            raise EvaluationError(f"capability {capability_id} has an unknown layer")
        if capability_id in CORE_CAPABILITIES and layer != "core_invariant":
            raise EvaluationError(f"capability {capability_id} must remain a core invariant")
        if capability_id in LOCAL_PROXY_CAPABILITIES and layer != "local_e2e_proxy":
            raise EvaluationError(f"capability {capability_id} must remain a local proxy")
        if capability_id in OFFICIAL_CAPABILITIES and layer != "official_aml":
            raise EvaluationError(f"capability {capability_id} must remain an official gate")
        operator = specification.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise EvaluationError(f"capability {capability_id} has no operator")
        expected_operator = MANDATORY_OPERATOR_BY_CAPABILITY.get(capability_id)
        if expected_operator is not None and operator != expected_operator:
            raise EvaluationError(
                f"capability {capability_id} must use operator {expected_operator}"
            )
        fixture_name = specification.get("fixture")
        if not isinstance(fixture_name, str) or fixture_name not in fixtures:
            raise EvaluationError(f"capability {capability_id} has a missing fixture")
        expected_fixture = MANDATORY_FIXTURE_BY_CAPABILITY.get(capability_id)
        if expected_fixture is not None and fixture_name != expected_fixture:
            raise EvaluationError(
                f"capability {capability_id} must use fixture {expected_fixture}"
            )

    for surface_id, specification in surfaces.items():
        if not isinstance(surface_id, str) or not surface_id.strip():
            raise EvaluationError("surface ids must be non-empty strings")
        if not isinstance(specification, Mapping):
            raise EvaluationError(f"surface {surface_id} must be an object")
        operator = specification.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise EvaluationError(f"surface {surface_id} has no operator")
        contract = MANDATORY_SURFACE_CONTRACTS.get(surface_id)
        if contract is not None:
            if operator != contract["operator"]:
                raise EvaluationError(f"surface {surface_id} has an invalid operator binding")
            if specification.get("module") != contract["module"]:
                raise EvaluationError(f"surface {surface_id} has an invalid module binding")
            if specification.get("attributes") != contract["attributes"]:
                raise EvaluationError(f"surface {surface_id} has an invalid attribute contract")

    for fixture_name, fixture in fixtures.items():
        if not isinstance(fixture, Mapping):
            raise EvaluationError(f"fixture {fixture_name} must be an object")
        alias = fixture.get("alias")
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", alias):
            raise EvaluationError(f"fixture {fixture_name} must declare a stable alias")
    b2_fixture = fixtures.get("proxy_three_segment_chain")
    if not isinstance(b2_fixture, Mapping) or not isinstance(b2_fixture.get("segments"), list):
        raise EvaluationError("B2 must declare a three-segment evidence chain")
    if len(b2_fixture["segments"]) != 3:
        raise EvaluationError("B2 must contain exactly three evidence segments")

    for profile_name, minimum_surfaces in PROFILE_MINIMUM_SURFACES.items():
        profile = profiles.get(profile_name)
        if not isinstance(profile, Mapping):
            raise EvaluationError(f"manifest profile {profile_name} is required")
        required_surfaces = set(
            _string_list(
                profile.get("required_surfaces"),
                field=f"profiles.{profile_name}.required_surfaces",
            )
        )
        required_capabilities = set(
            _string_list(
                profile.get("required_capabilities"),
                field=f"profiles.{profile_name}.required_capabilities",
            )
        )
        observed_capabilities = set(
            _string_list(
                profile.get("observed_capabilities"),
                field=f"profiles.{profile_name}.observed_capabilities",
            )
        )
        unknown_surfaces = required_surfaces - set(surfaces)
        unknown_capabilities = (required_capabilities | observed_capabilities) - set(capabilities)
        if unknown_surfaces:
            raise EvaluationError(
                f"profile {profile_name} references unknown surfaces: "
                + ", ".join(sorted(unknown_surfaces))
            )
        if unknown_capabilities:
            raise EvaluationError(
                f"profile {profile_name} references unknown capabilities: "
                + ", ".join(sorted(unknown_capabilities))
            )
        if not minimum_surfaces <= required_surfaces:
            raise EvaluationError(f"profile {profile_name} omits a required product surface")
        if not MANDATORY_CAPABILITIES <= required_capabilities:
            raise EvaluationError(f"profile {profile_name} omits a mandatory local gate")
        if not OFFICIAL_CAPABILITIES <= observed_capabilities:
            raise EvaluationError(f"profile {profile_name} omits the official claim gate")


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the reviewed, attested governance-v1 manifest.

    Capability/operator names are not enough to preserve a probe: weakening a
    fixture can make the same operator trivially green.  Standard
    ``governance-v1`` therefore binds the complete canonical JSON content to a
    reviewed SHA-256.
    """

    _validate_manifest_structure(manifest)
    if canonical_sha256(manifest) != CANONICAL_MANIFEST_SHA256:
        raise EvaluationError(
            "manifest content does not match the reviewed governance-v1 canonical SHA-256"
        )


def _load_manifest(
    path: Path,
    *,
    require_canonical: bool,
) -> LoadedManifest:
    data = _read_json(path)
    _validate_manifest_structure(data)
    digest = canonical_sha256(data)
    attested = digest == CANONICAL_MANIFEST_SHA256
    if require_canonical and not attested:
        raise EvaluationError(
            "manifest content does not match the reviewed governance-v1 canonical SHA-256"
        )
    return LoadedManifest(
        data=data,
        sha256=digest,
        label=path.name,
        attested=attested,
    )


def load_manifest(path: str | os.PathLike[str] | None = None) -> LoadedManifest:
    manifest_path = Path(path).resolve() if path is not None else DEFAULT_MANIFEST_PATH
    return _load_manifest(manifest_path, require_canonical=True)


def _git(*args: str, repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def code_state(repo_root: Path | None = None) -> dict[str, Any]:
    root = (repo_root or REPO_ROOT).resolve()
    try:
        commit = _git("rev-parse", "HEAD", repo_root=root)
        tree = _git("rev-parse", "HEAD^{tree}", repo_root=root)
        status = _git("status", "--porcelain", "--untracked-files=all", repo_root=root)
        return {
            "revision": commit,
            "tree": tree,
            "dirty": bool(status),
        }
    except (OSError, subprocess.SubprocessError):
        return {"revision": "unknown", "tree": "unknown", "dirty": None}


def assert_clean_baseline_source(
    *,
    expected_commit: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Check checkout provenance for a caller-managed freeze workflow.

    This helper is a precondition check, not an unbypassable freeze command: it
    does not create the clean snapshot, run the evaluator, or write a golden.
    The reviewed baseline remains protected separately by its fixed content,
    commit, tree, and dataset hashes.
    """

    state = code_state(repo_root)
    if state["dirty"] is not False:
        raise EvaluationError("baseline freeze requires a clean worktree")
    if state["revision"] != expected_commit:
        raise EvaluationError("baseline freeze requires the exact declared commit")
    return state


class _Normalizer:
    _id_pattern = re.compile(r"\b(raw|mem|tr|m)_[0-9a-f]{8,64}\b")
    _uuid_pattern = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )
    _timestamp_pattern = re.compile(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b"
    )
    _temp_pattern = re.compile(
        r"(?:/private)?/(?:tmp|var/folders)/[^\s\"']+"
    )

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def text(self, value: str) -> str:
        def replace_id(match: re.Match[str]) -> str:
            token = match.group(0)
            if token not in self.aliases:
                prefix = match.group(1)
                self.counts[prefix] = self.counts.get(prefix, 0) + 1
                self.aliases[token] = f"<{prefix}-alias-{self.counts[prefix]:03d}>"
            return self.aliases[token]

        value = self._id_pattern.sub(replace_id, value)
        value = self._uuid_pattern.sub("<uuid-alias>", value)
        value = self._timestamp_pattern.sub("<wall-clock>", value)
        return self._temp_pattern.sub("<temp-path>", value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        return value


def normalize_dynamic(value: Any) -> Any:
    """Replace UUID-like ids, wall clocks, and temporary paths deterministically."""

    return _Normalizer().value(value)


def _metric(numerator: int | float, denominator: int | float) -> dict[str, float | int]:
    if isinstance(denominator, bool) or not isinstance(denominator, (int, float)):
        raise EvaluationError("metric denominator must be numeric")
    if not math.isfinite(float(denominator)) or float(denominator) <= 0:
        raise EvaluationError("metric denominator must be greater than zero")
    if isinstance(numerator, bool) or not isinstance(numerator, (int, float)):
        raise EvaluationError("metric numerator must be numeric")
    if not math.isfinite(float(numerator)):
        raise EvaluationError("metric numerator must be finite")
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": float(numerator) / float(denominator),
    }


def _checks_result(
    checks: Mapping[str, bool],
    *,
    observations: Mapping[str, Any] | None = None,
    failure_codes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized = {str(name): bool(value) for name, value in sorted(checks.items())}
    failures = []
    for name, passed in normalized.items():
        if not passed:
            failures.append(
                {
                    "code": (failure_codes or {}).get(name, "invariant_failed"),
                    "check": name,
                    "message": f"required check failed: {name}",
                }
            )
    return {
        "status": "pass" if not failures else "fail",
        "metrics": {name: _metric(int(passed), 1) for name, passed in normalized.items()},
        "observations": {
            "checks": normalized,
            **dict(observations or {}),
        },
        "failures": failures,
    }


def _validate_probe_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError("operator result must be an object")
    required = {"status", "metrics", "observations", "failures"}
    if not required <= set(value):
        raise EvaluationError("operator result has the wrong schema")
    status = value.get("status")
    if status not in {"pass", "fail", "unverified"}:
        raise EvaluationError("operator result has an unsupported status")
    metrics = value.get("metrics")
    observations = value.get("observations")
    failures = value.get("failures")
    if not isinstance(metrics, Mapping) or not metrics:
        raise EvaluationError("operator result metrics must be non-empty")
    if not isinstance(observations, Mapping):
        raise EvaluationError("operator result observations must be an object")
    if not isinstance(failures, list) or any(not isinstance(item, Mapping) for item in failures):
        raise EvaluationError("operator result failures must be an array of objects")
    for metric_name, metric in metrics.items():
        if not isinstance(metric_name, str) or not metric_name:
            raise EvaluationError("metric names must be non-empty strings")
        if not isinstance(metric, Mapping):
            raise EvaluationError("metric values must be objects")
        if not {"numerator", "denominator", "value"} <= set(metric):
            raise EvaluationError("metric value has the wrong schema")
        numerator = metric["numerator"]
        denominator = metric["denominator"]
        measured = metric["value"]
        if any(isinstance(item, bool) for item in (numerator, denominator, measured)):
            raise EvaluationError("boolean metric values are invalid")
        if not all(isinstance(item, (int, float)) for item in (numerator, denominator, measured)):
            raise EvaluationError("metric values must be numeric")
        if not all(math.isfinite(float(item)) for item in (numerator, denominator, measured)):
            raise EvaluationError("metric values must be finite")
        if float(denominator) <= 0:
            raise EvaluationError("empty metric denominator is a hard failure")
        expected = float(numerator) / float(denominator)
        if not math.isclose(float(measured), expected, rel_tol=0.0, abs_tol=1e-12):
            raise EvaluationError("metric value does not match numerator/denominator")
    if status == "pass" and failures:
        raise EvaluationError("passing operator result cannot contain failures")
    if status == "fail" and not failures:
        raise EvaluationError("failing operator result must contain a failure")
    return dict(value)


@contextmanager
def _temporary_service():
    from ..api import MemoryService
    from ..config import RetrieverConfig

    descriptor, db_path = tempfile.mkstemp(prefix="governance-eval-", suffix=".db")
    os.close(descriptor)
    service = MemoryService(RetrieverConfig(db_path=db_path))
    try:
        yield service, db_path
    finally:
        service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


def _add_event(
    service: Any,
    fixture: Mapping[str, Any],
    *,
    content: str | None = None,
    user: str | None = None,
    role: str = "user",
    timestamp: int | None = None,
    suffix: str = "1",
) -> Any:
    user_id = user or str(fixture["user"])
    actual_content = content if content is not None else str(fixture["content"])
    actual_timestamp = int(timestamp if timestamp is not None else fixture["timestamp"])
    added = service.add(
        request_id=f"eval-{fixture['alias']}-{suffix}",
        user_id=user_id,
        session_id=f"eval-{fixture['alias']}-session-{suffix}",
        messages=[
            {
                "role": role,
                "content": actual_content,
                "timestamp": actual_timestamp,
            }
        ],
    )
    message_id = added.message_ids[0]
    return next(
        event
        for event in service.list_raw_events(user_id)
        if event.source_message_id == message_id
    )


def _propose(
    service: Any,
    event: Any,
    *,
    memory_key: str,
    content: str,
    memory_type: str = "fact",
    subject: str | None = None,
    authority: str | None = None,
    scope: Mapping[str, str] | None = None,
    supersedes_record_id: str | None = None,
) -> Any:
    return service.propose_memory(
        user_id=event.user_id,
        memory_key=memory_key,
        content=content,
        source_event_ids=[event.id],
        memory_type=memory_type,
        subject=subject,
        authority=authority or event.authority,
        scope=dict(scope or {}),
        supersedes_record_id=supersedes_record_id,
        created_by="evaluation-extractor",
    )


def _transition(service: Any, record: Any, target: str, *, reason: str) -> Any:
    return service.transition_memory(
        record_id=record.id,
        target_status=target,
        actor="evaluation-owner",
        actor_authority="owner",
        reason=reason,
        user_id=record.user_id,
    )


def _official_add_one(
    service: Any,
    *,
    alias: str,
    user: str,
    session: str,
    content: str,
    timestamp: int,
) -> dict[str, Any]:
    return service.official_add(
        {
            "request_id": f"{alias}-request",
            "user_id": user,
            "session_id": session,
            "messages": [{"role": "user", "content": content, "timestamp": timestamp}],
        }
    )


def _op_python_surface(specification: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    module_name = specification.get("module")
    attributes = specification.get("attributes")
    if not isinstance(module_name, str) or not module_name:
        raise EvaluationError("surface module must be a non-empty string")
    names = _string_list(attributes, field="surface.attributes")
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return _checks_result(
            {name: False for name in names},
            observations={"module": module_name, "module_imported": False},
        )
    checks: dict[str, bool] = {}
    for name in names:
        current: Any = module
        try:
            for part in name.split("."):
                current = getattr(current, part)
            checks[name] = callable(current)
        except AttributeError:
            checks[name] = False
    return _checks_result(
        checks,
        observations={"module": module_name, "module_imported": True},
    )


def _op_raw_immutable(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, db_path):
        event = _add_event(service, fixture)
        columns: set[str] = set()
        raw_blocked = False
        message_blocked = False
        connection = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_events)")}
            try:
                connection.execute(
                    "UPDATE raw_events SET authority='agent' WHERE id=?", (event.id,)
                )
                connection.commit()
            except sqlite3.DatabaseError:
                raw_blocked = True
                connection.rollback()
            try:
                connection.execute(
                    "UPDATE messages SET content='tampered' WHERE id=?",
                    (event.source_message_id,),
                )
                connection.commit()
            except sqlite3.DatabaseError:
                message_blocked = True
                connection.rollback()
        finally:
            connection.close()
        reread = next(item for item in service.list_raw_events(event.user_id) if item.id == event.id)
        checks = {
            "raw_body_not_duplicated": "content" not in columns and "role" not in columns,
            "raw_locator_resolves": reread.content == fixture["content"],
            "raw_row_update_blocked": raw_blocked,
            "source_message_update_blocked": message_blocked,
        }
        return _checks_result(checks, observations={"fixture_alias": fixture["alias"]})


def _op_authority_gate(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..governance import GovernanceError

    with _temporary_service() as (service, _db_path):
        event = _add_event(service, fixture)
        record = _propose(
            service,
            event,
            memory_key=str(fixture["memory_key"]),
            content=str(fixture["content"]),
        )
        agent_blocked = False
        try:
            service.transition_memory(
                record_id=record.id,
                target_status="confirmed",
                actor="evaluation-agent",
                actor_authority="agent",
                reason="agent cannot promote itself",
                user_id=record.user_id,
            )
        except GovernanceError:
            agent_blocked = True
        current = service.search_governed(
            user_id=record.user_id, memory_key=str(fixture["memory_key"])
        )
        rendered = canonical_json(current.to_dict())
        checks = {
            "agent_confirmation_blocked": agent_blocked,
            "candidate_not_current": current.records == [],
            "unknown_is_explicit": current.current_status == "unknown" and current.abstain,
            "owner_gate_preserved": current.owner_gate_required,
            "candidate_body_withheld": str(fixture["content"]) not in rendered,
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={
                "candidate_body_withheld": "unauthorized_leak",
                "unknown_is_explicit": "unknown_missing",
                "owner_gate_preserved": "owner_gate_missing",
            },
        )


def _op_traceability(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, _db_path):
        event = _add_event(service, fixture)
        record = _propose(
            service,
            event,
            memory_key=str(fixture["memory_key"]),
            content=str(fixture["content"]),
        )
        confirmed = _transition(service, record, "confirmed", reason="verified trace fixture")
        current = service.search_governed(
            user_id=event.user_id, memory_key=str(fixture["memory_key"])
        )
        audit = service.search_governed(
            user_id=event.user_id, memory_key=str(fixture["memory_key"]), mode="audit"
        )
        selected = current.records[0] if len(current.records) == 1 else None
        audit_statuses = [item.to_status for item in audit.state_events]
        checks = {
            "current_record_selected": selected is not None and selected.id == confirmed.id,
            "record_points_to_raw_event": selected is not None
            and event.id in selected.source_event_ids,
            "opaque_source_locator_present": selected is not None
            and f"raw_events:{event.id}" in selected.source_locator,
            "selection_reason_present": selected is not None and bool(selected.why_selected),
            "raw_evidence_resolves": any(
                item.id == event.id and item.source_message_id == event.source_message_id
                for item in audit.raw_events
            ),
            "state_chain_complete": audit_statuses == ["candidate", "confirmed"],
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"], "state_chain_length": len(audit_statuses)},
            failure_codes={
                "record_points_to_raw_event": "source_chain_missing",
                "opaque_source_locator_present": "source_chain_missing",
                "raw_evidence_resolves": "source_chain_missing",
                "state_chain_complete": "source_chain_missing",
            },
        )


def _op_current_replacement(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, _db_path):
        old_event = _add_event(
            service,
            fixture,
            content=str(fixture["old_content"]),
            timestamp=int(fixture["old_timestamp"]),
            suffix="old",
        )
        old = _transition(
            service,
            _propose(
                service,
                old_event,
                memory_key=str(fixture["memory_key"]),
                content=str(fixture["old_content"]),
            ),
            "confirmed",
            reason="old value confirmed",
        )
        new_event = _add_event(
            service,
            fixture,
            content=str(fixture["new_content"]),
            timestamp=int(fixture["new_timestamp"]),
            suffix="new",
        )
        new = _propose(
            service,
            new_event,
            memory_key=str(fixture["memory_key"]),
            content=str(fixture["new_content"]),
            supersedes_record_id=old.id,
        )
        new = _transition(service, new, "confirmed", reason="new value replaces old value")
        current = service.search_governed(
            user_id=str(fixture["user"]), memory_key=str(fixture["memory_key"])
        )
        audit = service.search_governed(
            user_id=str(fixture["user"]), memory_key=str(fixture["memory_key"]), mode="audit"
        )
        current_json = canonical_json(current.to_dict())
        audit_by_content = {item.content: item.current_status for item in audit.records}
        checks = {
            "one_current_value": len(current.records) == 1,
            "new_value_is_current": len(current.records) == 1
            and current.records[0].id == new.id
            and current.records[0].content == fixture["new_content"],
            "old_value_not_resurrected": str(fixture["old_content"]) not in current_json,
            "old_value_audited_as_superseded": audit_by_content.get(str(fixture["old_content"]))
            == "superseded",
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={"old_value_not_resurrected": "terminal_leak"},
        )


def _op_deletion_suppression(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..access import PERMISSION_READ, AccessContext

    with _temporary_service() as (service, _db_path):
        event = _add_event(service, fixture)
        record = _transition(
            service,
            _propose(
                service,
                event,
                memory_key=str(fixture["memory_key"]),
                content=str(fixture["content"]),
            ),
            "confirmed",
            reason="confirm before deletion",
        )
        _transition(service, record, "deleted", reason="owner deleted derived memory")
        current = service.search_governed(
            user_id=str(fixture["user"]), memory_key=str(fixture["memory_key"])
        )
        access = AccessContext(
            principal_id="evaluation-service",
            authority="service",
            scopes={},
            permissions=frozenset({PERMISSION_READ}),
            purpose=None,
            allowed_users=frozenset({str(fixture["user"])}),
        )
        pack = service.compile_context(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["memory_key"]),
            access_context=access,
        )
        rejected_event = _add_event(
            service,
            fixture,
            content=str(fixture["rejected_content"]),
            timestamp=int(fixture["rejected_timestamp"]),
            suffix="rejected",
        )
        rejected = _transition(
            service,
            _propose(
                service,
                rejected_event,
                memory_key=str(fixture["rejected_key"]),
                content=str(fixture["rejected_content"]),
            ),
            "confirmed",
            reason="confirm before rejection",
        )
        _transition(service, rejected, "rejected", reason="owner rejected derived memory")
        rejected_current = service.search_governed(
            user_id=str(fixture["user"]), memory_key=str(fixture["rejected_key"])
        )
        rejected_pack = service.compile_context(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["rejected_key"]),
            access_context=access,
        )
        current_json = canonical_json(current.to_dict())
        context_json = pack.to_json()
        rejected_current_json = canonical_json(rejected_current.to_dict())
        rejected_context_json = rejected_pack.to_json()
        checks = {
            "deleted_not_current": current.records == [],
            "deleted_current_is_unknown": current.current_status == "unknown" and current.abstain,
            "deleted_body_not_in_current": str(fixture["content"]) not in current_json,
            "deleted_body_not_in_context": str(fixture["content"]) not in context_json,
            "context_abstains": pack.abstain and pack.status == "unknown",
            "rejected_not_current": rejected_current.records == [],
            "rejected_current_is_unknown": rejected_current.current_status == "unknown"
            and rejected_current.abstain,
            "rejected_body_not_in_current": str(fixture["rejected_content"])
            not in rejected_current_json,
            "rejected_body_not_in_context": str(fixture["rejected_content"])
            not in rejected_context_json,
            "rejected_context_abstains": rejected_pack.abstain
            and rejected_pack.status == "unknown",
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={
                "deleted_body_not_in_current": "terminal_leak",
                "deleted_body_not_in_context": "terminal_leak",
                "deleted_current_is_unknown": "unknown_missing",
                "rejected_body_not_in_current": "terminal_leak",
                "rejected_body_not_in_context": "terminal_leak",
                "rejected_current_is_unknown": "unknown_missing",
            },
        )


def _op_direct_preference(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..access import PERMISSION_READ, AccessContext

    with _temporary_service() as (service, _db_path):
        event = _add_event(service, fixture)
        preference = _propose(
            service,
            event,
            memory_key=str(fixture["memory_key"]),
            content=str(fixture["content"]),
            memory_type="preference",
            subject=str(fixture["subject"]),
            authority="user",
            scope=fixture["scope"],
        )
        preference = _transition(
            service, preference, "confirmed", reason="direct preference confirmed"
        )
        current = service.search_governed(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["memory_key"]),
            scope=dict(fixture["scope"]),
        )
        access = AccessContext(
            principal_id="evaluation-service",
            authority="service",
            scopes=dict(fixture["scope"]),
            permissions=frozenset({PERMISSION_READ}),
            purpose=None,
            allowed_users=frozenset({str(fixture["user"])}),
        )
        pack = service.compile_context(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["memory_key"]),
            scope=dict(fixture["scope"]),
            access_context=access,
        )
        checks = {
            "direct_preference_confirmed": preference.current_status == "confirmed",
            "preference_is_current": len(current.records) == 1
            and current.records[0].content == fixture["content"],
            "subject_is_user": len(current.records) == 1
            and current.records[0].subject == fixture["user"],
            "scope_is_preserved": len(current.records) == 1
            and all(current.records[0].scope.get(k) == v for k, v in fixture["scope"].items()),
            "context_uses_preference": pack.status == "ready"
            and any(item.get("content") == fixture["content"] for item in pack.items),
            "source_locator_preserved": pack.status == "ready"
            and all(bool(item.get("source_locator")) for item in pack.items),
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={"source_locator_preserved": "source_chain_missing"},
        )


def _op_anti_stereotype(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..governance import GovernanceError

    with _temporary_service() as (service, _db_path):
        event = _add_event(service, fixture, role="assistant")
        preference = _propose(
            service,
            event,
            memory_key=str(fixture["memory_key"]),
            content=str(fixture["content"]),
            memory_type="preference",
            subject=str(fixture["subject"]),
            authority="agent",
            scope=fixture["scope"],
        )
        promotion_blocked = False
        try:
            _transition(
                service,
                preference,
                "confirmed",
                reason="attempted inferred preference promotion",
            )
        except GovernanceError:
            promotion_blocked = True
        current = service.search_governed(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["memory_key"]),
            scope=dict(fixture["scope"]),
        )
        rendered = canonical_json(current.to_dict())
        checks = {
            "assistant_preference_not_promoted": promotion_blocked,
            "inferred_preference_not_current": current.records == [],
            "inferred_body_withheld": str(fixture["content"]) not in rendered,
            "owner_gate_required": current.owner_gate_required,
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={
                "inferred_body_withheld": "unauthorized_leak",
                "owner_gate_required": "owner_gate_missing",
            },
        )


def _op_unknown_and_owner_gate(
    fixture: Mapping[str, Any], _runtime: EvaluationRuntime
) -> dict:
    from ..access import PERMISSION_READ, AccessContext

    with _temporary_service() as (service, _db_path):
        access = AccessContext(
            principal_id="evaluation-service",
            authority="service",
            scopes={},
            permissions=frozenset({PERMISSION_READ}),
            purpose=None,
            allowed_users=frozenset({str(fixture["user"])}),
        )
        missing = service.compile_context(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["missing_key"]),
            access_context=access,
        )
        event = _add_event(
            service,
            fixture,
            content=str(fixture["pending_content"]),
        )
        _propose(
            service,
            event,
            memory_key=str(fixture["pending_key"]),
            content=str(fixture["pending_content"]),
        )
        pending = service.compile_context(
            user_id=str(fixture["user"]),
            memory_key=str(fixture["pending_key"]),
            access_context=access,
            max_chars=1,
        )
        pending_json = pending.to_json()
        checks = {
            "missing_is_unknown": missing.status == "unknown" and missing.abstain,
            "missing_gap_present": any(
                item.get("code") == "no_confirmed_memory" for item in missing.gaps
            ),
            "pending_abstains": pending.abstain and pending.status == "budget_exceeded",
            "owner_gate_survives_budget": pending.owner_gate_required,
            "owner_gap_survives_budget": any(
                item.get("code") == "owner_confirmation_required" for item in pending.gaps
            ),
            "pending_body_withheld": str(fixture["pending_content"]) not in pending_json,
        }
        return _checks_result(
            checks,
            observations={"fixture_alias": fixture["alias"]},
            failure_codes={
                "missing_is_unknown": "unknown_missing",
                "missing_gap_present": "unknown_missing",
                "owner_gate_survives_budget": "owner_gate_missing",
                "owner_gap_survives_budget": "owner_gate_missing",
                "pending_body_withheld": "unauthorized_leak",
            },
        )


def _op_minimal_disclosure(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..access import PERMISSION_READ, AccessContext

    owner = str(fixture["authorized_user"])
    outsider = str(fixture["unauthorized_user"])
    key = str(fixture["memory_key"])
    with _temporary_service() as (service, _db_path):
        secret_event = _add_event(
            service,
            fixture,
            user=owner,
            content=str(fixture["secret_content"]),
            suffix="secret",
        )
        secret = _transition(
            service,
            _propose(
                service,
                secret_event,
                memory_key=key,
                content=str(fixture["secret_content"]),
            ),
            "confirmed",
            reason="private owner evidence",
        )
        outsider_access = AccessContext(
            principal_id="evaluation-outsider",
            authority="service",
            scopes={},
            permissions=frozenset({PERMISSION_READ}),
            purpose=None,
            allowed_users=frozenset({outsider}),
        )
        forbidden = service.compile_context(
            user_id=owner,
            memory_key=key,
            access_context=outsider_access,
        )
        cross_user = service.official_search(
            {"query": "PRIVATE-ALPHA", "user_id": outsider, "top_k": 100}
        )

        budget_event = _add_event(
            service,
            fixture,
            user=outsider,
            content=str(fixture["budget_content"]),
            suffix="budget",
            timestamp=int(fixture["timestamp"]) + 1,
        )
        _transition(
            service,
            _propose(
                service,
                budget_event,
                memory_key=key,
                content=str(fixture["budget_content"]),
            ),
            "confirmed",
            reason="budget fixture confirmed",
        )
        large = service.compile_context(
            user_id=outsider,
            memory_key=key,
            access_context=outsider_access,
            max_chars=10_000,
            max_tokens=10_000,
            token_counter=_ExactCharacterCounter(),
        )
        tiny = service.compile_context(
            user_id=outsider,
            memory_key=key,
            access_context=outsider_access,
            max_chars=1,
        )
        forbidden_json = forbidden.to_json()
        large_json = large.to_json()
        tiny_json = tiny.to_json()
        prohibited_fields = {
            "created_by",
            "confirmed_by",
            "state_reason",
            "state_events",
            "raw_events",
            "withheld_record_ids",
        }
        checks = {
            "forbidden_status": forbidden.status == "forbidden" and forbidden.abstain,
            "forbidden_has_no_secret": str(fixture["secret_content"]) not in forbidden_json,
            "cross_user_search_has_no_secret": all(
                str(fixture["secret_content"]) not in item.get("content", "")
                for item in cross_user.get("data", [])
            ),
            "ready_pack_within_char_budget": large.status == "ready"
            and large.budget["used_chars"] == len(large_json)
            and large.budget["used_chars"] <= 10_000,
            "ready_pack_within_token_budget": large.status == "ready"
            and large.budget["used_tokens"] == len(large_json)
            and large.budget["used_tokens"] <= 10_000,
            "internal_fields_minimized": prohibited_fields.isdisjoint(
                set(large.items[0]) if large.items else set()
            ),
            "tiny_budget_never_ready": tiny.status == "budget_exceeded"
            and tiny.abstain
            and not tiny.items,
            "tiny_budget_has_no_partial_body": str(fixture["budget_content"]) not in tiny_json,
            "fixture_record_created": secret.current_status == "confirmed",
        }
        return _checks_result(
            checks,
            observations={
                "fixture_alias": fixture["alias"],
                "semantic_sensitivity_detection": "not_claimed",
            },
            failure_codes={
                "forbidden_has_no_secret": "unauthorized_leak",
                "cross_user_search_has_no_secret": "unauthorized_leak",
                "ready_pack_within_char_budget": "budget_overrun",
                "ready_pack_within_token_budget": "budget_overrun",
                "tiny_budget_never_ready": "budget_overrun",
                "tiny_budget_has_no_partial_body": "budget_overrun",
            },
        )


def _op_proxy_attribution(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, _db_path):
        _official_add_one(
            service,
            alias=str(fixture["alias"]),
            user=str(fixture["user"]),
            session=f"{fixture['alias']}-session",
            content=str(fixture["message"]),
            timestamp=int(fixture["timestamp"]),
        )
        response = service.official_search(
            {"query": fixture["query"], "user_id": fixture["user"], "top_k": 100}
        )
        hits = [item for item in response.get("data", []) if fixture["expected"] in item["content"]]
        return _checks_result(
            {"attributed_evidence_retrieved": bool(hits)},
            observations={
                "fixture_alias": fixture["alias"],
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "entity_role_action_attribution_evidence_retrieval",
            },
        )


def _add_proxy_segments(service: Any, fixture: Mapping[str, Any], field: str) -> set[str]:
    user = str(fixture["user"])
    base = int(fixture["timestamp"])
    for index, content in enumerate(fixture[field], start=1):
        _official_add_one(
            service,
            alias=f"{fixture['alias']}-{index}",
            user=user,
            session=f"{fixture['alias']}-session-{index}",
            content=str(content),
            timestamp=base + index,
        )
    return {
        event.source_message_id
        for event in service.list_raw_events(user)
        if any(str(segment) == event.content for segment in fixture[field])
    }


def _covered_source_ids(response: Mapping[str, Any]) -> set[str]:
    covered: set[str] = set()
    for item in response.get("data", []):
        source_ids = item.get("source_message_ids", [])
        if isinstance(source_ids, list):
            covered.update(value for value in source_ids if isinstance(value, str))
    return covered


def _op_proxy_three_segment_chain(
    fixture: Mapping[str, Any], _runtime: EvaluationRuntime
) -> dict:
    with _temporary_service() as (service, _db_path):
        expected = _add_proxy_segments(service, fixture, "segments")
        response = service.official_search(
            {"query": fixture["query"], "user_id": fixture["user"], "top_k": 100}
        )
        covered = _covered_source_ids(response)
        found = len(expected & covered)
        denominator = len(fixture["segments"])
        if denominator != 3:
            raise EvaluationError("B2 fixture must have exactly three evidence segments")
        failures = [] if found == denominator and len(expected) == denominator else [
            {
                "code": "source_chain_missing",
                "check": "three_segment_chain_complete",
                "message": "the three-segment evidence chain was not completely retrieved",
            }
        ]
        return {
            "status": "pass" if not failures else "fail",
            "metrics": {"segments_retrieved": _metric(found, denominator)},
            "observations": {
                "fixture_alias": fixture["alias"],
                "chain_segments": denominator,
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "three_segment_evidence_chain_retrieval_not_answer_reasoning",
            },
            "failures": failures,
        }


def _op_proxy_evidence_path(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, _db_path):
        expected = _add_proxy_segments(service, fixture, "segments")
        response = service.official_search(
            {"query": fixture["query"], "user_id": fixture["user"], "top_k": 100}
        )
        covered = _covered_source_ids(response)
        returned_sources = [
            value
            for item in response.get("data", [])
            for value in item.get("source_message_ids", [])
            if isinstance(value, str)
        ]
        denominator = len(fixture["segments"])
        if denominator <= 0:
            raise EvaluationError("B3 fixture has an empty evidence denominator")
        completeness = len(expected & covered)
        valid_links = sum(1 for value in set(returned_sources) if value in expected)
        checks = {
            "path_sources_complete": completeness == denominator and len(expected) == denominator,
            "provenance_is_exposed": bool(returned_sources),
            "expected_sources_are_valid": valid_links >= denominator,
        }
        result = _checks_result(
            checks,
            observations={
                "fixture_alias": fixture["alias"],
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "evidence_set_path_support_and_source_validity",
            },
            failure_codes={
                "path_sources_complete": "source_chain_missing",
                "provenance_is_exposed": "source_chain_missing",
                "expected_sources_are_valid": "source_chain_missing",
            },
        )
        result["metrics"]["path_segments_retrieved"] = _metric(completeness, denominator)
        return result


def _op_proxy_rule_evidence(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    with _temporary_service() as (service, _db_path):
        _official_add_one(
            service,
            alias=str(fixture["alias"]),
            user=str(fixture["user"]),
            session=f"{fixture['alias']}-session",
            content=str(fixture["message"]),
            timestamp=int(fixture["timestamp"]),
        )
        response = service.official_search(
            {"query": fixture["query"], "user_id": fixture["user"], "top_k": 100}
        )
        found = any(str(fixture["expected"]) in item.get("content", "") for item in response["data"])
        return _checks_result(
            {"rule_evidence_retrieved": found},
            observations={
                "fixture_alias": fixture["alias"],
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "rule_evidence_retrieval_only_not_domain_reasoning",
            },
        )


def _op_proxy_workflow_evidence(
    fixture: Mapping[str, Any], _runtime: EvaluationRuntime
) -> dict:
    with _temporary_service() as (service, _db_path):
        expected = _add_proxy_segments(service, fixture, "steps")
        response = service.official_search(
            {"query": fixture["query"], "user_id": fixture["user"], "top_k": 100}
        )
        covered = _covered_source_ids(response)
        found = len(expected & covered)
        denominator = len(fixture["steps"])
        if denominator <= 0:
            raise EvaluationError("G3 fixture has an empty workflow denominator")
        failures = [] if found == denominator and len(expected) == denominator else [
            {
                "code": "source_chain_missing",
                "check": "workflow_evidence_complete",
                "message": "workflow evidence steps were not completely retrieved",
            }
        ]
        return {
            "status": "pass" if not failures else "fail",
            "metrics": {"workflow_steps_retrieved": _metric(found, denominator)},
            "observations": {
                "fixture_alias": fixture["alias"],
                "workflow_steps": denominator,
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "workflow_evidence_retrieval_not_agent_execution",
            },
            "failures": failures,
        }


def _op_proxy_wire_contract(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    from ..config import RetrieverConfig
    from ..server import RetrieverServer

    def call(
        port: int,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        authorized: bool = True,
    ) -> tuple[int, str, dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        raw = None if payload is None else canonical_json(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(raw))
        if authorized:
            headers["Authorization"] = "Bearer governance-eval-key"
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "")
            body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise EvaluationError("HTTP response body must be a JSON object")
            return status, content_type, body
        finally:
            connection.close()

    descriptor, db_path = tempfile.mkstemp(prefix="governance-wire-", suffix=".db")
    os.close(descriptor)
    server = None
    thread = None
    thread_started = False
    try:
        config = RetrieverConfig(
            db_path=db_path,
            host="127.0.0.1",
            port=0,
            auth_mode="bearer",
            api_key="governance-eval-key",
        )
        server = RetrieverServer(config, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        thread_started = True
        port = int(server.server_address[1])
        payload = {
            "request_id": fixture["request_id"],
            "user_id": fixture["user"],
            "session_id": fixture["session"],
            "messages": [
                {
                    "role": "user",
                    "content": fixture["message"],
                    "timestamp": fixture["timestamp"],
                }
            ],
        }
        health_status, health_type, health = call(
            port, "GET", config.health_path, authorized=False
        )
        unauthorized_status, _unauthorized_type, unauthorized = call(
            port, "POST", config.add_path, payload, authorized=False
        )
        add_status, add_type, add = call(port, "POST", config.add_path, payload)
        search_payload = {
            "query": fixture["query"],
            "user_id": fixture["user"],
            "top_k": fixture["top_k"],
        }
        search_status, search_type, search = call(
            port, "POST", config.search_path, search_payload
        )
        invalid_status, invalid_type, invalid = call(
            port,
            "POST",
            config.search_path,
            {**search_payload, "top_k": True},
        )
        data = search.get("data")
        scores = [item.get("score") for item in data] if isinstance(data, list) else []
        checks = {
            "health_is_public_2xx_json": health_status == 200
            and health_type.startswith("application/json")
            and health.get("status") == "ok",
            "unauthorized_is_401": unauthorized_status == 401
            and isinstance(unauthorized.get("detail"), dict),
            "add_http_200_json": add_status == 200
            and add_type.startswith("application/json"),
            "add_exact_shape": set(add) == {"success", "request_id", "user_id", "session_id"},
            "add_success_is_true": add.get("success") is True,
            "add_ids_echoed": all(
                add.get(key) == payload[key] for key in ("request_id", "user_id", "session_id")
            ),
            "search_http_200_json": search_status == 200
            and search_type.startswith("application/json"),
            "search_top_level_shape": set(search) == {"data"} and isinstance(data, list),
            "search_item_contract": bool(data)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and bool(item.get("id"))
                and isinstance(item.get("content"), str)
                and bool(item.get("content"))
                for item in data
            ),
            "write_is_immediately_searchable": bool(data)
            and any(str(fixture["message"]) in item.get("content", "") for item in data),
            "top_k_respected": isinstance(data, list) and len(data) <= int(fixture["top_k"]),
            "score_order_preserved": all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in scores
            )
            and scores == sorted(scores, reverse=True),
            "invalid_top_k_is_422_json": invalid_status == 422
            and invalid_type.startswith("application/json")
            and isinstance(invalid.get("detail"), dict),
        }
        return _checks_result(
            checks,
            observations={
                "fixture_alias": fixture["alias"],
                "evidence_class": "synthetic_local_proxy",
                "proxy_claim": "aml_add_search_http_wire_routing_and_serialization",
            },
        )
    finally:
        try:
            if server is not None:
                try:
                    if thread_started:
                        server.shutdown()
                finally:
                    try:
                        server.server_close()
                    finally:
                        try:
                            if thread_started and thread is not None:
                                thread.join(timeout=5)
                        finally:
                            server.service.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass


def _nested_metric(value: Mapping[str, Any], path: str) -> float:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise EvaluationError(f"legacy metric is missing: {path}")
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise EvaluationError(f"legacy metric is not numeric: {path}")
    if not math.isfinite(float(current)):
        raise EvaluationError(f"legacy metric is not finite: {path}")
    return float(current)


def verify_baseline_provenance(
    baseline: Mapping[str, Any], *, repo_root: Path | None = None
) -> dict[str, Any]:
    if baseline.get("schema") != BASELINE_SCHEMA:
        raise EvaluationError("unsupported legacy baseline schema")
    if canonical_sha256(baseline) != LEGACY_BASELINE_CANONICAL_SHA256:
        raise EvaluationError("legacy baseline content does not match the reviewed golden")
    if baseline.get("verified") is not True:
        raise EvaluationError("legacy baseline is unverified")
    source = baseline.get("source")
    if not isinstance(source, Mapping):
        raise EvaluationError("legacy baseline source is missing")
    if source.get("clean_snapshot") is not True:
        raise EvaluationError("legacy baseline was not produced from a clean snapshot")
    if source.get("snapshot_method") != "git_archive":
        raise EvaluationError("legacy baseline snapshot method is not trusted")
    if source.get("network_used") is not False:
        raise EvaluationError("legacy baseline must be produced without network access")
    commit = source.get("commit")
    tree = source.get("tree")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise EvaluationError("legacy baseline commit is invalid")
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise EvaluationError("legacy baseline tree is invalid")
    if commit != LEGACY_BASELINE_COMMIT or tree != LEGACY_BASELINE_TREE:
        raise EvaluationError("legacy baseline is not bound to the approved v1.1 commit")
    dataset = baseline.get("dataset")
    if not isinstance(dataset, Mapping):
        raise EvaluationError("legacy baseline dataset is missing")
    if dataset.get("canonical_dump_sha256") != LEGACY_BASELINE_DATASET_SHA256:
        raise EvaluationError("legacy baseline dataset hash is not approved")
    if baseline.get("stage") != LEGACY_BASELINE_STAGE:
        raise EvaluationError("legacy baseline stage is not approved")
    root = (repo_root or REPO_ROOT).resolve()
    try:
        _git("cat-file", "-e", f"{commit}^{{commit}}", repo_root=root)
        observed_tree = _git("show", "-s", "--format=%T", commit, repo_root=root)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationError("legacy baseline commit cannot be verified locally") from exc
    if observed_tree != tree:
        raise EvaluationError("legacy baseline tree does not match its commit")
    return {"commit": commit, "tree": tree, "clean_snapshot": True}


def _op_legacy_floor(fixture: Mapping[str, Any], runtime: EvaluationRuntime) -> dict:
    from .dataset import make_dataset
    from .harness import ABLATION_LADDER, run_stage

    relative = Path(str(fixture.get("baseline", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise EvaluationError("legacy baseline path must stay inside the evaluation package")
    baseline_path = runtime.manifest_path.parent.parent / relative
    baseline = _read_json(baseline_path)
    provenance = verify_baseline_provenance(baseline, repo_root=runtime.repo_root)
    dataset_spec = baseline.get("dataset")
    if not isinstance(dataset_spec, Mapping):
        raise EvaluationError("legacy baseline dataset specification is missing")
    required_dataset_fields = ("suite", "scale", "difficulty", "seed", "top_k")
    if any(field not in dataset_spec for field in required_dataset_fields):
        raise EvaluationError("legacy baseline dataset specification is incomplete")
    dataset = make_dataset(
        seed=int(dataset_spec["seed"]),
        scale=str(dataset_spec["scale"]),
        difficulty=str(dataset_spec["difficulty"]),
        suite=str(dataset_spec["suite"]),
    )
    dataset_sha = canonical_sha256(dataset.dump())
    expected_sha = dataset_spec.get("canonical_dump_sha256")
    dataset_matches = isinstance(expected_sha, str) and dataset_sha == expected_sha
    stage_name = baseline.get("stage")
    flags_by_stage = dict(ABLATION_LADDER)
    if stage_name not in flags_by_stage:
        raise EvaluationError("legacy baseline stage is unavailable")
    with tempfile.TemporaryDirectory(prefix="governance-legacy-floor-") as workdir:
        result = run_stage(
            str(stage_name),
            flags_by_stage[str(stage_name)],
            dataset,
            workdir=workdir,
            top_k=int(dataset_spec["top_k"]),
        )
    if result.skipped:
        raise EvaluationError("required legacy stage was skipped")
    observed = {"overall": result.overall, "by_kind": result.by_kind}
    floors = baseline.get("floors")
    if not isinstance(floors, list) or not floors:
        raise EvaluationError("legacy baseline floors are empty")
    comparisons = []
    passed_count = 0
    for floor in floors:
        if not isinstance(floor, Mapping):
            raise EvaluationError("legacy floor must be an object")
        metric_name = floor.get("metric")
        operator = floor.get("operator")
        expected = floor.get("value")
        if not isinstance(metric_name, str) or operator not in {"gte", "lte"}:
            raise EvaluationError("legacy floor has an invalid metric or operator")
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise EvaluationError("legacy floor value must be numeric")
        actual = _nested_metric(observed, metric_name)
        passed = actual >= float(expected) if operator == "gte" else actual <= float(expected)
        passed_count += int(passed)
        comparisons.append(
            {
                "metric": metric_name,
                "operator": operator,
                "floor": float(expected),
                "observed": actual,
                "passed": passed,
            }
        )
    floor_count = len(floors)
    failures = []
    if not dataset_matches:
        failures.append(
            {
                "code": "legacy_dataset_mismatch",
                "check": "dataset_sha256",
                "message": "current deterministic dataset does not match the frozen baseline",
            }
        )
    if result.overall.get("queries", 0) <= 0:
        failures.append(
            {
                "code": "empty_denominator",
                "check": "scored_queries",
                "message": "legacy evaluation has no scored queries",
            }
        )
    if passed_count != floor_count:
        failures.append(
            {
                "code": "legacy_floor_regression",
                "check": "all_legacy_floors",
                "message": "one or more deterministic legacy floors regressed",
            }
        )
    comparison = {
        "status": "pass" if not failures else "fail",
        "baseline_sha256": canonical_sha256(baseline),
        "source_commit": provenance["commit"],
        "source_tree": provenance["tree"],
        "dataset_sha256": dataset_sha,
        "dataset_matches": dataset_matches,
        "stage": stage_name,
        "comparisons": comparisons,
        "excluded": list(baseline.get("excluded", [])),
    }
    return {
        "status": "pass" if not failures else "fail",
        "metrics": {
            "legacy_floors_passed": _metric(passed_count, floor_count),
            "scored_queries_present": _metric(
                int(result.overall.get("queries", 0) > 0), 1
            ),
            "dataset_hash_matches": _metric(int(dataset_matches), 1),
        },
        "observations": {
            "fixture_alias": fixture["alias"],
            "evidence_class": "deterministic_local_legacy_floor",
        },
        "failures": failures,
        "baseline_comparison": comparison,
    }


def _op_official_unverified(fixture: Mapping[str, Any], _runtime: EvaluationRuntime) -> dict:
    # v1 intentionally has no mechanism that converts a user-authored JSON file
    # into an "official" artifact.  Until platform evidence is independently
    # verifiable, this gate remains closed without poisoning local test status.
    return {
        "status": "unverified",
        "metrics": {"official_artifact_verified": _metric(0, 1)},
        "observations": {
            "fixture_alias": fixture["alias"],
            "reason": str(fixture.get("reason") or "no_verifiable_official_artifact"),
        },
        "failures": [],
    }


SURFACE_OPERATORS: dict[str, Callable[[Mapping[str, Any], EvaluationRuntime], dict]] = {
    "python_surface": _op_python_surface,
}

CAPABILITY_OPERATORS: dict[str, Callable[[Mapping[str, Any], EvaluationRuntime], dict]] = {
    "raw_immutable": _op_raw_immutable,
    "authority_gate": _op_authority_gate,
    "traceability": _op_traceability,
    "current_replacement": _op_current_replacement,
    "deletion_suppression": _op_deletion_suppression,
    "direct_preference": _op_direct_preference,
    "anti_stereotype": _op_anti_stereotype,
    "unknown_and_owner_gate": _op_unknown_and_owner_gate,
    "minimal_disclosure": _op_minimal_disclosure,
    "proxy_attribution": _op_proxy_attribution,
    "proxy_three_segment_chain": _op_proxy_three_segment_chain,
    "proxy_evidence_path": _op_proxy_evidence_path,
    "proxy_rule_evidence": _op_proxy_rule_evidence,
    "proxy_workflow_evidence": _op_proxy_workflow_evidence,
    "proxy_wire_contract": _op_proxy_wire_contract,
    "legacy_floor": _op_legacy_floor,
    "official_unverified": _op_official_unverified,
}


def _operator_failure(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "fail",
        "metrics": {"operator_completed": _metric(0, 1)},
        "observations": {},
        "failures": [{"code": code, "message": message}],
    }


def _run_operator(
    operator_name: str,
    specification: Mapping[str, Any],
    runtime: EvaluationRuntime,
    registry: Mapping[str, Callable[[Mapping[str, Any], EvaluationRuntime], dict]],
) -> dict[str, Any]:
    operator = registry.get(operator_name)
    if operator is None:
        return _operator_failure("unknown_operator", f"unknown operator: {operator_name}")
    try:
        raw = operator(specification, runtime)
    except EvaluationError as exc:
        return _operator_failure("operator_evaluation_error", str(exc))
    except Exception as exc:  # fail closed at the product boundary
        return _operator_failure(
            "operator_exception",
            f"operator raised {type(exc).__name__}",
        )
    try:
        return _validate_probe_result(raw)
    except EvaluationError as exc:
        return _operator_failure("operator_result_schema", str(exc))


def _failure_artifact(
    *,
    profile: str,
    code: Mapping[str, Any],
    manifest_sha: str | None,
    message: str,
    manifest_schema: str | None = None,
    evaluation_mode: str = "standard",
    failure_code: str = "manifest_invalid",
    local_evidence: str = "invalid",
    reason: str = "evaluation_manifest_invalid",
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "suite": SUITE_NAME,
        "profile": profile,
        "evaluation_mode": evaluation_mode,
        "manifest": {
            "sha256": manifest_sha,
            "canonical_sha256": CANONICAL_MANIFEST_SHA256,
            "schema": manifest_schema,
            "attested": manifest_sha == CANONICAL_MANIFEST_SHA256,
        },
        "code": dict(code),
        "verdict": {
            "profile": "fail",
            "layers": {
                "core_invariant": {"status": "fail"},
                "local_e2e_proxy": {"status": "fail"},
                "official_aml": {"status": "unverified"},
            },
            "surfaces": {},
            "capabilities": {},
        },
        "metrics": {"surfaces": {}, "capabilities": {}},
        "baseline_comparison": {"status": "unverified"},
        "failures": [{"code": failure_code, "message": message}],
        "claim_boundary": {
            "local_evidence": local_evidence,
            "official_status": "unverified",
            "official_claim_allowed": False,
            "reason": reason,
        },
    }


def _manifest_digest_without_validation(path: Path) -> str | None:
    try:
        return canonical_sha256(_read_json(path))
    except EvaluationError:
        return None


def run_governance_suite(
    *,
    profile: str = "quick",
    manifest_path: str | os.PathLike[str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    operator_overrides: Mapping[
        str, Callable[[Mapping[str, Any], EvaluationRuntime], dict]
    ]
    | None = None,
    surface_operator_overrides: Mapping[
        str, Callable[[Mapping[str, Any], EvaluationRuntime], dict]
    ]
    | None = None,
) -> dict[str, Any]:
    """Run one profile and return a deterministic structured artifact."""

    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    manifest_file = (
        Path(manifest_path).resolve() if manifest_path is not None else DEFAULT_MANIFEST_PATH
    )
    state = code_state(root)
    test_overrides_active = bool(operator_overrides or surface_operator_overrides)
    try:
        loaded = _load_manifest(manifest_file, require_canonical=False)
        if not loaded.attested:
            artifact = _failure_artifact(
                profile=profile,
                code=state,
                manifest_sha=loaded.sha256,
                manifest_schema=str(loaded.data.get("schema")),
                message=(
                    "custom manifest content is not the reviewed governance-v1; "
                    "standard attestation is unavailable"
                ),
                evaluation_mode="custom_manifest_non_attestable",
                failure_code="custom_manifest_non_attestable",
                local_evidence="custom_manifest_non_attestable",
                reason="custom_manifest_non_attestable",
            )
            return normalize_dynamic(artifact)
        if profile not in loaded.data["profiles"]:
            raise EvaluationError(f"unknown profile: {profile}")
    except EvaluationError as exc:
        digest = _manifest_digest_without_validation(manifest_file)
        custom_manifest = (
            digest is not None and digest != CANONICAL_MANIFEST_SHA256
        )
        artifact = _failure_artifact(
            profile=profile,
            code=state,
            manifest_sha=digest,
            message=str(exc),
            evaluation_mode=(
                "custom_manifest_non_attestable" if custom_manifest else "standard"
            ),
            local_evidence=(
                "custom_manifest_non_attestable" if custom_manifest else "invalid"
            ),
            reason=(
                "custom_manifest_non_attestable"
                if custom_manifest
                else "evaluation_manifest_invalid"
            ),
        )
        if custom_manifest:
            artifact["failures"].append(
                {
                    "code": "custom_manifest_non_attestable",
                    "message": (
                        "manifest content is not the reviewed governance-v1 canonical JSON"
                    ),
                }
            )
        return normalize_dynamic(artifact)

    runtime = EvaluationRuntime(repo_root=root, manifest_path=manifest_file)
    manifest = loaded.data
    profile_spec = manifest["profiles"][profile]
    surface_registry = dict(SURFACE_OPERATORS)
    surface_registry.update(surface_operator_overrides or {})
    capability_registry = dict(CAPABILITY_OPERATORS)
    overrides = dict(operator_overrides or {})
    if "official_unverified" in overrides:
        artifact = _failure_artifact(
            profile=profile,
            code=state,
            manifest_sha=loaded.sha256,
            message="the official claim operator cannot be overridden",
        )
        artifact["evaluation_mode"] = "test_override_non_attestable"
        return normalize_dynamic(artifact)
    capability_registry.update(overrides)
    failures: list[dict[str, Any]] = []
    surface_verdicts: dict[str, Any] = {}
    surface_metrics: dict[str, Any] = {}

    for surface_id in sorted(profile_spec["required_surfaces"]):
        specification = manifest["surfaces"].get(surface_id)
        if not isinstance(specification, Mapping):
            result = _operator_failure(
                "required_surface_missing", f"required surface is missing: {surface_id}"
            )
        else:
            result = _run_operator(
                str(specification.get("operator", "")),
                specification,
                runtime,
                surface_registry,
            )
        surface_verdicts[surface_id] = {
            "required": True,
            "status": result["status"],
            "observations": result["observations"],
        }
        surface_metrics[surface_id] = result["metrics"]
        if result["status"] != "pass":
            failures.append(
                {
                    "code": "required_surface_failed",
                    "surface": surface_id,
                    "message": f"required surface failed: {surface_id}",
                }
            )
            failures.extend(
                {"surface": surface_id, **dict(item)} for item in result["failures"]
            )

    required_capabilities = set(profile_spec["required_capabilities"])
    observed_capabilities = set(profile_spec["observed_capabilities"])
    selected_capabilities = required_capabilities | observed_capabilities
    capability_verdicts: dict[str, Any] = {}
    capability_metrics: dict[str, dict[str, Any]] = {layer: {} for layer in LAYERS}
    baseline_comparison: dict[str, Any] = {"status": "unverified"}

    for capability_id in sorted(selected_capabilities):
        specification = manifest["capabilities"].get(capability_id)
        required = capability_id in required_capabilities
        if not isinstance(specification, Mapping):
            result = _operator_failure(
                "required_capability_missing",
                f"required capability is missing: {capability_id}",
            )
            layer = "core_invariant"
        else:
            layer = str(specification["layer"])
            fixture = manifest["fixtures"].get(specification.get("fixture"))
            if not isinstance(fixture, Mapping):
                result = _operator_failure(
                    "required_fixture_missing",
                    f"required fixture is missing: {capability_id}",
                )
            else:
                result = _run_operator(
                    str(specification.get("operator", "")),
                    fixture,
                    runtime,
                    capability_registry,
                )
        capability_verdicts[capability_id] = {
            "layer": layer,
            "required": required,
            "status": result["status"],
            "observations": result["observations"],
        }
        capability_metrics.setdefault(layer, {})[capability_id] = result["metrics"]
        if "baseline_comparison" in result:
            baseline_comparison = dict(result["baseline_comparison"])
        if required and result["status"] != "pass":
            failures.append(
                {
                    "code": "required_capability_failed",
                    "capability": capability_id,
                    "layer": layer,
                    "message": f"required capability failed: {capability_id}",
                }
            )
            failures.extend(
                {"capability": capability_id, "layer": layer, **dict(item)}
                for item in result["failures"]
            )

    layer_verdicts: dict[str, Any] = {}
    for layer in ("core_invariant", "local_e2e_proxy"):
        layer_ids = sorted(
            capability_id
            for capability_id in required_capabilities
            if manifest["capabilities"][capability_id]["layer"] == layer
        )
        layer_verdicts[layer] = {
            "status": (
                "pass"
                if layer_ids
                and all(capability_verdicts[item]["status"] == "pass" for item in layer_ids)
                else "fail"
            ),
            "required_capabilities": layer_ids,
        }
    official_statuses = [
        capability_verdicts[item]["status"]
        for item in sorted(observed_capabilities)
        if manifest["capabilities"][item]["layer"] == "official_aml"
    ]
    layer_verdicts["official_aml"] = {
        "status": "pass" if official_statuses and all(item == "pass" for item in official_statuses)
        else "unverified",
        "observed_capabilities": sorted(observed_capabilities),
    }

    official_claim_allowed = layer_verdicts["official_aml"]["status"] == "pass"
    if test_overrides_active:
        failures.append(
            {
                "code": "test_override_active",
                "message": "operator overrides make this artifact non-attestable",
            }
        )
    artifact = {
        "schema": RESULT_SCHEMA,
        "suite": manifest["suite"],
        "profile": profile,
        "evaluation_mode": (
            "test_override_non_attestable" if test_overrides_active else "standard"
        ),
        "manifest": {
            "schema": manifest["schema"],
            "sha256": loaded.sha256,
            "canonical_sha256": CANONICAL_MANIFEST_SHA256,
            "attested": loaded.attested,
            "file": loaded.label,
        },
        "code": state,
        "verdict": {
            "profile": "pass" if not failures else "fail",
            "layers": layer_verdicts,
            "surfaces": surface_verdicts,
            "capabilities": capability_verdicts,
        },
        "metrics": {
            "surfaces": surface_metrics,
            "capabilities": capability_metrics,
        },
        "baseline_comparison": baseline_comparison,
        "failures": sorted(
            failures,
            key=lambda item: (
                str(item.get("layer", "")),
                str(item.get("surface", "")),
                str(item.get("capability", "")),
                str(item.get("code", "")),
                str(item.get("check", "")),
            ),
        ),
        "claim_boundary": {
            "local_evidence": (
                "test_override_non_attestable"
                if test_overrides_active
                else "deterministic_synthetic_only"
            ),
            "official_status": layer_verdicts["official_aml"]["status"],
            "official_claim_allowed": official_claim_allowed,
            "official_reason": (
                "verified_official_artifact"
                if official_claim_allowed
                else "no_verifiable_official_artifact"
            ),
            "safety_and_ranking_are_not_averaged": True,
            "proxy_limitations": {
                "proxy.A2": "entity and attribution evidence retrieval proxy",
                "proxy.B2": "real three-segment evidence-chain retrieval proxy, not answer reasoning",
                "proxy.B3": "source-set and evidence-path validity proxy",
                "proxy.G1": "rule-evidence retrieval proxy only, not domain reasoning",
                "proxy.G3": "workflow-step evidence retrieval proxy, not agent execution",
                "proxy.G5": "AML Add/Search HTTP routing, serialization, auth, and wire-shape proxy",
            },
            "not_claimed": [
                "official AML score improvement",
                "official AML capability pass",
                "semantic sensitivity classification",
                "hosted multi-tenant readiness",
            ],
        },
    }
    critical = {
        item.get("code")
        for item in artifact["failures"]
        if item.get("code") in CRITICAL_FAILURE_CODES
    }
    if critical:
        artifact["verdict"]["profile"] = "fail"
    return normalize_dynamic(artifact)


def write_result(path: str | os.PathLike[str], result: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    output.write_text(rendered + "\n", encoding="utf-8")


__all__ = [
    "MANIFEST_SCHEMA",
    "RESULT_SCHEMA",
    "BASELINE_SCHEMA",
    "CANONICAL_MANIFEST_SHA256",
    "SUITE_NAME",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_BASELINE_PATH",
    "CORE_CAPABILITIES",
    "LOCAL_PROXY_CAPABILITIES",
    "OFFICIAL_CAPABILITIES",
    "MANDATORY_OPERATOR_BY_CAPABILITY",
    "EvaluationError",
    "LoadedManifest",
    "canonical_json",
    "canonical_sha256",
    "validate_manifest",
    "load_manifest",
    "code_state",
    "assert_clean_baseline_source",
    "normalize_dynamic",
    "verify_baseline_provenance",
    "run_governance_suite",
    "write_result",
]
