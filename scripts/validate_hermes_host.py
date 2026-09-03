#!/usr/bin/env python3
"""Run a two-session governed-memory smoke through an isolated Hermes profile."""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
from scripts.validate_codex_host import _json_dump, _prompt, _version

SESSION_ID = re.compile(
    r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{8}_\d{6}_[0-9a-f]+)\b"
)


def _run(
    command: list[str],
    *,
    timeout: int = 60,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        input=input_text,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {Path(command[0]).name} {command[1]} ({completed.returncode})"
        )
    return completed


def _extract_answer(output: str) -> dict[str, str]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == set(OUTPUT_FIELDS):
            return value
    raise ValueError("Hermes output did not contain the required JSON object")


def _session_ids(hermes: str, profile: str) -> list[str]:
    completed = _run(
        [hermes, "-p", profile, "sessions", "list", "--source", "tool", "--limit", "10"]
    )
    found = []
    for value in SESSION_ID.findall(completed.stdout + completed.stderr):
        if value not in found:
            found.append(value)
    return found


def _successful_memory_calls(raw: str) -> int:
    count = 0
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for message in value.get("messages", []) if isinstance(value, dict) else []:
            if (
                isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_name")
                == "mcp__flowgrid_memory__memory_query_current"
                and message.get("tool_call_id")
                and "<untrusted_tool_result" in str(message.get("content", ""))
            ):
                count += 1
    return count


def _expected_from_nonce(nonce: str, session_number: int) -> tuple[dict[str, str], tuple[str, ...]]:
    expected = {
        "release_channel": f"release-{'old' if session_number == 1 else 'new'}-{nonce}",
        "stable_owner": f"owner-current-{nonce}",
        "retired_token": f"retired-token-{nonce}" if session_number == 1 else "unknown",
        "rejected_direction": "unknown",
        "pending_candidate": "unknown",
        "missing_memory": "unknown",
    }
    forbidden = (
        (f"rejected-direction-{nonce}", f"pending-candidate-{nonce}")
        if session_number == 1
        else (
            f"release-old-{nonce}",
            f"retired-token-{nonce}",
            f"rejected-direction-{nonce}",
            f"pending-candidate-{nonce}",
        )
    )
    return expected, forbidden


