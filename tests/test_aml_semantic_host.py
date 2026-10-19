"""Run against a complete installed candidate plus only the new host modules.

All provider subprocesses are mocked. Uses synthetic local loopback HTTP only.
"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from aml_retriever import aml_semantic_cues as cues
from aml_retriever import aml_hosted as hosted
from aml_retriever import aml_semantic_host as host
from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget, BudgetStopped


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.key = self.root/'model.key'; self.key.write_text('synthetic-not-a-real-key\n'); self.key.chmod(0o600)
        self.config = hosted.HostedConfig(credential='synthetic-api-auth', db_path=str(self.root/'source.sqlite'), port=0,
            operation_timeout_seconds=270,request_timeout_seconds=300)
        profile = hosted.retrieval_config_digest(hosted.hosted_retrieval_config(self.config))
        self.policy = BudgetPolicy('synthetic-host', 'a'*64, profile, 100000, 20)
        self.semantic = host.SemanticHostConfig(str(self.key), str(self.root/'cues.sqlite'), str(self.root/'budget.sqlite'), self.policy, True)
        self.identity = {'runtime_digest_sha256': 'a'*64, 'identity_source': 'test_injection',
                         'source_state': 'uncommitted', 'product_version': '0.1.1',
                         'aml_adapter_version': '1.1.0', 'source_commit': None, 'source_digest_sha256': None}
        self.identity_patch = patch.object(hosted, 'runtime_identity', return_value=self.identity)
        self.identity_patch.start(); self.addCleanup(self.identity_patch.stop)
        self.calls = 0; self.invalid = False; self.timeout = False
        self.worker = patch.object(cues.subprocess, 'run', side_effect=self.response)
        self.worker.start(); self.addCleanup(self.worker.stop)
        self.server = None
        self.addCleanup(self.stop)
        self.add = {'request_id':'r','user_id':'u','session_id':'s','messages':[
            {'role':'user','content':'I avoid animal-derived ingredients.', 'timestamp':1704067200000}]}

    def response(self, *args, **kwargs):
        self.calls += 1
        if self.timeout: raise TimeoutError('synthetic')
        wire = json.loads(kwargs['input'])['request']
        stage = wire['response_format']['json_schema']['name']
        data = json.loads(wire['messages'][1]['content'])
        result = {'cues':[{'index':m['index'],'keywords':['vegan','diet']} for m in data['messages']]} if stage == 'aml_add_cues' else {'queries':['diet']}
        if self.invalid: result = {'confirmed':True}
        raw = json.dumps({'model':cues.MODEL, 'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(result)}}],
                          'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}}).encode()
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b'')

    def start(self):
        self.server = host.create_server(self.config, self.semantic)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown(); self.thread.join(3)
            self.server.server_close(); self.server.service.close(); self.server = None

    def http(self, path, payload=None, auth=True):
        headers = {'Content-Type':'application/json'}
        if auth: headers['Authorization'] = 'Bearer synthetic-api-auth'
        req = urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,
            data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as response: return response.code, json.load(response)

    def test_http_top100_auth_isolation_delete_and_restart(self):
        self.start()
        status, health = self.http('/health')
        self.assertEqual(status,200); self.assertTrue(health['semantic_budget_enforced'])
        self.assertNotIn('synthetic-not-a-real-key', json.dumps(health))
        self.assertEqual(self.http('/add',self.add,auth=False)[0],401); self.assertEqual(self.calls,0)
        self.assertEqual(self.http('/add',self.add)[0],200)
        changed=copy.deepcopy(self.add); changed['messages'][0]['content']='replacement'
        self.assertEqual(self.http('/add',changed)[0],200); self.assertEqual(self.calls,1)
        query={'user_id':'u','query':'diet','top_k':100}
        status,result=self.http('/search',query)
        self.assertEqual(status,200); self.assertEqual(result['data'][0]['content'],self.add['messages'][0]['content'])
        self.assertEqual(self.http('/search',{**query,'user_id':'other'})[1]['data'],[])
        self.stop(); self.start()
        self.assertEqual(self.http('/add',self.add)[0],200); self.assertEqual(self.calls,3)
        self.server.service.delete_user('u')
        self.assertEqual(self.http('/search',query)[1]['data'],[])
        with self.server.service.db.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM memory_records').fetchone()[0],0)
        self.assertEqual(self.server.model_budget.snapshot()['attempts'],4)

    def test_timeout_retains_hold_and_never_reasks_after_restart(self):
        self.timeout=True; self.start()
        self.assertGreaterEqual(self.http('/add',self.add)[0],500)
        self.assertGreaterEqual(self.http('/add',self.add)[0],500)
        self.assertEqual(self.calls,1)
        self.stop(); self.start()
        self.assertGreaterEqual(self.http('/search',{'user_id':'u','query':'diet','top_k':100})[0],500)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.server.model_budget.snapshot()['charged_or_held_microusd'],5376)

    def test_host_twenty_messages_complete_and_timeout_bounds(self):
        self.add['messages'] = [{'role':'user','content':'Synthetic statement '+str(i)} for i in range(20)]
        with patch.object(cues.subprocess,'run',side_effect=self.response) as worker:
            self.start()
            self.assertEqual(self.http('/add',self.add)[0],200)
            self.assertEqual(self.calls,2)
            wires=[json.loads(c.kwargs['input'])['request'] for c in worker.call_args_list]
            offered=[m for w in wires for m in json.loads(w['messages'][1]['content'])['messages']]
            self.assertEqual([m['index'] for m in offered],list(range(20)))
            self.assertTrue(all(c.kwargs['timeout']==130 for c in worker.call_args_list))
            self.assertTrue(all('timeout=120' in c.args[0][-1] for c in worker.call_args_list))
        self.assertEqual(self.http('/health')[1]['semantic_cues_add_input_coverage'],'complete')
        with self.server.service.db.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],20)
            self.assertEqual(con.execute('SELECT count(*) FROM memory_records').fetchone()[0],0)

    def test_second_host_chunk_failure_has_no_partial_raw_add(self):
        self.add['messages'] = [{'role':'user','content':'Synthetic statement '+str(i)} for i in range(20)]
        original=self.response
        def second_failure(*args,**kwargs):
            if self.calls==1: self.timeout=True
            return original(*args,**kwargs)
        with patch.object(cues.subprocess,'run',side_effect=second_failure):
            self.start()
            self.assertGreaterEqual(self.http('/add',self.add)[0],500)
            self.assertGreaterEqual(self.http('/add',self.add)[0],500)
        self.assertEqual(self.calls,2)
        with self.server.service.db.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        self.assertEqual(self.server.model_budget.snapshot()['unresolved'],1)

    def test_semantic_failure_halts_but_known_usage_stays_settled(self):
        self.invalid=True; self.start()
        self.assertGreaterEqual(self.http('/add',self.add)[0],500)
        self.assertGreaterEqual(self.http('/search',{'user_id':'u','query':'diet','top_k':100})[0],500)
        state=self.server.model_budget.snapshot()
        self.assertTrue(state['halted']); self.assertEqual(state['unresolved'],0)
        self.assertEqual(state['charged_or_held_microusd'],17); self.assertEqual(self.calls,1)

    def test_runtime_and_profile_drift_rejected_before_key_read(self):
        for policy in (replace(self.policy, implementation_sha256='b'*64), replace(self.policy, profile_sha256='c'*64)):
            with patch.object(host,'_private_key',side_effect=AssertionError('must not read')):
                with self.assertRaises(ValueError): host.create_server(self.config,replace(self.semantic,policy=policy))

    def test_disabled_and_invalid_deadline_do_not_read_credentials(self):
        with self.assertRaises(ValueError): replace(self.semantic,enabled=False).validate(self.config)
        with self.assertRaises(ValueError): self.semantic.validate(replace(self.config,operation_timeout_seconds=20))
        self.assertFalse(self.semantic.validate(self.config)['credentials_read'])

    def test_busy_whole_operation_rejected_without_later_paid_work(self):
        self.start()
        entered=threading.Event(); release=threading.Event()
        original=self.response
        def delayed(*args,**kwargs):
            entered.set(); release.wait(3)
            return original(*args,**kwargs)
        with patch.object(cues.subprocess,'run',side_effect=delayed):
            result=[]
            thread=threading.Thread(target=lambda:result.append(self.http('/add',self.add)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                second={**self.add,'request_id':'second'}
                self.assertGreaterEqual(self.http('/add',second)[0],500)
            finally:
                release.set(); thread.join(3)
            self.assertEqual(result[0][0],200)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.server.model_budget.snapshot()['attempts'],1)

    def test_host_attempt_limit_survives_restart(self):
        self.policy=replace(self.policy,max_attempts=1)
        self.semantic=replace(self.semantic,policy=self.policy)
        self.start(); self.assertEqual(self.http('/add',self.add)[0],200)
        self.stop(); self.start()
        self.assertGreaterEqual(self.http('/search',{'user_id':'u','query':'diet','top_k':100})[0],500)
        self.assertEqual(self.calls,1)

    def test_cli_check_does_not_read_keys_create_ledger_or_call_provider(self):
        p=self.root/'config.json'
        p.write_text(json.dumps({'api_key_file':str(self.root/'missing-api.key'),
            'hosted':{'db_path':self.config.db_path,'port':0,
                      'operation_timeout_seconds':270,'request_timeout_seconds':300},
            'semantic':{'model_key_file':str(self.root/'missing-model.key'), 'cue_db_path':self.semantic.cue_db_path,
                        'budget_db_path':self.semantic.budget_db_path,'enabled':True},
            'policy':self.policy.__dict__}))
        with patch.object(host,'_private_key',side_effect=AssertionError('must not read')):
            self.assertEqual(host.main(['--config',str(p),'--check']),0)
        self.assertFalse(Path(self.semantic.budget_db_path).exists()); self.assertEqual(self.calls,0)


if __name__ == '__main__': unittest.main()
