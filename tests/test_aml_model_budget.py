"""Offline finite-budget behavior; no credentials or model API calls."""
import concurrent.futures
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

# Load this new standalone stdlib module without relying on the dirty root's
# package initialization or an older installed package containing it already.
spec = importlib.util.spec_from_file_location('cue_budget_under_test', Path(__file__).resolve().parents[1] / 'aml_retriever/aml_model_budget.py')
budget = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = budget
spec.loader.exec_module(budget)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name).resolve()
        self.path = self.private / 'budget.sqlite'
        self.policy = budget.BudgetPolicy('offline-test', 'a'*64, 'b'*64, 20000, 2)
        self.ledger = budget.ModelBudget(self.path, self.policy, enabled=True)

    def reserve(self, ledger=None):
        return (ledger or self.ledger).reserve('c'*64, budget.MODEL, 16384, 768)

    def settle(self, token):
        self.ledger.settle(token, {'prompt_tokens': 30, 'completion_tokens': 20, 'total_tokens': 50}, 'd'*64)

    def test_default_disabled(self):
        with self.assertRaises(budget.BudgetStopped):
            budget.ModelBudget(self.path, self.policy)

    def test_attempt_cap_persists_and_identical_settlement_is_idempotent(self):
        for _ in range(2):
            token = self.reserve(); self.settle(token); self.settle(token)
        restarted = budget.ModelBudget(self.path, self.policy, enabled=True)
        with self.assertRaisesRegex(budget.BudgetStopped, 'limit'): self.reserve(restarted)
        self.assertEqual(restarted.snapshot()['charged_or_held_microusd'], 34)

    def test_unknown_retains_hold_across_restart(self):
        token = self.reserve(); self.ledger.unknown(token); self.ledger.unknown(token)
        restarted = budget.ModelBudget(self.path, self.policy, enabled=True)
        self.assertEqual(restarted.snapshot()['charged_or_held_microusd'], 5376)
        with self.assertRaisesRegex(budget.BudgetStopped, 'unresolved'): self.reserve(restarted)
        with self.assertRaises(budget.BudgetStopped): self.settle(token)

    def test_crash_reserved_also_blocks_restart(self):
        self.reserve()
        restarted = budget.ModelBudget(self.path, self.policy, enabled=True)
        with self.assertRaisesRegex(budget.BudgetStopped, 'unresolved'): self.reserve(restarted)

    def test_total_cap_reserves_worst_case(self):
        other = budget.ModelBudget(self.private/'small.sqlite', replace(self.policy, budget_microusd=5375), enabled=True)
        with self.assertRaisesRegex(budget.BudgetStopped, 'limit'): self.reserve(other)
        self.assertEqual(other.snapshot()['attempts'], 0)

    def test_configuration_cannot_reopen_or_increase_existing_scope(self):
        for policy in (replace(self.policy, max_attempts=10), replace(self.policy, scope='new'),
                       replace(self.policy, implementation_sha256='e'*64), replace(self.policy, profile_sha256='f'*64)):
            with self.assertRaisesRegex(budget.BudgetStopped, 'binding'):
                budget.ModelBudget(self.path, policy, enabled=True)

    def test_concurrent_reservations_allow_only_one_pending(self):
        def attempt(_):
            try: return self.reserve()
            except budget.BudgetStopped: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(16)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.ledger.snapshot()['attempts'], 1)

    def test_bad_usage_never_releases_hold(self):
        token = self.reserve()
        for usage in ({'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2},
                      {'prompt_tokens': 32769, 'completion_tokens': 0, 'total_tokens': 32769},
                      {'prompt_tokens': 2, 'completion_tokens': 769, 'total_tokens': 771},
                      {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 2}):
            with self.assertRaises(budget.BudgetStopped): self.ledger.settle(token, usage, 'd'*64)
        self.assertEqual(self.ledger.snapshot()['charged_or_held_microusd'], 5376)

    def test_late_failure_does_not_erase_known_charge(self):
        token = self.reserve(); self.settle(token); self.ledger.unknown(token)
        self.assertEqual(self.ledger.snapshot()['unresolved'], 0)
        with self.assertRaises(budget.BudgetStopped):
            self.ledger.settle(token, {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}, 'e'*64)

    def test_halt_persists_without_erasing_settled_usage(self):
        token=self.reserve(); self.settle(token); self.ledger.halt()
        restarted=budget.ModelBudget(self.path,self.policy,enabled=True)
        self.assertTrue(restarted.snapshot()['halted'])
        self.assertEqual(restarted.snapshot()['charged_or_held_microusd'],17)
        with self.assertRaisesRegex(budget.BudgetStopped,'halted'): self.reserve(restarted)

    def test_private_permissions_and_symlink(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.private/'link.sqlite'; link.symlink_to(self.path)
        with self.assertRaises(OSError): budget.ModelBudget(link, self.policy, enabled=True)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): budget.ModelBudget(self.path, self.policy, enabled=True)


if __name__ == '__main__': unittest.main()
