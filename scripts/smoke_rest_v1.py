#!/usr/bin/env python3
"""Real loopback smoke for authenticated governed REST v1."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aml_retriever.auth import MEMORY_PERMISSIONS, TrustedPrincipal
from aml_retriever.rest_v1 import GovernedRestAdapter, GovernedRestConfig


def _directive(content: str) -> str:
    return "@flowgrid.memory/v1\n" + json.dumps(
        {
            "proposals": [
                {
                    "memory_key": "profile.city",
                    "memory_type": "fact",
                    "subject": "$user",
                    "content": content,
                }
            ]
        },
        separators=(",", ":"),
    )


def _request(adapter, method, path, body=None, *, token=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(*adapter.address, timeout=5)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), json.loads(raw)
    finally:
        connection.close()


def run_smoke() -> dict[str, object]:
    token = "local-smoke-token"
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as directory:
        adapter = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=str(Path(directory) / "smoke.db"),
                principal=TrustedPrincipal(
                    principal_id="smoke-owner",
                    authority="owner",
                    allowed_users=frozenset({"smoke-user"}),
                    scopes={"project": "smoke-project"},
                    permissions=frozenset(MEMORY_PERMISSIONS),
                    purpose="smoke audit",
                    allowed_audit_purposes=frozenset({"smoke audit"}),
                ),
                auth_mode="bearer",
                bearer_token=token,
                port=0,
                erase_enabled=False,
            )
        )
        thread = threading.Thread(target=adapter.serve_forever, daemon=True)
        thread.start()
        try:
            status, headers, health = _request(adapter, "GET", "/v1/health")
            checks["anonymous_minimal_health"] = (
                status == 200
                and set(health) == {"product_version", "ready"}
                and health["ready"] is True
                and headers.get("Cache-Control") == "no-store"
                and "Access-Control-Allow-Origin" not in headers
            )
            status, _headers, denied = _request(
                adapter,
                "POST",
                "/v1/memories/query",
                {"user_id": "smoke-user", "scope": {"project": "smoke-project"}},
            )
            checks["bearer_required"] = (
                status == 401 and denied.get("error", {}).get("code") == "unauthorized"
            )

            with mock.patch("aml_retriever.server._Handler.do_POST") as legacy:
                status, _headers, added = _request(
                    adapter,
                    "POST",
                    "/v1/events",
                    {
                        "request_id": "smoke-add",
                        "user_id": "smoke-user",
                        "session_id": "smoke-session",
                        "scope": {"project": "smoke-project"},
                        "messages": [
                            {"role": "user", "content": _directive("private-smoke-city")}
                        ],
                    },
                    token=token,
                )
                raw_event_id = added.get("receipt", {}).get("raw_event_ids", [None])[0]
                checks["ingest"] = status == 200 and isinstance(raw_event_id, str)
                status, _headers, extracted = _request(
                    adapter,
                    "POST",
                    "/v1/extractions",
                    {
                        "user_id": "smoke-user",
                        "raw_event_ids": [raw_event_id],
                        "idempotency_key": "smoke-extract",
                        "scope": {"project": "smoke-project"},
                    },
                    token=token,
                )
                record_id = extracted.get("receipt", {}).get("record_ids", [None])[0]
                checks["candidate_only_extraction"] = (
                    status == 200 and isinstance(record_id, str)
                )
                status, _headers, transitioned = _request(
                    adapter,
                    "POST",
                    "/v1/memories/transition",
                    {
                        "user_id": "smoke-user",
                        "record_id": record_id,
                        "memory_key": "profile.city",
                        "target_status": "confirmed",
                        "reason": "smoke owner reviewed source",
                        "scope": {"project": "smoke-project"},
                    },
                    token=token,
                )
                checks["owner_transition"] = (
                    status == 200
                    and transitioned.get("record", {}).get("current_status") == "confirmed"
                    and set(transitioned.get("record", {}))
                    == {"record_id", "current_status"}
                )
                status, _headers, current = _request(
                    adapter,
                    "POST",
                    "/v1/memories/query",
                    {
                        "user_id": "smoke-user",
                        "memory_key": "profile.city",
                        "scope": {"project": "smoke-project"},
                    },
                    token=token,
                )
                checks["governed_current"] = (
                    status == 200
                    and current.get("result", {}).get("status") == "ready"
                    and current.get("result", {}).get("items", [{}])[0].get("content")
                    == "private-smoke-city"
                )
                status, _headers, context = _request(
                    adapter,
                    "POST",
                    "/v1/context/compile",
                    {
                        "user_id": "smoke-user",
                        "memory_key": "profile.city",
                        "scope": {"project": "smoke-project"},
                        "max_chars": 100000,
                    },
                    token=token,
                )
                checks["ready_context"] = (
                    status == 200
                    and context.get("context", {}).get("status") == "ready"
                    and context.get("injectable") is True
                )
                status, _headers, audit = _request(
                    adapter,
                    "POST",
                    "/v1/memories/query",
                    {
                        "user_id": "smoke-user",
                        "memory_key": "profile.city",
                        "mode": "audit",
                        "scope": {"project": "smoke-project"},
                    },
                    token=token,
                )
                checks["audit_gate"] = (
                    status == 200
                    and audit.get("result", {}).get("status") == "ready"
                    and audit.get("result", {})
                    .get("omitted", {})
                    .get("by_reason", {})
                    .get("audit_evidence", 0)
                    >= 1
                )
                checks["legacy_handler_not_called"] = legacy.call_count == 0

            status, _headers, denied = _request(
                adapter,
                "POST",
                "/v1/memories/query",
                {"user_id": "smoke-user", "scope": {"project": "wrong-project"}},
                token=token,
            )
            checks["scope_denial"] = (
                status == 403 and "wrong-project" not in json.dumps(denied)
            )
            status, _headers, denied = _request(
                adapter,
                "POST",
                "/v1/memories/query",
                {"user_id": "wrong-user", "scope": {"project": "smoke-project"}},
                token=token,
            )
            checks["user_denial"] = status == 403 and "wrong-user" not in json.dumps(denied)
            status, _headers, denied = _request(
                adapter,
                "POST",
                "/v1/admin/erase-user",
                {"user_id": "smoke-user", "reason": "secret erase reason"},
                token=token,
            )
            checks["erase_default_off"] = (
                status == 403
                and "smoke-user" not in json.dumps(denied)
                and "secret erase" not in json.dumps(denied)
            )
            status, _headers, invalid = _request(
                adapter,
                "POST",
                "/v1/events",
                {
                    "request_id": "forged",
                    "user_id": "smoke-user",
                    "messages": [{"role": "user", "content": "private-error-sentinel"}],
                    "authority": "owner",
                },
                token=token,
            )
            checks["fixed_safe_error"] = (
                status == 422 and "private-error-sentinel" not in json.dumps(invalid)
            )
        finally:
            adapter.close()
            thread.join(timeout=5)
            checks["clean_shutdown"] = not thread.is_alive()

    return {
        "profile": "governed-rest-v1-loopback",
        "passed": all(checks.values()),
        "checks": checks,
        "memory_content_emitted": False,
        "secret_emitted": False,
        "database_path_emitted": False,
    }


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
