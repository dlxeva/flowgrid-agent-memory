#!/usr/bin/env python3
"""Run a disposable, synthetic AML Cycle 2 concurrency rehearsal.

This is intentionally a local evidence tool.  It starts the real Add/Search
HTTP server on loopback, exercises Streaming-style checkpoints, retries,
concurrent reads/writes and user isolation, then deletes the temporary
database.  The JSON report contains aggregate metrics only: no request body,
memory text, credential, URL or database path is persisted.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aml_retriever.config import RetrieverConfig
from aml_retriever.server import RetrieverServer
from aml_retriever.aml_hosted import HostedAMLServer, HostedConfig


REPORT_SCHEMA = "flowgrid.aml-cycle2-rehearsal/v1"


@dataclass
class _Metric:
    latencies_ms: list[float] = field(default_factory=list)
    failures: int = 0

    def record(self, elapsed_ms: float, passed: bool) -> None:
        self.latencies_ms.append(elapsed_ms)
        if not passed:
            self.failures += 1

    def summary(self) -> dict[str, float | int]:
        values = sorted(self.latencies_ms)
        return {
            "requests": len(values),
            "failures": self.failures,
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "max_ms": round(values[-1], 3) if values else 0.0,
        }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return round(values[index], 3)


def _post_attempt(base_url: str, path: str, payload: dict, *, credential: str = "", timeout: float = 30.0) -> tuple[int, dict, str | None, float]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(
        base_url + path,
        data=raw,
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, response.headers.get("Retry-After"), (time.perf_counter() - started) * 1_000
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        return error.code, body, error.headers.get("Retry-After"), (time.perf_counter() - started) * 1_000
    except Exception:
        # Do not expose URL, key, payload, user ID or exception text.
        return 0, {}, None, (time.perf_counter() - started) * 1_000


def _post(base_url: str, path: str, payload: dict) -> tuple[int, dict, float]:
    status, body, _, elapsed = _post_attempt(base_url, path, payload)
    return status, body, elapsed


class _AttemptMetrics:
    """Thread-safe physical-attempt counts, distinct from logical requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = {"add": [], "search": []}
        self.statuses = {"add": {}, "search": {}}
        self.logical = {"add": 0, "search": 0}
        self.retry_waits = 0
        self.retry_budget_exhaustions = 0
        self.invalid_retry_after = 0
        self.max_attempts_observed = 0

    def record(self, path: str, status: int, elapsed_ms: float) -> None:
        route = "add" if path == "/add" else "search"
        with self.lock:
            self.latencies[route].append(elapsed_ms)
            counts = self.statuses[route]
            counts[status] = counts.get(status, 0) + 1

    def finish(self, path: str, attempts: int) -> None:
        route = "add" if path == "/add" else "search"
        with self.lock:
            self.logical[route] += 1
            self.max_attempts_observed = max(self.max_attempts_observed, attempts)

    def increment(self, field: str) -> None:
        with self.lock:
            setattr(self, field, getattr(self, field) + 1)

    def summary(self) -> dict:
        with self.lock:
            routes = {}
            for route in ("add", "search"):
                values = sorted(self.latencies[route])
                counts = self.statuses[route]
                routes[route] = {
                    "logical_requests": self.logical[route],
                    "actual_attempts": len(values), "successful_200_attempts": counts.get(200, 0),
                    "throttled_429_attempts": counts.get(429, 0),
                    "transport_error_attempts": counts.get(0, 0),
                    "other_status_attempts": sum(count for status, count in counts.items() if status not in {0, 200, 429}),
                    "per_attempt_latency_ms": {
                        "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95),
                        "max": round(values[-1], 3) if values else 0.0,
                    },
                }
            return {
                "routes": routes, "retry_waits_after_429": self.retry_waits,
                "retry_budget_exhaustions": self.retry_budget_exhaustions,
                "invalid_retry_after": self.invalid_retry_after,
                "max_attempts_observed_per_logical_request": self.max_attempts_observed,
            }


