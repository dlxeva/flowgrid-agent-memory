"""Synthetic concurrency/accounting regression. No external calls or keys."""
import concurrent.futures
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget, BudgetStopped, MODEL
from aml_retriever import aml_semantic_host as host
from aml_retriever import aml_semantic_cues as cues
import tests.test_aml_semantic_host as legacy

class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name).resolve()/'ledger.sqlite'
        self.policy=BudgetPolicy('capacity','a'*64,'b'*64,None,None,
                                 max_concurrent_calls=4,isolate_unknown_calls=True)
        self.ledger=ModelBudget(self.path,self.policy,enabled=True)
    def reserve(self): return self.ledger.reserve('c'*64,MODEL,16384,768)
    def settle(self,t): self.ledger.settle(t,{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50},'d'*64)
    def test_no_caps_and_all_receipts_preserved(self):
        for _ in range(300): self.settle(self.reserve())
        state=ModelBudget(self.path,self.policy,enabled=True).snapshot()
        self.assertEqual((state['attempts'],state['charged_or_held_microusd'],state['unresolved']),(300,5100,0))
        self.assertIsNone(state['budget_microusd'])
    def test_four_atomic_reservations_and_no_fifth(self):
        def attempt(_):
            try:return self.reserve()
            except BudgetStopped:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
            tokens=list(filter(None,p.map(attempt,range(16))))
        self.assertEqual(len(tokens),4)
        for t in tokens:self.settle(t)
        self.settle(self.reserve())
        self.assertEqual(self.ledger.snapshot()['attempts'],5)
    def test_unknown_hold_survives_restart_without_stopping_other_calls(self):
        t=self.reserve();self.ledger.unknown(t)
        self.ledger=ModelBudget(self.path,self.policy,enabled=True)
        self.settle(self.reserve())
        state=self.ledger.snapshot()
        self.assertEqual((state['unresolved'],state['charged_or_held_microusd']),(1,5393))
    def test_old_binding_and_ledger_unchanged(self):
        p=BudgetPolicy('legacy','a'*64,'b'*64,20000,2)
        self.assertNotIn('max_concurrent_calls',json.loads(p.binding))
        self.assertNotIn('isolate_unknown_calls',json.loads(p.binding))
        old=ModelBudget(self.path.parent/'old.sqlite',p,enabled=True)
        with self.assertRaisesRegex(BudgetStopped,'binding'):
            ModelBudget(old.path,replace(p,budget_microusd=None),enabled=True)
    def test_explicit_boolean_and_concurrency_validation(self):
        for value in (0,9,True):
            with self.assertRaises(ValueError):replace(self.policy,max_concurrent_calls=value)
        with self.assertRaises(ValueError):replace(self.policy,budget_microusd=False)

class CapacityHTTPTests(unittest.TestCase):
    response=legacy.HostTests.response
    start=legacy.HostTests.start
    stop=legacy.HostTests.stop
    http=legacy.HostTests.http
    concurrent_http=legacy.HostTests.concurrent_http
    def setUp(self):
        legacy.HostTests.setUp(self)
        self.policy=replace(self.policy,budget_microusd=None,max_attempts=None,
                            max_concurrent_calls=4,isolate_unknown_calls=True)
        self.semantic=replace(self.semantic,policy=self.policy,model_concurrency=4,
                              isolate_provider_failures=True)
    def test_parallel_real_service_adds_isolation_and_accounting(self):
        lock=threading.Lock();active=0;peak=0
        def slow(*a,**kw):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            try:
                time.sleep(.04)
                return self.response(*a,**kw)
            finally:
                with lock:active-=1
        with patch.object(cues.subprocess,'run',side_effect=slow):
            self.start()
            payloads=[{**self.add,'user_id':'u'+str(i),'request_id':'r'+str(i)} for i in range(16)]
            results=self.concurrent_http(payloads)
        self.assertEqual([r[0] for r in results],[200]*16)
        self.assertEqual(peak,4)
        self.assertEqual(self.server.model_budget.snapshot()['unresolved'],0)
        self.assertEqual(self.server.model_budget.snapshot()['attempts'],16)
        self.assertEqual(self.http('/search',{'user_id':'absent','query':'diet','top_k':100})[1]['data'],[])
        self.assertEqual(self.server.service._service._tenant_locks,{})
    def test_same_user_duplicate_still_pays_once(self):
        self.start()
        self.assertEqual([r[0] for r in self.concurrent_http([dict(self.add) for _ in range(16)])],[200]*16)
        self.assertEqual(self.calls,1)
    def test_provider_failure_isolated_no_automatic_retry(self):
        self.start();self.timeout=True
        self.assertEqual(self.http('/add',self.add)[0],503)
        self.timeout=False
        self.assertEqual(self.http('/add',{**self.add,'user_id':'other'})[0],200)
        self.assertEqual(self.http('/add',self.add)[0],503)
        self.assertEqual(self.calls,2)
        state=self.server.model_budget.snapshot()
        self.assertFalse(state['halted']);self.assertEqual(state['unresolved'],1)
        self.stop();self.start()
        self.assertEqual(self.http('/search',{'user_id':'other','query':'diet','top_k':100})[0],200)
        self.assertEqual(self.server.model_budget.snapshot()['unresolved'],1)
    def test_health_discloses_no_fee_cap_and_concurrency(self):
        self.start();status,health=self.http('/health')
        self.assertEqual(status,200)
        self.assertFalse(health['semantic_budget_enforced'])
        self.assertFalse(health['semantic_call_quota_enforced'])
        self.assertEqual(health['semantic_model_concurrency'],4)
    def test_configuration_must_match_ledger_concurrency(self):
        with self.assertRaisesRegex(ValueError,'concurrency'):
            replace(self.semantic,model_concurrency=2).validate(self.config)

