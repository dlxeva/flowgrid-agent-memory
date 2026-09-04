"""Safe local CLI for the installable FlowGrid Agent Memory product."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

from ._version import AML_ADAPTER_VERSION, PRODUCT_VERSION
from .access import PERMISSION_AUDIT, PERMISSION_READ, AccessContext, DisclosurePolicy
from .extraction import DIRECTIVE_PREFIX
from .facade import FlowGridMemory
from .migrations import SchemaReport, inspect_schema
from .owner_review import OwnerReviewSession


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INCOMPATIBLE = 3
EXIT_OPERATIONAL = 4
EXIT_INTERRUPTED = 130


def _emit(payload: dict[str, object], *, stream=None) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream or sys.stdout,
    )


def _fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def _safe_schema(report: SchemaReport) -> dict[str, object]:
    """Schema report intentionally contains no filesystem path."""

    return report.to_dict()


def run_governed_demo(db_path: str) -> dict[str, object]:
    """Run a real governed flow and return only non-content proof signals."""

    demo_nonce = uuid.uuid4().hex
    user_id = f"flowgrid-demo-{demo_nonce}"
    scope = {"project": "flowgrid-local-demo"}
    proposal = {
        "memory_key": "demo.response_style",
        "memory_type": "preference",
        "subject": "$user",
        "content": "concise and evidence-first",
        "confidence": 1.0,
    }
    directive = DIRECTIVE_PREFIX + "\n" + json.dumps(
        {"proposals": [proposal]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    access = AccessContext(
        principal_id="local-demo-owner",
        authority="owner",
        scopes=scope,
        permissions=frozenset({PERMISSION_READ}),
        purpose="local governed demo",
        allowed_users=frozenset({user_id}),
    )
    audit_access = AccessContext(
        principal_id="local-demo-auditor",
        authority="owner",
        scopes=scope,
        permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
        purpose="local demo lifecycle verification",
        allowed_users=frozenset({user_id}),
    )
    audit_policy = DisclosurePolicy(
        allowed_audit_purposes=frozenset({"local demo lifecycle verification"})
    )

    with FlowGridMemory(db_path=db_path) as memory:
        ingested = memory.ingest_raw_events(
            request_id=f"demo-ingest-{demo_nonce}",
            user_id=user_id,
            session_id=f"demo-session-{demo_nonce}",
            messages=({"role": "user", "content": directive},),
        )
        compiled = memory.extract_candidates(
            user_id=user_id,
            raw_event_ids=ingested.raw_event_ids,
            idempotency_key=f"demo-extract-{demo_nonce}",
            trusted_scope=scope,
        )
        before = memory.query_current(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
        )
        if compiled.proposal_count != 1 or len(compiled.record_ids) != 1:
            raise RuntimeError("demo invariant failed")
        confirmed = memory.transition_memory(
            user_id=user_id,
            record_id=compiled.record_ids[0],
            target_status="confirmed",
            actor="local-demo-owner",
            actor_authority="owner",
            reason="owner confirmed the direct-user source evidence",
        )
        after = memory.query_current(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
        )
        pack = memory.compile_context(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
            max_chars=4096,
        )
        replacement_source = memory.ingest_raw_events(
            request_id=f"demo-replacement-source-{demo_nonce}",
            user_id=user_id,
            session_id=f"demo-replacement-session-{demo_nonce}",
            messages=(
                {
                    "role": "user",
                    "content": "Prefer detailed answers when reviewing an incident.",
                },
            ),
        )
        replacement = memory.propose_memory(
            user_id=user_id,
            memory_key="demo.response_style",
            memory_type="preference",
            subject=user_id,
            content="detailed during incident review",
            source_event_ids=replacement_source.raw_event_ids,
            status="candidate",
            authority="user",
            created_by="local-demo-deterministic-proposer",
            scope=scope,
            supersedes_record_id=confirmed.id,
            state_reason="new direct-user evidence proposed a replacement",
        )
        replacement_before = memory.query_current(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
        )
        replacement_confirmed = memory.transition_memory(
            user_id=user_id,
            record_id=replacement.id,
            target_status="confirmed",
            actor="local-demo-owner",
            actor_authority="owner",
            reason="owner confirmed replacement and explicit supersession",
        )
        final_current = memory.query_current(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
        )
        final_pack = memory.compile_context(
            user_id=user_id,
            access_context=access,
            memory_key="demo.response_style",
            max_chars=4096,
        )
        audit = memory.query_audit(
            user_id=user_id,
            access_context=audit_access,
            memory_key="demo.response_style",
            disclosure_policy=audit_policy,
        )

    before_state = before.state
    after_state = after.state
    replacement_before_state = replacement_before.state
    final_state = final_current.state
    checks = {
        "raw_event_ingested": len(ingested.raw_event_ids) == 1,
        "candidate_created": compiled.proposal_count == 1,
        "candidate_not_current": bool(
            before.allowed
            and before_state is not None
            and before_state.abstain
            and before_state.current_status == "unknown"
            and before_state.owner_gate_required
            and not before_state.records
        ),
        "owner_confirmed_current": bool(
            after.allowed
            and after_state is not None
            and not after_state.abstain
            and after_state.current_status == "confirmed"
            and len(after_state.records) == 1
            and confirmed.current_status == "confirmed"
        ),
        "authorized_context_ready": bool(
            pack.status == "ready" and not pack.abstain and len(pack.items) == 1
        ),
        "replacement_candidate_did_not_override_current": bool(
            replacement_before.allowed
            and replacement_before_state is not None
            and [item.id for item in replacement_before_state.records] == [confirmed.id]
        ),
        "authorized_supersession_hides_old": bool(
            replacement_confirmed.current_status == "confirmed"
            and final_current.allowed
            and final_state is not None
            and [item.id for item in final_state.records] == [replacement.id]
            and final_pack.status == "ready"
            and [item.get("id") for item in final_pack.items] == [replacement.id]
        ),
        "authorized_audit_preserves_lifecycle": bool(
            audit.allowed
            and audit.state is not None
            and {item.id for item in audit.state.records} == {confirmed.id, replacement.id}
            and {item.current_status for item in audit.state.records}
            == {"superseded", "confirmed"}
        ),
    }
    if not all(checks.values()):
        raise RuntimeError("demo invariant failed")
    return {
        "status": "ok",
        "product": "FlowGrid Agent Memory",
        "flow": (
            "raw_event->candidate->owner_confirmation->current_state->context_pack"
            "->replacement_candidate->authorized_supersession"
        ),
        "checks": checks,
        "context_status": final_pack.status,
        "memory_content_emitted": False,
    }


def _add_database_choice(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db", metavar="PATH", help="explicit SQLite database path")
    group.add_argument(
        "--ephemeral",
        action="store_true",
        help="use an automatically removed temporary SQLite database",
    )


def _doctor_for_path(db_path: str) -> tuple[dict[str, object], int]:
    report = inspect_schema(db_path)
    fts5 = _fts5_available()
    ok = fts5 and report.status == "ready" and report.compatible
    payload: dict[str, object] = {
        "status": "ok" if ok else "attention_required",
        "product_version": PRODUCT_VERSION,
        "aml_adapter_version": AML_ADAPTER_VERSION,
        "python_supported": sys.version_info >= (3, 11),
        "sqlite_fts5": fts5,
        "schema": _safe_schema(report),
        "database_opened_writable": False,
    }
    return payload, EXIT_OK if ok else EXIT_INCOMPATIBLE


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.ephemeral:
        with tempfile.TemporaryDirectory(prefix="flowgrid-memory-doctor-") as directory:
            db_path = str(Path(directory) / "doctor.db")
            with FlowGridMemory(db_path=db_path):
                pass
            payload, code = _doctor_for_path(db_path)
            # The writable open was explicit and constrained to an ephemeral
            # directory; the schema inspection itself remained read-only.
            payload["database_opened_writable"] = True
            payload["ephemeral_cleanup"] = "automatic"
            _emit(payload)
            return code
    if args.db == ":memory:":
        raise ValueError("use --ephemeral instead of --db :memory:")
    payload, code = _doctor_for_path(args.db)
    _emit(payload)
    return code


def _cmd_demo(args: argparse.Namespace) -> int:
    if args.ephemeral:
        with tempfile.TemporaryDirectory(prefix="flowgrid-memory-demo-") as directory:
            result = run_governed_demo(str(Path(directory) / "demo.db"))
            result["ephemeral_cleanup"] = "automatic"
            _emit(result)
            return EXIT_OK
    if args.db == ":memory:":
        raise ValueError("use --ephemeral instead of --db :memory:")
    _emit(run_governed_demo(args.db))
    return EXIT_OK


def _parse_review_scope(values: list[str]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise ValueError("review scope must use KEY=VALUE")
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value or key in scope:
            raise ValueError("review scope is invalid")
        scope[key] = value
    return scope


def _cmd_review(args: argparse.Namespace) -> int:
    if args.db == ":memory:" or not Path(args.db).is_file():
        # A typo must not create a fresh empty database and falsely
        # report that no candidates require review.
        raise ValueError("review requires an existing database")
    scope = _parse_review_scope(args.scope)
    if args.decision is None and args.reason is not None:
        raise ValueError("reason requires a decision")
    if args.decision is not None and (args.record is None or args.reason is None):
        raise ValueError("decision requires record and reason")
    with FlowGridMemory(db_path=args.db) as memory:
        review = OwnerReviewSession(
            memory=memory,
            user_id=args.user,
            actor=args.actor,
            scope=scope,
        )
        if args.decision is None:
            result = review.list_pending(
                limit=args.limit,
                record_id=args.record,
            )
        else:
            result = review.decide(
                record_id=args.record,
                decision=args.decision,
                reason=args.reason,
            )
    _emit(result.to_dict())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowgrid-memory",
        description="Local governed FlowGrid Agent Memory",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=(
            f"%(prog)s {PRODUCT_VERSION} "
            f"(AML Add/Search adapter {AML_ADAPTER_VERSION})"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser(
        "doctor", help="read-only schema and runtime compatibility check"
    )
    _add_database_choice(doctor)
    doctor.set_defaults(func=_cmd_doctor)

    demo = subcommands.add_parser(
        "demo", help="run the governed candidate-to-context proof flow"
    )
    _add_database_choice(demo)
    demo.set_defaults(func=_cmd_demo)

    review = subcommands.add_parser(
        "review", help="inspect and decide exact-scope owner candidates"
    )
    review.add_argument(
        "--db", required=True, metavar="PATH", help="existing SQLite database path"
    )
    review.add_argument("--user", required=True, help="concrete governed user ID")
    review.add_argument("--actor", required=True, help="owner identity written to audit events")
    review.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="exact review scope; repeat for multiple fields",
    )
    review.add_argument(
        "--limit", type=int, default=20, help="queue item limit (1-100)"
    )
    review.add_argument(
        "--record", default=None, help="inspect or decide one pending record"
    )
    review.add_argument(
        "--decision", choices=("confirm", "reject"), default=None
    )
    review.add_argument(
        "--reason", default=None, help="required audit reason for a decision"
    )
    review.set_defaults(func=_cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _emit(
            {"status": "error", "reason": "local_memory_operation_interrupted"},
            stream=sys.stderr,
        )
        return EXIT_INTERRUPTED
    except Exception:
        # Never echo exception text: a lower layer or injected extractor could
        # include a memory body, path, credential, or traceback detail.
        _emit(
            {"status": "error", "reason": "local_memory_operation_failed"},
            stream=sys.stderr,
        )
        return EXIT_OPERATIONAL


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_INCOMPATIBLE",
    "EXIT_OPERATIONAL",
    "EXIT_INTERRUPTED",
    "run_governed_demo",
    "build_parser",
    "main",
]
