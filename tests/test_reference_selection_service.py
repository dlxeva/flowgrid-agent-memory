"""Reference-only selection integration with synthetic local transports."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aml_retriever.api import ApiError, MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever import aml_semantic_cues as cues
from aml_retriever.aml_semantic_cues import SemanticCueService, OpenAICueTransport
from aml_retriever.aml_semantic_host import _GuardedTransport
from aml_retriever.aml_model_budget import BudgetPolicy, ModelBudget
from aml_retriever.reference_selection import prepare


def raw(body):
    return json.dumps({'model':cues.MODEL,'choices':[{'finish_reason':'stop','message':{
        'role':'assistant','content':json.dumps(body)}}],
        'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}).encode()


class ReferenceSelectionServiceTests(unittest.TestCase):
    def make(self, failure=None):
        core=MemoryService(RetrieverConfig().with_flags(views=False,official_evidence_packing=True,
            experimental_reference_expansion=True,experimental_reference_selection=True))
        self.addCleanup(core.close)
        calls=[]
        policy='Private storage only; revocation confirmation is unknown; publishing requires approval.'
        def transport(wire):
            calls.append(copy.deepcopy(wire))
            data=json.loads(wire['messages'][1]['content'])
            if data.get('stage')!='reference_select':return raw({'queries':['Quartz plan']})
            if failure=='timeout':raise TimeoutError('synthetic timeout')
            if failure=='invalid':return raw({'excerpts':[{'source_id':'unknown','quote':'invented'}]})
            if failure=='baseline':return raw({'excerpts':[{'source_id':data['baseline_sources'][0]['id'],
                'quote':data['baseline_sources'][0]['content']}]})
            return raw({'excerpts':[{'source_id':s['id'],'quote':policy} for s in data['candidate_sources']]})
        service=SemanticCueService(core,transport,enabled=True)
        self.addCleanup(service.cue_db.close)
        core.official_add({'user_id':'u','session_id':'s','request_id':'r','messages':[
            {'role':'user','content':'Quartz plan uses SEC-731. Baseline statement remains verbatim.'},
            {'role':'user','content':'SEC-731 '+policy+' Password value: SYNTH-PRIVATE-917.'}]})
        return core,service,calls,policy

    def test_only_new_evidence_is_selected_with_one_additional_call(self):
        for k in (1,100):
            with self.subTest(top_k=k):
                core,service,calls,policy=self.make()
                query={'user_id':'u','query':'Quartz plan','top_k':k}
                core.config.flags['experimental_reference_selection']=False
                core.config.flags['experimental_reference_expansion']=False
                baseline=service.official_search(query)['data']
                calls.clear()
                core.config.flags['experimental_reference_selection']=True
                core.config.flags['experimental_reference_expansion']=True
                result=service.official_search(query)['data']
                self.assertEqual(len(calls),2)
                self.assertEqual([json.loads(w['messages'][1]['content']).get('stage') for w in calls],[None,'reference_select'])
                self.assertLessEqual(len(result),k)
                self.assertIn(policy,'\n'.join(r['content'] for r in result))
                self.assertNotIn('SYNTH-PRIVATE-917','\n'.join(r['content'] for r in result))
                for before,after in zip(baseline,result):
                    self.assertEqual(before['id'],after['id'])
                    self.assertTrue(after['content'].startswith(before['content']))
                    self.assertEqual(after['source_spans'][:len(before['source_spans'])],before['source_spans'])
                selected_data=json.loads(calls[1]['messages'][1]['content'])
                self.assertTrue(selected_data['baseline_sources'])
                self.assertTrue(selected_data['candidate_sources'])

    def test_no_new_sources_keeps_single_search_call(self):
        _,service,calls,_=self.make()
        result=service.official_search({'user_id':'empty','query':'Quartz plan','top_k':100})
        self.assertEqual(result,{'data':[]})
        self.assertEqual(len(calls),1)
        # An existing tenant with evidence but no references also needs no selector.
        service.core.official_add({'user_id':'plain','session_id':'s','request_id':'r',
            'messages':[{'role':'user','content':'Quartz plan has no linked ticket.'}]})
        calls.clear()
        self.assertTrue(service.official_search({'user_id':'plain','query':'Quartz plan','top_k':100})['data'])
        self.assertEqual(len(calls),1)

    def test_failure_never_returns_raw_new_evidence_or_retries(self):
        for failure in ('timeout','invalid','baseline'):
            with self.subTest(failure=failure):
                _,service,calls,_=self.make(failure)
                with self.assertRaisesRegex(ApiError,'reference selection failed') as caught:
                    service.official_search({'user_id':'u','query':'Quartz plan','top_k':1})
                self.assertEqual(caught.exception.status,503)
                self.assertEqual(len(calls),2)

    def test_dependencies_conflicts_and_core_entry_reject_before_transport(self):
        for flag in ('official_evidence_packing','experimental_reference_expansion',
                     'experimental_clause_selection','experimental_evidence_selection'):
            with self.subTest(flag=flag):
                core,service,calls,_=self.make()
                core.config.flags[flag]=flag in ('experimental_clause_selection','experimental_evidence_selection')
                with self.assertRaises(ApiError) as caught:
                    service.official_search({'user_id':'u','query':'Quartz plan','top_k':100})
                self.assertEqual(caught.exception.status,503)
                self.assertEqual(calls,[])
        core,_,calls,_=self.make()
        with patch.object(core,'_official_search_unpacked',side_effect=AssertionError('raw retrieval called')):
            with self.assertRaisesRegex(ApiError,'requires semantic service'):
                core.official_search({'user_id':'u','query':'Quartz plan','top_k':100})
        self.assertEqual(calls,[])


class ReferenceSelectionGuardTests(unittest.TestCase):
    def rows(self):
        def row(sid,text):
            return {'id':sid,'content':text,'source_message_ids':[sid],'source_spans':[{
                'source_id':sid,'role':'user','message_time':'unknown','start':0,'end':len(text)}]}
        base=[row('seed','Quartz plan uses SEC-731.')]
        return base,base+[row('added','SEC-731 private storage only; sensitive value.')]

    def ledger(self,root):
        return ModelBudget(root/'budget.sqlite3',BudgetPolicy(scope='reference-test',
            implementation_sha256='a'*64,profile_sha256='b'*64,budget_microusd=100000,
            max_attempts=10,input_token_limit=65536,output_token_limit=4096,input_bytes_limit=65536),enabled=True)

    def test_canonical_settles_tamper_fails_before_reservation(self):
        base,expanded=self.rows();wire=prepare('Quartz plan permissions',None,base,expanded)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();root.chmod(0o700);ledger=self.ledger(root)
            transport=OpenAICueTransport('synthetic',reserve=ledger.reserve,settle=ledger.settle,
                unknown=ledger.unknown,enabled=True)
            guard=_GuardedTransport(transport,ledger,cues)
            body={'excerpts':[{'source_id':'added','quote':'private storage only;'}]}
            with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=raw(body))) as worker:
                guard(wire)
                bad=copy.deepcopy(wire);bad['messages'][0]['content']='tampered'
                with self.assertRaisesRegex(ValueError,'cue_wire_drift'):transport(bad)
                self.assertEqual(worker.call_count,1)
            state=ledger.snapshot()
            self.assertEqual(state['attempts'],1);self.assertEqual(state['unresolved'],0)
            self.assertFalse(state['halted'])

    def test_guard_rejects_unknown_baseline_and_invented_or_extra_output(self):
        base,expanded=self.rows();wire=prepare('Quartz plan permissions',None,base,expanded)
        bad_bodies=[
            {'excerpts':[{'source_id':'seed','quote':base[0]['content']}]},
            {'excerpts':[{'source_id':'missing','quote':'private storage only;'}]},
            {'excerpts':[{'source_id':'added','quote':'publication approved'}]},
            {'excerpts':[],'answer':'extra'}]
        for body in bad_bodies:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();root.chmod(0o700);ledger=self.ledger(root)
                transport=OpenAICueTransport('synthetic',reserve=ledger.reserve,settle=ledger.settle,
                    unknown=ledger.unknown,enabled=True)
                guard=_GuardedTransport(transport,ledger,cues)
                with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=raw(body))) as worker:
                    with self.assertRaisesRegex(ValueError,'cue_shape'):guard(wire)
                    self.assertEqual(worker.call_count,1)
                self.assertEqual(ledger.snapshot()['attempts'],1)
                self.assertEqual(ledger.snapshot()['unresolved'],0)
                self.assertFalse(ledger.snapshot()['halted'])