def _post_with_retry(
    base_url: str, path: str, payload: dict, *, credential: str,
    metrics: _AttemptMetrics, max_attempts: int = 6, total_deadline_seconds: float = 60.0,
) -> tuple[int, dict, float]:
    """Retry only 429, honoring bounded integer Retry-After delays.

    The same payload and IDs are retained through each physical attempt.
    Reported logical latency includes attempt time and every retry wait.
    """
    if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 12
            or isinstance(total_deadline_seconds, bool) or not isinstance(total_deadline_seconds, (int, float))
            or not math.isfinite(total_deadline_seconds) or not 0 < total_deadline_seconds <= 120):
        raise ValueError("retry configuration rejected")
    started = time.perf_counter()
    status, body, attempts = 0, {}, 0
    try:
        for index in range(max_attempts):
            remaining = total_deadline_seconds - (time.perf_counter() - started)
            if remaining <= 0:
                metrics.increment("retry_budget_exhaustions")
                break
            status, body, retry_after, elapsed = _post_attempt(
                base_url, path, payload, credential=credential, timeout=min(30.0, remaining),
            )
            attempts += 1
            metrics.record(path, status, elapsed)
            if status != 429:
                break
            if not isinstance(retry_after, str) or not retry_after.isascii() or not retry_after.isdecimal() or len(retry_after) > 2:
                metrics.increment("invalid_retry_after")
                break
            wait_seconds = int(retry_after)
            remaining = total_deadline_seconds - (time.perf_counter() - started)
            if index + 1 >= max_attempts or wait_seconds > 5 or wait_seconds >= remaining:
                metrics.increment("retry_budget_exhaustions")
                break
            metrics.increment("retry_waits")
            time.sleep(wait_seconds)
    finally:
        metrics.finish(path, attempts)
    return status, body, (time.perf_counter() - started) * 1_000


def _parallel(
    functions: list[Callable[[], tuple[bool, float]]], workers: int
) -> list[tuple[bool, float]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(function) for function in functions]
        results: list[tuple[bool, float]] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception:
                # Aggregate evidence must not persist exception text because it
                # may contain a URL, path or payload fragment.
                results.append((False, 0.0))
        return results


def _marker(user_index: int, checkpoint: int) -> str:
    return f"cycle2user{user_index:04d}checkpoint{checkpoint:04d}"