class SchedulingTests(unittest.TestCase):
    def test_blocked_tenant_does_not_block_unrelated_tenant(self):
        from types import SimpleNamespace
        a=threading.Event();b=threading.Event();release=threading.Event();seen=[]
        def backend(payload):
            seen.append(payload['n'])
            if payload['n']=='a1':a.set();self.assertTrue(release.wait(3))
            if payload['n']=='b':b.set()
            return payload
        svc=host._AdmissionService(SimpleNamespace(official_add=backend),capacity=16,concurrency=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            one=pool.submit(svc.official_add,{'user_id':'a','n':'a1'});self.assertTrue(a.wait(2))
            two=pool.submit(svc.official_add,{'user_id':'a','n':'a2'})
            three=pool.submit(svc.official_add,{'user_id':'b','n':'b'})
            try:self.assertTrue(b.wait(2));self.assertNotIn('a2',seen)
            finally:release.set()
            one.result();two.result();three.result()
        self.assertEqual(seen.index('a1')<seen.index('a2'),True)

class CapacityStressTests(CapacityHTTPTests):
    # Reuse host setup/helpers, without rerunning inherited cases in this class.
    test_parallel_real_service_adds_isolation_and_accounting = None
    test_same_user_duplicate_still_pays_once = None
    test_provider_failure_isolated_no_automatic_retry = None
    test_health_discloses_no_fee_cap_and_concurrency = None
    test_configuration_must_match_ledger_concurrency = None

    def test_one_failed_worker_does_not_stop_other_inflight_tenants(self):
        def mixed(*a,**kw):
            wire=json.loads(kw['input'])['request']
            data=json.loads(wire['messages'][1]['content'])
            time.sleep(.01)
            if data.get('messages',[{}])[0].get('content')=='FAIL_SYNTHETIC':
                raise TimeoutError('synthetic')
            return self.response(*a,**kw)
        with patch.object(cues.subprocess,'run',side_effect=mixed):
            self.start()
            payloads=[{**self.add,'user_id':'u'+str(i),'request_id':'r'+str(i),
                       'messages':[{'role':'user','content':'FAIL_SYNTHETIC' if i==0 else 'vegan diet '+str(i)}]}
                      for i in range(16)]
            results=self.concurrent_http(payloads)
        self.assertEqual([r[0] for r in results],[503]+[200]*15)
        state=self.server.model_budget.snapshot()
        self.assertEqual((state['attempts'],state['unresolved'],state['halted']),(16,1,False))

    def test_direct_service_duplicate_is_serialized_without_http_scheduler(self):
        self.start();service=self.server.service._service
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
            results=list(p.map(lambda _:service.official_add(dict(self.add)),range(16)))
        self.assertTrue(all(r['success'] for r in results));self.assertEqual(self.calls,1)
        self.assertEqual(service._tenant_locks,{})

    def test_stream_of_256_adds_and_256_searches_retains_isolation(self):
        self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
            for start in range(0,256,16):
                payloads=[{**self.add,'user_id':'u'+str(i),'request_id':'r'+str(i),
                           'messages':[{'role':'user','content':'vegan diet tenant '+str(i)}]}
                          for i in range(start,start+16)]
                adds=list(p.map(lambda x:self.http('/add',x),payloads))
                self.assertEqual([x[0] for x in adds],[200]*16)
                searches=list(p.map(lambda x:self.http('/search',{
                    'user_id':x['user_id'],'query':'diet','top_k':100}),payloads))
                for payload,(status,result) in zip(payloads,searches):
                    self.assertEqual(status,200)
                    expected=payload['messages'][0]['content']
                    actual=[r['content'] for r in result['data']]
                    self.assertIn(expected,actual)
                    self.assertTrue(set(actual) <= {expected, 'user: '+expected})
        state=self.server.model_budget.snapshot()
        self.assertEqual((state['attempts'],state['unresolved'],state['halted']),(512,0,False))
        service=self.server.service._service
        with service.lock:
            self.assertEqual(dict(service.cue_db.execute(
                'SELECT status,count(*) FROM aml_cue_attempts GROUP BY status')),
                {'complete':256})
        self.assertEqual(service._tenant_locks,{})
