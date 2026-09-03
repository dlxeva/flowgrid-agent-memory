#!/usr/bin/env python3
"""Run one bounded synthetic blind evaluation of the model extractor.

This is not an AML submission or a production-quality claim. It sends only
nonce-tagged synthetic statements to an explicitly selected model provider,
then verifies structural extraction, provenance, candidate-only persistence,
and current-state abstention locally.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aml_retriever import (
    AccessContext,
    DisclosurePolicy,
    FlowGridMemory,
    PERMISSION_AUDIT,
    PERMISSION_READ,
    QuoteAnchoredModelExtractor,
    quote_anchored_identity,
)
from aml_retriever._version import PRODUCT_VERSION


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", default=shutil.which("hermes"))
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--nonce", default=None)
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "eval_out/natural-language-extractor-blind.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.hermes:
        raise SystemExit("hermes executable is unavailable")
    hermes = str(Path(args.hermes).resolve())
    nonce = args.nonce or secrets.token_hex(6)
    output_path = Path(args.output).resolve()
    raw_output_path = output_path.with_name(output_path.stem + ".provider-output.json")
    usage_path = output_path.with_name(output_path.stem + ".usage.json")

    expected = {
        "profile.home_city": f"Cedarhaven-{nonce}",
        "project.communication_style": "weekly updates with no emojis",
        "project.milestone": f"Aurora-{nonce}",
        "workflow.deploy_check": "always run tests before publishing",
    }
    catalog = {
        "profile.home_city": {
            "memory_type": "fact",
            "description": (
                "Current home city explicitly stated by the user. Content must be exactly "
                "the city name, without surrounding sentence text."
            ),
        },
        "project.communication_style": {
            "memory_type": "preference",
            "description": (
                "Durable project communication preference. Content must be exactly the "
                "preferred style phrase and subject must be $user."
            ),
        },
        "project.milestone": {
            "memory_type": "event",
            "description": (
                "Explicitly completed project milestone. Content must be exactly the milestone name."
            ),
        },
        "workflow.deploy_check": {
            "memory_type": "procedure",
            "description": (
                "Durable deployment procedure. Content must be exactly the required action phrase, "
                "including words such as always that preserve its strength."
            ),
        },
    }
    messages = [
        {"role": "user", "content": f"I currently live in Cedarhaven-{nonce}.", "timestamp": 1_780_000_000_001},
        {"role": "user", "content": f"For Project Atlas-{nonce}, I prefer weekly updates with no emojis.", "timestamp": 1_780_000_000_002},
        {"role": "user", "content": f"On 2026-08-15, I completed milestone Aurora-{nonce}.", "timestamp": 1_780_000_000_003},
        {"role": "user", "content": "For deployments, always run tests before publishing.", "timestamp": 1_780_000_000_004},
        {"role": "user", "content": f"People from Northvale-{nonce} are always impatient.", "timestamp": 1_780_000_000_005},
        {"role": "user", "content": f"If I moved someday, I might choose Maybeport-{nonce}.", "timestamp": 1_780_000_000_006},
        {"role": "user", "content": f"Temporary API token for this example only: tok_{nonce}.", "timestamp": 1_780_000_000_007},
        {"role": "user", "content": f"Example only, not a fact: I live in Exampleton-{nonce}.", "timestamp": 1_780_000_000_008},
    ]
    forbidden_values = [
        f"Northvale-{nonce}",
        f"Maybeport-{nonce}",
        f"tok_{nonce}",
        f"Exampleton-{nonce}",
    ]

    identity = quote_anchored_identity(
        runner_config={
            "host": "hermes-oneshot",
            "provider": args.provider,
            "model": args.model,
            "temperature": "provider-default",
            "timeout_seconds": args.timeout,
        },
        key_catalog=catalog,
    )
    captured: dict[str, str] = {}

    def runner(prompt: str) -> str:
        completed = subprocess.run(
            [
                hermes,
                "--usage-file",
                str(usage_path),
                "-z",
                prompt,
                "--provider",
                args.provider,
                "-m",
                args.model,
                "--ignore-user-config",
                "--ignore-rules",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=args.timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError("provider invocation failed")
        captured["output"] = completed.stdout.strip()
        return captured["output"]

    checks: dict[str, bool] = {}
    proposal_summary: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="flowgrid-model-extraction-") as directory:
        memory = FlowGridMemory(db_path=str(Path(directory) / "memory.db"))
        try:
            ingested = memory.ingest_raw_events(
                request_id=f"blind-{nonce}",
                user_id="synthetic-user",
                session_id="synthetic-session",
                trusted_scope={"project": "synthetic-blind"},
                messages=messages,
            )
            extractor = QuoteAnchoredModelExtractor(
                identity=identity,
                runner=runner,
                key_catalog=catalog,
            )
            receipt = memory.extract_candidates(
                user_id="synthetic-user",
                raw_event_ids=ingested.raw_event_ids,
                idempotency_key=f"model-{nonce}",
                trusted_scope={"project": "synthetic-blind"},
                extractor=extractor,
            )
            access = AccessContext(
                principal_id="synthetic-evaluator",
                authority="owner",
                scopes={"user": "synthetic-user", "project": "synthetic-blind"},
                permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
                purpose="evaluation",
                allowed_users=frozenset({"synthetic-user"}),
            )
            policy = DisclosurePolicy(allowed_audit_purposes=frozenset({"evaluation"}))
            observed: dict[str, str] = {}
            all_candidate = True
            all_current_abstain = True
            source_ids = set(ingested.raw_event_ids)
            all_sources_in_batch = True
            for key in catalog:
                audit = memory.query_audit(
                    user_id="synthetic-user",
                    access_context=access,
                    memory_key=key,
                    scope={"project": "synthetic-blind"},
                    disclosure_policy=policy,
                )
                current = memory.query_current(
                    user_id="synthetic-user",
                    access_context=access,
                    memory_key=key,
                    scope={"project": "synthetic-blind"},
                )
                records = audit.state.records if audit.allowed and audit.state else []
                for record in records:
                    observed[key] = record.content
                    all_candidate = all_candidate and record.status == "candidate"
                    all_sources_in_batch = all_sources_in_batch and set(record.source_event_ids) <= source_ids
                    proposal_summary.append({
                        "memory_key": record.memory_key,
                        "memory_type": record.memory_type,
                        "content": record.content,
                        "status": record.status,
                        "source_event_count": len(record.source_event_ids),
                    })
                all_current_abstain = all_current_abstain and bool(
                    current.allowed and current.state and current.state.abstain and not current.state.records
                )

            serialized_summary = json.dumps(proposal_summary, ensure_ascii=False)
            checks = {
                "exact_expected_keys": set(observed) == set(expected),
                "exact_expected_content": observed == expected,
                "proposal_count_exact": receipt.proposal_count == len(expected),
                "all_persisted_candidate": all_candidate,
                "all_current_queries_abstain": all_current_abstain,
                "all_sources_from_input_batch": all_sources_in_batch,
                "negative_and_secret_values_absent": all(
                    value not in serialized_summary for value in forbidden_values
                ),
            }
        finally:
            memory.close()

    raw_value: object = captured.get("output", "")
    try:
        raw_value = json.loads(str(raw_value))
    except json.JSONDecodeError:
        pass
    _write_json(raw_output_path, raw_value)
    status = "pass" if checks and all(checks.values()) else "fail"
    report = {
        "schema_version": 1,
        "status": status,
        "claim": "one pinned model run passed quote-anchored synthetic extraction checks" if status == "pass" else "one pinned model run failed one or more synthetic extraction checks",
        "scope": "synthetic local adapter evaluation; not production behavior or an official AML result",
        "product_version": PRODUCT_VERSION,
        "provider": args.provider,
        "model": args.model,
        "fixture": {
            "nonce": nonce,
            "positive_cases": len(expected),
            "negative_cases": len(messages) - len(expected),
            "contains_real_user_data": False,
        },
        "checks": checks,
        "proposals": proposal_summary,
        "extractor_fingerprint": identity.fingerprint,
        "artifacts": {
            "provider_output": str(raw_output_path),
            "usage": str(usage_path),
        },
        "limitations": [
            "single provider and model configuration",
            "small synthetic English fixture",
            "no semantic sensitive-data classifier claim",
            "no live user-impact or official AML score claim",
        ],
    }
    _write_json(output_path, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
