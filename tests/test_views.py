"""视图与 provenance 测试：确定性、可配置、可回指原始消息。"""
import os
import tempfile
import unittest

from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB
from aml_retriever.views import (
    affected_window_starts,
    build_segments,
    build_windows,
    segment_boundaries,
    window_starts,
)


def _msgs(n, start_ms=1_700_000_000_000, step_ms=60_000):
    return [
        {"id": f"m{i}", "role": "user", "content": f"消息{i}", "ts_ms": start_ms + i * step_ms,
         "created_at": ""}
        for i in range(n)
    ]


class TestWindowMath(unittest.TestCase):
    def test_window_starts_deterministic(self):
        self.assertEqual(window_starts(7, 3, 1), [0, 2, 4, 6])
        self.assertEqual(window_starts(0, 3, 1), [])
        self.assertEqual(window_starts(1, 3, 1), [0])

    def test_no_overlap_is_disjoint(self):
        self.assertEqual(window_starts(6, 3, 0), [0, 3])

    def test_affected_windows_are_bounded(self):
        affected = affected_window_starts(10, 12, 3, 1)
        self.assertTrue(all(s + 3 > 10 for s in affected))
        self.assertNotIn(0, affected)


class TestSegmentBoundaries(unittest.TestCase):
    def test_split_by_max_messages(self):
        bounds = segment_boundaries(_msgs(7), max_messages=3, max_gap_seconds=10**9)
        self.assertEqual(bounds, [(0, 2), (3, 5), (6, 6)])

    def test_split_by_time_gap(self):
        msgs = _msgs(4)
        msgs[2]["ts_ms"] = msgs[1]["ts_ms"] + 10_000_000  # 大间隙
        bounds = segment_boundaries(msgs, max_messages=100, max_gap_seconds=1800)
        self.assertEqual(bounds, [(0, 1), (2, 3)])

    def test_empty(self):
        self.assertEqual(segment_boundaries([], 3, 1800), [])


class TestProvenance(unittest.TestCase):
    def test_views_point_back_to_messages(self):
        msgs = _msgs(5)
        windows = build_windows(msgs, "u1", "s1", 3, 1)
        for view in windows:
            self.assertTrue(view.source_message_ids)
            for mid in view.source_message_ids:
                self.assertIn(mid, {m["id"] for m in msgs})

    def test_view_ids_unique_across_users(self):
        msgs = _msgs(3)
        a = build_windows(msgs, "userA", "s1", 3, 1)[0].view_id
        b = build_windows(msgs, "userB", "s1", 3, 1)[0].view_id
        self.assertNotEqual(a, b)

    def test_segments_cover_all_messages(self):
        msgs = _msgs(10)
        segs = build_segments(msgs, "u1", "s1", 4, 10**9)
        covered = [mid for s in segs for mid in s.source_message_ids]
        self.assertEqual(covered, [m["id"] for m in msgs])


class TestIncrementalEqualsFullScan(unittest.TestCase):
    """增量重建的视图必须与全量扫描参考实现完全一致。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_incremental_matches_reference(self):
        cfg = RetrieverConfig(db_path=self.path, window_size=3, window_overlap=1,
                              segment_max_messages=4, segment_max_gap_seconds=1800)
        db = RetrieverDB(cfg)
        base = 1_700_000_000_000
        total = 0
        # 分多批 Add（模拟平台 20 条一块的分块写入）
        for batch in range(4):
            payload = [
                {"role": "user", "content": f"消息{total + i}", "timestamp": base + (total + i) * 60_000}
                for i in range(3)
            ]
            db.add(request_id=f"r{batch}", user_id="u1", session_id="s1", messages=payload)
            total += 3

        rows = db.query(
            "SELECT view_id, view_type, content, start_seq, end_seq FROM views "
            "WHERE user_id='u1' ORDER BY view_type, start_seq"
        )
        actual = [(r["view_id"], r["view_type"], r["content"]) for r in rows]

        msg_rows = db.query(
            "SELECT id, role, content, created_at, seq FROM messages "
            "WHERE user_id='u1' ORDER BY seq"
        )
        reference_msgs = [dict(r) for r in msg_rows]
        for m in reference_msgs:
            m["ts_ms"] = None
        expected_views = build_windows(reference_msgs, "u1", "s1", 3, 1) + build_segments(
            reference_msgs, "u1", "s1", 4, 10**9
        )
        expected = sorted(
            [(v.view_id, v.view_type, v.content) for v in expected_views],
            key=lambda x: (x[1], x[0]),
        )
        self.assertEqual(sorted(actual, key=lambda x: (x[1], x[0])), expected)
        db.close()

    def test_window_size_is_configurable(self):
        cfg = RetrieverConfig(db_path=self.path, window_size=2, window_overlap=0,
                              segment_max_messages=50, segment_max_gap_seconds=10**9)
        db = RetrieverDB(cfg)
        db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": f"消息{i}"} for i in range(4)],
        )
        rows = db.query(
            "SELECT source_ids FROM views WHERE user_id='u1' AND view_type='window'"
        )
        self.assertEqual(len(rows), 2)  # 4 条消息 / 窗口 2 无重叠 -> 2 个窗口
        db.close()


if __name__ == "__main__":
    unittest.main()
