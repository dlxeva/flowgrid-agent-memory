"""Installed corrected candidate: direct properties, no function overlay."""
import unittest
from flowgrid_memory import ConformanceReport, ConformanceCase, run_extractor_conformance
from aml_retriever.extraction import ExtractorIdentity

class FixedConformanceCompletenessTests(unittest.TestCase):
    def complete(self):
        return run_extractor_conformance(
            ExtractorIdentity('synthetic-complete','1','synthetic-fixture'),lambda request: [])

    def test_empty_and_partial_cannot_pass(self):
        self.assertFalse(ConformanceReport(()).passed)
        self.assertFalse(ConformanceReport((ConformanceCase(
            'extractor.interface','pass','INTERFACE_ACCEPTED'),)).passed)

    def test_actual_complete_contract_passes(self):
        report=self.complete()
        self.assertEqual(len(report.security_contract),12)
        self.assertTrue(report.passed)
        self.assertIsNone(report.behavioral_quality_passed)

    def test_duplicate_unknown_or_untyped_cases_cannot_fill_gaps(self):
        cases=self.complete().security_contract
        self.assertFalse(ConformanceReport((cases[0],)+cases[:-1]).passed)
        self.assertFalse(ConformanceReport(cases[:-1]+(ConformanceCase('unknown','pass','synthetic'),)).passed)
        self.assertFalse(ConformanceReport(cases[:-1]+(object(),)).passed)

    def test_failures_remain_failures_and_quality_is_separate(self):
        cases=self.complete().security_contract
        self.assertFalse(ConformanceReport((ConformanceCase(cases[0].id,'fail','synthetic'),)+cases[1:]).passed)
        report=ConformanceReport(cases,(ConformanceCase('behavioral.negative_text','fail','synthetic'),))
        self.assertTrue(report.passed)
        self.assertFalse(report.behavioral_quality_passed)
