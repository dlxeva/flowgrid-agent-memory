"""Real HTTP checks for the dedicated hosted AML security boundary."""
from __future__ import annotations

import contextlib
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from aml_retriever._version import AML_ADAPTER_VERSION, PRODUCT_VERSION
from aml_retriever.aml_hosted import HostedAMLServer, HostedConfig, HostedConfigurationError, runtime_identity
from aml_retriever.release_integrity import runtime_source_digest


KEY = "synthetic-test-credential-aml-only"
MARKER = "Synthetic comet lighthouse memory"


def payload(request_id="request-1", user_id="synthetic-user-1"):
    return {
        "request_id": request_id, "user_id": user_id, "session_id": "synthetic-session",
        "messages": [{"role": "user", "content": MARKER, "timestamp": 1704067200000}],
    }


class ConfigurationTests(unittest.TestCase):
    def test_no_empty_credential_or_insecure_mode(self):
        for credential in ("", " ", "x\n", "é", None):
            with self.subTest(credential=credential), self.assertRaises(HostedConfigurationError):
                HostedConfig(credential=credential, db_path=":memory:")
        with self.assertRaises(HostedConfigurationError):
            HostedConfig(credential=KEY, db_path=":memory:", auth_mode="none")
        self.assertNotIn(KEY, repr(HostedConfig(credential=KEY, db_path=":memory:")))

    def test_bounds_and_loopback_are_enforced(self):
        for kwargs in (
            {"db_path": "relative.db"}, {"host": "0.0.0.0"}, {"max_inflight": 0},
            {"max_body_bytes": True}, {"read_timeout_seconds": float("inf")},
            {"request_timeout_seconds": 1}, {"max_connections": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(HostedConfigurationError):
                HostedConfig(credential=KEY, **{"db_path": ":memory:", **kwargs})

    def test_environment_is_independent_of_legacy_auth_defaults(self):
        with mock.patch.dict(os.environ, {"AML_AUTH_MODE": "none", "AML_API_KEY": KEY}, clear=True):
            with self.assertRaises(HostedConfigurationError):
                HostedConfig.from_env()
        with mock.patch.dict(os.environ, {"AML_HOSTED_KEY": KEY, "AML_HOSTED_DB_PATH": ":memory:"}, clear=True):
            self.assertEqual(HostedConfig.from_env().auth_mode, "bearer")

    def test_private_key_file_and_ambiguous_configuration(self):
        with tempfile.TemporaryDirectory(prefix="aml-key-test-") as directory:
            path = Path(directory) / "key"
            path.write_text(KEY + "\n", encoding="utf-8")
            path.chmod(0o600)
            env = {"AML_HOSTED_KEY_FILE": str(path), "AML_HOSTED_DB_PATH": ":memory:"}
            if os.name != "posix":
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(HostedConfigurationError):
                        HostedConfig.from_env()
                return
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(HostedConfig.from_env().credential, KEY)
            with mock.patch.dict(os.environ, {**env, "AML_HOSTED_KEY": KEY}, clear=True):
                with self.assertRaises(HostedConfigurationError):
                    HostedConfig.from_env()
            if os.name != "nt":
                path.chmod(0o644)
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(HostedConfigurationError):
                        HostedConfig.from_env()

    def test_key_file_rejected_without_posix_permission_semantics(self):
        with mock.patch.dict(os.environ, {"AML_HOSTED_KEY_FILE": "/synthetic/private-key", "AML_HOSTED_DB_PATH": ":memory:"}, clear=True), \
                mock.patch("aml_retriever.aml_hosted.os.name", "nt"), \
                self.assertRaises(HostedConfigurationError):
            HostedConfig.from_env()
        with mock.patch.dict(os.environ, {"AML_HOSTED_KEY": KEY, "AML_HOSTED_DB_PATH": ":memory:"}, clear=True), \
                mock.patch("aml_retriever.aml_hosted.os.name", "nt"):
            self.assertEqual(HostedConfig.from_env().credential, KEY)

    def test_version_cannot_be_overridden_by_environment(self):
        with mock.patch.dict(os.environ, {"AML_PRODUCT_VERSION": "999", "AML_SOURCE_COMMIT": "f" * 40}):
            identity = runtime_identity()
        self.assertEqual(identity["product_version"], PRODUCT_VERSION)
        self.assertEqual(identity["aml_adapter_version"], AML_ADAPTER_VERSION)
        self.assertNotEqual(identity["source_commit"], "f" * 40)
        if identity["source_state"] == "uncommitted":
            self.assertIsNone(identity["source_commit"])

    def test_release_mode_rejects_unknown_or_dirty_identity(self):
        for state, source in (("unknown", "unknown"), ("uncommitted", "build_manifest"), ("committed", "git_checkout")):
            with mock.patch("aml_retriever.aml_hosted.runtime_identity", return_value={"source_state": state, "identity_source": source}):
                with self.assertRaises(HostedConfigurationError):
                    HostedAMLServer(HostedConfig(credential=KEY, db_path=":memory:", require_build_identity=True))

    def test_build_manifest_format_and_versions_are_validated(self):
        manifest = {
            "schema": "flowgrid.agent-memory.release-identity/v1",
            "product_version": PRODUCT_VERSION, "adapter_version": AML_ADAPTER_VERSION,
            "source_commit": "a" * 40, "source_digest_sha256": "b" * 64,
            "runtime_digest_sha256": "c" * 64, "runtime_files": [],
            "source_state": "committed",
        }
        with mock.patch("aml_retriever.aml_hosted.importlib.metadata.version", return_value=PRODUCT_VERSION), \
                mock.patch("aml_retriever.aml_hosted.Path.is_file", return_value=True), \
                mock.patch("aml_retriever.aml_hosted.runtime_source_digest", return_value=([], "c" * 64)), \
                mock.patch("aml_retriever.aml_hosted.Path.read_text", return_value=json.dumps(manifest)):
            identity = runtime_identity()
        self.assertEqual(identity["source_commit"], "a" * 40)
        for change in (
            {"product_version": "999"}, {"adapter_version": "999"},
            {"schema": "invented"}, {"source_digest_sha256": "invalid"},
            {"source_state": "uncommitted"}, {"source_commit": None},
        ):
            with self.subTest(change=change), \
                    mock.patch("aml_retriever.aml_hosted.importlib.metadata.version", return_value=PRODUCT_VERSION), \
                    mock.patch("aml_retriever.aml_hosted.Path.is_file", return_value=True), \
                    mock.patch("aml_retriever.aml_hosted.runtime_source_digest", return_value=([], "c" * 64)), \
                    mock.patch("aml_retriever.aml_hosted.Path.read_text", return_value=json.dumps({**manifest, **change})), \
                    self.assertRaises(HostedConfigurationError):
                runtime_identity()


class RuntimeIntegrityTests(unittest.TestCase):
    """Real byte-level changes in disposable copies of the installed packages."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aml-runtime-integrity-")
        self.root = Path(self.directory.name)
        source = Path(__file__).resolve().parent.parent
        for package in ("aml_retriever", "flowgrid_memory"):
            shutil.copytree(source / package, self.root / package, ignore=shutil.ignore_patterns("__pycache__", "release_identity.json"))
        runtime_files, digest = runtime_source_digest(self.root)
        self.manifest_path = self.root / "aml_retriever/release_identity.json"
        self.identity = {
            "schema": "flowgrid.agent-memory.release-identity/v1",
            "product_version": PRODUCT_VERSION, "adapter_version": AML_ADAPTER_VERSION,
            "source_commit": "a" * 40, "source_digest_sha256": "b" * 64,
            "runtime_digest_sha256": digest, "runtime_files": runtime_files,
            "source_state": "committed",
        }
        self.write_identity(self.identity)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch("aml_retriever.aml_hosted.__file__", str(self.root / "aml_retriever/aml_hosted.py")))
        self.stack.enter_context(mock.patch("aml_retriever.aml_hosted.importlib.metadata.version", return_value=PRODUCT_VERSION))

    def tearDown(self):
        self.stack.close()
        self.directory.cleanup()

    def write_identity(self, identity):
        self.manifest_path.write_text(json.dumps(identity), encoding="utf-8")

    def test_clean_frozen_package_passes_and_reports_checked_digest(self):
        identity = runtime_identity()
        self.assertEqual(identity["runtime_digest_sha256"], self.identity["runtime_digest_sha256"])
        self.assertEqual(identity["source_commit"], "a" * 40)
        server = HostedAMLServer(HostedConfig(credential=KEY, db_path=":memory:", port=0, require_build_identity=True), quiet=True)
        server.server_close()
        server.service.close()

    def test_source_fixed_fixture_baseline_and_type_marker_tampering_fail_closed(self):
        for relative in (
            "aml_retriever/api.py",
            "aml_retriever/evaluation/fixtures/governance_v1.json",
            "aml_retriever/evaluation/baselines/legacy_v11_small.json",
            "flowgrid_memory/py.typed",
        ):
            path = self.root / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"\n")
                with self.subTest(relative=relative), self.assertRaises(HostedConfigurationError):
                    runtime_identity()
                with self.assertRaises(HostedConfigurationError):
                    HostedAMLServer(HostedConfig(credential=KEY, db_path=":memory:", port=0), quiet=True)
            finally:
                path.write_bytes(original)
        self.assertEqual(runtime_identity()["source_commit"], "a" * 40)

    def test_missing_fixed_resource_refuses_startup(self):
        for relative in ("aml_retriever/api.py", "aml_retriever/evaluation/fixtures/governance_v1.json", "aml_retriever/evaluation/baselines/legacy_v11_small.json"):
            path = self.root / relative
            original = path.read_bytes()
            try:
                path.unlink()
                with self.subTest(relative=relative), self.assertRaises(HostedConfigurationError):
                    runtime_identity()
            finally:
                path.write_bytes(original)

    def test_added_runtime_file_cannot_hide_behind_old_commit(self):
        for relative in ("aml_retriever/injected.py", "aml_retriever/evaluation/fixtures/injected.json"):
            path = self.root / relative
            try:
                path.write_text("{}", encoding="utf-8")
                with self.subTest(relative=relative), self.assertRaises(HostedConfigurationError):
                    runtime_identity()
            finally:
                path.unlink()

    def test_missing_runtime_attestation_or_file_list_is_rejected(self):
        for key in ("runtime_digest_sha256", "runtime_files"):
            identity = dict(self.identity)
            identity.pop(key)
            self.write_identity(identity)
            with self.subTest(key=key), self.assertRaises(HostedConfigurationError):
                runtime_identity()
        self.write_identity({**self.identity, "runtime_files": self.identity["runtime_files"][:-1]})
        with self.assertRaises(HostedConfigurationError):
            runtime_identity()
        self.manifest_path.unlink()
        with self.assertRaises(HostedConfigurationError):
            runtime_identity()

    @unittest.skipIf(os.name == "nt", "symlink privilege is host-dependent on Windows")
    def test_runtime_resource_symlink_is_rejected(self):
        baseline = self.root / "aml_retriever/evaluation/baselines/legacy_v11_small.json"
        outside = self.root / "external-baseline.json"
        outside.write_bytes(baseline.read_bytes())
        baseline.unlink()
        try:
            baseline.symlink_to(outside)
        except OSError:
            self.skipTest("host permission does not allow creating a symlink")
        with self.assertRaises(HostedConfigurationError):
            runtime_identity()


@unittest.skipUnless(os.name == "posix", "Linux deployment installer requires POSIX bash paths")
class WheelInstallerTests(unittest.TestCase):
    def test_same_version_new_reviewed_wheel_is_actually_reinstalled(self):
        """Install two tiny zero-dependency synthetic wheels into the same venv.

        Their name/version match; their sole identity probe returns different
        synthetic commit markers. This exercises pip's same-version handling
        without running the real service or relying on packaging dependencies.
        """
        script = Path(__file__).resolve().parent.parent / "deploy/aml/install-wheel.sh"

        def wheel(directory: Path, commit: str) -> Path:
            info = "flowgrid_agent_memory-0.1.1.dist-info"
            contents = {
                "aml_retriever/__init__.py": b"",
                "aml_retriever/aml_hosted.py": (
                    "def runtime_identity():\n    return "
                    + repr({"identity_source": "build_manifest", "source_state": "committed", "source_commit": commit})
                    + "\n"
                ).encode("utf-8"),
                info + "/METADATA": b"Metadata-Version: 2.1\nName: flowgrid-agent-memory\nVersion: 0.1.1\nRequires-Python: >=3.11\n\n",
                info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: synthetic-installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            }
            record = "".join(
                name + ",sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=") + "," + str(len(data)) + "\n"
                for name, data in sorted(contents.items())
            ) + info + "/RECORD,,\n"
            contents[info + "/RECORD"] = record.encode("utf-8")
            directory.mkdir()
            path = directory / "flowgrid_agent_memory-0.1.1-py3-none-any.whl"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in sorted(contents.items()):
                    archive.writestr(name, data)
            return path

        with tempfile.TemporaryDirectory(prefix="aml-installer-test-") as directory:
            root = Path(directory)
            old = wheel(root / "first", "a" * 40)
            reviewed = wheel(root / "reviewed", "b" * 40)
            venv = root / "venv"
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
            env.update({"AML_INSTALL_PYTHON": sys.executable, "PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
            for artifact, expected in ((old, "a" * 40), (reviewed, "b" * 40)):
                subprocess.run(
                    ["bash", str(script), str(artifact), hashlib.sha256(artifact.read_bytes()).hexdigest(), str(venv)],
                    cwd=root, env=env, check=True, capture_output=True, timeout=90,
                )
                actual = subprocess.run(
                    [str(venv / "bin/python"), "-I", "-c", "from aml_retriever.aml_hosted import runtime_identity; print(runtime_identity()['source_commit'])"],
                    cwd=root, env=env, check=True, capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                self.assertEqual(actual, expected)


class HostedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aml-hosted-test-")
        self.server = HostedAMLServer(HostedConfig(
            credential=KEY, db_path=str(Path(self.directory.name) / "memory.db"),
            port=0, max_body_bytes=16384, max_inflight=1, max_connections=4,
            read_timeout_seconds=0.15, operation_timeout_seconds=0.3,
            request_timeout_seconds=0.8,
        ), quiet=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.service.close()
        self.directory.cleanup()

    def request(self, path, data=None, key=KEY, method="POST", headers=None):
        raw = json.dumps(data).encode("utf-8") if data is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.base + path, data=raw, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read()), dict(response.headers)

    def raw(self, request: bytes) -> tuple[int, dict]:
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            pieces = []
            while chunk := connection.recv(65536):
                pieces.append(chunk)
        response = b"".join(pieces)
        head, body = response.split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), json.loads(body)

    def framed(self, body=b"{}", extra=b"", content_length=None):
        length = len(body) if content_length is None else content_length
        return (b"POST /add HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                + KEY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(length).encode() + b"\r\n" + extra + b"\r\n" + body)

    def test_health_contains_only_safe_identity(self):
        status, body, headers = self.request("/health", method="GET", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["product_version"], PRODUCT_VERSION)
        self.assertEqual(headers["X-FlowGrid-Memory-Version"], PRODUCT_VERSION)
        self.assertNotIn(self.directory.name, json.dumps(body))
        self.assertNotIn(KEY, json.dumps(body))

    def test_authentication_missing_bad_and_correct(self):
        for key in (None, "wrong"):
            status, body, _ = self.request("/add", payload(), key=key)
            self.assertEqual(status, 401)
            self.assertEqual(body, {"detail": {"reason": "authentication required"}})
        self.assertEqual(self.request("/add", payload())[0], 200)

    def test_bearer_token_and_x_api_key_modes(self):
        for mode, header, value in (("token", "Authorization", "Token " + KEY), ("x-api-key", "X-Api-Key", KEY)):
            object.__setattr__(self.server.config, "auth_mode", mode)
            self.assertEqual(self.request("/search", {"user_id": "u", "query": "q", "top_k": 100}, key=None, headers={header: value})[0], 200)

    def test_admin_stats_and_governance_routes_absent(self):
        for path in ("/stats", "/admin/delete_user", "/v1/memories/transition", "/v1/events", "/health?key=" + KEY):
            for method in ("GET", "POST"):
                self.assertEqual(self.request(path, {}, method=method)[0], 404)
        self.assertEqual(self.request("/add", method="GET")[0], 405)
        self.assertEqual(self.request("/health", {}, method="POST")[0], 405)

    def test_add_search_immediate_idempotent_top_k_100_and_isolation(self):
        original = payload()
        status, body, _ = self.request("/add", original)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"success": True, "request_id": original["request_id"], "user_id": original["user_id"], "session_id": original["session_id"]})
        query = {"user_id": original["user_id"], "query": "comet lighthouse", "top_k": 100}
        status, initial, _ = self.request("/search", query)
        self.assertEqual(status, 200)
        self.assertTrue(initial["data"])
        self.assertLessEqual(len(initial["data"]), 100)
        self.request("/add", original)
        self.assertEqual(self.request("/search", query)[1], initial)
        self.assertEqual(self.server.service.db.count(original["user_id"]), 1)
        self.assertEqual(self.request("/search", {**query, "user_id": "different-user"})[1], {"data": []})
        self.assertEqual(self.server.service.search_governed(user_id=original["user_id"]).records, [])

    def test_invalid_and_oversized_bodies_do_not_write(self):
        cases = (
            (self.framed(b"not-json"), 400),
            (self.framed(b'{"x":NaN}'), 400),
            (self.framed(b'{"user_id":"a","user_id":"b"}'), 400),
            (self.framed(b"[]"), 422),
            (self.framed(b"{}", content_length=99999), 413),
            (self.framed(b"{}", extra=b"Content-Length: 2\r\n"), 400),
            (self.framed(b"{}", extra=b"Transfer-Encoding: chunked\r\n"), 400),
            (self.framed(b"{}", extra=b"Authorization: Bearer " + KEY.encode() + b"\r\n"), 401),
            (self.framed(b"{}", extra=b"Expect: 100-continue\r\n"), 417),
            (self.framed(b"{}", content_length="-1"), 400),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                status, body = self.raw(raw)
                self.assertEqual(status, expected)
                self.assertEqual(set(body), {"detail"})
        self.assertEqual(self.server.service.db.count(), 0)

    def test_short_body_and_content_type_rejected(self):
        self.assertEqual(self.raw(self.framed(b"{}", content_length=3))[0], 400)
        self.assertEqual(self.request("/add", payload(), headers={"Content-Type": "text/plain"})[0], 415)

    def test_request_framing_missing_length_and_invalid_parser_errors(self):
        headers = b"POST /add HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + KEY.encode() + b"\r\nContent-Type: application/json\r\n"
        status, body = self.raw(headers + b"\r\n{}")
        self.assertEqual(status, 411)
        self.assertEqual(body["detail"]["reason"], "Content-Length required")
        status, body = self.raw(b"INVALID " + KEY.encode() + b" BADVERSION\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertNotIn(KEY, json.dumps(body))

    def test_accepted_memory_survives_service_restart(self):
        self.assertEqual(self.request("/add", payload())[0], 200)
        config = self.server.config
        self.server.shutdown()
        self.server.server_close()
        self.server.service.close()
        self.thread.join(2)
        self.server = HostedAMLServer(config, quiet=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        status, body, _ = self.request("/search", {"user_id": "synthetic-user-1", "query": "comet lighthouse", "top_k": 100})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"])

    def test_connection_admission_is_bounded(self):
        held = 0
        try:
            while self.server.connections.acquire(blocking=False):
                held += 1
            status, body, headers = self.request("/health", method="GET", key=None)
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(body["detail"]["reason"], "capacity limit reached")
        finally:
            for _ in range(held):
                self.server.connections.release()
        self.assertEqual(self.request("/health", method="GET", key=None)[0], 200)

    def test_slow_header_connection_has_absolute_deadline(self):
        # Keep every idle interval shorter than the socket idle timeout.
        # The absolute lifetime must still end this incomplete request.
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(b"GET /health HTTP/1.1\r\nX-Slow: ")
            started = time.monotonic()
            while time.monotonic() - started < 1.5:
                try:
                    connection.sendall(b"a")
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("slow header connection exceeded absolute deadline")
            self.assertLess(time.monotonic() - started, 1.5)

    def test_runtime_failure_is_500_and_safe(self):
        with mock.patch.object(self.server.service, "official_search", side_effect=RuntimeError(KEY + MARKER)):
            status, body, _ = self.request("/search", {"user_id": "u", "query": MARKER, "top_k": 100})
        self.assertEqual(status, 500)
        self.assertEqual(body, {"detail": {"reason": "internal operation failed"}})

    def test_rate_limit_has_retry_after_and_recovers(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(_payload):
            started.set()
            release.wait(2)
            return {"data": []}

        with mock.patch.object(self.server.service, "official_search", side_effect=blocking):
            first = threading.Thread(target=lambda: self.request("/search", {}))
            first.start()
            self.assertTrue(started.wait(1))
            status, body, headers = self.request("/add", payload())
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(body["detail"]["reason"], "capacity limit reached")
            release.set()
            first.join(2)
        self.assertEqual(self.request("/add", payload())[0], 200)

    def test_timeout_keeps_runtime_slot_until_work_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(_payload):
            started.set()
            release.wait(2)
            return {"data": []}

        try:
            with mock.patch.object(self.server.service, "official_search", side_effect=blocking):
                status, body, _ = self.request("/search", {})
                self.assertTrue(started.is_set())
                self.assertEqual(status, 504)
                self.assertEqual(body["detail"]["reason"], "operation timed out")
                self.assertEqual(self.request("/add", payload())[0], 429)
                release.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if self.server.operations.acquire(blocking=False):
                        self.server.operations.release()
                        break
                    time.sleep(0.01)
                else:
                    self.fail("runtime slot was not released")
        finally:
            release.set()
        self.assertEqual(self.request("/add", payload())[0], 200)

    def test_body_read_timeout_is_bounded(self):
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(self.framed(b"{", content_length=10))
            response = connection.recv(65536)
            while b"request timed out" not in response:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        self.assertIn(b"408 Request Timeout", response)
        self.assertIn(b"request timed out", response)

    def test_logs_omit_credential_path_ids_and_content(self):
        self.server.quiet = False
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            self.request("/add", payload())
            self.request("/private/" + KEY, {}, method="POST")
            with mock.patch.object(self.server.service, "official_search", side_effect=RuntimeError(MARKER)):
                self.request("/search", {"user_id": "synthetic-user-1", "query": MARKER, "top_k": 100})
        logged = buffer.getvalue()
        for secret in (KEY, MARKER, "synthetic-user-1", self.directory.name, "/private/"):
            self.assertNotIn(secret, logged)
        self.assertIn("route=/add", logged)
        self.assertIn("route=unknown", logged)


if __name__ == "__main__":
    unittest.main()