def run_rehearsal(
    *,
    users: int = 24,
    checkpoints: int = 6,
    add_concurrency: int = 16,
    search_concurrency: int = 32,
    search_rounds: int = 4,
    hosted: bool = False,
    hosted_max_inflight: int = 32,
) -> dict[str, object]:
    for name, value in {
        "users": users,
        "checkpoints": checkpoints,
        "add_concurrency": add_concurrency,
        "search_concurrency": search_concurrency,
        "search_rounds": search_rounds,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(hosted, bool) or isinstance(hosted_max_inflight, bool) or not isinstance(hosted_max_inflight, int) or not 1 <= hosted_max_inflight <= 128:
        raise ValueError("hosted configuration rejected")

    started = time.perf_counter()
    add_metric = _Metric()
    search_metric = _Metric()
    attempts_metric = _AttemptMetrics()
    gates = {
        "all_adds_accepted": True,
        "retry_idempotency": True,
        "streaming_immediate_visibility": True,
        "search_response_contract": True,
        "user_isolation": True,
    }
    tempdir = tempfile.mkdtemp(prefix="aml-cycle2-rehearsal-")
    database = os.path.join(tempdir, "rehearsal.db")
    credential = secrets.token_urlsafe(32) if hosted else ""
    try:
        if hosted:
            server = HostedAMLServer(HostedConfig(
                credential=credential, db_path=database, host="127.0.0.1", port=0,
                max_inflight=hosted_max_inflight, max_connections=max(96, hosted_max_inflight),
            ), quiet=True)
        else:
            server = RetrieverServer(RetrieverConfig(db_path=database, host="127.0.0.1", port=0), quiet=True)
    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    report: dict[str, object]

    def post(path: str, payload: dict) -> tuple[int, dict, float]:
        if hosted:
            return _post_with_retry(base_url, path, payload, credential=credential, metrics=attempts_metric)
        return _post(base_url, path, payload)

    try:
        for checkpoint in range(checkpoints):
            add_calls: list[Callable[[], tuple[bool, float]]] = []
            for user_index in range(users):
                user_id = f"rehearsal-user-{user_index:04d}"
                request_id = f"rehearsal-{user_index:04d}-{checkpoint:04d}"
                payload = {
                    "request_id": request_id,
                    "user_id": user_id,
                    "session_id": f"{user_id}-session-{checkpoint:04d}",
                    "messages": [
                        {
                            "role": "user",
                            "timestamp": 1_704_067_200_000 + checkpoint * 1_000,
                            "content": (
                                "Synthetic AML rehearsal memory marker "
                                + _marker(user_index, checkpoint)
                            ),
                        }
                    ],
                }

                def add_once(payload=payload) -> tuple[bool, float]:
                    status, body, elapsed = post("/add", payload)
                    passed = (
                        status == 200
                        and body.get("success") is True
                        and body.get("request_id") == payload["request_id"]
                        and body.get("user_id") == payload["user_id"]
                        and body.get("session_id") == payload["session_id"]
                    )
                    return passed, elapsed

                add_calls.append(add_once)
                # Every logical write is delivered twice.  The second delivery
                # uses the exact same request_id and payload, matching a retry.
                add_calls.append(add_once)

            for passed, elapsed in _parallel(add_calls, add_concurrency):
                add_metric.record(elapsed, passed)
                gates["all_adds_accepted"] &= passed

            expected_rows = users * (checkpoint + 1)
            gates["retry_idempotency"] &= server.service.db.count() == expected_rows

            checkpoint_searches: list[Callable[[], tuple[bool, float]]] = []
            for user_index in range(users):
                user_id = f"rehearsal-user-{user_index:04d}"
                expected = {_marker(user_index, index) for index in range(checkpoint + 1)}

                def search_checkpoint(user_id=user_id, expected=expected) -> tuple[bool, float]:
                    status, body, elapsed = post(
                        "/search",
                        {
                            "query": "Synthetic AML rehearsal memory marker",
                            "user_id": user_id,
                            "top_k": 100,
                        },
                    )
                    data = body.get("data") if isinstance(body, dict) else None
                    shape_ok = (
                        status == 200
                        and isinstance(data, list)
                        and len(data) <= 100
                        and all(
                            isinstance(item, dict)
                            and isinstance(item.get("id"), str)
                            and bool(item.get("id"))
                            and isinstance(item.get("content"), str)
                            and bool(item.get("content"))
                            for item in data
                        )
                    )
                    returned = "\n".join(
                        item.get("content", "") for item in data or [] if isinstance(item, dict)
                    )
                    visible = expected.issubset(
                        {marker for marker in expected if marker in returned}
                    )
                    return shape_ok and visible, elapsed

                checkpoint_searches.append(search_checkpoint)

            for passed, elapsed in _parallel(checkpoint_searches, search_concurrency):
                search_metric.record(elapsed, passed)
                gates["streaming_immediate_visibility"] &= passed
                gates["search_response_contract"] &= passed

        isolation_calls: list[Callable[[], tuple[bool, float]]] = []
        for user_index in range(users):
            other = (user_index + 1) % users
            foreign_marker = _marker(other, checkpoints - 1)

            def search_foreign(
                user_index=user_index, foreign_marker=foreign_marker
            ) -> tuple[bool, float]:
                status, body, elapsed = post(
                    "/search",
                    {
                        "query": foreign_marker,
                        "user_id": f"rehearsal-user-{user_index:04d}",
                        "top_k": 100,
                    },
                )
                data = body.get("data") if isinstance(body, dict) else None
                leaked = any(
                    foreign_marker in item.get("content", "")
                    for item in data or []
                    if isinstance(item, dict)
                )
                return status == 200 and isinstance(data, list) and not leaked, elapsed

            isolation_calls.append(search_foreign)

        for passed, elapsed in _parallel(isolation_calls, search_concurrency):
            search_metric.record(elapsed, passed)
            gates["user_isolation"] &= passed

        stress_calls: list[Callable[[], tuple[bool, float]]] = []
        for _round in range(search_rounds):
            for user_index in range(users):
                marker = _marker(user_index, checkpoints - 1)

                def search_latest(
                    user_index=user_index, marker=marker
                ) -> tuple[bool, float]:
                    status, body, elapsed = post(
                        "/search",
                        {
                            "query": marker,
                            "user_id": f"rehearsal-user-{user_index:04d}",
                            "top_k": 100,
                        },
                    )
                    data = body.get("data") if isinstance(body, dict) else None
                    found = any(
                        marker in item.get("content", "")
                        for item in data or []
                        if isinstance(item, dict)
                    )
                    return status == 200 and isinstance(data, list) and found, elapsed

                stress_calls.append(search_latest)

        for passed, elapsed in _parallel(stress_calls, search_concurrency):
            search_metric.record(elapsed, passed)
            gates["search_response_contract"] &= passed

        expected_unique_writes = users * checkpoints
        gates["retry_idempotency"] &= server.service.db.count() == expected_unique_writes
        report = {
            "schema": REPORT_SCHEMA,
            "status": "pending_cleanup",
            "evidence_level": "observed_local_synthetic",
            "official_aml_result": False,
            "public_https_validated": False,
            "synthetic_data_only": True,
            "transport": "dedicated_hosted_loopback" if hosted else "legacy_loopback",
            "temporary_database_deleted_after_run": False,
            "configuration": {
                "users": users,
                "checkpoints": checkpoints,
                "add_concurrency": add_concurrency,
                "search_concurrency": search_concurrency,
                "search_rounds": search_rounds,
                "top_k": 100,
                "duplicate_deliveries_per_write": 2,
                "hosted_max_inflight": hosted_max_inflight if hosted else None,
            },
            "operations": {
                "unique_writes": expected_unique_writes,
                "add_http_requests": users * checkpoints * 2,
                "logical_add_deliveries": users * checkpoints * 2,
                "logical_search_requests": users * (checkpoints + 1 + search_rounds),
                "checkpoint_searches": users * checkpoints,
                "isolation_searches": users,
                "stress_searches": users * search_rounds,
            },
            "latency": {
                "add": add_metric.summary(),
                "search": search_metric.summary(),
            },
            "gates": gates,
            "elapsed_seconds": 0.0,
        }
        if hosted:
            attempt_summary = attempts_metric.summary()
            report["http_attempts"] = attempt_summary
            report["retry_policy"] = {"max_attempts_per_logical_request": 6, "total_deadline_seconds": 60, "max_retry_after_seconds": 5}
            report["operations"]["add_http_requests"] = attempt_summary["routes"]["add"]["actual_attempts"]
            report["operations"]["search_http_requests"] = attempt_summary["routes"]["search"]["actual_attempts"]
            gates["bounded_429_retry_protocol"] = attempt_summary["invalid_retry_after"] == 0 and attempt_summary["retry_budget_exhaustions"] == 0
            gates["no_transport_or_unexpected_status_failures"] = all(
                route["transport_error_attempts"] == 0 and route["other_status_attempts"] == 0
                for route in attempt_summary["routes"].values()
            )
            gates["physical_attempt_accounting"] = all(
                route["actual_attempts"] == route["successful_200_attempts"] + route["throttled_429_attempts"] + route["transport_error_attempts"] + route["other_status_attempts"]
                and route["logical_requests"] == route["successful_200_attempts"]
                for route in attempt_summary["routes"].values()
            )
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
        thread.join(timeout=5)
        shutil.rmtree(tempdir, ignore_errors=True)

    cleanup_passed = not os.path.exists(tempdir)
    gates["temporary_database_cleanup"] = cleanup_passed
    report["temporary_database_deleted_after_run"] = cleanup_passed
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    passed = all(gates.values()) and add_metric.failures == 0 and search_metric.failures == 0
    report["status"] = "passed" if passed else "failed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=24)
    parser.add_argument("--checkpoints", type=int, default=6)
    parser.add_argument("--add-concurrency", type=int, default=16)
    parser.add_argument("--search-concurrency", type=int, default=32)
    parser.add_argument("--search-rounds", type=int, default=4)
    parser.add_argument("--hosted", action="store_true", help="Exercise the authenticated dedicated hosted transport")
    parser.add_argument("--hosted-max-inflight", type=int, default=32, help="Hosted admission limit; default 32")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = run_rehearsal(
        users=args.users,
        checkpoints=args.checkpoints,
        add_concurrency=args.add_concurrency,
        search_concurrency=args.search_concurrency,
        search_rounds=args.search_rounds,
        hosted=args.hosted,
        hosted_max_inflight=args.hosted_max_inflight,
    )
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
