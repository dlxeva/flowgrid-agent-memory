import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "rehearse_aml_cycle2.py"
SPEC = importlib.util.spec_from_file_location("rehearse_aml_cycle2", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TestCycle2Rehearsal(unittest.TestCase):
    def test_small_rehearsal_passes_all_local_gates(self):
        report = MODULE.run_rehearsal(
            users=3,
            checkpoints=2,
            add_concurrency=4,
            search_concurrency=4,
            search_rounds=2,
        )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["gates"].values()))
        self.assertEqual(report["operations"]["unique_writes"], 6)
        self.assertEqual(report["operations"]["add_http_requests"], 12)
        self.assertGreater(report["latency"]["add"]["requests"], 0)
        self.assertGreater(report["latency"]["search"]["requests"], 0)

    def test_report_is_aggregate_only(self):
        report = MODULE.run_rehearsal(
            users=2,
            checkpoints=1,
            add_concurrency=2,
            search_concurrency=2,
            search_rounds=1,
        )
        serialized = json.dumps(report, sort_keys=True)
        for forbidden in (
            "content",
            "request_id",
            "session_id",
            "api_key",
            "base_url",
            "db_path",
            "cycle2user",
            "rehearsal-user",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIs(report["official_aml_result"], False)
        self.assertIs(report["public_https_validated"], False)
        self.assertIs(report["synthetic_data_only"], True)

    def test_invalid_configuration_fails_before_starting_server(self):
        with self.assertRaises(ValueError):
            MODULE.run_rehearsal(users=0)
        with self.assertRaises(ValueError):
            MODULE.run_rehearsal(search_concurrency=True)
        with self.assertRaises(ValueError):
            MODULE.run_rehearsal(hosted_max_inflight=0)

    def test_small_hosted_rehearsal_distinguishes_attempts_from_deliveries(self):
        original = MODULE.HostedAMLServer

        class SlowSyntheticServer(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                add = self.service.official_add

                def slightly_slow_add(payload):
                    MODULE.time.sleep(0.04)
                    return add(payload)

                self.service.official_add = slightly_slow_add

        with mock.patch.object(MODULE, "HostedAMLServer", SlowSyntheticServer):
            report = MODULE.run_rehearsal(
                users=3, checkpoints=1, add_concurrency=6, search_concurrency=2,
                search_rounds=1, hosted=True, hosted_max_inflight=2,
            )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["transport"], "dedicated_hosted_loopback")
        add = report["http_attempts"]["routes"]["add"]
        self.assertEqual(add["logical_requests"], 6)
        self.assertEqual(add["successful_200_attempts"], 6)
        self.assertGreater(add["throttled_429_attempts"], 0)
        self.assertEqual(add["actual_attempts"], 6 + add["throttled_429_attempts"])
        self.assertEqual(report["operations"]["add_http_requests"], add["actual_attempts"])
        self.assertEqual(report["operations"]["logical_add_deliveries"], 6)
        self.assertEqual(report["http_attempts"]["retry_budget_exhaustions"], 0)
        self.assertTrue(report["temporary_database_deleted_after_run"])
        self.assertFalse(report["public_https_validated"])
        serialized = json.dumps(report)
        for forbidden in ("Bearer", "credential", "content", "request_id", "session_id", "base_url", "db_path", "cycle2user", "rehearsal-user"):
            self.assertNotIn(forbidden, serialized)

    def test_429_retry_honors_header_and_retains_exact_payload(self):
        metrics = MODULE._AttemptMetrics()
        payload = {"synthetic": True}
        with mock.patch.object(MODULE, "_post_attempt", side_effect=[
            (429, {"detail": {"reason": "capacity limit reached"}}, "1", 2.0),
            (200, {"success": True}, None, 3.0),
        ]) as attempt, mock.patch.object(MODULE.time, "sleep") as sleep:
            status, _, _ = MODULE._post_with_retry("http://127.0.0.1", "/add", payload, credential="synthetic", metrics=metrics)
        self.assertEqual(status, 200)
        sleep.assert_called_once_with(1)
        self.assertEqual(attempt.call_count, 2)
        self.assertTrue(all(call.args[2] is payload for call in attempt.call_args_list))
        self.assertEqual(metrics.summary()["routes"]["add"]["actual_attempts"], 2)

    def test_retry_attempt_and_wait_budgets_are_finite(self):
        metrics = MODULE._AttemptMetrics()
        with mock.patch.object(MODULE, "_post_attempt", return_value=(429, {}, "0", 1.0)) as attempt, mock.patch.object(MODULE.time, "sleep"):
            status, _, _ = MODULE._post_with_retry("http://127.0.0.1", "/search", {}, credential="synthetic", metrics=metrics, max_attempts=2)
        self.assertEqual(status, 429)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(metrics.summary()["retry_budget_exhaustions"], 1)
        for header in (None, "invalid", "999", "-1"):
            metrics = MODULE._AttemptMetrics()
            with mock.patch.object(MODULE, "_post_attempt", return_value=(429, {}, header, 1.0)) as attempt, mock.patch.object(MODULE.time, "sleep") as sleep:
                MODULE._post_with_retry("http://127.0.0.1", "/add", {}, credential="synthetic", metrics=metrics)
            self.assertEqual(attempt.call_count, 1)
            sleep.assert_not_called()
        metrics = MODULE._AttemptMetrics()
        with mock.patch.object(MODULE, "_post_attempt", return_value=(429, {}, "5", 1.0)), mock.patch.object(MODULE.time, "sleep") as sleep:
            MODULE._post_with_retry("http://127.0.0.1", "/add", {}, credential="synthetic", metrics=metrics, total_deadline_seconds=0.01)
        sleep.assert_not_called()
        self.assertEqual(metrics.summary()["retry_budget_exhaustions"], 1)


if __name__ == "__main__":
    unittest.main()
