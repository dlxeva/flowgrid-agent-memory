"""Offline injected-provider checks against a75c's real core, no API calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import threading
import tempfile
import unittest
import urllib.request
from unittest.mock import patch
from types import SimpleNamespace
from aml_retriever.api import MemoryService,ApiError
from aml_retriever.config import RetrieverConfig
from aml_retriever.aml_hosted import HostedConfig

try:
    from aml_retriever import aml_semantic_cues as cues
except ImportError:
    PATH=Path(__file__).resolve().parents[1]/'aml_retriever/aml_semantic_cues.py'
    spec=importlib.util.spec_from_file_location('aml_retriever.aml_semantic_cues',PATH)
    cues=importlib.util.module_from_spec(spec);sys.modules[spec.name]=cues;spec.loader.exec_module(cues)

def raw(value):
    return json.dumps({'model':cues.MODEL,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value)}}],
        'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}}).encode()

class Provider:
    def __init__(self):self.calls=[];self.add={'cues':[{'index':0,'keywords':['vegan','diet','plant based']}]};self.search={'queries':['food preference']};self.error=False
    def __call__(self,r):
        self.calls.append(copy.deepcopy(r))
        if self.error:raise TimeoutError()
        stage=r['response_format']['json_schema']['name']
        return raw(self.add if stage=='aml_add_cues' else self.search)

class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.core=MemoryService(RetrieverConfig(db_path=':memory:'));self.provider=Provider()
        self.service=cues.SemanticCueService(self.core,self.provider,enabled=True)
        self.add={'request_id':'r','user_id':'u','session_id':'s','messages':[{'role':'user','content':'I avoid all animal-derived ingredients.','timestamp':1704067200000}]}
        self.search={'query':'diet','user_id':'u','top_k':5}
    def tearDown(self):self.service.close()

    def test_raw_evidence_and_no_confirmation(self):
        self.service.official_add(self.add)
        self.assertEqual(self.core.official_search(self.search)['data'],[])
        result=self.service.official_search(self.search)['data'];self.assertEqual(len(result),1)
        self.assertEqual(result[0]['content'],self.add['messages'][0]['content']);self.assertNotIn('vegan',result[0]['content'])
        with self.core.db.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM memory_records').fetchone()[0],0)

    def test_repeat_does_not_call_model_or_change_source(self):
        self.service.official_add(self.add)
        second=copy.deepcopy(self.add);second['messages'][0]['content']='different'
        self.service.official_add(second);self.assertEqual(len(self.provider.calls),1)
        with self.core.db.connection() as con:self.assertEqual(con.execute('SELECT content FROM messages').fetchone()[0],self.add['messages'][0]['content'])

    def test_timeout_unknown_no_retry(self):
        self.provider.error=True
        for _ in range(2):
            with self.assertRaises(ApiError):self.service.official_add(self.add)
        self.assertEqual(len(self.provider.calls),1)
        with self.core.db.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_out_of_range_and_governance_fields_rejected(self):
        self.provider.add={'cues':[{'index':99,'keywords':['diet']}]}
        with self.assertRaises(ApiError):self.service.official_add(self.add)
        with self.core.db.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        self.add['request_id']='new';self.provider.add={'cues':[],'confirmed':True}
        with self.assertRaises(ApiError):self.service.official_add(self.add)

    def test_isolation_and_delete_cascade(self):
        self.service.official_add(self.add)
        self.assertEqual(self.service.official_search({**self.search,'user_id':'other'})['data'],[])
        self.service.delete_user('u')
        for table in ['aml_cue_index','aml_cue_fts','aml_cue_attempts']:self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.service.official_search(self.search)['data'],[])

    def test_changed_source_hash_rejected(self):
        self.service.official_add(self.add)
        # Raw evidence has its own immutability trigger. Corrupt only this
        # disposable adapter index hash to exercise use-time provenance checks.
        with self.service.cue_db:self.service.cue_db.execute("UPDATE aml_cue_index SET source_hash='invalid' WHERE user_id='u'")
        self.assertEqual(self.service.official_search(self.search)['data'],[])

    def test_protocol_bounds_and_duplicate_json(self):
        r=cues.request('search',{'query':'example','options':[]})
        self.assertEqual(r['model'],cues.MODEL);self.assertFalse(r['store'])
        self.assertTrue(r['response_format']['json_schema']['strict'])
        with self.assertRaises(ValueError):cues.decode('{"queries":[],"queries":[]}')
        with self.assertRaises(ValueError):cues.strings(['x']*4,3)
        with self.assertRaises(ValueError):cues.request('search',{'query':'x'*20000})

    def test_openai_bounds_match_local_validator_without_truncation(self):
        wire=cues.request('add',{'messages':[{'index':0,'content':'synthetic source'}]})
        prompt=wire['messages'][0]['content']
        self.assertIn('at most 8 distinct keywords',prompt)
        self.assertIn('Never repeat indices or keywords',prompt)
        self.assertIn('Only use supplied indices',prompt)
        self.assertIn('160 characters and 480 UTF-8 bytes',prompt)
        self.assertTrue(wire['response_format']['json_schema']['strict'])
        self.provider.add={'cues':[{'index':0,'keywords':['k'+str(i) for i in range(8)]}]}
        self.service.official_add(self.add)
        self.provider.add={'cues':[{'index':0,'keywords':['k'+str(i) for i in range(9)]}]}
        other={**self.add,'request_id':'over-limit'}
        with self.assertRaises(ApiError):self.service.official_add(other)
        with self.core.db.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM messages WHERE request_id=?',('over-limit',)).fetchone()[0],0)
        self.assertEqual(self.service.cue_db.execute('SELECT status FROM aml_cue_attempts WHERE request_id=?',('over-limit',)).fetchone()[0],'unknown')

    def test_openai_query_bounds_are_stated_and_enforced(self):
        prompt=cues.request('search',{'query':'diet','options':None})['messages'][0]['content']
        self.assertIn('at most 3 distinct queries',prompt)
        self.assertIn('160 characters and 480 UTF-8 bytes',prompt)
        self.provider.search={'queries':['q'+str(i) for i in range(4)]}
        with self.assertRaises(ApiError):self.service.official_search(self.search)

    def test_openai_v11_rejects_old_sidecar_without_silent_upgrade(self):
        self.assertEqual(cues.PROTOCOL,'aml-semantic-cues/v1.1')
        with tempfile.TemporaryDirectory() as directory:
            config=RetrieverConfig(db_path=str(Path(directory)/'raw.sqlite'))
            sidecar=str(Path(directory)/'cues.sqlite')
            core=MemoryService(config)
            service=cues.SemanticCueService(core,self.provider,enabled=True,cue_db_path=sidecar)
            with service.cue_db:
                service.cue_db.execute('UPDATE aml_cue_profile SET protocol=?',('aml-semantic-cues/v1',))
            service.close();core=MemoryService(config)
            try:
                with self.assertRaisesRegex(ValueError,'profile_mismatch'):
                    cues.SemanticCueService(core,self.provider,enabled=True,cue_db_path=sidecar)
            finally:core.close()

    def test_whole_message_selection(self):
        messages=[{'role':'user','content':'x'*20000,'timestamp':None},{'role':'user','content':'short','timestamp':None}]
        self.assertEqual([m['index'] for m in cues.select_messages(messages)],[1])

    def test_unknown_deletion(self):
        self.provider.error=True
        with self.assertRaises(ApiError):self.service.official_add(self.add)
        self.service.delete_user('u')
        self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM aml_cue_attempts').fetchone()[0],0)

    def test_default_disabled_and_existing_db_rejected(self):
        with self.assertRaises(ValueError):cues.SemanticCueService(self.core,self.provider)
        core=MemoryService(RetrieverConfig(db_path=':memory:'))
        try:
            core.official_add(self.add)
            with self.assertRaises(ValueError):cues.SemanticCueService(core,self.provider,enabled=True)
        finally:core.close()

    def test_real_http_injected_host(self):
        # Core is still empty. Reuse the same adapter tables, no credentials from env.
        server=cues.create_hosted_server(HostedConfig(credential='synthetic-secret',db_path=':memory:',port=0),self.core,self.provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            for path,payload in [('/add',self.add),('/search',self.search)]:
                req=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer synthetic-secret','Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:body=json.load(response)
            self.assertEqual(body['data'][0]['content'],self.add['messages'][0]['content'])
            with urllib.request.urlopen(base+'/health',timeout=5) as response:health=json.load(response)
            self.assertEqual(health['semantic_cues_model'],cues.MODEL)
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_transport_budget_denial_never_spawns(self):
        def deny(*_):raise ValueError('scope not approved')
        t=cues.OpenAICueTransport('synthetic',reserve=deny,settle=lambda *_:None,unknown=lambda *_:None,enabled=True)
        with patch.object(cues.subprocess,'run') as run:
            with self.assertRaises(ValueError):t(cues.request('search',{'query':'diet','options':None}))
            run.assert_not_called()

    def test_transport_success_and_failure_receipts(self):
        records=[]
        t=cues.OpenAICueTransport('synthetic',reserve=lambda *_:'attempt',settle=lambda *x:records.append(('settled',x)),unknown=lambda *x:records.append(('unknown',x)),enabled=True)
        req=cues.request('search',{'query':'diet','options':None})
        with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=raw({'queries':[]}))) as run:
            t(req);self.assertEqual(run.call_count,1);self.assertEqual(records[0][0],'settled')
            self.assertEqual(run.call_args.kwargs['timeout'],25)
        with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=2,stdout=b'')) as run:
            with self.assertRaises(ApiError):t(req)
            self.assertEqual(run.call_count,1);self.assertEqual(records[-1][0],'unknown')
        bad=copy.deepcopy(req);bad['model']='deepseek-flash'
        with patch.object(cues.subprocess,'run') as run:
            with self.assertRaises(ValueError):t(bad)
            run.assert_not_called()

    def test_persistent_restart_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            config=RetrieverConfig(db_path=str(Path(directory)/'memory.sqlite'))
            sidecar=str(Path(directory)/'cues.sqlite')
            core=MemoryService(config);service=cues.SemanticCueService(core,self.provider,enabled=True,cue_db_path=sidecar)
            service.official_add(self.add);service.close()
            reopened=MemoryService(config)
            try:
                service=cues.SemanticCueService(reopened,self.provider,enabled=True,cue_db_path=sidecar)
                service.official_add(self.add);self.assertEqual(len(self.provider.calls),1)
                self.assertEqual(service.official_search(self.search)['data'][0]['content'],self.add['messages'][0]['content'])
            finally:service.close()

    def test_atomic_raw_and_cue_rollback(self):
        original=self.core.db._add_locked
        def fail_after_raw(*args):
            original(*args);raise RuntimeError('synthetic commit failure')
        with patch.object(self.core.db,'_add_locked',side_effect=fail_after_raw):
            with self.assertRaises(ApiError):self.service.official_add(self.add)
        with self.core.db.connection() as con:
            for table in ['messages','raw_events']:self.assertEqual(con.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        for table in ['aml_cue_index','aml_cue_fts']:self.assertEqual(self.service.cue_db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.service.official_add(self.add)  # Known model response is recovered.
        self.assertEqual(len(self.provider.calls),1)

    def test_truncated_and_wrong_model_completions(self):
        body=json.loads(raw({'queries':[]}));body['choices'][0]['finish_reason']='length'
        with self.assertRaises(ValueError):cues.completion(json.dumps(body).encode())
        body['choices'][0]['finish_reason']='stop';body['model']='deepseek-flash'
        with self.assertRaises(ValueError):cues.completion(json.dumps(body).encode())

    def test_existing_unowned_request_refuses_before_model(self):
        self.core.official_add(self.add)
        with self.assertRaises(ApiError):self.service.official_add(self.add)
        self.assertEqual(self.provider.calls,[])

class DeepSeekTests(unittest.TestCase):
    def wire(self,stage='search'):
        return cues.request(stage,{'query':'diet','options':None} if stage=='search' else {'messages':[]},provider='deepseek')

    def response(self,value):
        body=json.loads(raw(value));body['model']=cues.DEEPSEEK_MODEL
        return json.dumps(body).encode()

    def test_explicit_protocol_and_default_unchanged(self):
        r=self.wire();self.assertEqual(r['model'],'deepseek-flash')
        self.assertEqual(r['response_format'],{'type':'json_object'})
        self.assertEqual(r['thinking'],{'type':'disabled'})
        self.assertEqual(r['max_tokens'],768)
        self.assertNotIn('store',r);self.assertNotIn('max_completion_tokens',r)
        self.assertIn('JSON',r['messages'][0]['content'])
        self.assertEqual(cues.request('search',{'query':'diet'})['model'],cues.MODEL)
        with self.assertRaises(ValueError):cues.request('search',{},provider='other')

    def test_decoder_never_relabels(self):
        response=self.response({'queries':[]})
        cues.completion(response,provider='deepseek')
        with self.assertRaises(ValueError):cues.completion(response)
        with self.assertRaises(ValueError):cues.completion(raw({'queries':[]}),provider='deepseek')
        body=json.loads(response);body['choices'][0]['message']['content']=''
        with self.assertRaises(ValueError):cues.completion(json.dumps(body).encode(),provider='deepseek')

    def test_transport_scope_and_no_fallback(self):
        records=[]
        t=cues.DeepSeekCueTransport('synthetic',reserve=lambda *x:records.append(('reserve',x)) or 'slot',settle=lambda *x:records.append(('settle',x)),unknown=lambda *x:records.append(('unknown',x)),enabled=True)
        with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=self.response({'queries':[]}))) as run:
            t(self.wire())
            self.assertEqual(records[0][1][1],cues.DEEPSEEK_MODEL)
            packet=json.loads(run.call_args.kwargs['input']);self.assertEqual(packet['provider'],'deepseek')
            self.assertEqual(packet['request'],self.wire());self.assertEqual(run.call_count,1)
        with patch.object(cues.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=raw({'queries':[]}))) as run:
            with self.assertRaises(ApiError):t(self.wire())
            self.assertEqual(run.call_count,1);self.assertEqual(records[-1][0],'unknown')
        with patch.object(cues.subprocess,'run') as run:
            with self.assertRaises(ValueError):t(cues.request('search',{'query':'diet','options':None}))
            run.assert_not_called()
        def deny(*_):raise ValueError('not authorized')
        t=cues.DeepSeekCueTransport('synthetic',reserve=deny,settle=lambda *_:None,unknown=lambda *_:None,enabled=True)
        with patch.object(cues.subprocess,'run') as run:
            with self.assertRaises(ValueError):t(self.wire())
            run.assert_not_called()
        with self.assertRaises(ValueError):cues.DeepSeekCueTransport('synthetic',reserve=deny,settle=deny,unknown=deny)

    def test_real_http_and_raw_only(self):
        calls=[]
        def provider(r):
            calls.append(r)
            data=json.loads(r['messages'][1]['content'])
            return self.response({'cues':[{'index':0,'keywords':['diet']}]} if 'messages' in data else {'queries':[]})
        core=MemoryService(RetrieverConfig(db_path=':memory:'))
        server=cues.create_hosted_server(HostedConfig(credential='synthetic-secret',db_path=':memory:',port=0),core,provider,provider='deepseek')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        content='I avoid animal-derived ingredients.'
        try:
            for path,payload in [('/add',{'user_id':'u','session_id':'s','request_id':'r','messages':[{'role':'user','content':content}]}),('/search',{'user_id':'u','query':'diet','top_k':5})]:
                req=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer synthetic-secret','Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:body=json.load(response)
            self.assertEqual(body['data'][0]['content'],content)
            with urllib.request.urlopen(base+'/health',timeout=5) as response:health=json.load(response)
            self.assertEqual(health['semantic_cues_provider'],'deepseek')
            self.assertEqual(health['semantic_cues_model'],cues.DEEPSEEK_MODEL)
            with core.db.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM memory_records').fetchone()[0],0)
            self.assertEqual(len(calls),2)
        finally:server.shutdown();server.server_close();thread.join(5);core.close()

    def test_sidecar_rejects_provider_switch_on_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            config=RetrieverConfig(db_path=str(Path(directory)/'raw.sqlite'));sidecar=str(Path(directory)/'cues.sqlite')
            core=MemoryService(config);service=cues.SemanticCueService(core,lambda _:None,enabled=True,cue_db_path=sidecar,provider='deepseek');service.close()
            core=MemoryService(config)
            try:
                with self.assertRaises(ValueError):cues.SemanticCueService(core,lambda _:None,enabled=True,cue_db_path=sidecar)
                service=cues.SemanticCueService(core,lambda _:None,enabled=True,cue_db_path=sidecar,provider='deepseek')
                service.cue_db.close()
            finally:core.close()

    def test_governance_injection_rejected_without_source_write(self):
        core=MemoryService(RetrieverConfig(db_path=':memory:'))
        service=cues.SemanticCueService(core,lambda _:self.response({'cues':[],'confirmed':True}),enabled=True,provider='deepseek')
        payload={'request_id':'r','user_id':'u','session_id':'s','messages':[{'role':'user','content':'hello'}]}
        try:
            with self.assertRaises(ApiError):service.official_add(payload)
            with core.db.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            self.assertEqual(service.cue_db.execute('SELECT model,status FROM aml_cue_attempts').fetchone()[:],(cues.DEEPSEEK_MODEL,'unknown'))
        finally:service.close()

    def test_v11_bounds_are_stated_without_weakening_validation(self):
        wire=cues.request('add',{'messages':[{'index':0,'content':'synthetic source'}]},provider='deepseek')
        prompt=wire['messages'][0]['content']
        self.assertIn('at most 8 distinct keywords',prompt)
        self.assertIn('"maxItems":8',prompt)
        self.assertIn('480 UTF-8 bytes',prompt)
        self.assertIn('Never repeat indices or keywords',prompt)
        core=MemoryService(RetrieverConfig(db_path=':memory:'))
        # Reproduce the real failure shape with synthetic text: ten keywords.
        response=self.response({'cues':[{'index':0,'keywords':['k'+str(i) for i in range(10)]}]})
        service=cues.SemanticCueService(core,lambda _:response,enabled=True,provider='deepseek')
        try:
            with self.assertRaises(ApiError):service.official_add({'user_id':'u','session_id':'s','request_id':'r','messages':[{'role':'user','content':'synthetic source'}]})
            with core.db.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        finally:service.close()

    def test_v11_rejects_old_v1_sidecar_without_silent_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            config=RetrieverConfig(db_path=str(Path(directory)/'raw.sqlite'));sidecar=str(Path(directory)/'cues.sqlite')
            core=MemoryService(config);service=cues.SemanticCueService(core,lambda _:None,enabled=True,cue_db_path=sidecar,provider='deepseek')
            with service.cue_db:service.cue_db.execute('UPDATE aml_cue_profile SET protocol=?',(cues.PROTOCOL,))
            service.close();core=MemoryService(config)
            try:
                with self.assertRaisesRegex(ValueError,'profile_mismatch'):cues.SemanticCueService(core,lambda _:None,enabled=True,cue_db_path=sidecar,provider='deepseek')
            finally:core.close()

if __name__=='__main__':unittest.main()
