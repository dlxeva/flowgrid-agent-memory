#!/usr/bin/env python3
"""Run the governed-memory Codex smoke through the interactive TUI host."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pexpect

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aml_retriever._version import PRODUCT_VERSION
from aml_retriever.evaluation.host_validation import (
    advance_host_trial,
    assess_host_answer,
    create_host_trial,
)
from scripts.validate_codex_host import (
    _json_dump,
    _prompt,
    _resolve_mcp_python,
    _toml_value,
    _version,
)

ANSI_ESCAPE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _read_rollout(path: Path) -> tuple[str | None, int]:
    final_message = None
    successful_calls = 0
    if not path.is_file():
        return final_message, successful_calls
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload", {})
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "mcp_tool_call_end":
            invocation = payload.get("invocation", {})
            result = payload.get("result", {})
            if (
                invocation.get("server") == "flowgrid_memory"
                and invocation.get("tool")
                in {"memory_query_current", "memory_compile_context"}
                and isinstance(result, dict)
                and "Ok" in result
            ):
                successful_calls += 1
        if payload.get("type") == "task_complete":
            final_message = payload.get("last_agent_message")
    return final_message, successful_calls


def _copy_auth(codex_home: Path) -> None:
    source = Path.home() / ".codex/auth.json"
    if not source.is_file():
        raise RuntimeError("Codex auth file is unavailable")
    codex_home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, codex_home / "auth.json")


def _run_tui_session(
    *,
    codex: str,
    model: str,
    mcp_python: str,
    workdir: Path,
    codex_home: Path,
    db_path: Path,
    principal_path: Path,
    prompt: str,
    timeout: int,
    transcript_path: Path,
) -> tuple[object, str, int, Path]:
    _copy_auth(codex_home)
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
        ("mcp_servers.flowgrid_memory.default_tools_approval_mode", "approve"),
        ("mcp_servers.flowgrid_memory.enabled_tools", ["memory_query_current", "memory_compile_context"]),
    ]
    args = [
        "--no-alt-screen",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "--cd",
        str(workdir),
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "memories",
        "--disable",
        "skill_search",
    ]
    for key, value in config:
        args.extend(["--config", f"{key}={_toml_value(value)}"])
    args.append(prompt)

    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env["TERM"] = "xterm-256color"
    transcript_chunks: list[str] = []
    child = pexpect.spawn(
        codex,
        args,
        cwd=str(workdir),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=1,
        dimensions=(48, 180),
    )
    deadline = time.monotonic() + timeout
    rollout_path = None
    final_message = None
    successful_calls = 0
    trusted_generated_directory = False
    try:
        while time.monotonic() < deadline:
            try:
                transcript_chunks.append(child.read_nonblocking(size=8192, timeout=0.5))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("".join(transcript_chunks), encoding="utf-8")
            normalized_terminal = re.sub(
                r"\s+",
                "",
                ANSI_ESCAPE.sub("", "".join(transcript_chunks)),
            )
            if (
                not trusted_generated_directory
                and "Doyoutrustthecontentsofthisdirectory?" in normalized_terminal
            ):
                child.sendline("1")
                trusted_generated_directory = True
            rollout_candidates = sorted(codex_home.glob("sessions/**/*.jsonl"))
            if rollout_candidates:
                rollout_path = rollout_candidates[-1]
                final_message, successful_calls = _read_rollout(rollout_path)
                if final_message is not None:
                    break
        if final_message is None:
            raise TimeoutError("Codex TUI session did not complete")
        child.sendline("/quit")
        try:
            child.expect(pexpect.EOF, timeout=5)
        except pexpect.TIMEOUT:
            child.terminate(force=True)
    finally:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("".join(transcript_chunks), encoding="utf-8")
        if child.isalive():
            child.terminate(force=True)
    if rollout_path is None:
        raise RuntimeError("Codex TUI rollout was not created")
    answer = json.loads(final_message)
    return answer, "".join(transcript_chunks), successful_calls, rollout_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--mcp-python")
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "eval_out/codex-tui-host-smoke.json"),
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

    with tempfile.TemporaryDirectory(prefix="flowgrid-codex-tui-host-") as directory:
        trial_dir = Path(directory)
        host_dir = trial_dir / "isolated-host"
        host_dir.mkdir()
        db_path = trial_dir / "memory.db"
        principal_path = trial_dir / "principal.json"

        state = create_host_trial(db_path=str(db_path), nonce=nonce)
        _json_dump(
            principal_path,
            {
                "principal_id": "codex-tui-host-smoke",
                "authority": "owner",
                "allowed_users": [state.user_id],
                "scopes": dict(state.scope),
                "permissions": ["memory:read"],
            },
        )
        prompt = _prompt(user_id=state.user_id, scope=state.scope)
        sessions = []
        artifacts: list[str] = []
        raw_base = output_path.with_suffix("")

        expected_sets = [
            (state.expected_session_one, state.forbidden_session_one),
        ]
        for session_number in (1, 2):
            if session_number == 2:
                expected_sets.append(advance_host_trial(state))
            expected, forbidden = expected_sets[session_number - 1]
            answer, transcript, calls, rollout = _run_tui_session(
                codex=codex,
                model=args.model,
                mcp_python=mcp_python,
                workdir=host_dir,
                codex_home=trial_dir / f"codex-home-{session_number}",
                db_path=db_path,
                principal_path=principal_path,
                prompt=prompt,
                timeout=args.timeout,
                transcript_path=Path(f"{raw_base}.session-{session_number}.tty.txt"),
            )
            transcript_path = Path(f"{raw_base}.session-{session_number}.tty.txt")
            rollout_artifact = Path(f"{raw_base}.session-{session_number}.rollout.jsonl")
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(transcript, encoding="utf-8")
            shutil.copy2(rollout, rollout_artifact)
            artifacts.extend([str(rollout_artifact), str(transcript_path)])
            assessment = assess_host_answer(
                answer,
                expected=expected,
                forbidden_values=forbidden,
                raw_output=rollout_artifact.read_text(encoding="utf-8"),
                memory_tool_calls=calls,
            )
            sessions.append({"session": session_number, "assessment": assessment})

        passed = all(item["assessment"]["status"] == "pass" for item in sessions)
        report = {
            "schema_version": 1,
            "status": "pass" if passed else "fail",
            "claim": "two fresh interactive Codex sessions used governed current memory across owner supersession and deletion",
            "scope": "synthetic local Codex TUI MCP smoke",
            "product_version": PRODUCT_VERSION,
            "host": {
                "name": "codex-cli-tui",
                "version": _version(codex),
                "model": args.model,
                "isolated_codex_homes": True,
                "user_config_loaded": False,
                "project_rules_loaded": False,
            },
            "transport": {
                "name": "official-mcp-sdk-stdio",
                "server": "flowgrid-agent-memory",
            },
            "fixture": {
                "nonce": nonce,
                "synthetic_only": True,
                "session_count": 2,
                "owner_update_between_sessions": ["supersession", "deletion"],
            },
            "sessions": sessions,
            "raw_artifacts": artifacts,
            "boundaries": [
                "interactive TUI path used because codex exec 0.145.0 cancels MCP calls",
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
