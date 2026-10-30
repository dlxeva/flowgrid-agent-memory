"""Sixteen real local HTTP Search requests, mocked provider worker only."""
from collections import Counter
from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from aml_retriever import aml_hosted as hosted
from aml_retriever import aml_semantic_cues as cues
from tests import test_aml_semantic_host as host_tests

AUDIT = []

class ReferenceSelectionQueueTests(unittest.TestCase):
    model_concurrency = 1
    tenants = ('u',)

    def setUp(self):
        self.h=host_tests.HostTests('runTest');self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        profile={'flags':{'official_evidence_packing':True,
            'experimental_reference_expansion':True,'experimental_reference_selection':True}}
        self.h.config=replace(self.h.config,retrieval_json=json.dumps(profile))
        self.h.policy=replace(self.h.policy,
            profile_sha256=hosted.retrieval_config_digest(hosted.hosted_retrieval_config(self.h.config)),
            max_attempts=100,budget_microusd=1000000,
            max_concurrent_calls=self.model_concurrency,
            input_token_limit=65536,input_bytes_limit=65536,output_token_limit=4096)
        self.h.semantic=replace(self.h.semantic,policy=self.h.policy,model_concurrency=self.model_concurrency)
        self.stages=[];self.selection_sizes=[];self.delay=.01
        self.activity_lock=threading.Lock();self.active=Counter();self.peak=0;self.tenant_peak=Counter()
        self.policy='Private storage only; revocation confirmation is unknown; publishing requires approval.'
        worker=patch.object(cues.subprocess,'run',side_effect=self.response)
        worker.start();self.addCleanup(worker.stop)
        self.h.start()
        texts=['Quartz plan uses SEC-731.', 'SEC-731 '+self.policy+' Secret: SYNTH-PRIVATE-917.']
        for tenant in self.tenants:
            for i,text in enumerate(texts):
                status,_=self.h.http('/add',{'user_id':tenant,'session_id':f's{i}',
                    'request_id':f'r{i}','messages':[{'role':'user','content':text}]})
                self.assertEqual(status,200)
        self.assertEqual(self.stages,['add','add']*len(self.tenants))
        self.stages.clear()

    def response(self,*args,**kwargs):
        data=json.loads(json.loads(kwargs['input'])['request']['messages'][1]['content'])
        stage=data.get('stage','add' if 'messages' in data else 'search')
        self.stages.append(stage)
        tenant=data.get('query','setup').split(' tenant=')[-1]
        if stage != 'add':
            with self.activity_lock:
                self.active[tenant]+=1
                self.peak=max(self.peak,sum(self.active.values()))
                self.tenant_peak[tenant]=max(self.tenant_peak[tenant],self.active[tenant])
        try: time.sleep(self.delay)
        finally:
            if stage != 'add':
                with self.activity_lock:self.active[tenant]-=1
        if stage=='add':
            body={'cues':[{'index':m['index'],'keywords':['Quartz plan'] if 'Quartz' in m['content'] else ['SEC-731']}
                          for m in data['messages']]}
        elif stage=='reference_select':
            self.selection_sizes.append((len(data['baseline_sources']),len(data['candidate_sources'])))
            body={'excerpts':[{'source_id':s['id'],'quote':self.policy} for s in data['candidate_sources']]}
        else:body={'queries':['Quartz plan']}
        raw={'model':cues.MODEL,'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':json.dumps(body)}}],
            'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}}
        return SimpleNamespace(returncode=0,stdout=json.dumps(raw).encode(),stderr=b'')

    def test_sixteen_http_searches_each_use_one_reference_selection(self):
        barrier=threading.Barrier(17);results=[None]*16;elapsed=[None]*16
        def run(i):
            barrier.wait(3);start=time.monotonic()
            try:
                tenant=self.tenants[i%len(self.tenants)]
                results[i]=self.h.http('/search',{'user_id':tenant,
                    'query':'Quartz plan tenant='+tenant,'top_k':100})
            except Exception as exc:results[i]=(type(exc).__name__,{})
            elapsed[i]=time.monotonic()-start
        threads=[threading.Thread(target=run,args=(i,)) for i in range(16)]
        for t in threads:t.start()
        barrier.wait(3)
        for t in threads:t.join(8)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual([r[0] for r in results],[200]*16)
        self.assertEqual(Counter(self.stages),{'search':16,'reference_select':16})
        self.assertEqual(self.selection_sizes,[(1,1)]*16)
        for _,body in results:
            content='\n'.join(r['content'] for r in body['data'])
            self.assertIn('Quartz plan uses SEC-731.',content)
            self.assertIn(self.policy,content)
            self.assertNotIn('SYNTH-PRIVATE-917',content)
        snapshot=self.h.server.model_budget.snapshot()
        self.assertEqual(snapshot['attempts'],2*len(self.tenants)+32)
        self.assertEqual(self.peak,self.model_concurrency)
        self.assertTrue(all(n==1 for n in self.tenant_peak.values()))
        self.assertEqual(snapshot['unresolved'],0)
        self.assertFalse(snapshot['halted'])
        AUDIT.append({'model_concurrency':self.model_concurrency,
            'peak_parallel_model_calls':self.peak,'per_tenant_peak':dict(self.tenant_peak),
            'statuses':[r[0] for r in results], 'search_model_calls':len(self.stages),
            'stages':dict(Counter(self.stages)), 'selection_baseline_candidate_sizes':self.selection_sizes,
            'fake_worker_delay_seconds':self.delay,'max_elapsed_seconds':max(elapsed),'ledger':snapshot})


class ReferenceSelectionParallelQueueTests(ReferenceSelectionQueueTests):
    """Combined quality path: two tenants overlap, each tenant remains serial."""
    model_concurrency = 2
    tenants = ('u0', 'u1')
