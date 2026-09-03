"""隐私与生命周期测试：不记录内容日志、按 user_id 删除、彻底清理。"""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout

from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.server import RetrieverServer

SECRET = "身份证号 110101199003074designator"


class TestNoContentInLogs(unittest.TestCase):
    """访问日志只允许包含方法、路径、状态码与耗时，绝不含记忆内容。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.config = RetrieverConfig(db_path=self.path, host="127.0.0.1", port=0)
        self.server = RetrieverServer(self.config, quiet=False)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_request_content_never_printed(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self._post("/add", {
                "request_id": "p1", "user_id": "priv-u", "session_id": "s1",
                "messages": [{"role": "user", "content": SECRET}],
            })
            self._post("/search", {"query": SECRET, "user_id": "priv-u", "top_k": 5})
        logged = buffer.getvalue()
        self.assertNotIn(SECRET, logged)
        self.assertNotIn("110101199003074", logged)
        self.assertIn("/add", logged)  # 元信息仍然可观测


class TestDeleteLifecycle(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.service = MemoryService(RetrieverConfig(db_path=self.path))

    def tearDown(self):
        self.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _seed(self, user_id, n=6):
        self.service.official_add({
            "request_id": f"seed-{user_id}",
            "user_id": user_id,
            "session_id": "s1",
            "messages": [{"role": "user", "content": f"{user_id} 的第 {i} 条记忆 {SECRET}"}
                         for i in range(n)],
        })

    def test_delete_user_is_complete(self):
        self._seed("target")
        self._seed("bystander")
        report = self.service.delete_user("target")
        self.assertEqual(report["deleted_messages"], 6)

        body = self.service.official_search({"query": SECRET, "user_id": "target", "top_k": 100})
        self.assertEqual(body["data"], [])
        # 旁观用户不受影响
        body2 = self.service.official_search({"query": SECRET, "user_id": "bystander", "top_k": 100})
        self.assertTrue(body2["data"])

    def test_delete_removes_all_traces_in_storage(self):
        self._seed("target")
        self.service.delete_user("target")
        for table in ("messages", "views", "requests", "sessions"):
            left = self.service.db.query(
                f"SELECT COUNT(*) FROM {table} WHERE user_id='target'"
            )[0][0]
            self.assertEqual(left, 0, f"{table} 仍有 target 残留")
        left_fts = self.service.db.query("SELECT COUNT(*) FROM fts WHERE user_id='target'")[0][0]
        self.assertEqual(left_fts, 0)

    def test_purge_all(self):
        self._seed("a")
        self._seed("b")
        self.service.db.purge_all()
        self.assertEqual(self.service.db.count(), 0)

    def test_health_leaks_nothing(self):
        self._seed("target")
        body = self.service.health()
        self.assertEqual(set(body.keys()), {"status", "service"})


if __name__ == "__main__":
    unittest.main()
