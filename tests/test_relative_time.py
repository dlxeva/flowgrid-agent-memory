from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from aml_retriever.api import MemoryService
from aml_retriever.config import DEFAULT_FLAGS, RetrieverConfig
from aml_retriever.evaluation.dataset import RELATIVE_TIME_AS_OF, make_dataset
from aml_retriever.evaluation.harness import ABLATION_LADDER, run_stage
from aml_retriever.features import parse_relative_time_window
from aml_retriever.retriever import RetrieverDB


class TestRelativeTimeParser(unittest.TestCase):
    def test_last_week_uses_anchor_timezone_and_returns_utc_half_open_bounds(self):
        anchor = datetime(2025, 3, 12, 15, 30, tzinfo=timezone(timedelta(hours=8)))
        window = parse_relative_time_window("猎户座上周有什么进展？", anchor=anchor)
        self.assertEqual(window.start, datetime(2025, 3, 2, 16, tzinfo=timezone.utc))
        self.assertEqual(window.end, datetime(2025, 3, 9, 16, tzinfo=timezone.utc))
        self.assertTrue(window.contains(window.start))
        self.assertFalse(window.contains(window.end))

    def test_last_month_crosses_year_boundary(self):
        anchor = datetime(2025, 1, 20, 9, tzinfo=timezone.utc)
        window = parse_relative_time_window("What changed last month?", anchor=anchor)
        self.assertEqual(window.start, datetime(2024, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(window.end, datetime(2025, 1, 1, tzinfo=timezone.utc))

    def test_recent_chinese_months_clamp_calendar_day(self):
        anchor = datetime(2025, 5, 31, 12, tzinfo=timezone.utc)
        window = parse_relative_time_window("最近三个月有什么变化？", anchor=anchor)
        self.assertEqual(window.start, datetime(2025, 2, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(window.end, anchor)

    def test_recent_english_days_and_weeks(self):
        anchor = datetime(2025, 6, 20, 12, tzinfo=timezone.utc)
        days = parse_relative_time_window("events in the past 10 days", anchor=anchor)
        weeks = parse_relative_time_window("events in the last 2 weeks", anchor=anchor)
        self.assertEqual(days.start, anchor - timedelta(days=10))
        self.assertEqual(weeks.start, anchor - timedelta(weeks=2))

    def test_requires_aware_anchor_and_rejects_unmatched_text(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            parse_relative_time_window("上周", anchor=datetime(2025, 1, 1))
        self.assertIsNone(parse_relative_time_window(
            "最近的进展", anchor=datetime(2025, 1, 1, tzinfo=timezone.utc)
        ))
        self.assertIsNone(parse_relative_time_window(
            "past 121 days", anchor=datetime(2025, 1, 1, tzinfo=timezone.utc)
        ))

    def test_ambiguous_multiple_windows_abstain(self):
        anchor = datetime(2025, 6, 20, 12, tzinfo=timezone.utc)
        self.assertIsNone(parse_relative_time_window(
            "compare last week with last month", anchor=anchor
        ))
        self.assertIsNone(parse_relative_time_window(
            "比较过去三天和最近两周", anchor=anchor
        ))


class TestRelativeTimeRerank(unittest.TestCase):
    def _db(self, enabled: bool) -> RetrieverDB:
        cfg = RetrieverConfig(db_path=":memory:", relative_time_weight=30.0).with_flags(
            views=False,
            rrf=False,
            dedup=False,
            supersession=False,
            relative_time=enabled,
        )
        return RetrieverDB(cfg)

    def test_window_is_soft_boost_and_never_filters_outside_evidence(self):
        db = self._db(True)
        anchor = datetime(2025, 11, 19, 12, tzinfo=timezone.utc)
        try:
            db.add(request_id="relative", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "猎户座进展纪要：接口联调完成。",
                 "timestamp": int((anchor - timedelta(days=7)).timestamp() * 1000)},
                {"role": "user", "content": "猎户座进展纪要：仅整理格式。",
                 "timestamp": int((anchor - timedelta(days=1)).timestamp() * 1000)},
            ])
            result = db.search(user_id="u1", query="猎户座上周的进展是什么？", top_k=10,
                               reference_time=anchor)
            messages = [r for r in result.results if r.view == "message"]
            self.assertEqual(len(messages), 2)
            self.assertIn("接口联调", messages[0].content)
            self.assertIn("relative_time_window", messages[0].evidence_flags)
            self.assertNotIn("relative_time_window", messages[1].evidence_flags)
        finally:
            db.close()

    def test_reference_time_must_be_aware(self):
        db = self._db(True)
        try:
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                db.search(user_id="u1", query="上周", reference_time=datetime(2025, 1, 1))
        finally:
            db.close()

    def test_default_off_and_public_aml_service_shape_unchanged(self):
        self.assertFalse(DEFAULT_FLAGS["relative_time"])
        self.assertNotIn("reference_time", inspect.signature(MemoryService.search).parameters)
        self.assertIn("reference_time", inspect.signature(RetrieverDB.search).parameters)


class TestRelativeTimeEvaluation(unittest.TestCase):
    def test_suite_is_independent_and_has_fixed_as_of(self):
        dataset = make_dataset(seed=20260806, scale="smoke", difficulty="mixed",
                               suite="relative_time")
        self.assertTrue(dataset.queries)
        self.assertEqual({q.kind for q in dataset.queries}, {"relative_time"})
        self.assertEqual({q.reference_time for q in dataset.queries}, {RELATIVE_TIME_AS_OF})

    def test_l11_reports_pre_rerank_candidate_ceiling(self):
        dataset = make_dataset(seed=20260806, scale="smoke", difficulty="mixed",
                               suite="relative_time")
        flags = dict(next(flags for name, flags in ABLATION_LADDER
                          if name == "L11_relative_time_ctrl"))
        with tempfile.TemporaryDirectory(prefix="relative-eval-") as workdir:
            result = run_stage("L11_relative_time_ctrl", flags, dataset,
                               workdir=workdir, top_k=100)
        self.assertIn("candidate_recall@max", result.overall)
        self.assertGreater(result.overall["candidate_recall@max"], 0.0)


if __name__ == "__main__":
    unittest.main()
