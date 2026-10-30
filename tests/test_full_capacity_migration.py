"""No live config, systemd or provider access; temporary synthetic ledgers."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget, MODEL
from scripts import prepare_full_capacity_ledger as op

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.old=self.root/'old.sqlite';self.target=self.root/'full.sqlite'
        policy=BudgetPolicy('old','a'*64,'b'*64,20000,2)
        self.ledger=ModelBudget(self.old,policy,enabled=True)
        token=self.ledger.reserve('c'*64,MODEL,16384,768)
        self.ledger.settle(token,{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50},'d'*64)
        self.config=self.root/'service.json'
        self.data={'hosted':{'db_path':str(self.root/'source.sqlite'),
            'max_inflight':16,'max_connections':32,'operation_timeout_seconds':270,
            'request_timeout_seconds':300},'api_key_file':str(self.root/'api.key'),
            'semantic':{'enabled':True,'model_key_file':str(self.root/'model.key'),
            'cue_db_path':str(self.root/'cue.sqlite'),'budget_db_path':str(self.old)},
            'policy':json.loads(policy.binding)}
        self.config.write_text(json.dumps(self.data))
    def argv(self):
        return ['op','--config',str(self.config),'--target-ledger',str(self.target),
                '--new-runtime-sha256','e'*64]
    def test_check_is_read_only_and_never_queries_systemd(self):
        before=self.old.read_bytes()
        with patch('sys.argv',self.argv()),patch.object(op.subprocess,'check_output') as call,contextlib.redirect_stdout(io.StringIO()):
            op.main()
        call.assert_not_called();self.assertFalse(self.target.exists())
        self.assertEqual(self.old.read_bytes(),before)
    def test_prepare_copies_all_receipts_and_preserves_original_bytes(self):
        before=self.old.read_bytes();cfg=self.config.read_bytes();candidate=self.root/'candidate.json'
        with patch('sys.argv',self.argv()+['--prepare','--candidate-config',str(candidate)]),patch.object(
                op.subprocess,'check_output',return_value='MainPID=0\nActiveState=inactive\n'),contextlib.redirect_stdout(io.StringIO()):
            op.main()
        self.assertEqual(self.old.read_bytes(),before);self.assertEqual(self.config.read_bytes(),cfg)
        self.assertEqual(op.inspect(self.old)[1],op.inspect(self.target)[1])
        new=json.loads(candidate.read_text());self.assertIsNone(new['policy']['budget_microusd'])
        self.assertIsNone(new['policy']['max_attempts'])
        state=ModelBudget(self.target,BudgetPolicy(**new['policy']),enabled=True).snapshot()
        self.assertEqual((state['attempts'],state['charged_or_held_microusd']),(1,17))
    def test_live_service_refuses_before_creating_files(self):
        candidate=self.root/'candidate.json'
        with patch('sys.argv',self.argv()+['--prepare','--candidate-config',str(candidate)]),patch.object(
                op.subprocess,'check_output',return_value='MainPID=1\nActiveState=active\n'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'stopped'):op.main()
        self.assertFalse(self.target.exists());self.assertFalse(candidate.exists())
    def test_existing_halt_or_unknown_never_cleared(self):
        self.ledger.halt()
        with self.assertRaisesRegex(ValueError,'halt_retained'):op.inspect(self.old)
        self.assertTrue(self.ledger.snapshot()['halted'])
