#!/usr/bin/env python3
"""One-shot patch assembler for the v0.1.0 release branch.

This helper is removed by the applying workflow after the resulting tree passes
the complete acceptance suite and a fresh-wheel smoke test.
"""
from __future__ import annotations

import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch_context() -> None:
    path = "aml_retriever/context.py"
    text = read(path)
    text = replace_once(
        text,
        "from collections.abc import Mapping, Sequence\nfrom dataclasses import dataclass, field, replace\n",
        "from collections.abc import Mapping, Sequence\nfrom dataclasses import dataclass, field, replace\nfrom itertools import islice\n",
        label="context import",
    )
    text = replace_once(
        text,
        '''CONTEXT_STATUSES = frozenset(\n    {"ready", "unknown", "conflict", "budget_exceeded", "forbidden"}\n)\n\n''',
        '''CONTEXT_STATUSES = frozenset(\n    {"ready", "unknown", "conflict", "budget_exceeded", "forbidden"}\n)\n\n# Product-level resource ceilings. These apply even when a caller omits a\n# character budget, so a faulty trusted integration cannot create an unbounded\n# local allocation or repeated-serialization job.\nMAX_CONTEXT_RECORDS = 1_000\nMAX_CONTEXT_ITEM_BYTES = 512 * 1_024\nMAX_CONTEXT_TOTAL_ITEM_BYTES = 6 * 1_024 * 1_024\nMAX_CONTEXT_PACK_BYTES = 8 * 1_024 * 1_024\nMAX_CONTEXT_REQUEST_CHARS = 4 * 1_024 * 1_024\n\n''',
        label="context constants",
    )

    class_start = text.index("class ContextPack:")
    post_init = text.index("    def __post_init__(self) -> None:", class_start)
    cache_field = '''    _rendered_json: str | None = field(\n        default=None,\n        init=False,\n        repr=False,\n        compare=False,\n    )\n\n'''
    text = text[:post_init] + cache_field + text[post_init:]
    text = replace_once(
        text,
        '''    def to_json(self) -> str:\n        return canonical_json(self.to_dict())\n''',
        '''    def to_json(self) -> str:\n        cached = self._rendered_json\n        if cached is None:\n            cached = canonical_json(self.to_dict())\n            object.__setattr__(self, "_rendered_json", cached)\n        return cached\n''',
        label="context json cache",
    )
    text = replace_once(
        text,
        '''class _CounterFailure(ValueError):\n    pass\n\n\n''',
        '''class _CounterFailure(ValueError):\n    pass\n\n\nclass _ContextInputLimit(ValueError):\n    pass\n\n\n''',
        label="context limit error",
    )

    helper_start = text.index("def _public_item_sort_key")
    helper_end = text.index("def _normalize_result", helper_start)
    helpers = '''def _public_item_sort_key(\n    item: dict,\n    *,\n    rendered: str | None = None,\n) -> tuple:\n    return (\n        str(item.get("memory_key", "")),\n        str(item.get("memory_type", "")),\n        str(item.get("subject", "")),\n        canonical_json(item.get("scope", {})),\n        str(item.get("id", "")),\n        rendered if rendered is not None else canonical_json(item),\n    )\n\n\ndef _bounded_records(values: object) -> list[object]:\n    """Materialize at most one item beyond the public compiler ceiling."""\n\n    try:\n        iterator = iter(values)\n    except TypeError:\n        return [None]\n    records = list(islice(iterator, MAX_CONTEXT_RECORDS + 1))\n    if len(records) > MAX_CONTEXT_RECORDS:\n        raise _ContextInputLimit("record count exceeds context limit")\n    return records\n\n\ndef _public_items(\n    records: list[MemoryRecord],\n    *,\n    mode: str,\n    policy: DisclosurePolicy,\n) -> list[dict]:\n    """Render, size-check, and deterministically order public atomic items."""\n\n    decorated: list[tuple[tuple, dict]] = []\n    total_bytes = 0\n    for record in records:\n        item = _record_item(record, mode=mode, policy=policy)\n        rendered = canonical_json(item)\n        item_bytes = len(rendered.encode("utf-8"))\n        if item_bytes > MAX_CONTEXT_ITEM_BYTES:\n            raise _ContextInputLimit("one context item exceeds its byte limit")\n        total_bytes += item_bytes\n        if total_bytes > MAX_CONTEXT_TOTAL_ITEM_BYTES:\n            raise _ContextInputLimit("context items exceed their aggregate byte limit")\n        decorated.append((_public_item_sort_key(item, rendered=rendered), item))\n    decorated.sort(key=lambda pair: pair[0])\n    return [item for _key, item in decorated]\n\n\n'''
    text = text[:helper_start] + helpers + text[helper_end:]
    text = replace_once(
        text,
        '''    try:\n        records = list(result.records)\n    except TypeError:\n        records = [None]\n''',
        '''    records = _bounded_records(result.records)\n''',
        label="bounded context records",
    )
    text = replace_once(
        text,
        '''            items = sorted(\n                (_record_item(record, mode="current", policy=policy) for record in records),\n                key=_public_item_sort_key,\n            )\n''',
        '''            items = _public_items(\n                records,\n                mode="current",\n                policy=policy,\n            )\n''',
        label="current public items",
    )
    text = replace_once(
        text,
        '''            items = sorted(\n                (_record_item(record, mode="audit", policy=policy) for record in records),\n                key=_public_item_sort_key,\n            )\n''',
        '''            items = _public_items(\n                records,\n                mode="audit",\n                policy=policy,\n            )\n''',
        label="audit public items",
    )
    text = replace_once(
        text,
        '''        return True\n\n    def failure(\n''',
        '''        return True\n\n    @staticmethod\n    def _within_hard_output_limit(pack: ContextPack) -> bool:\n        return len(pack.to_json().encode("utf-8")) <= MAX_CONTEXT_PACK_BYTES\n\n    def failure(\n''',
        label="context output ceiling",
    )

    compile_start = text.index("    def compile(\n")
    compile_end = text.index("\n\n__all__ = [", compile_start)
    compile_method = '''    def compile(\n        self,\n        result: CurrentStateResult,\n        *,\n        max_chars: int | None = None,\n        max_tokens: int | None = None,\n    ) -> ContextPack:\n        """Compile an already-authorized current/audit result.\n\n        The mandatory envelope is retained even if a tiny budget cannot hold\n        it. In that case the pack reports ``budget_exceeded`` and abstains; it\n        never emits an empty ``ready`` response.\n\n        Character-only selection probes the full prefix once, then uses a\n        binary search for the largest fitting deterministic prefix. An exact\n        token counter is arbitrary host code and has no monotonic-prefix\n        contract, so token selection retains the conservative descending scan\n        under the hard record and byte ceilings.\n        """\n\n        if not isinstance(result, CurrentStateResult):\n            return self.failure(\n                reason="invalid_governed_result",\n                max_chars=max_chars,\n                max_tokens=max_tokens,\n            )\n\n        try:\n            (\n                status,\n                abstain,\n                reason,\n                items,\n                gaps,\n                conflicts,\n                owner_gate,\n                policy_count,\n                audit_evidence_count,\n                matched_count,\n                resolver_limit_count,\n            ) = _normalize_result(result, self.disclosure_policy)\n        except _ContextInputLimit:\n            return self.failure(\n                reason="context_input_limit_exceeded",\n                max_chars=max_chars,\n                max_tokens=max_tokens,\n            )\n\n        total_items = len(items)\n        error = self.preflight_error(max_chars=max_chars, max_tokens=max_tokens)\n        if error:\n            return self.failure(\n                reason=error,\n                max_chars=max_chars,\n                max_tokens=max_tokens,\n                owner_gate_required=owner_gate,\n                gaps=gaps,\n                conflicts=conflicts,\n                completeness=_completeness(\n                    matched_count=matched_count,\n                    returned_count=0,\n                    resolver_limit=resolver_limit_count,\n                    budget=total_items,\n                ),\n                omitted=_omitted(\n                    budget=total_items,\n                    policy=policy_count,\n                    audit_evidence=audit_evidence_count,\n                    resolver_limit=resolver_limit_count,\n                ),\n            )\n\n        def finalize_prefix(kept: int) -> ContextPack:\n            candidate = ContextPack(\n                status=status,\n                abstain=abstain,\n                reason=reason,\n                items=items[:kept],\n                gaps=gaps,\n                conflicts=conflicts,\n                owner_gate_required=owner_gate,\n                completeness=_completeness(\n                    matched_count=matched_count,\n                    returned_count=kept,\n                    resolver_limit=resolver_limit_count,\n                    budget=total_items - kept,\n                ),\n                omitted=_omitted(\n                    budget=total_items - kept,\n                    policy=policy_count,\n                    audit_evidence=audit_evidence_count,\n                    resolver_limit=resolver_limit_count,\n                ),\n            )\n            return self._finalize(\n                candidate,\n                max_chars=max_chars,\n                max_tokens=max_tokens,\n            )\n\n        try:\n            if max_tokens is None:\n                # No-budget compilation and the common full-fit case remain a\n                # single probe. Only a rejecting character or hard byte limit\n                # enters the logarithmic search.\n                full = finalize_prefix(total_items)\n                full_within_hard_limit = self._within_hard_output_limit(full)\n                if full_within_hard_limit and self._fits(full):\n                    return full\n                if not full_within_hard_limit and max_chars is None:\n                    return self.failure(\n                        reason="context_output_limit_exceeded",\n                        max_chars=max_chars,\n                        max_tokens=max_tokens,\n                        owner_gate_required=owner_gate,\n                        gaps=gaps,\n                        conflicts=conflicts,\n                        completeness=_completeness(\n                            matched_count=matched_count,\n                            returned_count=0,\n                            resolver_limit=resolver_limit_count,\n                            budget=total_items,\n                        ),\n                        omitted=_omitted(\n                            budget=total_items,\n                            policy=policy_count,\n                            audit_evidence=audit_evidence_count,\n                            resolver_limit=resolver_limit_count,\n                        ),\n                    )\n\n                low = 0\n                high = total_items - 1\n                best_kept = -1\n                best_pack: ContextPack | None = None\n                while low <= high:\n                    kept = (low + high) // 2\n                    candidate = finalize_prefix(kept)\n                    if self._within_hard_output_limit(candidate) and self._fits(candidate):\n                        best_kept = kept\n                        best_pack = candidate\n                        low = kept + 1\n                    else:\n                        high = kept - 1\n                if best_pack is not None and (not total_items or best_kept > 0):\n                    return best_pack\n            else:\n                for kept in range(total_items, -1, -1):\n                    candidate = finalize_prefix(kept)\n                    if not self._within_hard_output_limit(candidate):\n                        continue\n                    if not self._fits(candidate):\n                        continue\n                    if total_items and kept == 0:\n                        break\n                    return candidate\n        except _CounterFailure:\n            return self.failure(\n                reason="token_counter_unavailable",\n                max_chars=max_chars,\n                max_tokens=max_tokens,\n                owner_gate_required=owner_gate,\n                gaps=gaps,\n                conflicts=conflicts,\n                completeness=_completeness(\n                    matched_count=matched_count,\n                    returned_count=0,\n                    resolver_limit=resolver_limit_count,\n                    budget=total_items,\n                ),\n                omitted=_omitted(\n                    budget=total_items,\n                    policy=policy_count,\n                    audit_evidence=audit_evidence_count,\n                    resolver_limit=resolver_limit_count,\n                ),\n            )\n\n        return self.failure(\n            reason="budget_exceeded",\n            max_chars=max_chars,\n            max_tokens=max_tokens,\n            owner_gate_required=owner_gate,\n            gaps=gaps,\n            conflicts=conflicts,\n            completeness=_completeness(\n                matched_count=matched_count,\n                returned_count=0,\n                resolver_limit=resolver_limit_count,\n                budget=total_items,\n            ),\n            omitted=_omitted(\n                budget=total_items,\n                policy=policy_count,\n                audit_evidence=audit_evidence_count,\n                resolver_limit=resolver_limit_count,\n            ),\n        )\n'''
    text = text[:compile_start] + compile_method + text[compile_end:]
    text = replace_once(
        text,
        '''__all__ = [\n    "CONTEXT_STATUSES",\n''',
        '''__all__ = [\n    "CONTEXT_STATUSES",\n    "MAX_CONTEXT_RECORDS",\n    "MAX_CONTEXT_ITEM_BYTES",\n    "MAX_CONTEXT_TOTAL_ITEM_BYTES",\n    "MAX_CONTEXT_PACK_BYTES",\n    "MAX_CONTEXT_REQUEST_CHARS",\n''',
        label="context exports",
    )
    write(path, text)