def _reassess_existing(output_path: Path) -> int:
    report = json.loads(output_path.read_text(encoding="utf-8"))
    nonce = report["fixture"]["nonce"]
    sessions = []
    for session_number in (1, 2):
        output_artifact = Path(f"{output_path.with_suffix('')}.session-{session_number}.output.txt")
        trace_artifact = Path(f"{output_path.with_suffix('')}.session-{session_number}.trace.jsonl")
        output = output_artifact.read_text(encoding="utf-8")
        raw_trace = trace_artifact.read_text(encoding="utf-8")
        expected, forbidden = _expected_from_nonce(nonce, session_number)
        assessment = assess_host_answer(
            _extract_answer(output),
            expected=expected,
            forbidden_values=forbidden,
            raw_output=raw_trace + output,
            memory_tool_calls=_successful_memory_calls(raw_trace),
        )
        old_session = report["sessions"][session_number - 1]
        sessions.append(
            {
                "session": session_number,
                "assessment": assessment,
                "session_id": old_session["session_id"],
            }
        )
    report["sessions"] = sessions
    passed = all(item["assessment"]["status"] == "pass" for item in sessions)
    report["status"] = "pass" if passed else "fail"
    _json_dump(output_path, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", default=shutil.which("hermes"))
    parser.add_argument("--mcp-python", default=str(Path.home() / ".hermes/hermes-agent/venv/bin/python"))
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--reassess-existing", action="store_true")
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "eval_out/hermes-host-smoke.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.hermes:
        raise SystemExit("hermes executable is unavailable")
    hermes = str(Path(args.hermes).resolve())
    mcp_python = str(Path(args.mcp_python).absolute())
    nonce = args.nonce or secrets.token_hex(8)
    profile = f"fgval{secrets.token_hex(4)}"
    output_path = Path(args.output).resolve()
    if args.reassess_existing:
        return _reassess_existing(output_path)
    raw_base = output_path.with_suffix("")
    profile_created = False

    with tempfile.TemporaryDirectory(prefix="flowgrid-hermes-host-") as directory:
        trial_dir = Path(directory)
        host_dir = trial_dir / "isolated-host"
        host_dir.mkdir()
        db_path = trial_dir / "memory.db"
        principal_path = trial_dir / "principal.json"
        state = create_host_trial(db_path=str(db_path), nonce=nonce)
        _json_dump(
            principal_path,
            {
                "principal_id": "hermes-host-smoke",
                "authority": "owner",
                "allowed_users": [state.user_id],
                "scopes": dict(state.scope),
                "permissions": ["memory:read"],
            },
        )
        prompt = _prompt(user_id=state.user_id, scope=state.scope)
        sessions = []
        artifacts: list[str] = []
        try:
            _run(
                [
                    hermes,
                    "profile",
                    "create",
                    profile,
                    "--no-alias",
                    "--no-skills",
                    "--description",
                    "Temporary synthetic FlowGrid Memory host validation profile.",
                ]
            )
            profile_created = True
            _run([hermes, "-p", profile, "config", "set", "model.provider", "deepseek"])
            _run(
                [
                    hermes,
                    "-p",
                    profile,
                    "config",
                    "set",
                    "model.default",
                    "deepseek-v4-flash",
                ]
            )
            _run(
                [
                    hermes,
                    "-p",
                    profile,
                    "mcp",
                    "add",
                    "flowgrid_memory",
                    "--command",
                    mcp_python,
                    "--connect-timeout",
                    "30",
                    "--env",
                    f"PYTHONPATH={REPO_ROOT}",
                    "--args",
                    "-m",
                    "aml_retriever.mcp_adapter",
                    "--db",
                    str(db_path),
                    "--principal-config",
                    str(principal_path),
                ],
                input_text="Y\n",
            )

            expected_sets = [(state.expected_session_one, state.forbidden_session_one)]
            for session_number in (1, 2):
                if session_number == 2:
                    expected_sets.append(advance_host_trial(state))
                expected, forbidden = expected_sets[session_number - 1]
                before = set(_session_ids(hermes, profile))
                completed = _run(
                    [
                        hermes,
                        "-p",
                        profile,
                        "chat",
                        "-q",
                        prompt,
                        "-Q",
                        "--ignore-rules",
                        "--source",
                        "tool",
                        "--in",
                        str(host_dir),
                        "--max-turns",
                        "20",
                        "--toolsets",
                        "flowgrid_memory",
                    ],
                    timeout=args.timeout,
                )
                output = completed.stdout + completed.stderr
                output_artifact = Path(f"{raw_base}.session-{session_number}.output.txt")
                output_artifact.parent.mkdir(parents=True, exist_ok=True)
                output_artifact.write_text(output, encoding="utf-8")
                answer = _extract_answer(output)
                after = _session_ids(hermes, profile)
                new_ids = [value for value in after if value not in before]
                if not new_ids:
                    raise RuntimeError("Hermes session ID was not recorded")
                session_id = new_ids[0]
                trace_artifact = Path(f"{raw_base}.session-{session_number}.trace.jsonl")
                _run(
                    [
                        hermes,
                        "-p",
                        profile,
                        "sessions",
                        "export",
                        str(trace_artifact),
                        "--session-id",
                        session_id,
                        "--format",
                        "jsonl",
                        "--redact",
                        "--yes",
                    ]
                )
                raw_trace = trace_artifact.read_text(encoding="utf-8")
                calls = _successful_memory_calls(raw_trace)
                assessment = assess_host_answer(
                    answer,
                    expected=expected,
                    forbidden_values=forbidden,
                    raw_output=raw_trace + output,
                    memory_tool_calls=calls,
                )
                sessions.append(
                    {
                        "session": session_number,
                        "assessment": assessment,
                        "session_id": session_id,
                    }
                )
                artifacts.extend([str(trace_artifact), str(output_artifact)])
        finally:
            if profile_created:
                subprocess.run(
                    [hermes, "profile", "delete", profile, "--yes"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )

        passed = all(item["assessment"]["status"] == "pass" for item in sessions)
        report = {
            "schema_version": 1,
            "status": "pass" if passed else "fail",
            "claim": "two fresh Hermes sessions used governed current memory across owner supersession and deletion",
            "scope": "synthetic local Hermes MCP smoke",
            "product_version": PRODUCT_VERSION,
            "host": {
                "name": "hermes-agent",
                "version": _version(hermes),
                "isolated_profile": True,
                "profile_deleted_after_export": True,
                "rules_and_native_memory_loaded": False,
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
                {
                    "status": "error",
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
