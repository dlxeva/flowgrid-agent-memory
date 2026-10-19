"""Injected providers only; complete input coverage is not model quality proof."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from aml_retriever.api import MemoryService, ApiError
from aml_retriever.config import RetrieverConfig
from aml_retriever import aml_semantic_cues as cues


class Provider:
    def __init__(self, provider='openai'):
        self.provider=provider;self.calls=[];self.fail_on=None;self.invalid_on=None
        self.callback=None;self.omit=False

    def __call__(self, wire):
        self.calls.append(copy.deepcopy(wire))
        if len(self.calls)==self.fail_on:raise TimeoutError()
        if self.callback:self.callback(len(self.calls))
        data=json.loads(wire['messages'][1]['content'])
        if 'messages' in data:
            value={'cues':[] if self.omit else [{'index':m['index'],'keywords':['hint'+str(m['index'])]}
                                               for m in data['messages']]}
            if len(self.calls)==self.invalid_on:value={'cues':[{'index':0,'keywords':['wrong chunk']}]}
        else:value={'queries':[]}
        return json.dumps({'model':cues.MODELS[self.provider],
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}}).encode()


def payload(n=20):
    return {'user_id':'u','session_id':'s','request_id':'r','messages':[
        {'role':'user','content':'original statement '+str(i),'timestamp':1704067200000+i} for i in range(n)]}


class CompleteCoverageTests(unittest.TestCase):
    def setUp(self):
        self.core=MemoryService(RetrieverConfig(db_path=':memory:'))
        self.provider=Provider()
        self.service=cues.SemanticCueService(self.core,self.provider,enabled=True,complete_add_coverage=True)

    def tearDown(self):
        self.service.close()

    def count(self, table):
        with self.core.db.connection() as con:return con.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_all_twenty_submitted_and_original_indices_kept(self):
        data=payload();self.service.official_add(data)
        groups=[json.loads(r['messages'][1]['content'])['messages'] for r in self.provider.calls]
        self.assertEqual([len(g) for g in groups],[12,8])
        self.assertEqual([m['index'] for group in groups for m in group],list(range(20)))
        self.assertEqual(self.count('messages'),20)
        self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM aml_cue_index').fetchone()[0],20)
        self.assertEqual(self.count('memory_records'),0)

    def test_tail_hint_returns_only_original_evidence(self):
        self.service.official_add(payload())
        response=self.service.official_search({'user_id':'u','query':'hint19','top_k':20})['data']
        self.assertTrue(response)
        self.assertEqual(response[0]['content'],'original statement 19')
        self.assertNotIn('hint19',response[0]['content'])
        self.assertEqual(self.service.official_search({'user_id':'other','query':'hint19','top_k':20})['data'],[])

    def test_second_failure_no_partial_raw_or_model_retry(self):
        self.provider.fail_on=2
        for _ in range(2):
            with self.assertRaises(ApiError):self.service.official_add(payload())
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.count('messages'),0)
        self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM aml_cue_index').fetchone()[0],0)
        self.assertEqual(self.service.cue_db.execute('SELECT status FROM aml_cue_attempts').fetchone()[0],'unknown')

    def test_chunk_local_index_cannot_point_to_previous_chunk(self):
        self.provider.invalid_on=2
        with self.assertRaises(ApiError):self.service.official_add(payload())
        self.assertEqual(self.count('messages'),0)

    def test_whole_plan_checks_late_oversize_before_any_call(self):
        data=payload();data['messages'][-1]['content']='x'*20000
        with self.assertRaises(ApiError):self.service.official_add(data)
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.count('messages'),0)

    def test_message_byte_limit_splits_without_truncation(self):
        messages=[{'role':'user','content':'x'*3000,'timestamp':None} for _ in range(7)]
        for provider in ['openai','deepseek']:
            batches=cues.complete_message_batches(messages,provider=provider)
            self.assertGreater(len(batches),1)
            self.assertEqual([m['index'] for b in batches for m in b],list(range(7)))
            for batch in batches:
                self.assertLessEqual(len(cues.encode(cues.request('add',{'messages':batch},provider=provider))),16384)
                self.assertTrue(all(m['content']=='x'*3000 for m in batch))

    def test_completed_replay_and_changed_input_no_model_calls(self):
        self.service.official_add(payload())
        changed=payload();changed['messages'][0]['content']='changed'
        self.service.official_add(changed)
        self.assertEqual(len(self.provider.calls),2)
        with self.core.db.connection() as con:
            self.assertEqual(con.execute('SELECT content FROM messages ORDER BY ts_ms LIMIT 1').fetchone()[0],'original statement 0')

    def test_model_ready_recovery_reuses_all_completed_chunks(self):
        original=self.core.official_add
        self.core.official_add=lambda _: (_ for _ in ()).throw(RuntimeError('synthetic raw failure'))
        with self.assertRaises(ApiError):self.service.official_add(payload())
        self.assertEqual(self.service.cue_db.execute('SELECT status FROM aml_cue_attempts').fetchone()[0],'model_ready')
        self.core.official_add=original
        self.service.official_add(payload())
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.count('messages'),20)

    def test_deletion_during_first_call_prevents_second_call(self):
        self.provider.callback=lambda n:self.service.delete_user('u') if n==1 else None
        with self.assertRaises(ApiError):self.service.official_add(payload())
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.count('messages'),0)
        self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM aml_cue_attempts').fetchone()[0],0)

    def test_delete_clears_all_chunks_and_core(self):
        self.service.official_add(payload());self.service.delete_user('u')
        self.assertEqual(self.count('messages'),0)
        for table in ['aml_cue_index','aml_cue_fts','aml_cue_attempts']:
            self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_default_remains_bounded_v11(self):
        core=MemoryService(RetrieverConfig(db_path=':memory:'));provider=Provider()
        service=cues.SemanticCueService(core,provider,enabled=True)
        try:
            service.official_add(payload())
            self.assertEqual(len(provider.calls),1)
            self.assertEqual(len(json.loads(provider.calls[0]['messages'][1]['content'])['messages']),12)
            self.assertEqual(service.protocol,cues.PROTOCOL)
        finally:service.close()

    def test_ambiguous_messages_may_be_omitted_without_invented_keywords(self):
        self.provider.omit=True
        self.service.official_add(payload())
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.count('messages'),20)
        self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM aml_cue_index').fetchone()[0],0)

    def test_profile_separation_and_restart_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            config=RetrieverConfig(db_path=str(Path(directory)/'raw.sqlite'))
            sidecar=str(Path(directory)/'cues.sqlite');provider=Provider('deepseek')
            service=cues.SemanticCueService(MemoryService(config),provider,enabled=True,provider='deepseek',
                cue_db_path=sidecar,complete_add_coverage=True)
            self.assertEqual(service.protocol,cues.COMPLETE_DEEPSEEK_PROTOCOL)
            service.official_add(payload());service.close()
            core=MemoryService(config)
            try:
                with self.assertRaisesRegex(ValueError,'profile_mismatch'):
                    cues.SemanticCueService(core,provider,enabled=True,provider='deepseek',cue_db_path=sidecar)
            finally:core.close()
            service=cues.SemanticCueService(MemoryService(config),provider,enabled=True,provider='deepseek',
                cue_db_path=sidecar,complete_add_coverage=True)
            try:
                service.official_add(payload())
                self.assertEqual(len(provider.calls),2)
            finally:service.close()

    def test_index_field_cannot_override_original_position(self):
        batch=cues.complete_message_batches([{'role':'user','content':'test','index':99}])
        self.assertEqual(batch[0][0]['index'],0)


if __name__=='__main__':unittest.main()
