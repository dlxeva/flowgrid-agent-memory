"""Public extractor conformance harness tests."""
from __future__ import annotations

import json
import unittest

import flowgrid_memory
from flowgrid_memory import (
    CONFORMANCE_REPORT_SCHEMA,
    EvidenceSpan,
    ExtractorIdentity,
    ProposalDraft,
    run_extractor_conformance,
)


IDENTITY = ExtractorIdentity(
    name="tests.conformant",
    version="1",
    implementation="deterministic-test",
    deterministic=True,
)


def conformant(request):
    source = request.raw_events[0]
    if "fictional example" in source.content:
        return []
    return [
        ProposalDraft(
            memory_key="test.preference",
            memory_type="preference",
            subject="$user",
            content=source.content,
            evidence_spans=(
                EvidenceSpan(source.id, 0, len(source.content), source.content),
            ),
        )
    ]


class TestConformanceHarness(unittest.TestCase):
    def test_identity_and_callable_pass_security_and_optional_quality(self):
        report = run_extractor_conformance(
            IDENTITY,
            conformant,
            include_behavioral_quality=True,
        )
        self.assertTrue(report.passed)
        self.assertTrue(all(case.status == "pass" for case in report.security_contract))
        self.assertEqual(
            [case.id for case in report.behavioral_quality],
            ["behavioral.negative_text"],
        )
        self.assertEqual(report.to_dict()["schema"], CONFORMANCE_REPORT_SCHEMA)

    def test_memory_extractor_protocol_object_is_accepted(self):
        class Extractor:
            identity = IDENTITY

            def extract(self, request):
                return conformant(request)

        report = run_extractor_conformance(Extractor())
        cases = {case.id: case for case in report.security_contract}
        self.assertEqual(cases["extractor.interface"].code, "INTERFACE_ACCEPTED")
        self.assertEqual(cases["extractor.output_contract"].code, "OUTPUT_ACCEPTED")
        self.assertTrue(report.passed)

    def test_abstaining_extractor_is_security_conformant(self):
        report = run_extractor_conformance(IDENTITY, lambda _request: [])
        cases = {case.id: case for case in report.security_contract}
        self.assertEqual(cases["extractor.output_contract"].status, "pass")
        self.assertEqual(cases["extractor.output_contract"].code, "OUTPUT_ABSTAINED")
        self.assertTrue(report.passed)

    def test_invalid_extractor_output_fails_without_echoing_payload(self):
        secret = "DO-NOT-ECHO-CONFORMANCE-SECRET"

        def invalid(_request):
            return [{"status": secret}]

        report = run_extractor_conformance(IDENTITY, invalid)
        output = report.to_json()
        self.assertFalse(report.passed)
        self.assertNotIn(secret, output)
        self.assertNotIn("status must", output)
        cases = {case.id: case for case in report.security_contract}
        self.assertEqual(cases["extractor.output_contract"].code, "OUTPUT_REJECTED")

    def test_report_json_has_only_stable_case_fields(self):
        first = run_extractor_conformance(IDENTITY, conformant).to_json()
        second = run_extractor_conformance(IDENTITY, conformant).to_json()
        self.assertEqual(first, second)
        parsed = json.loads(first)
        self.assertEqual(parsed["schema"], "flowgrid.extractor-conformance/v1")
        self.assertEqual(parsed["behavioral_quality"], [])
        for group in ("security_contract", "behavioral_quality"):
            for case in parsed[group]:
                self.assertEqual(set(case), {"id", "status", "code"})

    def test_invalid_interface_is_reported_without_running_output(self):
        report = run_extractor_conformance(object())
        cases = {case.id: case for case in report.security_contract}
        self.assertFalse(report.passed)
        self.assertEqual(cases["extractor.interface"].code, "INTERFACE_REJECTED")
        self.assertEqual(cases["extractor.output_contract"].code, "OUTPUT_NOT_RUN")

    def test_behavioral_profile_is_separate_from_security_contract(self):
        report = run_extractor_conformance(
            IDENTITY,
            conformant,
            include_behavioral_quality=False,
        )
        self.assertEqual(report.behavioral_quality, ())
        self.assertIsNone(report.behavioral_quality_passed)
        self.assertIn(
            "run_extractor_conformance",
            flowgrid_memory.__all__,
        )

    def test_optional_behavioral_failure_does_not_change_security_verdict(self):
        def always_proposes(request):
            source = request.raw_events[0]
            return [
                ProposalDraft(
                    memory_key="test.preference",
                    memory_type="preference",
                    subject="$user",
                    content=source.content,
                    evidence_spans=(EvidenceSpan(
                        source.id, 0, len(source.content), source.content
                    ),),
                )
            ]

        report = run_extractor_conformance(
            IDENTITY,
            always_proposes,
            include_behavioral_quality=True,
        )
        self.assertTrue(report.passed)
        self.assertFalse(report.behavioral_quality_passed)

    def test_source_identity_bounds_and_quote_are_independent_cases(self):
        report = run_extractor_conformance(IDENTITY, conformant)
        cases = {case.id: case for case in report.security_contract}
        for case_id in (
            "core.source_span_identity",
            "core.source_span_bounds",
            "core.source_span_quote",
        ):
            self.assertEqual(cases[case_id].status, "pass")


if __name__ == "__main__":
    unittest.main()
