"""Governance evaluation manifest, hard-fail, and claim-boundary tests."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aml_retriever.evaluation import governance_suite as suite


def _pass_operator(_fixture, _runtime):
    return {
        "status": "pass",
        "metrics": {"probe": {"numerator": 1, "denominator": 1, "value": 1.0}},
        "observations": {"synthetic": True},
        "failures": [],
    }


def _all_fast_capability_overrides(except_name=None):
    return {
        name: _pass_operator
        for name in suite.CAPABILITY_OPERATORS
        if name not in {"official_unverified", except_name}
    }


class ManifestCase(unittest.TestCase):
    def setUp(self):
        self.loaded = suite.load_manifest()
        self.manifest = copy.deepcopy(self.loaded.data)
        self.temp = tempfile.TemporaryDirectory(prefix="governance-manifest-test-")

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, value):
        path = Path(self.temp.name) / "manifest.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return path


class TestManifestIntegrity(ManifestCase):
    def test_manifest_hash_is_canonical_and_coverage_is_complete(self):
        self.assertEqual(self.loaded.sha256, suite.canonical_sha256(self.loaded.data))
        self.assertEqual(self.loaded.sha256, suite.CANONICAL_MANIFEST_SHA256)
        self.assertTrue(self.loaded.attested)
        capabilities = set(self.loaded.data["capabilities"])
        self.assertTrue(suite.CORE_CAPABILITIES <= capabilities)
        self.assertTrue(suite.LOCAL_PROXY_CAPABILITIES <= capabilities)
        self.assertTrue(suite.OFFICIAL_CAPABILITIES <= capabilities)

    def test_wrong_schema_fails_closed_in_structured_artifact(self):
        self.manifest["schema"] = "wrong/v0"
        result = suite.run_governance_suite(manifest_path=self.write_manifest(self.manifest))
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(result["failures"][0]["code"], "manifest_invalid")
        self.assertEqual(result["evaluation_mode"], "custom_manifest_non_attestable")
        self.assertTrue(
            any(
                item["code"] == "custom_manifest_non_attestable"
                for item in result["failures"]
            )
        )
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])

    def test_missing_required_capability_is_not_skipped(self):
        self.manifest["capabilities"].pop("D3")
        with self.assertRaises(suite.EvaluationError):
            suite.validate_manifest(self.manifest)

    def test_unknown_surface_reference_is_manifest_failure(self):
        self.manifest["profiles"]["quick"]["required_surfaces"].append("unknown_surface")
        with self.assertRaises(suite.EvaluationError):
            suite.validate_manifest(self.manifest)

    def test_mandatory_capability_cannot_be_rebound_to_another_fixture_or_operator(self):
        for field, value in (
            ("fixture", "proxy_rule_evidence"),
            ("operator", "proxy_rule_evidence"),
        ):
            bad = copy.deepcopy(self.manifest)
            bad["capabilities"]["D3"][field] = value
            with self.subTest(field=field), self.assertRaises(suite.EvaluationError):
                suite.validate_manifest(bad)

    def test_mandatory_surface_contract_cannot_be_rebound(self):
        bad = copy.deepcopy(self.manifest)
        bad["surfaces"]["governed_core"] = {
            "operator": "python_surface",
            "module": "builtins",
            "attributes": ["len"],
        }
        with self.assertRaises(suite.EvaluationError):
            suite.validate_manifest(bad)

    def test_official_gate_cannot_be_manifest_rebound_or_operator_overridden(self):
        bad = copy.deepcopy(self.manifest)
        bad["capabilities"]["official.artifact"]["operator"] = "proxy_rule_evidence"
        bad["capabilities"]["official.artifact"]["fixture"] = "proxy_rule_evidence"
        result = suite.run_governance_suite(manifest_path=self.write_manifest(bad))
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])

        result = suite.run_governance_suite(
            manifest_path=self.write_manifest(self.manifest),
            operator_overrides={"official_unverified": _pass_operator},
        )
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])

    def test_every_fixture_uses_stable_alias(self):
        for fixture in self.loaded.data["fixtures"].values():
            self.assertRegex(fixture["alias"], r"^[a-z0-9][a-z0-9-]*$")

    def test_empty_a2_expected_value_is_custom_and_cannot_go_green(self):
        self.manifest["fixtures"]["proxy_attribution"]["expected"] = ""
        path = self.write_manifest(self.manifest)
        with self.assertRaisesRegex(suite.EvaluationError, "canonical SHA-256"):
            suite.validate_manifest(self.manifest)
        result = suite.run_governance_suite(manifest_path=path)
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(result["evaluation_mode"], "custom_manifest_non_attestable")
        self.assertFalse(result["manifest"]["attested"])
        self.assertEqual(
            result["failures"][0]["code"], "custom_manifest_non_attestable"
        )
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])

    def test_simplified_b2_fixture_is_custom_and_cannot_go_green(self):
        self.manifest["fixtures"]["proxy_three_segment_chain"]["segments"] = [
            "easy shared token",
            "easy shared token",
            "easy shared token",
        ]
        path = self.write_manifest(self.manifest)
        with self.assertRaisesRegex(suite.EvaluationError, "canonical SHA-256"):
            suite.validate_manifest(self.manifest)
        result = suite.run_governance_suite(manifest_path=path)
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(result["evaluation_mode"], "custom_manifest_non_attestable")
        self.assertEqual(
            result["claim_boundary"]["local_evidence"],
            "custom_manifest_non_attestable",
        )
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])


class TestOperatorHardFailures(ManifestCase):
    def _run_with_operator(self, operator):
        return suite.run_governance_suite(
            manifest_path=self.write_manifest(self.manifest),
            operator_overrides={
                **_all_fast_capability_overrides(except_name="raw_immutable"),
                "raw_immutable": operator,
            },
            surface_operator_overrides={"python_surface": _pass_operator},
        )

    def test_unknown_operator_is_hard_failure(self):
        with mock.patch.dict(suite.SURFACE_OPERATORS, {}, clear=True):
            result = suite.run_governance_suite(
                manifest_path=self.write_manifest(self.manifest),
                operator_overrides=_all_fast_capability_overrides(),
            )
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(
            result["verdict"]["surfaces"]["governed_core"]["status"], "fail"
        )
        self.assertTrue(any(item["code"] == "unknown_operator" for item in result["failures"]))

    def test_g5_bind_failure_cleans_database_wal_and_shm(self):
        loaded = suite.load_manifest()
        fixture = loaded.data["fixtures"]["proxy_wire_contract"]
        runtime = suite.EvaluationRuntime(
            repo_root=suite.REPO_ROOT,
            manifest_path=suite.DEFAULT_MANIFEST_PATH,
        )
        created_paths = []

        def fail_bind(config, *args, **kwargs):
            del args, kwargs
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(config.db_path) + suffix)
                if suffix:
                    path.write_text("constructor residue", encoding="utf-8")
                created_paths.append(path)
            raise PermissionError("simulated loopback bind denial")

        try:
            with mock.patch(
                "aml_retriever.server.RetrieverServer", side_effect=fail_bind
            ):
                with self.assertRaisesRegex(PermissionError, "bind denial"):
                    suite.CAPABILITY_OPERATORS["proxy_wire_contract"](
                        fixture, runtime
                    )
            self.assertEqual(len(created_paths), 3)
            self.assertTrue(all(not path.exists() for path in created_paths))
        finally:
            for path in created_paths:
                path.unlink(missing_ok=True)

    def test_operator_exception_is_hard_failure(self):
        def explode(_fixture, _runtime):
            raise RuntimeError("secret temp path must not escape")

        result = self._run_with_operator(explode)
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertTrue(any(item["code"] == "operator_exception" for item in result["failures"]))

    def test_wrong_operator_result_schema_is_hard_failure(self):
        result = self._run_with_operator(lambda _fixture, _runtime: {})
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertTrue(
            any(item["code"] == "operator_result_schema" for item in result["failures"])
        )

    def test_empty_metric_denominator_is_hard_failure(self):
        def empty_denominator(_fixture, _runtime):
            return {
                "status": "pass",
                "metrics": {"empty": {"numerator": 0, "denominator": 0, "value": 0.0}},
                "observations": {},
                "failures": [],
            }

        result = self._run_with_operator(empty_denominator)
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertTrue(
            any(item["code"] == "operator_result_schema" for item in result["failures"])
        )

    def test_test_overrides_can_never_emit_an_attestable_pass_artifact(self):
        result = suite.run_governance_suite(
            manifest_path=self.write_manifest(self.manifest),
            operator_overrides=_all_fast_capability_overrides(),
            surface_operator_overrides={"python_surface": _pass_operator},
        )
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(result["evaluation_mode"], "test_override_non_attestable")
        self.assertEqual(
            result["claim_boundary"]["local_evidence"],
            "test_override_non_attestable",
        )
        self.assertTrue(
            any(item["code"] == "test_override_active" for item in result["failures"])
        )


class TestBaselineProvenance(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads(
            suite.DEFAULT_BASELINE_PATH.read_text(encoding="utf-8")
        )

    def test_frozen_baseline_is_bound_to_exact_clean_commit_and_dataset(self):
        verified = suite.verify_baseline_provenance(self.baseline)
        self.assertEqual(
            verified["commit"], "cdae7dbd38d73eda33793b30017559bdfb75eff5"
        )
        self.assertTrue(verified["clean_snapshot"])
        self.assertEqual(
            self.baseline["dataset"]["canonical_dump_sha256"],
            "245752a7f0f076207de85ef17b5c6af9fb07937893b9c073968c55876f523098",
        )

    def test_unverified_or_dirty_baseline_metadata_fails_closed(self):
        for field, value in (("verified", False),):
            bad = copy.deepcopy(self.baseline)
            bad[field] = value
            with self.assertRaises(suite.EvaluationError):
                suite.verify_baseline_provenance(bad)
        bad = copy.deepcopy(self.baseline)
        bad["source"]["clean_snapshot"] = False
        with self.assertRaises(suite.EvaluationError):
            suite.verify_baseline_provenance(bad)

    def test_another_local_commit_cannot_replace_the_approved_baseline(self):
        bad = copy.deepcopy(self.baseline)
        bad["source"]["commit"] = "641fce077669845d87097b00d3cbfc347e8ed16d"
        bad["source"]["tree"] = "0" * 40
        with self.assertRaises(suite.EvaluationError):
            suite.verify_baseline_provenance(bad)

    def test_dirty_worktree_cannot_be_used_to_freeze_baseline(self):
        expected = self.baseline["source"]["commit"]
        with mock.patch.object(
            suite,
            "code_state",
            return_value={"revision": expected, "tree": "x" * 40, "dirty": True},
        ):
            with self.assertRaisesRegex(suite.EvaluationError, "clean worktree"):
                suite.assert_clean_baseline_source(expected_commit=expected)

    def test_different_commit_cannot_be_used_to_freeze_baseline(self):
        expected = self.baseline["source"]["commit"]
        with mock.patch.object(
            suite,
            "code_state",
            return_value={"revision": "0" * 40, "tree": "x" * 40, "dirty": False},
        ):
            with self.assertRaisesRegex(suite.EvaluationError, "exact declared commit"):
                suite.assert_clean_baseline_source(expected_commit=expected)


class TestGovernanceSuiteEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.quick = suite.run_governance_suite(profile="quick")

    def test_quick_profile_passes_local_layers_without_opening_official_claim(self):
        self.assertEqual(self.quick["verdict"]["profile"], "pass")
        self.assertEqual(self.quick["verdict"]["layers"]["core_invariant"]["status"], "pass")
        self.assertEqual(self.quick["verdict"]["layers"]["local_e2e_proxy"]["status"], "pass")
        self.assertEqual(
            self.quick["verdict"]["layers"]["official_aml"]["status"], "unverified"
        )
        self.assertFalse(self.quick["claim_boundary"]["official_claim_allowed"])
        self.assertEqual(self.quick["failures"], [])

    def test_result_has_required_identity_verdict_metric_and_boundary_fields(self):
        for key in (
            "schema",
            "suite",
            "manifest",
            "code",
            "verdict",
            "metrics",
            "baseline_comparison",
            "failures",
            "claim_boundary",
        ):
            self.assertIn(key, self.quick)
        self.assertRegex(self.quick["manifest"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("revision", self.quick["code"])
        self.assertIn("dirty", self.quick["code"])

    def test_all_required_capabilities_pass_and_none_are_skipped(self):
        capabilities = self.quick["verdict"]["capabilities"]
        for capability in suite.CORE_CAPABILITIES | suite.LOCAL_PROXY_CAPABILITIES:
            self.assertEqual(capabilities[capability]["status"], "pass", capability)
        self.assertNotIn("skip", json.dumps(self.quick).lower())

    def test_legacy_floor_and_proxy_claim_boundaries_are_explicit(self):
        self.assertEqual(self.quick["baseline_comparison"]["status"], "pass")
        self.assertTrue(
            all(item["passed"] for item in self.quick["baseline_comparison"]["comparisons"])
        )
        b2 = self.quick["verdict"]["capabilities"]["proxy.B2"]["observations"]
        g1 = self.quick["verdict"]["capabilities"]["proxy.G1"]["observations"]
        self.assertEqual(b2["chain_segments"], 3)
        self.assertIn("not_answer_reasoning", b2["proxy_claim"])
        self.assertIn("only_not_domain_reasoning", g1["proxy_claim"])

    def test_product_profile_missing_rest_and_mcp_is_a_surface_failure_only(self):
        manifest = copy.deepcopy(suite.load_manifest().data)

        def simulated_surface_probe(specification, _runtime):
            missing = specification["module"] in {
                "aml_retriever.rest_v1",
                "aml_retriever.mcp_adapter",
            }
            return {
                "status": "fail" if missing else "pass",
                "metrics": {
                    "surface_available": {
                        "numerator": 0 if missing else 1,
                        "denominator": 1,
                        "value": 0.0 if missing else 1.0,
                    }
                },
                "observations": {"simulated_missing": missing},
                "failures": (
                    [{"code": "surface_unavailable", "message": "surface unavailable"}]
                    if missing
                    else []
                ),
            }

        with tempfile.TemporaryDirectory(prefix="governance-product-test-") as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = suite.run_governance_suite(
                profile="product",
                manifest_path=path,
                operator_overrides=_all_fast_capability_overrides(),
                surface_operator_overrides={"python_surface": simulated_surface_probe},
            )
        self.assertEqual(result["verdict"]["profile"], "fail")
        self.assertEqual(result["verdict"]["surfaces"]["governed_rest"]["status"], "fail")
        self.assertEqual(result["verdict"]["surfaces"]["mcp"]["status"], "fail")
        self.assertEqual(result["verdict"]["layers"]["core_invariant"]["status"], "pass")
        self.assertEqual(result["verdict"]["layers"]["local_e2e_proxy"]["status"], "pass")
        self.assertFalse(result["claim_boundary"]["official_claim_allowed"])

    def test_dynamic_values_are_normalized_to_stable_aliases(self):
        raw = {
            "id": "mem_0123456789abcdef0123456789abcdef",
            "event": "raw_abcdef0123456789abcdef01",
            "time": "2026-08-24T12:34:56.123+00:00",
            "path": "/private/tmp/governance-eval-random/example.db",
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "darwin_temp": "/private/var/folders/hz/random/T/eval/example.db",
        }
        normalized = suite.normalize_dynamic(raw)
        rendered = suite.canonical_json(normalized)
        self.assertNotIn("0123456789abcdef", rendered)
        self.assertNotIn("2026-08-24", rendered)
        self.assertNotIn("/private/tmp", rendered)
        self.assertNotIn("/private/var/folders", rendered)
        self.assertNotIn("123e4567-e89b", rendered)
        self.assertIn("<mem-alias-001>", rendered)
        self.assertIn("<raw-alias-001>", rendered)

    def test_write_result_is_valid_json_roundtrip(self):
        descriptor, path = tempfile.mkstemp(prefix="governance-result-", suffix=".json")
        os.close(descriptor)
        try:
            suite.write_result(path, self.quick)
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.quick)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
