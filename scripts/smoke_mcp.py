#!/usr/bin/env python3
"""Official MCP SDK v2 end-to-end stdio subprocess smoke test."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters, stdio_client


REPO = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = [
    "memory_ingest_events",
    "memory_extract_candidates",
    "memory_query_current",
    "memory_compile_context",
]
SENTINEL = "MCP-STDIO-PRIVATE-SENTINEL"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server-executable",
        help="installed flowgrid-memory-mcp executable; defaults to python -m",
    )
    parser.add_argument(
        "--server-cwd",
        default=str(REPO),
        help="explicit server working directory",
    )
    return parser


async def _run(server_executable: str | None, server_cwd: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="flowgrid-mcp-stdio-") as temporary:
        root = Path(temporary)
        db_path = root / "memory.db"
        principal_path = root / "trusted-principal.json"
        principal_path.write_text(
            json.dumps(
                {
                    "principal_id": "stdio-local-test",
                    "authority": "owner",
                    "allowed_users": ["u1"],
                    "scopes": {"project": "stdio-test"},
                    "permissions": ["memory:write", "memory:extract", "memory:read"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        if server_executable:
            command = server_executable
            arguments = [
                "--db",
                str(db_path),
                "--principal-config",
                str(principal_path),
            ]
        else:
            command = sys.executable
            arguments = [
                "-m",
                "aml_retriever.mcp_adapter",
                "--db",
                str(db_path),
                "--principal-config",
                str(principal_path),
            ]

        stderr_path = root / "server-stderr.log"
        params = StdioServerParameters(
            command=command,
            args=arguments,
            env=dict(os.environ),
            cwd=server_cwd,
        )
        directive = "@flowgrid.memory/v1\n" + json.dumps(
            {
                "proposals": [
                    {
                        "memory_key": "stdio.preference",
                        "memory_type": "preference",
                        "subject": "$user",
                        "content": SENTINEL,
                    }
                ]
            },
            separators=(",", ":"),
        )

        with stderr_path.open("w+", encoding="utf-8") as stderr_capture:
            transport = stdio_client(params, errlog=stderr_capture)
            server_process = None
            with anyio.fail_after(30):
                async with Client(transport) as client:
                    # Test-only handle used solely to prove the official
                    # transport reaped its subprocess at context exit.
                    frame = getattr(getattr(transport, "gen", None), "ag_frame", None)
                    if frame is not None:
                        server_process = frame.f_locals.get("process")
                    listed = await client.list_tools()
                    tools = [tool.name for tool in listed.tools]
                    resources = await client.list_resources()
                    templates = await client.list_resource_templates()
                    prompts = await client.list_prompts()
                    if tools != EXPECTED_TOOLS:
                        raise AssertionError("unexpected tool surface")
                    if resources.resources or templates.resource_templates or prompts.prompts:
                        raise AssertionError("unexpected MCP discovery surface")

                    ingested = await client.call_tool(
                        "memory_ingest_events",
                        {
                            "request_id": "stdio-ingest-1",
                            "user_id": "u1",
                            "messages": [{"role": "user", "content": directive}],
                            "scope": {"project": "stdio-test"},
                        },
                    )
                    extracted = await client.call_tool(
                        "memory_extract_candidates",
                        {
                            "user_id": "u1",
                            "raw_event_ids": ingested.structured_content["raw_event_ids"],
                            "idempotency_key": "stdio-extract-1",
                            "scope": {"project": "stdio-test"},
                        },
                    )
                    current = await client.call_tool(
                        "memory_query_current",
                        {
                            "user_id": "u1",
                            "memory_key": "stdio.preference",
                            "scope": {"project": "stdio-test"},
                        },
                    )
                    context = await client.call_tool(
                        "memory_compile_context",
                        {
                            "user_id": "u1",
                            "memory_key": "stdio.preference",
                            "scope": {"project": "stdio-test"},
                            "max_chars": 4096,
                        },
                    )
                    forbidden = await client.call_tool(
                        "MCP-STDIO-UNKNOWN-SENTINEL", {}
                    )
                    forged = await client.call_tool(
                        "memory_query_current",
                        {
                            "user_id": "u1",
                            "memory_key": "x",
                            "scope": {"project": "stdio-test"},
                            "principal": "MCP-STDIO-FORGED-SENTINEL",
                        },
                    )
            stderr_capture.seek(0)
            stderr_text = stderr_capture.read()
        for private in (SENTINEL, str(db_path), str(principal_path), "UNKNOWN-SENTINEL", "FORGED-SENTINEL"):
            if private in stderr_text:
                raise AssertionError("stdio stderr disclosed private input")
        if extracted.structured_content["proposal_count"] != 1:
            raise AssertionError("candidate extraction failed")
        for result in (current, context):
            if result.structured_content["status"] != "unknown":
                raise AssertionError("candidate was disclosed as current")
            if not result.structured_content["owner_gate_required"]:
                raise AssertionError("owner gate was lost")
            if SENTINEL in json.dumps(result.model_dump(by_alias=True)):
                raise AssertionError("candidate body leaked")
        if forbidden.structured_content["error"]["code"] != "tool_not_available":
            raise AssertionError("forbidden tool was not rejected")
        if forged.structured_content["error"]["code"] != "invalid_request":
            raise AssertionError("forged principal was not rejected")

        # The official stdio transport context must reap its child before it
        # returns.  This proof uses the transport's test-only process handle;
        # product code never reaches into the SDK implementation.
        if server_process is None or server_process.returncode is None:
            raise AssertionError("stdio server process was not reaped")

        return {
            "status": "ok",
            "sdk_transport": "official-client-stdio-subprocess",
            "tool_count": len(tools),
            "resources": 0,
            "templates": 0,
            "prompts": 0,
            "candidate_unknown": True,
            "owner_gate": True,
            "stderr_private_data": False,
            "orphan_process": False,
        }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = anyio.run(_run, args.server_executable, args.server_cwd)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
