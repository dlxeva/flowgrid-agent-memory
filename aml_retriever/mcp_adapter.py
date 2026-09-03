"""Optional official MCP SDK v2 stdio adapter.

Importing this module is safe in the dependency-free base installation.  The
official SDK is imported behind a guarded boundary and is required only when a
server is actually created.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from ._version import PRODUCT_VERSION
from .auth import TrustedPrincipal
from .mcp_tools import GovernedMCPTools, TOOL_NAMES

try:  # Optional dependency: base installs must remain importable without it.
    from mcp import types as _mcp_types
    from mcp.server import MCPServer as _MCPServer
except ImportError:  # pragma: no cover - exercised in the base environment
    _mcp_types = None
    _MCPServer = None


class MCPDependencyError(RuntimeError):
    """Fixed, non-sensitive signal that the optional SDK is unavailable."""

    def __init__(self):
        super().__init__("mcp_dependency_unavailable")


_TOOL_ARGUMENTS = {
    "memory_ingest_events": frozenset(
        {"request_id", "user_id", "messages", "session_id", "scope"}
    ),
    "memory_extract_candidates": frozenset(
        {"user_id", "raw_event_ids", "idempotency_key", "scope"}
    ),
    "memory_query_current": frozenset(
        {
            "user_id",
            "memory_key",
            "query",
            "scope",
            "max_records",
            "max_chars",
        }
    ),
    "memory_compile_context": frozenset(
        {
            "user_id",
            "memory_key",
            "query",
            "scope",
            "max_records",
            "max_chars",
            "max_tokens",
        }
    ),
}


def _sdk_error(code: str):
    """Build a fixed official-SDK CallToolResult without reflecting input."""

    if code not in {"invalid_request", "operation_failed", "tool_not_available"}:
        code = "operation_failed"
    assert _mcp_types is not None
    payload = {"status": "error", "error": {"code": code}}
    return _mcp_types.CallToolResult(
        content=[_mcp_types.TextContent(type="text", text=code)],
        structured_content=payload,
        is_error=True,
    )


def _safe_core_result(operation):
    """Keep unexpected core exceptions below the SDK logging boundary."""

    try:
        return operation()
    except Exception:
        return {"status": "error", "error": {"code": "operation_failed"}}


class _SafeToolBoundary:
    """Official ServerMiddleware that rejects reflective/extra tool input.

    SDK v2 intentionally ignores extra function arguments and its default tool
    errors may include the requested tool name or exception text.  This
    transport boundary performs exact raw-argument allowlisting before SDK
    validation and replaces unknown/validation/execution failures with fixed
    public results.
    """

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params
        if not isinstance(params, Mapping):
            return _sdk_error("invalid_request")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _sdk_error("tool_not_available")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            return _sdk_error("invalid_request")
        if set(arguments) - _TOOL_ARGUMENTS[name]:
            return _sdk_error("invalid_request")

        try:
            result = await call_next(ctx)
        except Exception:
            return _sdk_error("operation_failed")

        # Core failures are structured values so their fixed code survives
        # output-schema validation.  Mark them as tool errors for MCP clients.
        if _mcp_types is not None and isinstance(result, _mcp_types.CallToolResult):
            structured = result.structured_content
            if result.is_error:
                # The SDK's default execution error includes ``str(exc)``.
                # Preserve only errors already created by our fixed core;
                # replace every other tool failure before it reaches the wire.
                if not (
                    isinstance(structured, Mapping)
                    and structured.get("status") == "error"
                    and isinstance(structured.get("error"), Mapping)
                    and structured["error"].get("code")
                    in {
                        "access_denied",
                        "invalid_request",
                        "operation_failed",
                        "token_budget_unsupported",
                        "tool_closed",
                    }
                ):
                    return _sdk_error("operation_failed")
                return result
            if isinstance(structured, Mapping) and structured.get("status") == "error":
                return result.model_copy(update={"is_error": True})
        elif isinstance(result, Mapping):
            # ServerRunner presents middleware with the alias-serialized wire
            # dictionary.  Apply the same fixed-error rule to that public SDK
            # representation (``structuredContent`` / ``isError``).
            structured = result.get("structuredContent")
            fixed_core_error = (
                isinstance(structured, Mapping)
                and structured.get("status") == "error"
                and isinstance(structured.get("error"), Mapping)
                and structured["error"].get("code")
                in {
                    "access_denied",
                    "invalid_request",
                    "operation_failed",
                    "token_budget_unsupported",
                    "tool_closed",
                }
            )
            if result.get("isError") is True and not fixed_core_error:
                return _sdk_error("operation_failed")
            if fixed_core_error:
                safe_result = dict(result)
                safe_result["isError"] = True
                return safe_result
        return result


def create_mcp_server(*, db_path: str, principal: TrustedPrincipal):
    """Create the official SDK v2 server with exactly four safe tools.

    No database path or identity is inferred from the environment or current
    directory.  The facade is closed by the server lifespan on every normal or
    exceptional client disconnect.
    """

    if _MCPServer is None or _mcp_types is None:
        raise MCPDependencyError()
    if not isinstance(principal, TrustedPrincipal):
        raise TypeError("principal must be TrustedPrincipal")
    if not isinstance(db_path, str) or not _absolute_or_memory(db_path):
        raise ValueError("db_path must be absolute or ':memory:'")

    core = GovernedMCPTools(db_path=db_path, principal=principal)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {"flowgrid_memory": "active"}
        finally:
            core.close()

    server = _MCPServer(
        name="flowgrid-agent-memory",
        title="FlowGrid Agent Memory",
        description="Governed local current-state memory tools",
        instructions=(
            "Candidate extraction never confirms memory. Current/context tools "
            "may abstain and require owner confirmation."
        ),
        version=PRODUCT_VERSION,
        lifespan=lifespan,
        middleware=[_SafeToolBoundary()],
        log_level="ERROR",
    )

    @server.tool(
        name="memory_ingest_events",
        description="Ingest immutable raw events for an authorized user.",
        structured_output=True,
    )
    def memory_ingest_events(
        request_id: str,
        user_id: str,
        messages: list[dict[str, str]],
        session_id: str | None = None,
        scope: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return _safe_core_result(
            lambda: core.memory_ingest_events(
                request_id=request_id,
                user_id=user_id,
                messages=messages,
                session_id=session_id,
                scope=scope,
            )
        )

    @server.tool(
        name="memory_extract_candidates",
        description="Run only the built-in explicit directive candidate extractor.",
        structured_output=True,
    )
    def memory_extract_candidates(
        user_id: str,
        raw_event_ids: list[str],
        idempotency_key: str,
        scope: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return _safe_core_result(
            lambda: core.memory_extract_candidates(
                user_id=user_id,
                raw_event_ids=raw_event_ids,
                idempotency_key=idempotency_key,
                scope=scope,
            )
        )

    @server.tool(
        name="memory_query_current",
        description="Return only the governed current-state public context envelope.",
        structured_output=True,
    )
    def memory_query_current(
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        scope: dict[str, str] | None = None,
        max_records: int = 100,
        max_chars: int = 32_768,
    ) -> dict[str, object]:
        return _safe_core_result(
            lambda: core.memory_query_current(
                user_id=user_id,
                memory_key=memory_key,
                query=query,
                scope=scope,
                max_records=max_records,
                max_chars=max_chars,
            )
        )

    @server.tool(
        name="memory_compile_context",
        description="Compile a character-budgeted governed current context pack.",
        structured_output=True,
    )
    def memory_compile_context(
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        scope: dict[str, str] | None = None,
        max_records: int = 100,
        max_chars: int = 32_768,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        return _safe_core_result(
            lambda: core.memory_compile_context(
                user_id=user_id,
                memory_key=memory_key,
                query=query,
                scope=scope,
                max_records=max_records,
                max_chars=max_chars,
                max_tokens=max_tokens,
            )
        )

    return server


def _load_principal(path: str) -> TrustedPrincipal:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("invalid principal config")
    required = {"principal_id", "authority", "allowed_users", "scopes", "permissions"}
    optional = {"purpose", "allowed_audit_purposes"}
    if not required.issubset(value) or set(value) - required - optional:
        raise ValueError("invalid principal config")
    allowed_users = value["allowed_users"]
    permissions = value["permissions"]
    allowed_audit_purposes = value.get("allowed_audit_purposes", [])
    if not isinstance(allowed_users, list) or not isinstance(permissions, list):
        raise ValueError("invalid principal config")
    if not isinstance(allowed_audit_purposes, list):
        raise ValueError("invalid principal config")
    return TrustedPrincipal(
        principal_id=value["principal_id"],
        authority=value["authority"],
        allowed_users=frozenset(allowed_users),
        scopes=value["scopes"],
        permissions=frozenset(permissions),
        purpose=value.get("purpose"),
        allowed_audit_purposes=frozenset(allowed_audit_purposes),
    )


def _absolute_or_memory(value: str) -> bool:
    return value == ":memory:" or Path(value).is_absolute()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowgrid-memory-mcp",
        description="Run the local FlowGrid Agent Memory MCP v2 stdio server.",
    )
    parser.add_argument("--db", required=True, help="absolute SQLite path")
    parser.add_argument(
        "--principal-config",
        required=True,
        help="absolute trusted-principal JSON path",
    )
    args = parser.parse_args(argv)

    try:
        if not _absolute_or_memory(args.db) or not Path(args.principal_config).is_absolute():
            raise ValueError("paths must be explicit")
        principal = _load_principal(args.principal_config)
        server = create_mcp_server(db_path=args.db, principal=principal)
    except MCPDependencyError:
        print("flowgrid-memory-mcp: dependency_unavailable", file=sys.stderr)
        return 3
    except Exception:
        print("flowgrid-memory-mcp: configuration_error", file=sys.stderr)
        return 2

    try:
        # The official SDK owns framing and protocol negotiation.  Nothing in
        # this adapter prints to stdout before or during the stdio session.
        server.run()
        return 0
    except KeyboardInterrupt:  # pragma: no cover - interactive process path
        return 130
    except Exception:
        print("flowgrid-memory-mcp: runtime_error", file=sys.stderr)
        return 4


if __name__ == "__main__":  # pragma: no cover - console entry path
    raise SystemExit(main())


__all__ = ["MCPDependencyError", "create_mcp_server", "main"]