def patch_api() -> None:
    path = "aml_retriever/api.py"
    text = read(path)
    text = replace_once(
        text,
        "from .context import ContextCompiler, ContextPack, TokenCounter\n",
        '''from .context import (\n    MAX_CONTEXT_RECORDS,\n    ContextCompiler,\n    ContextPack,\n    TokenCounter,\n)\n''',
        label="api context import",
    )
    text = replace_once(
        text,
        '''            or max_records <= 0\n        ):\n''',
        '''            or max_records <= 0\n            or max_records > MAX_CONTEXT_RECORDS\n        ):\n''',
        label="api record ceiling",
    )
    write(path, text)


def patch_mcp() -> None:
    path = "aml_retriever/mcp_tools.py"
    text = read(path)
    text = replace_once(
        text,
        "from .api import ApiError\n",
        "from .api import ApiError\nfrom .context import MAX_CONTEXT_RECORDS\n",
        label="mcp context import",
    )
    text = replace_once(
        text,
        "MAX_RECORDS = 1_000\n",
        "MAX_RECORDS = MAX_CONTEXT_RECORDS\n",
        label="mcp record ceiling",
    )
    write(path, text)


def patch_rest() -> None:
    path = "aml_retriever/rest_v1.py"
    text = read(path)
    text = replace_once(
        text,
        "from .context import ContextCompiler\n",
        '''from .context import (\n    MAX_CONTEXT_RECORDS,\n    MAX_CONTEXT_REQUEST_CHARS,\n    ContextCompiler,\n)\n''',
        label="rest context import",
    )
    old_limit = '_positive_int(data.get("max_records", 100), maximum=10_000)'
    if text.count(old_limit) != 2:
        raise RuntimeError(f"rest record ceiling: expected two matches, found {text.count(old_limit)}")
    text = text.replace(
        old_limit,
        '_positive_int(data.get("max_records", 100), maximum=MAX_CONTEXT_RECORDS)',
    )
    text = replace_once(
        text,
        "max_chars = _nonnegative_int(max_chars, maximum=16 * 1_048_576)\n",
        "max_chars = _nonnegative_int(max_chars, maximum=MAX_CONTEXT_REQUEST_CHARS)\n",
        label="rest character ceiling",
    )
    write(path, text)


