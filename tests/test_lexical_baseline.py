import os
import tempfile
import unittest

from aml_retriever import Store, tokenize


class TestTokenizer(unittest.TestCase):
    def test_cjk_bigram(self):
        toks = set(tokenize("北京天气"))
        self.assertIn("北", toks)
        self.assertIn("京", toks)
        self.assertIn("北京", toks)
        self.assertIn("京天", toks)
        self.assertIn("天气", toks)

    def test_number_token(self):
        self.assertIn("12345", tokenize("订单 12345 已付款"))

    def test_date_token(self):
        toks = tokenize("会议在 2024-01-02 举行")
        self.assertIn("2024", toks)
        self.assertIn("01", toks)
        self.assertIn("02", toks)

    def test_case_insensitive(self):
        self.assertEqual(set(tokenize("Hello")), set(tokenize("hello")))


class TestAddSearchBaseline(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_add_then_search_immediate(self):
        r = self.store.add(request_id="r1", user_id="u1", content="北京今天下雪")
        self.assertFalse(r.idempotent)
        res = self.store.search(user_id="u1", query="北京")
        self.assertEqual(res.total, 1)
        self.assertEqual(res.results[0].content, "北京今天下雪")

    def test_chinese_search(self):
        self.store.add(request_id="r1", user_id="u1", content="北京今天下雪，气温零下")
        self.store.add(request_id="r2", user_id="u1", content="上海明天多云")
        res = self.store.search(user_id="u1", query="下雪")
        self.assertEqual(res.total, 1)
        self.assertIn("下雪", res.results[0].content)

    def test_number_search(self):
        self.store.add(request_id="r1", user_id="u1", content="订单 12345 已付款")
        res = self.store.search(user_id="u1", query="12345")
        self.assertEqual(res.total, 1)
        self.assertIn("12345", res.results[0].content)

    def test_date_search(self):
        self.store.add(request_id="r1", user_id="u1", content="会议在 2024-01-02 举行")
        res = self.store.search(user_id="u1", query="2024-01-02")
        self.assertEqual(res.total, 1)
        self.assertIn("2024-01-02", res.results[0].content)

    def test_exact_phrase_ranks_first(self):
        self.store.add(request_id="r1", user_id="u1", content="机器学习很有趣")
        self.store.add(request_id="r2", user_id="u1", content="机器会学习吗")
        res = self.store.search(user_id="u1", query="机器学习")
        self.assertGreaterEqual(res.total, 1)
        self.assertEqual(res.results[0].content, "机器学习很有趣")

    def test_user_isolation(self):
        self.store.add(request_id="r1", user_id="u1", content="u1 的私密备忘录")
        self.store.add(request_id="r2", user_id="u2", content="u2 的私密备忘录")
        res_u1 = self.store.search(user_id="u1", query="私密")
        res_u2 = self.store.search(user_id="u2", query="私密")
        self.assertEqual(res_u1.total, 1)
        self.assertEqual(res_u2.total, 1)
        self.assertEqual(res_u1.results[0].user_id, "u1")
        self.assertEqual(res_u2.results[0].user_id, "u2")

    def test_request_id_idempotent(self):
        r1 = self.store.add(request_id="r1", user_id="u1", content="第一条")
        r2 = self.store.add(request_id="r1", user_id="u1", content="重复提交的另一条内容")
        self.assertTrue(r2.idempotent)
        self.assertEqual(r1.message_id, r2.message_id)
        # 仍未重复落库
        res = self.store.search(user_id="u1", query="第一条")
        self.assertEqual(res.total, 1)

    def test_request_id_echo(self):
        r = self.store.add(request_id="echo-me", user_id="u1", content="x")
        self.assertEqual(r.request_id, "echo-me")

    def test_top_k(self):
        for i in range(5):
            self.store.add(request_id=f"r{i}", user_id="u1", content=f"共同关键词 条目{i}")
        res = self.store.search(user_id="u1", query="共同关键词", top_k=3)
        self.assertEqual(len(res.results), 3)
        self.assertEqual(res.total, 5)

    def test_view_extensibility_boundary(self):
        # 本切片只实现 'message'；不同 view 应隔离
        self.store.add(request_id="r1", user_id="u1", content="消息视图内容", view="message")
        self.store.add(request_id="r2", user_id="u1", content="窗口视图内容", view="window")
        msg = self.store.search(user_id="u1", query="内容", view="message")
        win = self.store.search(user_id="u1", query="内容", view="window")
        self.assertEqual(msg.total, 1)
        self.assertEqual(win.total, 1)
        self.assertEqual(msg.results[0].view, "message")
        self.assertEqual(win.results[0].view, "window")

    def test_persistence_across_instances(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "aml.db")
        with Store(db) as s:
            s.add(request_id="r1", user_id="u1", content="持久化测试 北京")
        with Store(db) as s2:
            res = s2.search(user_id="u1", query="北京")
            self.assertEqual(res.total, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
