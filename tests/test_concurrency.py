"""并发与一致性测试：对齐官方披露的 Add 64 workers / Search 32 workers。

官方披露（GitHub README）：Add 默认 64 global workers，Search 32 workers。
本测试在本机用线程池模拟同等并发度，验证：
  - 无数据丢失、无重复写入（幂等在并发下仍成立）
  - SQLite WAL + busy_timeout + 写锁重试下不出现 "database is locked" 失败
  - 并发 Search 结果稳定、不跨 user_id
"""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB

ADD_WORKERS = 64      # 官方 Add 默认并发
SEARCH_WORKERS = 32   # 官方 Search 并发
RECORDS_PER_WORKER = 4


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = RetrieverDB(RetrieverConfig(db_path=self.path))

    def tearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_concurrent_adds_no_loss_no_duplicate(self):
        errors: list[Exception] = []

        def worker(index: int):
            try:
                for j in range(RECORDS_PER_WORKER):
                    self.db.add(
                        request_id=f"req-{index}-{j}",
                        user_id=f"user-{index % 8}",
                        session_id=f"sess-{index}",
                        messages=[{"role": "user",
                                   "content": f"worker {index} record {j} 关于订单 {index}{j}"}],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=ADD_WORKERS) as pool:
            list(pool.map(worker, range(ADD_WORKERS)))

        self.assertEqual(errors, [], f"并发写入出现异常: {errors[:3]}")
        self.assertEqual(self.db.count(), ADD_WORKERS * RECORDS_PER_WORKER)

    def test_concurrent_idempotency(self):
        """同一 request_id 被 64 个线程同时提交，只能落库一次。"""
        errors: list[Exception] = []
        barrier = threading.Barrier(ADD_WORKERS)

        def worker(_index: int):
            try:
                barrier.wait(timeout=30)
                self.db.add(
                    request_id="same-request-id",
                    user_id="racer",
                    session_id="s1",
                    messages=[{"role": "user", "content": "并发幂等测试内容"}],
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=ADD_WORKERS) as pool:
            list(pool.map(worker, range(ADD_WORKERS)))

        self.assertEqual(errors, [], f"并发幂等出现异常: {errors[:3]}")
        self.assertEqual(self.db.count("racer"), 1)

    def test_concurrent_search_is_stable_and_isolated(self):
        for u in range(4):
            self.db.add(
                request_id=f"seed-{u}",
                user_id=f"user-{u}",
                session_id="s1",
                messages=[{"role": "user", "content": f"user {u} 的机密编号 {u}{u}{u}{u}"}
                          for _ in range(1)],
            )
        expected = {}
        for u in range(4):
            res = self.db.search(user_id=f"user-{u}", query="机密编号", top_k=10)
            expected[u] = [e.id for e in res.results]

        errors: list[Exception] = []
        mismatches: list[str] = []

        def worker(index: int):
            try:
                u = index % 4
                res = self.db.search(user_id=f"user-{u}", query="机密编号", top_k=10)
                if [e.id for e in res.results] != expected[u]:
                    mismatches.append(f"user-{u} 结果不稳定")
                for evidence in res.results:
                    if f"{u}{u}{u}{u}" not in evidence.content:
                        mismatches.append(f"user-{u} 出现跨用户内容")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            list(pool.map(worker, range(SEARCH_WORKERS * 4)))

        self.assertEqual(errors, [], f"并发检索出现异常: {errors[:3]}")
        self.assertEqual(mismatches, [], f"并发检索不稳定: {mismatches[:3]}")

    def test_mixed_read_write(self):
        """写入与检索同时进行：不得抛异常，且写后可读。"""
        errors: list[Exception] = []
        stop = threading.Event()

        def writer(index: int):
            try:
                for j in range(RECORDS_PER_WORKER):
                    self.db.add(
                        request_id=f"mix-{index}-{j}",
                        user_id="mixed",
                        session_id=f"s-{index}",
                        messages=[{"role": "user", "content": f"混合负载 {index}-{j} 关键词 himalaya"}],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                while not stop.is_set():
                    self.db.search(user_id="mixed", query="himalaya", top_k=20)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        for t in readers:
            t.start()
        with ThreadPoolExecutor(max_workers=ADD_WORKERS) as pool:
            list(pool.map(writer, range(ADD_WORKERS)))
        stop.set()
        for t in readers:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"混合读写出现异常: {errors[:3]}")
        final = self.db.search(user_id="mixed", query="himalaya", top_k=100)
        self.assertGreater(final.total, 0)


if __name__ == "__main__":
    unittest.main()