def patch_pyproject() -> None:
    path = "pyproject.toml"
    text = read(path)
    text = replace_once(
        text,
        '  "Programming Language :: Python :: 3.13",\n',
        '  "Programming Language :: Python :: 3.13",\n  "Typing :: Typed",\n',
        label="typed classifier",
    )
    text = replace_once(
        text,
        '''flowgrid-memory = "aml_retriever.product_cli:main"\nflowgrid-memory-rest = "aml_retriever.rest_v1:main"\nflowgrid-memory-mcp = "aml_retriever.mcp_adapter:main"\n''',
        '''flowgrid-memory = "flowgrid_memory.cli:main"\nflowgrid-memory-rest = "flowgrid_memory.rest:main"\nflowgrid-memory-mcp = "flowgrid_memory.mcp:main"\n''',
        label="product entrypoints",
    )
    text = replace_once(
        text,
        'include = ["aml_retriever*"]\n',
        'include = ["flowgrid_memory*", "aml_retriever*"]\n',
        label="package discovery",
    )
    text += '\n"flowgrid_memory" = ["py.typed"]\n'
    write(path, text)


def create_public_namespace() -> None:
    write(
        "flowgrid_memory/__init__.py",
        '''"""Stable public API for FlowGrid Agent Memory.\n\nImplementation and AML compatibility modules remain under ``aml_retriever``.\nNew integrations should import product contracts from this namespace.\n"""\nfrom aml_retriever._version import AML_ADAPTER_VERSION, PRODUCT_VERSION\nfrom aml_retriever.access import (\n    PERMISSION_AUDIT,\n    PERMISSION_EVIDENCE,\n    PERMISSION_READ,\n    AccessContext,\n    AccessDecision,\n    DisclosurePolicy,\n    authorize_memory_read,\n)\nfrom aml_retriever.compiler import CompilationReceipt\nfrom aml_retriever.context import (\n    CONTEXT_STATUSES,\n    MAX_CONTEXT_ITEM_BYTES,\n    MAX_CONTEXT_PACK_BYTES,\n    MAX_CONTEXT_RECORDS,\n    MAX_CONTEXT_REQUEST_CHARS,\n    MAX_CONTEXT_TOTAL_ITEM_BYTES,\n    ContextCompiler,\n    ContextPack,\n    TokenCounter,\n    canonical_json,\n)\nfrom aml_retriever.extraction import (\n    DIRECTIVE_PREFIX,\n    CallableMemoryExtractor,\n    DirectiveMemoryExtractor,\n    EvidenceSpan,\n    ExtractionConflict,\n    ExtractionError,\n    ExtractionRequest,\n    ExtractionValidationError,\n    ExtractorIdentity,\n    ExtractorInvocationError,\n    MemoryExtractor,\n    ProposalDraft,\n)\nfrom aml_retriever.facade import (\n    AuthorizedMemoryResult,\n    FlowGridMemory,\n    IngestReceipt,\n    PrivacyEraseReceipt,\n)\nfrom aml_retriever.governance import (\n    ALLOWED_TRANSITIONS,\n    AUTHORITIES,\n    CONFIRM_AUTHORITIES,\n    GOVERNANCE_AUTHORITIES,\n    GOVERNANCE_SCHEMA_VERSION,\n    MEMORY_STATES,\n    MEMORY_TYPES,\n    CurrentStateResult,\n    GovernanceConflict,\n    GovernanceError,\n    MemoryRecord,\n    MemoryStateEvent,\n    RawEvent,\n)\nfrom aml_retriever.migrations import SchemaReport, inspect_schema\nfrom aml_retriever.model_extraction import (\n    MODEL_EXTRACTOR_SCHEMA,\n    MODEL_PROMPT_VERSION,\n    QuoteAnchoredModelExtractor,\n    build_model_extraction_prompt,\n    quote_anchored_identity,\n)\n\n__version__ = PRODUCT_VERSION\n\n__all__ = [\n    "PRODUCT_VERSION",\n    "AML_ADAPTER_VERSION",\n    "__version__",\n    "FlowGridMemory",\n    "IngestReceipt",\n    "AuthorizedMemoryResult",\n    "PrivacyEraseReceipt",\n    "AccessContext",\n    "AccessDecision",\n    "DisclosurePolicy",\n    "PERMISSION_READ",\n    "PERMISSION_AUDIT",\n    "PERMISSION_EVIDENCE",\n    "authorize_memory_read",\n    "ContextPack",\n    "ContextCompiler",\n    "TokenCounter",\n    "CONTEXT_STATUSES",\n    "MAX_CONTEXT_RECORDS",\n    "MAX_CONTEXT_ITEM_BYTES",\n    "MAX_CONTEXT_TOTAL_ITEM_BYTES",\n    "MAX_CONTEXT_PACK_BYTES",\n    "MAX_CONTEXT_REQUEST_CHARS",\n    "canonical_json",\n    "RawEvent",\n    "MemoryRecord",\n    "MemoryStateEvent",\n    "CurrentStateResult",\n    "GovernanceError",\n    "GovernanceConflict",\n    "MEMORY_STATES",\n    "MEMORY_TYPES",\n    "AUTHORITIES",\n    "CONFIRM_AUTHORITIES",\n    "GOVERNANCE_AUTHORITIES",\n    "GOVERNANCE_SCHEMA_VERSION",\n    "ALLOWED_TRANSITIONS",\n    "CompilationReceipt",\n    "DIRECTIVE_PREFIX",\n    "ExtractionError",\n    "ExtractionValidationError",\n    "ExtractionConflict",\n    "ExtractorInvocationError",\n    "ExtractorIdentity",\n    "EvidenceSpan",\n    "ExtractionRequest",\n    "ProposalDraft",\n    "MemoryExtractor",\n    "DirectiveMemoryExtractor",\n    "CallableMemoryExtractor",\n    "MODEL_EXTRACTOR_SCHEMA",\n    "MODEL_PROMPT_VERSION",\n    "QuoteAnchoredModelExtractor",\n    "build_model_extraction_prompt",\n    "quote_anchored_identity",\n    "SchemaReport",\n    "inspect_schema",\n]\n''',
    )
    wrappers = {
        "flowgrid_memory/cli.py": "aml_retriever.product_cli",
        "flowgrid_memory/rest.py": "aml_retriever.rest_v1",
        "flowgrid_memory/mcp.py": "aml_retriever.mcp_adapter",
    }
    for path, module in wrappers.items():
        write(
            path,
            f'''"""Product entry-point wrapper around the internal implementation."""\nfrom {module} import main\n\n__all__ = ["main"]\n\nif __name__ == "__main__":  # pragma: no cover\n    raise SystemExit(main())\n''',
        )
    write("flowgrid_memory/py.typed", "")


