#!/usr/bin/env python3
"""Run a two-session Codex MCP governed-memory smoke with synthetic data."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aml_retriever._version import PRODUCT_VERSION
from aml_retriever.evaluation.host_validation import (
    OUTPUT_FIELDS,
    advance_host_trial,
    assess_host_answer,
    create_host_trial,
)


def _json_dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _mcp_available(python: str) -> bool:
    result = subprocess.run(
        [python, "-c", "import mcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def _resolve_mcp_python(explicit: str | None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(sys.executable)
    candidates.append(str(Path.home() / ".hermes/hermes-agent/venv/bin/python"))
    for candidate in candidates:
        resolved = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
        if resolved and Path(resolved).is_file() and _mcp_available(str(resolved)):
            # Keep a virtualenv launcher path intact. Resolving this symlink selects
            # the base interpreter and silently drops the venv's MCP installation.
            return str(Path(resolved).absolute())
    raise RuntimeError("official MCP SDK v2 Python is unavailable")


def _toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _count_memory_tool_calls(raw_jsonl: str) -> int:
    count = 0
    for line in raw_jsonl.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = value.get("item", {}) if isinstance(value, dict) else {}
        if (
            value.get("type") == "item.completed"
            and item.get("type") == "mcp_tool_call"
            and item.get("tool") in {"memory_query_current", "memory_compile_context"}
            and item.get("status") == "completed"
            and not item.get("error")
        ):
            count += 1
    return count


def _prompt(*, user_id: str, scope: Mapping[str, str]) -> str:
    fields = "\n".join(f"- {field}" for field in OUTPUT_FIELDS)
    key_map = {
        "release_channel": "project.release_channel",
        "stable_owner": "project.stable_owner",
        "retired_token": "project.retired_token",
        "rejected_direction": "project.rejected_direction",
        "pending_candidate": "project.pending_candidate",
        "missing_memory": "project.missing_memory",
    }
    return (
        "You are an isolated Agent host validation session. Use the FlowGrid Memory MCP "
        "current-query or context tool to retrieve governed memory for the exact user and "
        "scope below. Do not use shell commands, files, prior knowledge, or inference. "
        "For each output field, query the mapped memory key. If the governed result is not "
        "status ready, has no current item, abstains, or requires an owner gate, return the "
        "literal string unknown. Never use candidate, rejected, superseded, or deleted content. "
        "Return only the required JSON object.\n\n"
        f"user_id: {user_id}\n"
        f"scope: {json.dumps(dict(scope), sort_keys=True, separators=(',', ':'))}\n"
        f"field_to_memory_key: {json.dumps(key_map, sort_keys=True, separators=(',', ':'))}\n"
        f"required_fields:\n{fields}\n"
    )


def _schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {field: {"type": "string"} for field in OUTPUT_FIELDS},
        "required": list(OUTPUT_FIELDS),
        "additionalProperties": False,
    }


def _version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return (result.stdout or result.stderr).strip().splitlines()[-1]


def _run_session(
    *,
    codex: str,
    model: str,
    mcp_python: str,
    workdir: Path,
    db_path: Path,
    principal_path: Path,
    schema_path: Path,
    final_path: Path,
    prompt: str,
    timeout: int,
) -> tuple[object, str, str, int]:
    config = [
        ("mcp_servers.flowgrid_memory.command", mcp_python),
        (
            "mcp_servers.flowgrid_memory.args",
            [
                "-m",
                "aml_retriever.mcp_adapter",
                "--db",
                str(db_path),
                "--principal-config",
                str(principal_path),
            ],
        ),
        ("mcp_servers.flowgrid_memory.env.PYTHONPATH", str(REPO_ROOT)),
        ("mcp_servers.flowgrid_memory.startup_timeout_sec", 30),
        ("mcp_servers.flowgrid_memory.tool_timeout_sec", 30),
    ]
    command = [
        codex,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "memories",
        "--disable",
        "skill_search",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "--cd",
        str(workdir),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
    ]
    for key, value in config:
        command.extend(["--config", f"{key}={_toml_value(value)}"])
    command.append(prompt)
    completed = subprocess.run(
        command,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=dict(os.environ),
    )
    raw = completed.stdout
    stderr = completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(f"Codex host exited with status {completed.returncode}")
    answer = json.loads(final_path.read_text(encoding="utf-8"))
    return answer, raw, stderr, _count_memory_tool_calls(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--mcp-python")
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "eval_out/codex-host-smoke.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.codex:
        raise SystemExit("codex executable is unavailable")
    codex = str(Path(args.codex).resolve())
    mcp_python = _resolve_mcp_python(args.mcp_python)
    nonce = args.nonce or secrets.token_hex(8)
    output_path = Path(args.output).resolve()

    with tempfile.TemporaryDirectory(prefix="flowgrid-codex-host-") as directory:
        trial_dir = Path(directory)
        host_dir = trial_dir / "isolated-host"
        host_dir.mkdir()
        db_path = trial_dir / "memory.db"
        principal_path = trial_dir / "principal.json"
        schema_path = trial_dir / "answer.schema.json"
        _json_dump(schema_path, _schema())

        state = create_host_trial(db_path=str(db_path), nonce=nonce)
        _json_dump(
            principal_path,
            {
                "principal_id": "codex-host-smoke",
                "authority": "owner",
                "allowed_users": [state.user_id],
                "scopes": dict(state.scope),
                "permissions": ["memory:read"],
            },
        )
        prompt = _prompt(user_id=state.user_id, scope=state.scope)

        sessions = []
        session_one_final = trial_dir / "session-one-final.json"
        answer_one, raw_one, stderr_one, calls_one = _run_session(
            codex=codex,
            model=args.model,
            mcp_python=mcp_python,
            workdir=host_dir,
            db_path=db_path,
            principal_path=principal_path,
            schema_path=schema_path,
            final_path=session_one_final,
            prompt=prompt,
            timeout=args.timeout,
        )
        assessment_one = assess_host_answer(
            answer_one,
            expected=state.expected_session_one,
            forbidden_values=state.forbidden_session_one,
            raw_output=raw_one + stderr_one,
            memory_tool_calls=calls_one,
        )
        sessions.append({"session": 1, "assessment": assessment_one})

        expected_two, forbidden_two = advance_host_trial(state)
        session_two_final = trial_dir / "session-two-final.json"
        answer_two, raw_two, stderr_two, calls_two = _run_session(
            codex=codex,
            model=args.model,
            mcp_python=mcp_python,
            workdir=host_dir,
            db_path=db_path,
            principal_path=principal_path,
            schema_path=schema_path,
            final_path=session_two_final,
            prompt=prompt,
            timeout=args.timeout,
        )
        assessment_two = assess_host_answer(
            answer_two,
            expected=expected_two,
            forbidden_values=forbidden_two,
            raw_output=raw_two + stderr_two,
            memory_tool_calls=calls_two,
        )
        sessions.append({"session": 2, "assessment": assessment_two})

        raw_base = output_path.with_suffix("")
        raw_one_path = Path(f"{raw_base}.session-1.jsonl")
        raw_two_path = Path(f"{raw_base}.session-2.jsonl")
        raw_one_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_one_path = Path(f"{raw_base}.session-1.stderr.txt")
        stderr_two_path = Path(f"{raw_base}.session-2.stderr.txt")
        raw_one_path.write_text(raw_one, encoding="utf-8")
        raw_two_path.write_text(raw_two, encoding="utf-8")
        stderr_one_path.write_text(stderr_one, encoding="utf-8")
        stderr_two_path.write_text(stderr_two, encoding="utf-8")

        try:
            mcp_version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            version_probe = subprocess.run(
                [mcp_python, "-c", "import importlib.metadata; print(importlib.metadata.version('mcp'))"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            mcp_version = version_probe.stdout.strip()

        passed = all(item["assessment"]["status"] == "pass" for item in sessions)
        report = {
            "schema_version": 1,
            "status": "pass" if passed else "fail",
            "claim": "two fresh Codex sessions used governed current memory across owner supersession and deletion",
            "scope": "synthetic local Codex MCP smoke",
            "product_version": PRODUCT_VERSION,
            "host": {
                "name": "codex-cli",
                "version": _version(codex),
                "model": args.model,
                "ephemeral_sessions": True,
                "user_config_loaded": False,
                "rules_loaded": False,
            },
            "transport": {
                "name": "official-mcp-sdk-stdio",
                "sdk_version": mcp_version,
                "server": "flowgrid-agent-memory",
            },
            "fixture": {
                "nonce": nonce,
                "synthetic_only": True,
                "session_count": 2,
                "owner_update_between_sessions": ["supersession", "deletion"],
            },
            "sessions": sessions,
            "raw_artifacts": [
                str(raw_one_path),
                str(stderr_one_path),
                str(raw_two_path),
                str(stderr_two_path),
            ],
            "boundaries": [
                "not a no-memory or AML retrieval comparative trial",
                "not a real-user longitudinal benefit claim",
                "not an ordinary-language extraction claim",
            ],
        }
        _json_dump(output_path, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "reason": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
