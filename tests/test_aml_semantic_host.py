"""Run against a complete installed candidate plus only the new host modules.

All provider subprocesses are mocked. Uses synthetic local loopback HTTP only.
"""
import copy
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from aml_retriever import aml_semantic_cues as cues
from aml_retriever import aml_hosted as hosted
from aml_retriever import aml_semantic_host as host
from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget, BudgetStopped
from aml_retriever.api import ApiError


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.key = self.root/'model.key'; self.key.write_text('synthetic-not-a-real-key\n'); self.key.chmod(0o600)
        self.config = hosted.HostedConfig(credential='synthetic-api-auth', db_path=str(self.root/'source.sqlite'), port=0,
            operation_timeout_seconds=270,request_timeout_seconds=300,max_inflight=16,max_connections=32)
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

    def concurrent_http(self, payloads):
        barrier=threading.Barrier(len(payloads)+1)
        results=[None]*len(payloads)
        def run(index,payload):
            try:
                barrier.wait(3)
                results[index]=self.http('/add',payload)
            except Exception as error:
                results[index]=error
        threads=[threading.Thread(target=run,args=(i,p)) for i,p in enumerate(payloads)]
        for thread in threads: thread.start()
        barrier.wait(3)
        for thread in threads: thread.join(8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return results

    def test_sixteen_overlapping_http_adds_and_user_isolation(self):
        self.policy=replace(self.policy,max_attempts=64)
        self.semantic=replace(self.semantic,policy=self.policy)
        self.start()
        payloads=[]
        for i in range(16):
            payload=copy.deepcopy(self.add)
            payload.update(request_id='request-'+str(i),user_id='user-'+str(i))
            payload['messages'][0]['content']='I avoid animal-derived ingredients, user '+str(i)
            payloads.append(payload)
        results=self.concurrent_http(payloads)
        self.assertEqual([r[0] if isinstance(r,tuple) else str(r) for r in results],[200]*16)
        self.assertEqual(self.calls,16)
        for payload in payloads:
            status,result=self.http('/search',{'user_id':payload['user_id'],'query':'diet','top_k':100})
            self.assertEqual(status,200)
            self.assertEqual([r['content'] for r in result['data']],[payload['messages'][0]['content']])
        self.assertEqual(self.server.model_budget.snapshot()['attempts'],32)

    def test_duplicate_concurrent_http_adds_pay_once(self):
        self.start()
        results=self.concurrent_http([copy.deepcopy(self.add) for _ in range(16)])
        self.assertEqual([r[0] if isinstance(r,tuple) else str(r) for r in results],[200]*16)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.server.model_budget.snapshot()['attempts'],1)

    def blocked_admission(self,capacity=2):
        entered=threading.Event(); release=threading.Event(); calls=[]; results=[]
        def backend(payload):
            calls.append(payload)
            if payload=='active':
                entered.set()
                if not release.wait(4): raise AssertionError('test backend release timed out')
            return payload
        service=host._AdmissionService(SimpleNamespace(official_add=backend,official_search=backend),
                                       capacity=capacity,operation_seconds=270)
        def dispatch(payload,deadline=None,cancelled=None):
            try:
                results.append((payload,service.dispatch('/add',payload,
                    time.monotonic()+3 if deadline is None else deadline,
                    threading.Event() if cancelled is None else cancelled)))
            except Exception as error: results.append((payload,error))
        active=threading.Thread(target=dispatch,args=('active',)); active.start()
        self.addCleanup(lambda:active.join(4))
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        return service,release,calls,results,active,dispatch

    def test_full_queue_rejected_without_backend_call(self):
        service,release,calls,results,active,dispatch=self.blocked_admission(capacity=1)
        with self.assertRaises(ApiError) as raised:
            service.dispatch('/add','overflow',time.monotonic()+3,threading.Event())
        self.assertEqual(raised.exception.status,429)
        release.set(); active.join(3)
        self.assertEqual(calls,['active'])

    def test_waiting_operations_enter_backend_in_fifo_order(self):
        service,release,calls,results,active,dispatch=self.blocked_admission(capacity=3)
        appended=[threading.Event(),threading.Event()]
        class ObservedQueue(deque):
            def append(queue,ticket):
                super().append(ticket)
                appended[len(queue)-1].set()
        # Observe admission itself so ordering does not depend on scheduling sleeps.
        with service._condition: service._waiting=ObservedQueue(service._waiting)
        waiters=[]
        try:
            for index,payload in enumerate(('first','second')):
                waiter=threading.Thread(target=dispatch,args=(payload,)); waiter.start(); waiters.append(waiter)
                self.assertTrue(appended[index].wait(2))
            release.set()
        finally:
            release.set()
            active.join(3)
            for waiter in waiters: waiter.join(3)
        self.assertFalse(any(waiter.is_alive() for waiter in waiters))
        self.assertEqual(calls,['active','first','second'])

    def test_waiting_expiry_never_calls_backend_later(self):
        service,release,calls,results,active,dispatch=self.blocked_admission()
        waiter=threading.Thread(target=dispatch,args=('expired',time.monotonic()+0.1))
        waiter.start(); waiter.join(2)
        self.assertFalse(waiter.is_alive())
        error=dict(results)['expired']
        self.assertIsInstance(error,ApiError); self.assertEqual(error.status,429)
        release.set(); active.join(3)
        self.assertEqual(calls,['active'])

    def test_waiting_cancellation_never_calls_backend_later(self):
        service,release,calls,results,active,dispatch=self.blocked_admission()
        cancelled=threading.Event(); started=threading.Event()
        def waiting():
            started.set(); dispatch('cancelled',cancelled=cancelled)
        waiter=threading.Thread(target=waiting); waiter.start()
        self.assertTrue(started.wait(1)); cancelled.set(); waiter.join(2)
        self.assertFalse(waiter.is_alive())
        error=dict(results)['cancelled']
        self.assertIsInstance(error,ApiError); self.assertEqual(error.status,429)
        release.set(); active.join(3)
        self.assertEqual(calls,['active'])

    def test_server_shutdown_cancels_before_waiting_for_http_loop(self):
        self.start()
        cancelled = threading.Event()
        original = self.server.deadline_shutdown
        def cancel():
            original()
            cancelled.set()
        self.server.deadline_shutdown = cancel
        with patch.object(hosted.ThreadingHTTPServer, 'shutdown',
                          side_effect=lambda: self.assertTrue(cancelled.is_set())) as stop:
            self.server.shutdown()
            stop.assert_called_once_with()

    def test_shutdown_cancels_active_and_rejects_waiting_backend_work(self):
        service,release,calls,results,active,dispatch=self.blocked_admission()
        appended=threading.Event()
        class ObservedQueue(deque):
            def append(queue,ticket):
                super().append(ticket); appended.set()
        with service._condition:
            service._waiting=ObservedQueue(service._waiting)
            active_cancelled=service._active_cancelled
        waiter=threading.Thread(target=dispatch,args=('waiting',)); waiter.start()
        try:
            self.assertTrue(appended.wait(2))
            service.cancel_pending()
            self.assertTrue(active_cancelled.is_set())
            waiter.join(2)
            self.assertFalse(waiter.is_alive())
            error=dict(results)['waiting']
            self.assertIsInstance(error,ApiError); self.assertEqual(error.status,429)
            with self.assertRaises(ApiError) as raised:
                service.dispatch('/add','after-shutdown',time.monotonic()+3,threading.Event())
            self.assertEqual(raised.exception.status,429)
        finally:
            release.set(); active.join(3); waiter.join(3)
        self.assertEqual(calls,['active'])

    def test_http_waiting_deadline_prevents_later_backend_work(self):
        entered=threading.Event(); queued=threading.Event(); release=threading.Event()
        calls=[]; first_result=[]
        def backend(payload):
            calls.append(payload['request_id'])
            entered.set()
            if not release.wait(3): raise AssertionError('test release timed out')
            return {'status':'ok'}
        admission=host._AdmissionService(SimpleNamespace(official_add=backend,
            official_search=backend,close=lambda:None,config=hosted.hosted_retrieval_config(self.config)),
            capacity=2,operation_seconds=0.15)
        def dispatch(route,payload,deadline,cancelled):
            if payload['request_id']=='waiting': queued.set()
            return admission.dispatch(route,payload,deadline,cancelled)
        self.server=hosted.HostedAMLServer(replace(self.config,operation_timeout_seconds=0.15),
            service=admission,quiet=True,deadline_dispatch=dispatch)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        first=threading.Thread(target=lambda:first_result.append(self.http('/add',self.add)))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            status,_=self.http('/add',{**self.add,'request_id':'waiting'})
            self.assertTrue(queued.is_set())
            self.assertIn(status,(429,504))
        finally:
            release.set(); first.join(3)
            self.stop()
        self.assertFalse(first.is_alive())
        self.assertEqual(calls,['r'])

    def test_transport_deadline_caps_worker_timeout(self):
        self.start()
        wire=cues.request('search',{'query':'diet'},provider='openai')
        transport=cues.OpenAICueTransport('synthetic-key',reserve=self.server.model_budget.reserve,
            settle=self.server.model_budget.settle,unknown=self.server.model_budget.unknown,
            enabled=True,network_seconds=120,process_seconds=130)
        with patch.object(cues.subprocess,'run',side_effect=self.response) as worker:
            transport(wire,deadline=time.monotonic()+0.5,cancelled=threading.Event())
        self.assertGreater(worker.call_args.kwargs['timeout'],0)
        self.assertLessEqual(worker.call_args.kwargs['timeout'],0.5)

    def test_expired_or_cancelled_transport_does_not_reserve_or_launch(self):
        transport=cues.OpenAICueTransport('synthetic-key',reserve=lambda *a:self.fail('reserved'),
            settle=lambda *a:self.fail('settled'),unknown=lambda *a:self.fail('unknown'),enabled=True)
        wire=cues.request('search',{'query':'diet'},provider='openai')
        cancelled=threading.Event(); cancelled.set()
        for deadline,event in ((time.monotonic()-1,threading.Event()),(time.monotonic()+3,cancelled)):
            with self.subTest(cancelled=event.is_set()):
                with self.assertRaisesRegex(ValueError,'cue_request_cancelled' if event.is_set() else 'cue_request_deadline'):
                    transport(wire,deadline=deadline,cancelled=event)
        self.assertEqual(self.calls,0)

    def test_provider_failure_halts_overlapping_paid_calls(self):
        self.invalid=True; self.start()
        payloads=[{**copy.deepcopy(self.add),'user_id':'user-'+str(i),'request_id':'request-'+str(i)} for i in range(16)]
        results=self.concurrent_http(payloads)
        self.assertTrue(all(isinstance(r,tuple) and r[0]>=500 for r in results),results)
        self.assertEqual(self.calls,1)
        state=self.server.model_budget.snapshot()
        self.assertTrue(state['halted']); self.assertEqual(state['attempts'],1)

    def test_queue_configuration_rejects_invalid_capacity_and_http_limits(self):
        for capacity in (True,15,65,16.5):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError): replace(self.semantic,queue_capacity=capacity).validate(self.config)
        for config in (replace(self.config,max_inflight=15),replace(self.config,max_connections=17)):
            with self.assertRaises(ValueError): self.semantic.validate(config)

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
                      'operation_timeout_seconds':270,'request_timeout_seconds':300,
                      'max_inflight':16,'max_connections':32},
            'semantic':{'model_key_file':str(self.root/'missing-model.key'), 'cue_db_path':self.semantic.cue_db_path,
                        'budget_db_path':self.semantic.budget_db_path,'enabled':True},
            'policy':self.policy.__dict__}))
        with patch.object(host,'_private_key',side_effect=AssertionError('must not read')):
            self.assertEqual(host.main(['--config',str(p),'--check']),0)
        self.assertFalse(Path(self.semantic.budget_db_path).exists()); self.assertEqual(self.calls,0)


if __name__ == '__main__': unittest.main()
