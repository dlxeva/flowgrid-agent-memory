"""Direct supported relative-count grammar checks; candidate defaults stay off."""
from datetime import datetime,timedelta,timezone
import unittest
from aml_retriever import features
from aml_retriever.config import DEFAULT_FLAGS


class RelativeCountFixedTests(unittest.TestCase):
    def parse(self,text):return features.parse_relative_time_window(text,anchor=datetime(2026,10,13,12,tzinfo=timezone.utc))

    def test_malformed_chinese_counts_rejected(self):
        for count in ('一二','十十','一十十','百十十','百百','两两','十百'):
            with self.subTest(count=count):self.assertIsNone(self.parse('过去'+count+'天'))

    def test_supported_count_durations(self):
        for count,days in (('一',1),('两',2),('十',10),('十一',11),('二十',20),('二十一',21),
                           ('百',100),('一百',100),('一百一',101),('一百一十',110),('一百二十',120),('1',1),('120',120)):
            window=self.parse('过去'+count+'天');self.assertIsNotNone(window)
            self.assertEqual(window.end-window.start,timedelta(days=days))
            self.assertTrue(window.contains(window.start));self.assertFalse(window.contains(window.end))

    def test_calendar_english_ambiguity_and_bounds(self):
        week=self.parse('上周');english=self.parse('last week')
        self.assertEqual((week.start,week.end),(english.start,english.end))
        self.assertEqual(week.start,datetime(2026,10,5,tzinfo=timezone.utc))
        self.assertEqual(week.end,datetime(2026,10,12,tzinfo=timezone.utc))
        self.assertEqual(self.parse('past 10 days').end-self.parse('past 10 days').start,timedelta(days=10))
        for text in ('上周和上个月','过去0天','过去121天'):self.assertIsNone(self.parse(text))

    def test_candidate_defaults_remain_off(self):
        for key in ('relative_time','temporal_intent','preference_role_boost'):self.assertIs(DEFAULT_FLAGS[key],False)
