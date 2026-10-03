"""Fail-closed AML Add/Search transport for a dedicated hosted deployment.

Only the official compatibility adapter is exposed. Governance transitions,
administration, statistics and host adapters are absent from this service.
The implementation uses the Python standard library and never logs a request
target, body, user identifier, credential, or exception text.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hmac
import importlib.metadata
import json
import math
import os
import re
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ._version import AML_ADAPTER_VERSION, PRODUCT_VERSION
from .api import ApiError, MemoryService
from .config import RetrieverConfig
from .release_integrity import runtime_source_digest


_REASONS = {
    400: "invalid request", 401: "authentication required", 404: "route not found",
    405: "method not allowed", 408: "request timed out", 411: "Content-Length required",
    413: "request body too large", 415: "application/json required",
    417: "expectation not supported", 422: "request validation failed",
    429: "capacity limit reached", 500: "internal operation failed",
    504: "operation timed out", 505: "HTTP version not supported",
    431: "request headers too large",
}
_AUTH_MODES = frozenset({"bearer", "token", "x-api-key"})


class HostedConfigurationError(ValueError):
    """Startup failures deliberately contain no path, credential or env value."""


def _invalid_configuration() -> HostedConfigurationError:
    return HostedConfigurationError("AML hosted configuration rejected")


@dataclass(frozen=True)
class HostedConfig:
    credential: str = field(repr=False)
    db_path: str
    host: str = "127.0.0.1"
    port: int = 8081
    auth_mode: str = "bearer"
    max_body_bytes: int = 32 * 1024 * 1024
    max_inflight: int = 32
    max_connections: int = 96
    retry_after_seconds: int = 1
    read_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 60.0
    operation_timeout_seconds: float = 45.0
    require_build_identity: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.credential, str)
            or not self.credential.strip()
            or self.credential != self.credential.strip()
            or len(self.credential.encode("utf-8")) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in self.credential)
            or not isinstance(self.db_path, str)
            or not self.db_path
            or (self.db_path != ":memory:" and not Path(self.db_path).is_absolute())
            or self.auth_mode not in _AUTH_MODES
            or self.host not in {"127.0.0.1", "localhost"}
        ):
            raise _invalid_configuration()
        for value, lower, upper in (
            (self.port, 0, 65535), (self.max_body_bytes, 1, 32 * 1024 * 1024),
            (self.max_inflight, 1, 128), (self.max_connections, 1, 256),
            (self.retry_after_seconds, 1, 60),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise _invalid_configuration()
        for value in (
            self.read_timeout_seconds, self.request_timeout_seconds,
            self.operation_timeout_seconds,
        ):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 300:
                raise _invalid_configuration()
        if self.operation_timeout_seconds >= self.request_timeout_seconds:
            raise _invalid_configuration()
        if self.max_connections < self.max_inflight:
            raise _invalid_configuration()
        if not isinstance(self.require_build_identity, bool):
            raise _invalid_configuration()

    @classmethod
    def from_env(cls) -> "HostedConfig":
        """Hosted settings are independent of legacy AML_CONFIG/auth defaults."""
        try:
            credential = os.environ.get("AML_HOSTED_KEY", "")
            key_file = os.environ.get("AML_HOSTED_KEY_FILE", "")
            if bool(credential) == bool(key_file):
                raise _invalid_configuration()
            if key_file:
                # Windows stat/chmod bits do not attest an ACL. Until a real
                # ACL verifier exists, only POSIX private key files are allowed.
                if os.name != "posix":
                    raise _invalid_configuration()
                path = Path(key_file)
                mode = path.stat().st_mode
                if not path.is_absolute() or not stat.S_ISREG(mode) or mode & 0o077:
                    raise _invalid_configuration()
                with path.open("r", encoding="utf-8") as handle:
                    credential = handle.read(4097).rstrip("\r\n")
            require = os.environ.get("AML_HOSTED_REQUIRE_BUILD_IDENTITY", "0")
            if require not in {"0", "1"}:
                raise _invalid_configuration()
            return cls(
                credential=credential,
                db_path=os.environ.get("AML_HOSTED_DB_PATH", ""),
                host=os.environ.get("AML_HOSTED_HOST", "127.0.0.1"),
                port=int(os.environ.get("AML_HOSTED_PORT", "8081")),
                auth_mode=os.environ.get("AML_HOSTED_AUTH_MODE", "bearer"),
                max_body_bytes=int(os.environ.get("AML_HOSTED_MAX_BODY_BYTES", str(32 * 1024 * 1024))),
                max_inflight=int(os.environ.get("AML_HOSTED_MAX_INFLIGHT", "32")),
                max_connections=int(os.environ.get("AML_HOSTED_MAX_CONNECTIONS", "96")),
                retry_after_seconds=int(os.environ.get("AML_HOSTED_RETRY_AFTER_SECONDS", "1")),
                read_timeout_seconds=float(os.environ.get("AML_HOSTED_READ_TIMEOUT_SECONDS", "10")),
                request_timeout_seconds=float(os.environ.get("AML_HOSTED_REQUEST_TIMEOUT_SECONDS", "60")),
                operation_timeout_seconds=float(os.environ.get("AML_HOSTED_OPERATION_TIMEOUT_SECONDS", "45")),
                require_build_identity=require == "1",
            )
        except (OSError, UnicodeError, ValueError, TypeError):
            raise _invalid_configuration() from None


def runtime_identity() -> dict:
    """Report package/code identity from local artifacts, never env overrides.

    An installed build may ship ``release_identity.json``. An editable checkout
    reports its Git state. Uncommitted code never claims the old HEAD as its
    deployed source commit. Installed sources and fixed evaluation resources
    must match the manifest's runtime digest and exact file set.
    """
    identity = {
        "product_version": PRODUCT_VERSION, "aml_adapter_version": AML_ADAPTER_VERSION,
        "source_commit": None, "source_digest_sha256": None,
        "runtime_digest_sha256": None,
        "source_state": "unknown", "identity_source": "unknown",
    }
    try:
        installed = importlib.metadata.version("flowgrid-agent-memory")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed is not None and installed != PRODUCT_VERSION:
        raise _invalid_configuration()
    root = Path(__file__).resolve().parent.parent
    manifest = Path(__file__).with_name("release_identity.json")
    if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
        raise _invalid_configuration()
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            commit = data["source_commit"]
            digest = data["source_digest_sha256"]
            runtime_digest = data["runtime_digest_sha256"]
            runtime_files = data["runtime_files"]
            state = data["source_state"]
            if (data["schema"] != "flowgrid.agent-memory.release-identity/v1"
                    or data["product_version"] != PRODUCT_VERSION
                    or data["adapter_version"] != AML_ADAPTER_VERSION
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(runtime_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", runtime_digest)
                    or not isinstance(runtime_files, list)
                    or state not in {"committed", "uncommitted"}
                    or (state == "committed" and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)))
                    or (state == "uncommitted" and commit is not None)):
                raise _invalid_configuration()
            actual_files, actual_digest = runtime_source_digest(root)
            if runtime_files != actual_files or not hmac.compare_digest(runtime_digest, actual_digest):
                raise _invalid_configuration()
            return {**identity, "source_commit": commit, "source_digest_sha256": digest,
                    "runtime_digest_sha256": actual_digest,
                    "source_state": state, "identity_source": "build_manifest"}
        except (OSError, ValueError, KeyError, TypeError):
            raise _invalid_configuration() from None
    if installed is not None and not (root / ".git").exists():
        # An installed release may not silently downgrade to an unidentified
        # development instance when its manifest has disappeared.
        raise _invalid_configuration()
    if (root / ".git").exists():
        try:
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True, timeout=3,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, check=True, timeout=3,
            ).stdout
            if re.fullmatch(r"[0-9a-f]{40}", commit):
                identity.update(source_commit=None if status else commit,
                                source_state="uncommitted" if status else "committed",
                                identity_source="git_checkout")
        except (OSError, subprocess.SubprocessError):
            pass
    return identity


def _reject_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class _HostedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "flowgrid-memory-aml"
    sys_version = ""

    def log_message(self, _format, *args) -> None:
        # BaseHTTPRequestHandler logs raw request targets and error fragments.
        return

    def version_string(self) -> str:
        return self.server_version

    def _send(self, status: int, body: dict) -> None:
        self.close_connection = True
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-FlowGrid-Memory-Version", self.server.identity["product_version"])
        self.send_header("X-FlowGrid-AML-Adapter-Version", self.server.identity["aml_adapter_version"])
        if self.server.identity["source_commit"]:
            self.send_header("X-FlowGrid-Memory-Commit", self.server.identity["source_commit"])
        if self.server.identity["source_digest_sha256"]:
            self.send_header("X-FlowGrid-Memory-Source-Digest", self.server.identity["source_digest_sha256"])
        if self.server.identity["runtime_digest_sha256"]:
            self.send_header("X-FlowGrid-Memory-Runtime-Digest", self.server.identity["runtime_digest_sha256"])
        if status == 429:
            self.send_header("Retry-After", str(self.server.config.retry_after_seconds))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, status: int) -> None:
        self._send(status, {"detail": {"reason": _REASONS.get(status, "request rejected")}})

    def send_error(self, code, message=None, explain=None) -> None:
        # Parser errors must use the same safe JSON shape, with no input echo.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self._error(code)

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
            self.request_version = "HTTP/1.0"
            self._error(505)
            return False
        return True

    def handle_expect_100(self) -> bool:
        self._error(417)
        return False

    def _authenticated(self) -> bool:
        mode = self.server.config.auth_mode
        header = "X-Api-Key" if mode == "x-api-key" else "Authorization"
        values = self.headers.get_all(header, [])
        if len(values) != 1:
            return False
        value = values[0]
        if mode != "x-api-key":
            prefix = "Bearer " if mode == "bearer" else "Token "
            if not value.startswith(prefix):
                return False
            value = value[len(prefix):]
        return hmac.compare_digest(value.encode("utf-8"), self.server.config.credential.encode("utf-8"))

    def _body(self) -> dict:
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []):
            raise ApiError(400, "")
        if self.headers.get_all("Expect", []):
            raise ApiError(417, "")
        if not lengths:
            raise ApiError(411, "")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 12:
            raise ApiError(400, "")
        length = int(lengths[0])
        if length <= 0:
            raise ApiError(400, "")
        if length > self.server.config.max_body_bytes:
            raise ApiError(413, "")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(415, "")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "")
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError, RecursionError):
            raise ApiError(400, "") from None
        if not isinstance(payload, dict):
            raise ApiError(422, "")
        return payload

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "flowgrid-memory-aml", **self.server.identity})
        else:
            self._error(405 if self.path in {"/add", "/search"} else 404)

    def do_POST(self) -> None:
        route = self.path if self.path in {"/add", "/search"} else "unknown"
        started = time.monotonic()
        status = 500
        try:
            if route == "unknown":
                raise ApiError(405 if self.path == "/health" else 404, "")
            if not self._authenticated():
                raise ApiError(401, "")
            # Reject saturated work before reading or buffering the body.
            if not self.server.operations.acquire(blocking=False):
                raise ApiError(429, "")
            transferred = False
            try:
                payload = self._body()
                operation = self.server.service.official_add if route == "/add" else self.server.service.official_search
                future = self.server.executor.submit(operation, payload)
                future.add_done_callback(lambda _: self.server.operations.release())
                transferred = True
                body = future.result(timeout=self.server.config.operation_timeout_seconds)
                status = 200
                self._send(status, body)
            finally:
                if not transferred:
                    self.server.operations.release()
        except ApiError as exc:
            status = exc.status if exc.status in _REASONS else 500
            self._error(status)
        except (TimeoutError, socket.timeout):
            status = 504 if "future" in locals() else 408
            self._error(status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self._error(500)
        finally:
            if not self.server.quiet:
                elapsed = (time.monotonic() - started) * 1000
                print(f"[aml-hosted] route={route} status={status} elapsed_ms={elapsed:.1f}", flush=True)

    def _unsupported(self) -> None:
        self._error(405 if self.path in {"/health", "/add", "/search"} else 404)

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _unsupported


class HostedAMLServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: HostedConfig, service: MemoryService | None = None, *, quiet: bool = False):
        self.config = config
        self.identity = runtime_identity()
        if config.require_build_identity and (
            self.identity["identity_source"] != "build_manifest" or self.identity["source_state"] != "committed"
        ):
            raise _invalid_configuration()
        self.quiet = quiet
        self.operations = threading.BoundedSemaphore(config.max_inflight)
        self.connections = threading.BoundedSemaphore(config.max_connections)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=config.max_inflight, thread_name_prefix="aml-operation")
        self.service = service or MemoryService(RetrieverConfig(db_path=config.db_path, host=config.host, port=config.port))
        try:
            super().__init__((config.host, config.port), _HostedHandler)
        except Exception:
            self.executor.shutdown(wait=True, cancel_futures=True)
            if service is None:
                self.service.close()
            raise

    def process_request(self, request, client_address) -> None:
        if not self.connections.acquire(blocking=False):
            raw = json.dumps({"detail": {"reason": _REASONS[429]}}).encode("utf-8")
            response = (
                f"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\nRetry-After: {self.config.retry_after_seconds}\r\n"
                "Connection: close\r\nCache-Control: no-store\r\n\r\n"
            ).encode("ascii") + raw
            try:
                request.settimeout(0.1)
                request.sendall(response)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        # Absolute connection lifetime also bounds slow/drip-fed HTTP headers.
        def expire() -> None:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(self.config.request_timeout_seconds, expire)
        timer.daemon = True
        request.settimeout(self.config.read_timeout_seconds)
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self.connections.release()

    def handle_error(self, request, client_address) -> None:
        # ThreadingHTTPServer otherwise prints exception text and traceback.
        return

    def server_close(self) -> None:
        super().server_close()
        self.executor.shutdown(wait=True, cancel_futures=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dedicated authenticated AML service (loopback behind TLS proxy)")
    parser.parse_args(argv)
    server = None
    try:
        config = HostedConfig.from_env()
        server = HostedAMLServer(config)
        print("[aml-hosted] ready", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("[aml-hosted] startup or runtime failure", flush=True)
        return 1
    finally:
        if server is not None:
            server.server_close()
            server.service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
