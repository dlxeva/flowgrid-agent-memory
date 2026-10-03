#!/usr/bin/env python3
"""Real HTTP smoke for a dedicated AML service, using synthetic data only.

Default: create a disposable loopback instance and erase its temporary DB.
An explicit external base URL uses a credential from an environment variable;
its synthetic records remain in the dedicated service database. No route for
remote deletion exists. Reports contain check names and booleans only.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aml_retriever.aml_hosted import HostedAMLServer, HostedConfig


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The approved endpoint is the only recipient of its credential.
        return None


def call(base: str, path: str, payload=None, *, credential: str | None = None, method="POST", headers=None):
    body = payload if isinstance(payload, bytes) else (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    )
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if credential is not None:
        request_headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(base.rstrip("/") + path, data=body, headers=request_headers, method=method)
    try:
        try:
            response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=65)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode("utf-8")), dict(response.headers)
    except Exception:
        return 0, {}, {}


def run(base: str, credential: str, local_server: HostedAMLServer | None = None) -> dict:
    checks: dict[str, bool] = {}
    user = "synthetic-hosted-smoke-" + uuid.uuid4().hex
    add = {
        "request_id": user + "-request", "user_id": user, "session_id": user + "-session",
        "messages": [{"role": "user", "timestamp": 1704067200000,
                      "content": "Synthetic memory: release checkpoint comet lighthouse."}],
    }
    query = {"user_id": user, "query": "comet lighthouse", "top_k": 100}
    status, health, headers = call(base, "/health", method="GET")
    checks["unauthenticated_health"] = status == 200 and health.get("status") == "ok"
    checks["version_identity"] = bool(health.get("product_version")) and headers.get("X-FlowGrid-Memory-Version") == health.get("product_version")
    checks["honest_source_identity"] = (
        (health.get("source_state") == "committed" and bool(health.get("source_commit")))
        or (health.get("source_state") in {"uncommitted", "unknown"} and health.get("source_commit") is None)
    )
    checks["missing_credential_denied"] = call(base, "/add", add)[0] == 401
    checks["wrong_credential_denied"] = call(base, "/add", add, credential="synthetic-wrong-key")[0] == 401
    status, accepted, _ = call(base, "/add", add, credential=credential)
    checks["correct_credential_add"] = status == 200
    checks["official_add_shape"] = accepted == {"success": True, "request_id": add["request_id"], "user_id": user, "session_id": add["session_id"]}
    status, before, _ = call(base, "/search", query, credential=credential)
    data = before.get("data")
    checks["search_top_k_100"] = status == 200 and isinstance(data, list) and 0 < len(data) <= 100
    checks["immediate_evidence_visibility"] = isinstance(data, list) and any("comet lighthouse" in item.get("content", "") for item in data)
    checks["official_search_shape"] = isinstance(data, list) and all(isinstance(item.get("id"), str) and isinstance(item.get("content"), str) for item in data)
    retry_status, _, _ = call(base, "/add", add, credential=credential)
    status, after, _ = call(base, "/search", query, credential=credential)
    checks["idempotent_add_retry"] = retry_status == 200 and status == 200 and after == before
    status, isolated, _ = call(base, "/search", {**query, "user_id": user + "-other"}, credential=credential)
    checks["user_isolation"] = status == 200 and isolated.get("data") == []
    checks["stats_absent"] = call(base, "/stats", method="GET", credential=credential)[0] == 404
    checks["admin_absent"] = call(base, "/admin/delete_user", {"user_id": user}, credential=credential)[0] == 404
    checks["governance_route_absent"] = call(base, "/v1/memories/transition", {}, credential=credential)[0] == 404
    checks["invalid_json_denied"] = call(base, "/add", b"not-json", credential=credential)[0] == 400
    checks["duplicate_json_keys_denied"] = call(base, "/add", b'{"user_id":"a","user_id":"b"}', credential=credential)[0] == 400
    checks["nan_json_denied"] = call(base, "/add", b'{"x":NaN}', credential=credential)[0] == 400
    checks["contract_validation_denied"] = call(base, "/search", {"user_id": user, "query": "x", "top_k": True}, credential=credential)[0] == 422
    checks["oversize_declared_body_denied"] = call(base, "/add", b"{}", credential=credential, headers={"Content-Length": str(32 * 1024 * 1024 + 1)})[0] == 413
    if local_server is not None:
        held = 0
        try:
            while local_server.operations.acquire(blocking=False):
                held += 1
            status, body, rate_headers = call(base, "/search", query, credential=credential)
            checks["capacity_limit_429_retry_after"] = status == 429 and rate_headers.get("Retry-After") == "1" and body == {"detail": {"reason": "capacity limit reached"}}
        finally:
            for _ in range(held):
                local_server.operations.release()
        checks["capacity_recovers"] = call(base, "/search", query, credential=credential)[0] == 200
        checks["no_automatic_derived_memory"] = local_server.service.search_governed(user_id=user).records == []
    return {
        "schema": "flowgrid.aml-hosted-smoke/v1", "synthetic_data_only": True,
        "official_aml_acceptance": "unverified", "checks": checks,
        "passed": all(checks.values()), "passed_checks": sum(checks.values()),
        "total_checks": len(checks), "external_synthetic_records_retained": local_server is None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dedicated AML hosted HTTP smoke; synthetic data only")
    parser.add_argument("--base-url", default="", help="Explicit candidate endpoint; default is disposable loopback")
    parser.add_argument("--credential-env", default="AML_HOSTED_KEY", help="Environment variable containing the Bearer credential")
    args = parser.parse_args(argv)
    if args.base_url:
        target = urllib.parse.urlsplit(args.base_url)
        credential = os.environ.get(args.credential_env, "")
        if not credential or target.username or target.password or target.query or target.fragment or (
            target.scheme != "https" and not (target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost"})
        ):
            print(json.dumps({"passed": False, "reason": "smoke configuration rejected"}))
            return 2
        report = run(args.base_url, credential)
    else:
        with tempfile.TemporaryDirectory(prefix="aml-hosted-smoke-") as directory:
            credential = secrets.token_urlsafe(32)
            server = HostedAMLServer(HostedConfig(credential=credential, db_path=str(Path(directory) / "memory.db"), port=0), quiet=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                report = run(f"http://127.0.0.1:{server.server_address[1]}", credential, server)
            finally:
                server.shutdown()
                server.server_close()
                server.service.close()
                thread.join(timeout=2)
        report["temporary_database_removed"] = not Path(directory).exists()
        report["passed"] = report["passed"] and report["temporary_database_removed"]
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
