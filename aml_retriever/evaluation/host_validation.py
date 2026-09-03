"""Reusable governed-memory fixtures for real Agent host validation.

The fixture uses synthetic opaque values.  A host receives only the user, scope,
and memory keys; expected values are never included in its prompt.  Two fresh
host sessions share one SQLite database while an owner-controlled transition
between sessions supersedes one current value and deletes another.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..extraction import DIRECTIVE_PREFIX
from ..facade import FlowGridMemory


OUTPUT_FIELDS = (
    "release_channel",
    "stable_owner",
    "retired_token",
    "rejected_direction",
    "pending_candidate",
    "missing_memory",
)


@dataclass(frozen=True)
class HostTrialState:
    db_path: str
    user_id: str
    scope: Mapping[str, str]
    nonce: str
    old_release_record_id: str
    retired_record_id: str
    expected_session_one: Mapping[str, str]
    forbidden_session_one: tuple[str, ...]
    hidden_values: Mapping[str, str]


def _directive(*, memory_key: str, content: str) -> str:
    proposal = {
        "memory_key": memory_key,
        "memory_type": "fact",
        "subject": "$user",
        "content": content,
        "confidence": 1.0,
    }
    return DIRECTIVE_PREFIX + "\n" + json.dumps(
        {"proposals": [proposal]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _candidate(
    memory: FlowGridMemory,
    *,
    user_id: str,
    scope: Mapping[str, str],
    nonce: str,
    ordinal: int,
    memory_key: str,
    content: str,
) -> str:
    ingested = memory.ingest_raw_events(
        request_id=f"host-trial-ingest-{nonce}-{ordinal}",
        user_id=user_id,
        session_id=f"host-trial-source-{nonce}-{ordinal}",
        messages=(
            {
                "role": "user",
                "content": _directive(memory_key=memory_key, content=content),
            },
        ),
        trusted_scope=scope,
    )
    compiled = memory.extract_candidates(
        user_id=user_id,
        raw_event_ids=ingested.raw_event_ids,
        idempotency_key=f"host-trial-extract-{nonce}-{ordinal}",
        trusted_scope=scope,
    )
    if compiled.proposal_count != 1 or len(compiled.record_ids) != 1:
        raise RuntimeError("host trial fixture compilation failed")
    return compiled.record_ids[0]


def _transition(
    memory: FlowGridMemory,
    *,
    user_id: str,
    record_id: str,
    target_status: str,
    reason: str,
) -> None:
    memory.transition_memory(
        user_id=user_id,
        record_id=record_id,
        target_status=target_status,
        actor="host-trial-owner",
        actor_authority="owner",
        reason=reason,
    )


def create_host_trial(*, db_path: str, nonce: str) -> HostTrialState:
    """Create the first-session state without exposing expected values."""

    if not Path(db_path).is_absolute():
        raise ValueError("db_path must be absolute")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 64:
        raise ValueError("nonce must be a short non-empty string")

    user_id = f"host-trial-user-{nonce}"
    scope = {"project": f"host-trial-project-{nonce}"}
    values = {
        "old_release": f"release-old-{nonce}",
        "new_release": f"release-new-{nonce}",
        "stable_owner": f"owner-current-{nonce}",
        "retired_token": f"retired-token-{nonce}",
        "rejected_direction": f"rejected-direction-{nonce}",
        "pending_candidate": f"pending-candidate-{nonce}",
    }

    with FlowGridMemory(db_path=db_path) as memory:
        old_release = _candidate(
            memory,
            user_id=user_id,
            scope=scope,
            nonce=nonce,
            ordinal=1,
            memory_key="project.release_channel",
            content=values["old_release"],
        )
        _transition(
            memory,
            user_id=user_id,
            record_id=old_release,
            target_status="confirmed",
            reason="owner confirmed the initial release channel",
        )
        stable_owner = _candidate(
            memory,
            user_id=user_id,
            scope=scope,
            nonce=nonce,
            ordinal=2,
            memory_key="project.stable_owner",
            content=values["stable_owner"],
        )
        _transition(
            memory,
            user_id=user_id,
            record_id=stable_owner,
            target_status="confirmed",
            reason="owner confirmed the stable owner",
        )
        retired = _candidate(
            memory,
            user_id=user_id,
            scope=scope,
            nonce=nonce,
            ordinal=3,
            memory_key="project.retired_token",
            content=values["retired_token"],
        )
        _transition(
            memory,
            user_id=user_id,
            record_id=retired,
            target_status="confirmed",
            reason="owner confirmed the temporary token",
        )
        rejected = _candidate(
            memory,
            user_id=user_id,
            scope=scope,
            nonce=nonce,
            ordinal=4,
            memory_key="project.rejected_direction",
            content=values["rejected_direction"],
        )
        _transition(
            memory,
            user_id=user_id,
            record_id=rejected,
            target_status="rejected",
            reason="owner rejected this direction",
        )
        _candidate(
            memory,
            user_id=user_id,
            scope=scope,
            nonce=nonce,
            ordinal=5,
            memory_key="project.pending_candidate",
            content=values["pending_candidate"],
        )

    expected = {
        "release_channel": values["old_release"],
        "stable_owner": values["stable_owner"],
        "retired_token": values["retired_token"],
        "rejected_direction": "unknown",
        "pending_candidate": "unknown",
        "missing_memory": "unknown",
    }
    return HostTrialState(
        db_path=db_path,
        user_id=user_id,
        scope=scope,
        nonce=nonce,
        old_release_record_id=old_release,
        retired_record_id=retired,
        expected_session_one=expected,
        forbidden_session_one=(values["rejected_direction"], values["pending_candidate"]),
        hidden_values=values,
    )


def advance_host_trial(state: HostTrialState) -> tuple[Mapping[str, str], tuple[str, ...]]:
    """Apply owner-confirmed supersession and deletion between host sessions."""

    values = state.hidden_values
    with FlowGridMemory(db_path=state.db_path) as memory:
        source = memory.ingest_raw_events(
            request_id=f"host-trial-replacement-source-{state.nonce}",
            user_id=state.user_id,
            session_id=f"host-trial-owner-update-{state.nonce}",
            messages=(
                {
                    "role": "user",
                    "content": f"Release channel is now {values['new_release']}",
                },
            ),
            trusted_scope=state.scope,
        )
        replacement = memory.propose_memory(
            user_id=state.user_id,
            memory_key="project.release_channel",
            memory_type="fact",
            subject=state.user_id,
            content=values["new_release"],
            source_event_ids=source.raw_event_ids,
            status="candidate",
            authority="user",
            created_by="host-trial-owner-update",
            scope=state.scope,
            supersedes_record_id=state.old_release_record_id,
            state_reason="new direct-user evidence proposed a replacement",
        )
        _transition(
            memory,
            user_id=state.user_id,
            record_id=replacement.id,
            target_status="confirmed",
            reason="owner confirmed replacement and supersession",
        )
        _transition(
            memory,
            user_id=state.user_id,
            record_id=state.retired_record_id,
            target_status="deleted",
            reason="owner deleted the temporary token",
        )

    expected = {
        "release_channel": values["new_release"],
        "stable_owner": values["stable_owner"],
        "retired_token": "unknown",
        "rejected_direction": "unknown",
        "pending_candidate": "unknown",
        "missing_memory": "unknown",
    }
    forbidden = (
        values["old_release"],
        values["retired_token"],
        values["rejected_direction"],
        values["pending_candidate"],
    )
    return expected, forbidden


def assess_host_answer(
    answer: object,
    *,
    expected: Mapping[str, str],
    forbidden_values: tuple[str, ...],
    raw_output: str,
    memory_tool_calls: int,
) -> dict[str, object]:
    """Score one host response without averaging away governance failures."""

    is_mapping = isinstance(answer, Mapping)
    field_checks = {
        field: bool(is_mapping and answer.get(field) == expected[field])
        for field in OUTPUT_FIELDS
    }
    forbidden_leaks = [value for value in forbidden_values if value in raw_output]
    checks = {
        "used_memory_tool": memory_tool_calls > 0,
        "exact_fields": bool(is_mapping and set(answer) == set(OUTPUT_FIELDS)),
        "expected_values": all(field_checks.values()),
        "forbidden_values_absent": not forbidden_leaks,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "field_checks": field_checks,
        "memory_tool_calls": memory_tool_calls,
        "forbidden_leak_count": len(forbidden_leaks),
    }


__all__ = [
    "HostTrialState",
    "OUTPUT_FIELDS",
    "advance_host_trial",
    "assess_host_answer",
    "create_host_trial",
]