def patch_readmes() -> None:
    english = read("README.md")
    english = replace_once(
        english,
        '''[Governed REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·\n[Owner review](docs/OWNER_REVIEW.md) · [Local security](docs/LOCAL_SECURITY.md) ·\n[Data lifecycle](docs/DATA_LIFECYCLE.md) · [Evaluation](docs/EVAL.md) ·\n[Local acceptance](docs/ACCEPTANCE_V0_1.md)''',
        '''[Governed REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·\n[Owner review](docs/OWNER_REVIEW.md) · [Public API](docs/PUBLIC_API.md) ·\n[Container](docs/CONTAINER.md) · [Local security](docs/LOCAL_SECURITY.md) ·\n[Data lifecycle](docs/DATA_LIFECYCLE.md) · [Evaluation](docs/EVAL.md) ·\n[Acceptance](docs/ACCEPTANCE_CRITERIA.md)''',
        label="english doc links",
    )
    english = replace_once(
        english,
        "from aml_retriever import AccessContext, FlowGridMemory, PERMISSION_READ\n",
        "from flowgrid_memory import AccessContext, FlowGridMemory, PERMISSION_READ\n",
        label="english public import",
    )
    english = replace_once(
        english,
        '''`FlowGridMemory` is the stable transport-neutral boundary shared by the CLI,\nREST, and MCP adapters. The database path is mandatory and the underlying\ndatabase object is not exposed.\n''',
        '''`FlowGridMemory` is the stable transport-neutral boundary shared by the CLI,\nREST, and MCP adapters. The database path is mandatory and the underlying\ndatabase object is not exposed. New product integrations should import from\n`flowgrid_memory`; `aml_retriever` remains the implementation and AML\ncompatibility namespace.\n''',
        label="english namespace note",
    )
    english = replace_once(
        english,
        "## Governed local adapters\n",
        '''## Container image\n\nThe default OCI target is a non-networked CLI image and runs\n`flowgrid-memory doctor --ephemeral`. A separate `mcp` build target supports\nstdio use. The verified container contract intentionally provides no REST port\ntarget; governed REST remains a host-local loopback service. See\n[Container contract](docs/CONTAINER.md).\n\n## Governed local adapters\n''',
        label="english container section",
    )
    english = replace_once(
        english,
        "## Contributing and security\n",
        '''## Release evidence\n\nThe release workflow rebuilds and tests the package, performs a fresh-wheel\ninstall, publishes checksums, acceptance JSON, SPDX SBOM, and provenance JSON,\nand creates GitHub Sigstore attestations for the wheel and sdist. Stable\ncriteria are documented in [Acceptance criteria](docs/ACCEPTANCE_CRITERIA.md).\n\n## Contributing and security\n''',
        label="english release section",
    )
    write("README.md", english)

    chinese = read("README.zh-CN.md")
    chinese = replace_once(
        chinese,
        '''[受治理 REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·\n[Owner 审核](docs/OWNER_REVIEW.md) · [本地安全](docs/LOCAL_SECURITY.md) ·\n[数据生命周期](docs/DATA_LIFECYCLE.md) ·\n[评测](docs/EVAL.md) · [本地验收](docs/ACCEPTANCE_V0_1.md)''',
        '''[受治理 REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·\n[Owner 审核](docs/OWNER_REVIEW.md) · [公共 API](docs/PUBLIC_API.md) ·\n[容器契约](docs/CONTAINER.md) · [本地安全](docs/LOCAL_SECURITY.md) ·\n[数据生命周期](docs/DATA_LIFECYCLE.md) ·\n[评测](docs/EVAL.md) · [验收标准](docs/ACCEPTANCE_CRITERIA.md)''',
        label="chinese doc links",
    )
    chinese = replace_once(
        chinese,
        "from aml_retriever import AccessContext, FlowGridMemory, PERMISSION_READ\n",
        "from flowgrid_memory import AccessContext, FlowGridMemory, PERMISSION_READ\n",
        label="chinese public import",
    )
    chinese = replace_once(
        chinese,
        '''`FlowGridMemory` 是 CLI、REST 与 MCP 共用的稳定门面，与传输方式无关。数据库路径\n必须显式给出，也不会向调用方暴露底层 DB 或通用 SQL 入口。\n''',
        '''`FlowGridMemory` 是 CLI、REST 与 MCP 共用的稳定门面，与传输方式无关。数据库路径\n必须显式给出，也不会向调用方暴露底层 DB 或通用 SQL 入口。新的产品集成统一从\n`flowgrid_memory` 导入；`aml_retriever` 保留为实现层与 AML 兼容命名空间。\n''',
        label="chinese namespace note",
    )
    chinese = replace_once(
        chinese,
        "## 受治理的本地适配器\n",
        '''## 容器镜像\n\n默认 OCI target 是无网络监听的 CLI 镜像，启动后运行\n`flowgrid-memory doctor --ephemeral`。另有 `mcp` 构建 target 提供 stdio\n能力。当前容器契约明确不提供 REST 端口 target；受治理 REST 继续作为宿主机\nloopback 服务。详见 [容器契约](docs/CONTAINER.md)。\n\n## 受治理的本地适配器\n''',
        label="chinese container section",
    )
    chinese = replace_once(
        chinese,
        "## 参与贡献与安全报告\n",
        '''## 发布证据\n\n发布工作流会重新构建并执行全量测试，在全新环境安装 wheel，生成校验和、验收\nJSON、SPDX SBOM 与 provenance JSON，并为 wheel 和 sdist 创建 GitHub Sigstore\n证明。稳定验收条件见 [验收标准](docs/ACCEPTANCE_CRITERIA.md)。\n\n## 参与贡献与安全报告\n''',
        label="chinese release section",
    )
    write("README.zh-CN.md", chinese)


