"""官方 Add / Search 契约测试（字段级 + 端到端 HTTP）。

契约来源：docs/API_CONTRACT.md（官方 api-guide 章节 05/06 抓取核对）。
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from aml_retriever.api import ApiError, MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.server import RetrieverServer


def _payload(request_id="req-1", user_id="u1", session_id="s1", contents=("记忆正文",)):
    return {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
        "messages": [
            {"role": "user", "timestamp": 1704067200000 + i * 1000, "content": c}
            for i, c in enumerate(contents)
        ],
    }


class ServiceCase(unittest.TestCase):
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


class TestAddContract(ServiceCase):
    def test_add_response_shape(self):
        body = self.service.official_add(_payload())
        self.assertEqual(
            set(body.keys()), {"success", "request_id", "user_id", "session_id"}
        )
        self.assertIs(body["success"], True)
        self.assertEqual(body["request_id"], "req-1")
        self.assertEqual(body["user_id"], "u1")
        self.assertEqual(body["session_id"], "s1")

    def test_ids_are_echoed_verbatim(self):
        rid = "eval:run_abc123:locomo_refined:conv-0:chunk-0"
        uid = "eval:run_abc123:locomo:conv-0"
        sid = "eval:run_abc123:sample:0"
        body = self.service.official_add(_payload(rid, uid, sid))
        self.assertEqual((body["request_id"], body["user_id"], body["session_id"]), (rid, uid, sid))

    def test_add_then_search_immediately(self):
        """官方硬性要求：Add 返回后记忆必须立即可检索。"""
        self.service.official_add(_payload(contents=("我住在杭州市西湖区",)))
        body = self.service.official_search({"query": "杭州", "user_id": "u1", "top_k": 10})
        self.assertTrue(body["data"])

    def test_missing_required_fields_are_422(self):
        for key in ("request_id", "user_id", "session_id", "messages"):
            payload = _payload()
            payload.pop(key)
            with self.assertRaises(ApiError) as ctx:
                self.service.official_add(payload)
            self.assertEqual(ctx.exception.status, 422)

    def test_empty_content_rejected(self):
        payload = _payload()
        payload["messages"][0]["content"] = "   "
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertEqual(ctx.exception.status, 422)

    def test_bad_timestamp_rejected(self):
        payload = _payload()
        payload["messages"][0]["timestamp"] = "not-a-number"
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertEqual(ctx.exception.status, 422)

    def test_timestamp_is_optional(self):
        payload = _payload()
        payload["messages"][0].pop("timestamp")
        body = self.service.official_add(payload)
        self.assertIs(body["success"], True)

    # -- role 必填（2026-08-07 收紧）---------------------------------------
    def test_missing_role_is_422(self):
        payload = _payload()
        payload["messages"][0].pop("role")
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("role", ctx.exception.reason)

    def test_blank_or_non_string_role_is_422(self):
        for bad in ("", "   ", None, 1, True, ["user"], {"r": "user"}):
            payload = _payload()
            payload["messages"][0]["role"] = bad
            with self.assertRaises(ApiError) as ctx:
                self.service.official_add(payload)
            self.assertEqual(ctx.exception.status, 422, f"role={bad!r} 应当 422")

    def test_role_error_points_at_offending_index(self):
        payload = _payload(contents=("第一条", "第二条", "第三条"))
        payload["messages"][2].pop("role")
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertIn("messages[2]", ctx.exception.reason)

    def test_unknown_role_value_is_accepted(self):
        """官方未穷举 role 取值，非 user/assistant 的 role 不应被我们擅自拒绝。"""
        payload = _payload()
        payload["messages"][0]["role"] = "system"
        self.assertIs(self.service.official_add(payload)["success"], True)

    # -- timestamp 严格校验，不静默截断 -------------------------------------
    def test_fractional_timestamp_is_rejected_not_truncated(self):
        payload = _payload()
        payload["messages"][0]["timestamp"] = 1704067200000.7
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("truncated", ctx.exception.reason)

    def test_boolean_timestamp_is_rejected(self):
        payload = _payload()
        payload["messages"][0]["timestamp"] = True
        with self.assertRaises(ApiError) as ctx:
            self.service.official_add(payload)
        self.assertEqual(ctx.exception.status, 422)

    def test_integral_float_timestamp_is_accepted_losslessly(self):
        """1.7046072e12 这类 JSON 浮点是无损整数，接受即可，不算截断。"""
        payload = _payload()
        payload["messages"][0]["timestamp"] = 1704067200000.0
        self.assertIs(self.service.official_add(payload)["success"], True)

    def test_nan_or_inf_timestamp_is_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            payload = _payload()
            payload["messages"][0]["timestamp"] = bad
            with self.assertRaises(ApiError) as ctx:
                self.service.official_add(payload)
            self.assertEqual(ctx.exception.status, 422, f"timestamp={bad!r} 应当 422")

    # -- 额外字段与幂等 -----------------------------------------------------
    def test_unused_fields_are_tolerated(self):
        """现行规范不发送 metadata/app_id/agent_id/async_mode，但收到也不能崩。"""
        payload = _payload()
        payload.update({"metadata": {"x": 1}, "app_id": "a", "agent_id": "b", "async_mode": True})
        body = self.service.official_add(payload)
        self.assertIs(body["success"], True)

    def test_unknown_fields_inside_message_are_tolerated(self):
        """message 对象里出现未声明字段同样只忽略，不能整批 422。"""
        payload = _payload()
        payload["messages"][0].update({"name": "alice", "tool_call_id": "t1", "extra": None})
        self.assertIs(self.service.official_add(payload)["success"], True)

    def test_same_request_id_with_different_payload_is_first_write_wins(self):
        """同 (request_id, user_id) 重复提交但正文不同：首写生效，不覆盖不追加。

        这是本实现对官方未声明行为的显式选择，见 MemoryService.official_add 文档。
        """
        first = _payload(request_id="dup-1", contents=("原始正文 Alpha",))
        self.assertIs(self.service.official_add(first)["success"], True)

        second = _payload(request_id="dup-1", contents=("被改写的正文 Beta", "多出来的一条"))
        self.assertIs(self.service.official_add(second)["success"], True)

        self.assertEqual(self.service.db.count("u1"), 1, "重复 request_id 不得追加消息")
        body = self.service.official_search({"query": "Alpha", "user_id": "u1", "top_k": 10})
        self.assertTrue(any("Alpha" in item["content"] for item in body["data"]))
        beta = self.service.official_search({"query": "Beta", "user_id": "u1", "top_k": 10})
        self.assertFalse(any("Beta" in item["content"] for item in beta["data"]),
                         "第二次提交的正文不应进入索引")

    def test_same_request_id_different_user_is_not_idempotent(self):
        """幂等键是 (request_id, user_id)，换 user 必须各自独立落库。"""
        self.service.official_add(_payload(request_id="shared", user_id="ua",
                                           contents=("ua 的内容",)))
        self.service.official_add(_payload(request_id="shared", user_id="ub",
                                           contents=("ub 的内容",)))
        self.assertEqual(self.service.db.count("ua"), 1)
        self.assertEqual(self.service.db.count("ub"), 1)


class TestSearchContract(ServiceCase):
    def setUp(self):
        super().setUp()
        self.service.official_add(
            _payload(contents=tuple(f"第 {i} 条记忆，关于项目 Apollo 的进展" for i in range(8)))
        )

    def test_search_response_shape(self):
        body = self.service.official_search({"query": "Apollo", "user_id": "u1", "top_k": 5})
        self.assertIsInstance(body, dict)
        self.assertIn("data", body)
        self.assertIsInstance(body["data"], list)
        self.assertNotIn("items", body)
        for item in body["data"]:
            self.assertIsInstance(item["id"], str)
            self.assertTrue(item["id"])
            self.assertIsInstance(item["content"], str)
            self.assertTrue(item["content"])
            self.assertIsInstance(item["score"], float)

    def test_top_k_upper_bound(self):
        body = self.service.official_search({"query": "记忆", "user_id": "u1", "top_k": 3})
        self.assertLessEqual(len(body["data"]), 3)

    def test_no_result_returns_empty_array(self):
        body = self.service.official_search(
            {"query": "完全不存在的内容zzzz", "user_id": "u1", "top_k": 10}
        )
        self.assertEqual(body["data"], [])

    def test_options_are_accepted(self):
        body = self.service.official_search(
            {"query": "哪个选项符合记忆？", "options": ["A. Apollo", "B. Zeus"],
             "user_id": "u1", "top_k": 5}
        )
        self.assertIsInstance(body["data"], list)

    def test_bad_options_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.official_search(
                {"query": "q", "options": [1, 2], "user_id": "u1", "top_k": 5}
            )
        self.assertEqual(ctx.exception.status, 422)

    def test_missing_query_or_user_is_422(self):
        for payload in ({"user_id": "u1", "top_k": 5}, {"query": "q", "top_k": 5}):
            with self.assertRaises(ApiError) as ctx:
                self.service.official_search(payload)
            self.assertEqual(ctx.exception.status, 422)

    # -- top_k 必填且必须是真整数（2026-08-07 收紧）-------------------------
    def test_missing_top_k_is_422(self):
        """官方 Search 把 top_k 列为必填，缺失时不得回落到服务端默认值。"""
        with self.assertRaises(ApiError) as ctx:
            self.service.official_search({"query": "Apollo", "user_id": "u1"})
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("top_k", ctx.exception.reason)

    def test_null_top_k_is_422(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.official_search({"query": "Apollo", "user_id": "u1", "top_k": None})
        self.assertEqual(ctx.exception.status, 422)

    def test_non_integer_top_k_is_422(self):
        bad_values = [
            5.0,            # 即便数值上是整数，float 也不接受
            5.7,            # 更不能截断成 5
            "5",            # 数字字符串不隐式转换
            "  5 ",
            True,           # bool 是 int 子类，必须单独拦
            False,
            [5],
            {"k": 5},
        ]
        for bad in bad_values:
            with self.assertRaises(ApiError) as ctx:
                self.service.official_search(
                    {"query": "Apollo", "user_id": "u1", "top_k": bad}
                )
            self.assertEqual(ctx.exception.status, 422, f"top_k={bad!r} 应当 422")
            self.assertIn("top_k", ctx.exception.reason)

    def test_negative_top_k_is_422(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.official_search({"query": "Apollo", "user_id": "u1", "top_k": -1})
        self.assertEqual(ctx.exception.status, 422)

    def test_official_top_k_100_is_accepted(self):
        """正式评测固定 top_k=100，必须走通且不被上限逻辑误伤。"""
        body = self.service.official_search({"query": "Apollo", "user_id": "u1", "top_k": 100})
        self.assertIsInstance(body["data"], list)
        self.assertLessEqual(len(body["data"]), 100)

    def test_top_k_above_max_is_clamped_not_rejected(self):
        """超过服务端上限只钳制，不报错——宁可少返回也不能让评测整批失败。"""
        body = self.service.official_search({"query": "Apollo", "user_id": "u1", "top_k": 10**6})
        self.assertLessEqual(len(body["data"]), self.service.config.top_k_max)

    def test_search_unknown_fields_are_tolerated(self):
        body = self.service.official_search(
            {"query": "Apollo", "user_id": "u1", "top_k": 5,
             "session_id": "s1", "filters": {"x": 1}, "rerank": True}
        )
        self.assertIsInstance(body["data"], list)

    def test_results_ordered_by_score_desc(self):
        body = self.service.official_search({"query": "Apollo 进展", "user_id": "u1", "top_k": 20})
        scores = [item["score"] for item in body["data"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_cross_user_isolation(self):
        self.service.official_add(
            _payload(request_id="r2", user_id="u2", contents=("u2 的秘密内容 Zeus",))
        )
        body = self.service.official_search({"query": "Zeus", "user_id": "u1", "top_k": 50})
        self.assertTrue(all("秘密内容" not in item["content"] for item in body["data"]))


class TestHttpEndToEnd(unittest.TestCase):
    """真实 HTTP 端到端 smoke：验证传输层、鉴权与健康检查。"""

    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls.config = RetrieverConfig(
            db_path=cls.path, host="127.0.0.1", port=0,
            auth_mode="bearer", api_key="test-key-123",
        )
        cls.server = RetrieverServer(cls.config, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.path + suffix)
            except OSError:
                pass

    def _call(self, path, payload=None, method="POST", token="test-key-123"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_needs_no_auth(self):
        status, body = self._call("/health", method="GET", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_add_and_search_round_trip(self):
        status, body = self._call("/add", _payload(request_id="http-1", user_id="http-u",
                                                   contents=("我的车牌是 浙A88888",)))
        self.assertEqual(status, 200)
        self.assertIs(body["success"], True)

        status, body = self._call("/search", {"query": "车牌", "user_id": "http-u", "top_k": 10})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"])
        self.assertTrue(any("浙A88888" in item["content"] for item in body["data"]))

    def test_unauthorized_is_401(self):
        status, body = self._call("/search", {"query": "x", "user_id": "u", "top_k": 1},
                                  token="wrong-key")
        self.assertEqual(status, 401)
        self.assertIn("reason", body["detail"])

    def test_validation_error_is_422(self):
        status, body = self._call("/add", {"request_id": "x"})
        self.assertEqual(status, 422)
        self.assertIn("reason", body["detail"])

    def test_http_missing_role_is_422(self):
        payload = _payload(request_id="http-role", user_id="http-u3")
        payload["messages"][0].pop("role")
        status, body = self._call("/add", payload)
        self.assertEqual(status, 422)
        self.assertIn("role", body["detail"]["reason"])

    def test_http_missing_top_k_is_422(self):
        status, body = self._call("/search", {"query": "车牌", "user_id": "http-u"})
        self.assertEqual(status, 422)
        self.assertIn("top_k", body["detail"]["reason"])

    def test_http_float_top_k_is_422(self):
        status, body = self._call("/search", {"query": "车牌", "user_id": "http-u", "top_k": 10.5})
        self.assertEqual(status, 422)
        self.assertIn("top_k", body["detail"]["reason"])

    def test_http_string_top_k_is_422(self):
        status, _ = self._call("/search", {"query": "车牌", "user_id": "http-u", "top_k": "10"})
        self.assertEqual(status, 422)

    def test_http_fractional_timestamp_is_422(self):
        payload = _payload(request_id="http-ts", user_id="http-u4")
        payload["messages"][0]["timestamp"] = 1704067200000.5
        status, _ = self._call("/add", payload)
        self.assertEqual(status, 422)

    def test_bad_json_is_400(self):
        url = f"http://127.0.0.1:{self.port}/add"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        req.add_header("Authorization", "Bearer test-key-123")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)

    def test_unknown_path_is_404(self):
        status, _ = self._call("/nope", {"a": 1})
        self.assertEqual(status, 404)

    def test_no_202_or_task_id(self):
        """官方明确禁止返回 202 / task id / 状态查询地址。"""
        status, body = self._call("/add", _payload(request_id="http-2", user_id="http-u2"))
        self.assertEqual(status, 200)
        self.assertNotIn("task_id", body)
        self.assertNotIn("status_url", body)


if __name__ == "__main__":
    unittest.main()
