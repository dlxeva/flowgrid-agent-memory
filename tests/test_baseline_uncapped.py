"""Optional ceilings on the unchanged serial 2978c5d serving baseline."""
import concurrent.futures
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget, BudgetStopped
from tests import test_aml_semantic_host as host_tests


class UncappedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = BudgetPolicy('baseline-test', 'a'*64, 'b'*64, None, None)

    def ledger(self, **kwargs):
        return ModelBudget(self.root/'budget.sqlite', replace(self.base, **kwargs), enabled=True)

    def reserve(self, ledger):
        return ledger.reserve('c'*64, self.base.model, 16384, 768)

    def settled(self, ledger):
        token = self.reserve(ledger)
        ledger.settle(token, {'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}, 'd'*64)
        return token

    def test_both_none_allows_multiple_settled_calls_and_keeps_accounting(self):
        ledger = self.ledger()
        for _ in range(5): self.settled(ledger)
        self.assertEqual(ledger.snapshot(), {'attempts':5,'charged_or_held_microusd':85,
            'unresolved':0,'budget_microusd':None,'halted':False})
        self.assertEqual(ModelBudget(ledger.path, self.base, enabled=True).snapshot(), ledger.snapshot())

    def test_no_fee_cap_retains_attempt_cap(self):
        ledger = self.ledger(max_attempts=2)
        self.settled(ledger); self.settled(ledger)
        with self.assertRaisesRegex(BudgetStopped, 'budget_limit'): self.reserve(ledger)

    def test_no_attempt_cap_retains_fee_cap(self):
        ledger = self.ledger(budget_microusd=self.base.hold+17)
        self.settled(ledger); self.settled(ledger)
        with self.assertRaisesRegex(BudgetStopped, 'budget_limit'): self.reserve(ledger)

    def test_finite_binding_is_byte_identical_to_original_format(self):
        policy = replace(self.base, budget_microusd=20000, max_attempts=2)
        original = {'scope':'baseline-test','implementation_sha256':'a'*64,
            'profile_sha256':'b'*64,'budget_microusd':20000,'max_attempts':2,
            'input_token_limit':32768,'output_token_limit':768,'input_bytes_limit':16384,
            'model':'gpt-4o-mini-2024-07-18'}
        self.assertEqual(policy.binding, json.dumps(original, sort_keys=True, separators=(',', ':')))

    def test_serial_pending_guard_is_unchanged(self):
        ledger = self.ledger()
        def attempt(_):
            try: return self.reserve(ledger)
            except BudgetStopped: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(attempt, range(16)))
        self.assertEqual(sum(t is not None for t in tokens), 1)
        self.assertEqual(ledger.snapshot()['unresolved'], 1)

    def test_unknown_hold_and_halt_survive_restart(self):
        ledger = self.ledger(); token = self.reserve(ledger); ledger.unknown(token); ledger.halt()
        restarted = ModelBudget(ledger.path, self.base, enabled=True)
        state = restarted.snapshot()
        self.assertEqual(state['charged_or_held_microusd'], self.base.hold)
        self.assertEqual(state['unresolved'], 1); self.assertTrue(state['halted'])
        with self.assertRaisesRegex(BudgetStopped, 'budget_halted'): self.reserve(restarted)
        with self.assertRaisesRegex(BudgetStopped, 'budget_settlement_state'):
            restarted.settle(token, {'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}, 'd'*64)

    def test_uncapped_still_rejects_bad_receipts_and_binding_changes(self):
        ledger = self.ledger(); token = self.reserve(ledger)
        with self.assertRaisesRegex(BudgetStopped, 'budget_receipt'):
            ledger.settle(token, {'prompt_tokens':1,'completion_tokens':1,'total_tokens':1}, 'd'*64)
        self.assertEqual(ledger.snapshot()['charged_or_held_microusd'], self.base.hold)
        with self.assertRaisesRegex(BudgetStopped, 'budget_binding_drift'):
            ModelBudget(ledger.path, replace(self.base, max_attempts=2), enabled=True)

    def test_caps_reject_booleans_zero_negative_and_nonintegers(self):
        for field in ('budget_microusd','max_attempts'):
            for value in (True, False, 0, -1, 1.0, '1'):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(self.base, **{field:value})

    def test_high_quality_profile_is_not_backported(self):
        with self.assertRaisesRegex(ValueError, 'budget_protocol_drift'):
            replace(self.base, input_token_limit=65536, output_token_limit=4096, input_bytes_limit=65536)
        for field in ('input_token_limit','output_token_limit','input_bytes_limit'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.base, **{field:None})


class UncappedHostTests(unittest.TestCase):
    def make(self):
        h = host_tests.HostTests('runTest'); h.setUp(); self.addCleanup(h.doCleanups)
        h.policy = replace(h.policy, budget_microusd=None, max_attempts=None)
        h.semantic = replace(h.semantic, policy=h.policy)
        h.start()
        return h

    def test_health_and_report_caps_truthful_with_serial_baseline(self):
        h = self.make()
        status, health = h.http('/health')
        self.assertEqual(status, 200)
        self.assertFalse(health['semantic_budget_enforced'])
        self.assertFalse(health['semantic_call_quota_enforced'])
        self.assertEqual(health['semantic_model_concurrency'], 1)
        report = h.semantic.validate(h.config)
        self.assertFalse(report['fee_cap_enabled']); self.assertFalse(report['call_cap_enabled'])
        self.assertEqual((report['input_token_limit'],report['output_token_limit'],report['input_bytes_limit']),
                         (32768,768,16384))
        self.assertEqual(h.http('/add', h.add)[0], 200)
        self.assertEqual(h.http('/search', {'user_id':'u','query':'diet','top_k':100})[0], 200)
        self.assertEqual(h.calls, 2)

    def test_provider_failure_keeps_original_global_stop(self):
        h = self.make(); h.timeout = True
        self.assertGreaterEqual(h.http('/add',h.add)[0], 500)
        self.assertTrue(h.server.model_budget.snapshot()['halted'])
        self.assertGreaterEqual(h.http('/search',{'user_id':'u','query':'diet','top_k':100})[0],500)
        self.assertEqual(h.calls, 1)


if __name__ == '__main__': unittest.main()