def patch_install() -> None:
    path = "docs/INSTALL.md"
    text = read(path)
    text = replace_once(
        text,
        '''/tmp/flowgrid-memory-venv/bin/python -c \\\n  'import aml_retriever; print(aml_retriever.PRODUCT_VERSION)'\n''',
        '''/tmp/flowgrid-memory-venv/bin/python -c \\\n  'import flowgrid_memory; print(flowgrid_memory.PRODUCT_VERSION)'\n''',
        label="install public import",
    )
    text += '''\n## Container targets\n\nThe default image is the non-networked `cli` target. The optional `mcp` target\ninstalls the MCP extra and communicates over stdio. No container target exposes\nthe governed REST adapter because its verified boundary is a literal loopback\nlistener on one trusted host. See [CONTAINER.md](CONTAINER.md).\n'''
    write(path, text)


def write_docs() -> None:
    write(
        "docs/PUBLIC_API.md",
        '''# Stable public Python API\n\nThe distribution name is `flowgrid-agent-memory`. New integrations import the\nproduct API from `flowgrid_memory`:\n\n```python\nfrom flowgrid_memory import AccessContext, FlowGridMemory, PERMISSION_READ\n```\n\nThe public namespace contains the facade, request/result data contracts,\ndisclosure and access policies, governed lifecycle types, extraction protocols,\ncontext contracts, version constants, and schema inspection. It intentionally\ndoes not export `RetrieverDB`, `MemoryService`, `Store`, raw SQL helpers, or\nmigration implementation functions.\n\n`aml_retriever` remains importable for AML Add/Search compatibility and existing\n0.1 integrations. It is an implementation/compatibility namespace and may add\ndeprecation warnings in a later major compatibility cycle.\n\nThe installed command entry points also route through the product namespace:\n\n- `flowgrid-memory` -> `flowgrid_memory.cli`\n- `flowgrid-memory-rest` -> `flowgrid_memory.rest`\n- `flowgrid-memory-mcp` -> `flowgrid_memory.mcp`\n\nThe wrappers contain no policy logic. They delegate to the internal adapters so\nthere remains one implementation of each trust boundary.\n''',
    )
    write(
        "docs/CONTAINER.md",
        '''# Container contract\n\nThe OCI image is a local execution package. It is not a hosted REST deployment.\n\n## Default CLI target\n\n```bash\ndocker build --target cli -t flowgrid-agent-memory:0.1.0 .\ndocker run --rm flowgrid-agent-memory:0.1.0\n```\n\nThe default invocation runs `flowgrid-memory doctor --ephemeral`, opens no\nnetwork listener, writes no persistent database, and exits after the diagnostic.\nRun another CLI command by appending its arguments:\n\n```bash\ndocker run --rm flowgrid-agent-memory:0.1.0 demo --ephemeral\n```\n\nFor a persistent database, mount a directory and pass an explicit absolute path\ninside the container. The image runs as UID/GID 10001; the mounted directory\nmust be writable by that identity.\n\n## MCP stdio target\n\n```bash\ndocker build --target mcp -t flowgrid-agent-memory:mcp-0.1.0 .\ndocker run --rm -i \\\n  -v "$PWD/data:/data" \\\n  -v "$PWD/config:/config:ro" \\\n  flowgrid-agent-memory:mcp-0.1.0 \\\n  --db /data/memory.db \\\n  --principal-config /config/mcp-principal.json\n```\n\nThe MCP target communicates over stdin/stdout and publishes no TCP port.\n\n## REST boundary\n\nThe verified REST adapter accepts only the literal `127.0.0.1` bind address. In\na normal bridged container, that address belongs to the container namespace and\nis not a reliable host-published service. The Dockerfile therefore provides no\nREST target and declares no exposed port. Run `flowgrid-memory-rest` directly on\nthe trusted host with the documented configuration. A future container-network\nmode requires a separate threat model and release contract.\n''',
    )
    write(
        "docs/ACCEPTANCE_CRITERIA.md",
        '''# Release acceptance criteria\n\nThis document defines stable gates. Commit-specific counts, hashes, runner\nversions, and artifact sizes belong in generated release evidence, not in this\nfile.\n\nA releasable commit must satisfy all of the following:\n\n1. Core tests pass on Python 3.11, 3.12, and 3.13.\n2. The official MCP-SDK suite passes on Python 3.11.\n3. Every transition into `confirmed` has durable same-user source evidence.\n4. Resolver truncation remains explicit through `ContextPack.completeness`.\n5. Owner review, current-state query, context compilation, transition, and\n   privacy erasure preserve their documented authorization boundaries.\n6. Context compilation respects record, item, aggregate, and final-pack limits.\n7. A wheel and sdist build successfully from the tagged source.\n8. The wheel installs in a fresh environment and exposes `flowgrid_memory` plus\n   the three product command entry points.\n9. Release assets include SHA-256 checksums, acceptance JSON, SPDX SBOM, and\n   provenance JSON.\n10. GitHub artifact attestations are created for the wheel and sdist.\n\nThe release workflow runs these gates before creating the tag and GitHub\nRelease. Generated assets are the evidence for one exact commit and version.\nLocal development can execute the same main test gate with:\n\n```bash\n./scripts/run_tests.sh --with-mcp\n```\n''',
    )
    write(
        "docs/ACCEPTANCE_V0_1.md",
        '''# FlowGrid Agent Memory v0.1 acceptance\n\nThe previous version of this page embedded mutable test counts, artifact hashes,\nlocal Docker versions, and historical review claims. Those values could drift\naway from the commit displayed by GitHub.\n\nStable gates now live in [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md). The\n`v0.1.0` GitHub Release contains commit-bound generated evidence:\n\n- `acceptance.json`\n- `checksums.txt`\n- `sbom.spdx.json`\n- `provenance.json`\n- wheel and sdist artifacts\n\nGitHub artifact attestations provide the signed build-provenance and SBOM\nrecords for the published archives.\n''',
    )
    write(
        "docs/REPOSITORY_GOVERNANCE.md",
        '''# Repository governance\n\nThe intended `main` rules are:\n\n- changes arrive through pull requests;\n- the four CI checks are required: Core Python 3.11/3.12/3.13 and MCP Python\n  3.11;\n- force pushes and branch deletion are disabled;\n- workflow and release changes are owned by `@dlxeva`;\n- releases are created only by `.github/workflows/release.yml` after its full\n  acceptance and fresh-install gates pass.\n\n`CODEOWNERS` records review ownership in the repository. GitHub branch rules are\nrepository-administration state and must mirror this document.\n''',
    )
    write(
        "docs/RELEASE_NOTES.md",
        '''# FlowGrid Agent Memory 0.1.0\n\nThe first governed-memory release establishes an evidence-backed current-truth\nlayer for local AI agents.\n\nHighlights:\n\n- confirmed records require durable same-user source evidence;\n- resolver and budget truncation are explicit through completeness metadata;\n- a local Owner Review CLI closes the human governance loop;\n- REST transitions use exact authorized primary-key metadata lookup;\n- ContextCompiler character budgeting is logarithmic and resource-bounded;\n- `flowgrid_memory` is the stable public Python namespace;\n- the OCI contract defaults to a non-networked CLI image with a separate MCP\n  stdio target;\n- release archives ship with checksums, acceptance evidence, SPDX SBOM,\n  provenance JSON, and GitHub Sigstore attestations.\n\nThe release remains alpha and supports one trusted local host. It does not claim\na hosted multitenant perimeter, production natural-language extraction quality,\nor a new official AML score.\n''',
    )
    write(".github/CODEOWNERS", "* @dlxeva\n/.github/workflows/ @dlxeva\n/Dockerfile @dlxeva\n")


