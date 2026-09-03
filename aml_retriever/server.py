"""AML Retriever — 官方 Add/Search HTTP wrapper（仅标准库）。

暴露三个端点（路径可配置，官方明确"请求/响应格式固定，不随 URL 路径变化"）：
  POST {add_path}    默认 /add
  POST {search_path} 默认 /search
  GET  {health_path} 默认 /health   —— 无需鉴权，返回 2xx

鉴权模式（官方支持 Token / Bearer / X-Api-Key；none 仅用于公开 smoke）：
  none | bearer | token | x-api-key

隐私：**绝不把记忆内容写入日志**，访问日志只记录方法、路径、状态码与耗时。
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import ApiError, MemoryService
from .config import RetrieverConfig

MAX_BODY_BYTES = 32 * 1024 * 1024  # 32MB，覆盖 20 条消息 / 2000 词的分块上限


class _Handler(BaseHTTPRequestHandler):
    server_version = "aml-retriever/1.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- utilities
    @property
    def config(self) -> RetrieverConfig:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def service(self) -> MemoryService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A003
        """只记录访问元信息，绝不记录请求/响应正文。"""
        if getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def _send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        mode = (self.config.auth_mode or "none").strip().lower()
        if mode == "none":
            return True
        expected = self.config.api_key or ""
        if not expected:
            return True  # 未配置密钥时不强制（等价 none），避免误锁死接口
        if mode == "x-api-key":
            return self.headers.get("X-Api-Key", "") == expected
        header = self.headers.get("Authorization", "")
        prefix = "Bearer " if mode == "bearer" else "Token "
        return header.startswith(prefix) and header[len(prefix) :].strip() == expected

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "invalid Content-Length header")
        if length <= 0:
            raise ApiError(400, "request body is required")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "request body must be valid UTF-8 JSON")

    # -------------------------------------------------------------- handlers
    def do_GET(self):  # noqa: N802
        started = time.time()
        path = self.path.split("?", 1)[0]
        if path == self.config.health_path:
            # Health 无需鉴权，任意 2xx 即视为正常
            self._send_json(200, self.service.health())
        elif path == "/stats":
            if not self._authorized():
                self._send_json(401, ApiError(401, "unauthorized").to_body())
            else:
                self._send_json(200, self.service.stats())
        else:
            self._send_json(404, ApiError(404, f"unknown path: {path}").to_body())
        self._trace("GET", path, started)

    def do_POST(self):  # noqa: N802
        started = time.time()
        path = self.path.split("?", 1)[0]
        try:
            if path not in (self.config.add_path, self.config.search_path, "/admin/delete_user"):
                raise ApiError(404, f"unknown path: {path}")
            if not self._authorized():
                raise ApiError(401, "unauthorized: invalid or missing credential")

            payload = self._read_json()
            if path == self.config.add_path:
                self._send_json(200, self.service.official_add(payload))
            elif path == self.config.search_path:
                self._send_json(200, self.service.official_search(payload))
            else:
                user_id = payload.get("user_id") if isinstance(payload, dict) else None
                if not user_id:
                    raise ApiError(422, "'user_id' is required")
                self._send_json(200, self.service.delete_user(user_id))
        except ApiError as exc:
            self._send_json(exc.status, exc.to_body())
        except Exception:  # 不外泄内部细节与记忆内容
            self._send_json(500, ApiError(500, "internal error").to_body())
        self._trace("POST", path, started)

    def _trace(self, method: str, path: str, started: float) -> None:
        if getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            return
        elapsed_ms = (time.time() - started) * 1000.0
        # 只有元信息，无正文
        print(f"[aml] {method} {path} {elapsed_ms:.1f}ms", flush=True)


class RetrieverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: RetrieverConfig, service: MemoryService | None = None,
                 quiet: bool = False):
        self.config = config
        self.service = service or MemoryService(config)
        self.quiet = quiet
        super().__init__((config.host, config.port), _Handler)


def serve(config: RetrieverConfig | None = None, quiet: bool = False) -> None:
    config = config or RetrieverConfig.from_env()
    httpd = RetrieverServer(config, quiet=quiet)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(
        f"[aml] listening on http://{host}:{port} "
        f"(add={config.add_path} search={config.search_path} health={config.health_path} "
        f"auth={config.auth_mode} db={config.db_path})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        httpd.service.close()


if __name__ == "__main__":
    serve()