def write_release_evidence_script() -> None:
    write(
        "scripts/generate_release_evidence.py",
        r'''#!/usr/bin/env python3
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]+)?", args.version):
        raise SystemExit("invalid version")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise SystemExit("invalid commit")
    if not args.tests_passed or not args.fresh_install_passed:
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
''',
    )


def write_release_workflow() -> None:
    write(
        ".github/workflows/release.yml",
        '''name: Release\n\non:\n  workflow_dispatch:\n  push:\n    branches: ["main"]\n\npermissions:\n  contents: write\n  id-token: write\n  attestations: write\n  artifact-metadata: write\n\nconcurrency:\n  group: release-${{ github.ref }}\n  cancel-in-progress: false\n\njobs:\n  release:\n    if: github.event_name == 'workflow_dispatch' || contains(github.event.head_commit.message, '[release]')\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v7\n        with:\n          fetch-depth: 0\n      - uses: actions/setup-python@v7\n        with:\n          python-version: "3.11"\n      - name: Resolve release version\n        id: version\n        shell: bash\n        run: |\n          set -euo pipefail\n          version="$(python -c 'from flowgrid_memory import __version__; print(__version__)')"\n          tag="v${version}"\n          echo "version=${version}" >> "$GITHUB_OUTPUT"\n          echo "tag=${tag}" >> "$GITHUB_OUTPUT"\n          if gh release view "$tag" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then\n            echo "release ${tag} already exists" >&2\n            exit 1\n          fi\n        env:\n          GH_TOKEN: ${{ github.token }}\n      - name: Install build and test dependencies\n        run: |\n          python -m pip install --upgrade pip 'setuptools>=68' build\n          python -m pip install -e '.[mcp]'\n      - name: Run full acceptance suite\n        run: ./scripts/run_tests.sh --with-mcp\n      - name: Build wheel and sdist\n        run: python -m build\n      - name: Fresh wheel install and smoke\n        shell: bash\n        run: |\n          set -euo pipefail\n          wheel="$(find dist -maxdepth 1 -type f -name '*.whl' -print -quit)"\n          test -n "$wheel"\n          python -m venv /tmp/flowgrid-release-venv\n          /tmp/flowgrid-release-venv/bin/python -m pip install --upgrade pip\n          /tmp/flowgrid-release-venv/bin/python -m pip install "$wheel" 'mcp>=2,<3'\n          /tmp/flowgrid-release-venv/bin/python -c 'import flowgrid_memory; assert flowgrid_memory.__version__'\n          /tmp/flowgrid-release-venv/bin/python -c 'import aml_retriever; assert aml_retriever.PRODUCT_VERSION'\n          /tmp/flowgrid-release-venv/bin/flowgrid-memory --version\n          /tmp/flowgrid-release-venv/bin/flowgrid-memory doctor --ephemeral\n          /tmp/flowgrid-release-venv/bin/flowgrid-memory demo --ephemeral\n          /tmp/flowgrid-release-venv/bin/flowgrid-memory-rest --help >/dev/null\n          /tmp/flowgrid-release-venv/bin/flowgrid-memory-mcp --help >/dev/null\n      - name: Generate checksums, acceptance, SBOM, and provenance\n        shell: bash\n        run: |\n          set -euo pipefail\n          run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"\n          python scripts/generate_release_evidence.py \\\n            --dist dist \\\n            --output release-evidence \\\n            --version "${{ steps.version.outputs.version }}" \\\n            --commit "$GITHUB_SHA" \\\n            --repository "$GITHUB_REPOSITORY" \\\n            --run-url "$run_url" \\\n            --tests-passed \\\n            --fresh-install-passed\n      - name: Attest wheel and sdist provenance\n        uses: actions/attest@v4.2.2\n        with:\n          subject-path: |\n            dist/*.whl\n            dist/*.tar.gz\n      - name: Attest SPDX SBOM\n        uses: actions/attest@v4.2.2\n        with:\n          subject-path: |\n            dist/*.whl\n            dist/*.tar.gz\n          sbom-path: release-evidence/sbom.spdx.json\n      - name: Create annotated tag and GitHub Release\n        shell: bash\n        run: |\n          set -euo pipefail\n          tag="${{ steps.version.outputs.tag }}"\n          git config user.name "github-actions[bot]"\n          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"\n          git tag -a "$tag" "$GITHUB_SHA" -m "FlowGrid Agent Memory ${tag}"\n          git push origin "$tag"\n          mkdir -p release-assets\n          cp dist/* release-assets/\n          cp release-evidence/* release-assets/\n          gh release create "$tag" release-assets/* \\\n            --repo "$GITHUB_REPOSITORY" \\\n            --verify-tag \\\n            --title "FlowGrid Agent Memory ${tag}" \\\n            --notes-file docs/RELEASE_NOTES.md\n        env:\n          GH_TOKEN: ${{ github.token }}\n''',
    )


def write_tests() -> None:
    write(
        "tests/test_public_namespace.py",
        '''"""Stable product namespace and compatibility boundary."""\nimport importlib\nimport tomllib\nimport unittest\nfrom pathlib import Path\n\nimport aml_retriever\nimport flowgrid_memory\n\n\nclass TestPublicNamespace(unittest.TestCase):\n    def test_facade_and_contracts_are_identity_preserving(self):\n        self.assertIs(flowgrid_memory.FlowGridMemory, aml_retriever.FlowGridMemory)\n        self.assertIs(flowgrid_memory.ContextPack, aml_retriever.ContextPack)\n        self.assertEqual(flowgrid_memory.__version__, aml_retriever.PRODUCT_VERSION)\n\n    def test_internal_database_and_aml_objects_are_not_public_exports(self):\n        for name in ("RetrieverDB", "MemoryService", "Store", "RetrieverConfig"):\n            self.assertFalse(hasattr(flowgrid_memory, name), name)\n            self.assertNotIn(name, flowgrid_memory.__all__)\n\n    def test_product_entrypoint_wrappers_import_without_side_effects(self):\n        for module in ("flowgrid_memory.cli", "flowgrid_memory.rest", "flowgrid_memory.mcp"):\n            loaded = importlib.import_module(module)\n            self.assertTrue(callable(loaded.main))\n\n    def test_distribution_discovers_both_namespaces(self):\n        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))\n        include = project["tool"]["setuptools"]["packages"]["find"]["include"]\n        self.assertIn("flowgrid_memory*", include)\n        scripts = project["project"]["scripts"]\n        self.assertEqual(scripts["flowgrid-memory"], "flowgrid_memory.cli:main")\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
    )
    write(
        "tests/test_release_evidence.py",
        '''"""Generated release evidence is complete and commit-bound."""\nimport json\nimport subprocess\nimport sys\nimport tempfile\nimport unittest\nfrom pathlib import Path\n\n\nclass TestReleaseEvidence(unittest.TestCase):\n    def test_generator_writes_checksums_acceptance_sbom_and_provenance(self):\n        with tempfile.TemporaryDirectory() as directory:\n            root = Path(directory)\n            dist = root / "dist"\n            output = root / "out"\n            dist.mkdir()\n            (dist / "flowgrid_agent_memory-0.1.0-py3-none-any.whl").write_bytes(b"wheel")\n            (dist / "flowgrid_agent_memory-0.1.0.tar.gz").write_bytes(b"sdist")\n            subprocess.run(\n                [\n                    sys.executable,\n                    "scripts/generate_release_evidence.py",\n                    "--dist",\n                    str(dist),\n                    "--output",\n                    str(output),\n                    "--version",\n                    "0.1.0",\n                    "--commit",\n                    "a" * 40,\n                    "--repository",\n                    "dlxeva/flowgrid-agent-memory",\n                    "--run-url",\n                    "https://github.com/dlxeva/flowgrid-agent-memory/actions/runs/1",\n                    "--tests-passed",\n                    "--fresh-install-passed",\n                ],\n                check=True,\n            )\n            expected = {\n                "checksums.txt",\n                "acceptance.json",\n                "provenance.json",\n                "sbom.spdx.json",\n            }\n            self.assertEqual({path.name for path in output.iterdir()}, expected)\n            acceptance = json.loads((output / "acceptance.json").read_text())\n            self.assertEqual(acceptance["commit"], "a" * 40)\n            self.assertEqual(len(acceptance["artifacts"]), 2)\n            sbom = json.loads((output / "sbom.spdx.json").read_text())\n            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")\n            self.assertEqual(len(sbom["files"]), 2)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
    )
    write(
        "tests/test_release_contract.py",
        '''"""Repository-level release and container contracts."""\nimport unittest\nfrom pathlib import Path\n\n\nclass TestReleaseContract(unittest.TestCase):\n    def test_default_container_is_non_networked_cli_and_mcp_is_separate(self):\n        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")\n        self.assertIn("FROM runtime AS mcp", dockerfile)\n        self.assertIn("FROM runtime AS cli", dockerfile)\n        self.assertTrue(dockerfile.rstrip().endswith('CMD ["doctor", "--ephemeral"]'))\n        self.assertNotIn('ENTRYPOINT ["flowgrid-memory-rest"]', dockerfile)\n        self.assertNotIn("EXPOSE ", dockerfile)\n\n    def test_release_workflow_requires_acceptance_evidence_and_attestation(self):\n        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")\n        for required in (\n            "./scripts/run_tests.sh --with-mcp",\n            "python -m build",\n            "generate_release_evidence.py",\n            "actions/attest@v4.2.2",\n            "artifact-metadata: write",\n            "gh release create",\n        ):\n            self.assertIn(required, workflow)\n\n    def test_mutable_acceptance_hashes_are_not_committed(self):\n        acceptance = Path("docs/ACCEPTANCE_V0_1.md").read_text(encoding="utf-8")\n        self.assertNotIn("fresh wheel SHA-256", acceptance)\n        self.assertIn("ACCEPTANCE_CRITERIA.md", acceptance)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
    )


def patch_dockerfile() -> None:
    write(
        "Dockerfile",
        '''# Multi-architecture OCI index digest resolved from Docker Official Images.\nFROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime\n\nENV PYTHONDONTWRITEBYTECODE=1 \\\n    PYTHONUNBUFFERED=1\n\nWORKDIR /opt/flowgrid-memory\nCOPY pyproject.toml README.md LICENSE /opt/flowgrid-memory/\nCOPY aml_retriever /opt/flowgrid-memory/aml_retriever\nCOPY flowgrid_memory /opt/flowgrid-memory/flowgrid_memory\nRUN python -m pip install --no-cache-dir . \\\n    && useradd --create-home --uid 10001 flowgrid\n\nUSER flowgrid\n\n# Optional stdio target. It opens no TCP listener and installs the only optional\n# runtime dependency group.\nFROM runtime AS mcp\nUSER root\nRUN python -m pip install --no-cache-dir '.[mcp]'\nUSER flowgrid\nENTRYPOINT ["flowgrid-memory-mcp"]\n\n# Default image target: a non-networked, self-cleaning diagnostic. The verified\n# REST boundary remains a literal host loopback service and intentionally has no\n# container target or exposed port.\nFROM runtime AS cli\nENTRYPOINT ["flowgrid-memory"]\nCMD ["doctor", "--ephemeral"]\n''',
    )


def main() -> None:
    patch_context()
    patch_api()
    patch_mcp()
    patch_rest()
    patch_pyproject()
    create_public_namespace()
    patch_readmes()
    patch_install()
    write_docs()
    write_release_evidence_script()
    write_release_workflow()
    write_tests()
    patch_dockerfile()


if __name__ == "__main__":
    main()
