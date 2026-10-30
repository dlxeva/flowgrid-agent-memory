import json
import threading
import unittest
import urllib.request

from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.aml_semantic_cues import SemanticCueService, MODEL
from aml_retriever.aml_hosted import HostedConfig, HostedAMLServer


class ServiceReferenceExpansionTests(unittest.TestCase):
    def test_chinese_adjacent_reference_preserves_identifier_boundaries(self):
        from aml_retriever.reference_expansion import references
        self.assertEqual(references('工单CW-731正式生效；文档CW-842归本项目。'),
                         {'CW-731', 'CW-842'})
        self.assertEqual(references('xCW-731 CW-731x _CW-731 CW-731_ 9CW-731'), set())
        core = MemoryService(RetrieverConfig().with_flags(
            views=False, official_evidence_packing=True,
            experimental_reference_expansion=True))
        self.addCleanup(core.close)
        for i, text in enumerate(['青禾审批的唯一修订工单CW-731归本项目。',
                                  'CW-731正式生效：审计员必须在场。']):
            core.official_add({'user_id': 'u', 'session_id': str(i),
                               'request_id': str(i),
                               'messages': [{'role': 'user', 'content': text}]})
        rows = core.official_search({'user_id': 'u', 'query': '青禾审批', 'top_k': 100})['data']
        self.assertIn('审计员必须在场', '\n'.join(r['content'] for r in rows))

    def test_semantic_finalization_opt_in_and_no_added_provider_calls(self):
        core = MemoryService(RetrieverConfig().with_flags(views=False, official_evidence_packing=True))
        self.addCleanup(core.close)
        calls=[]
        def transport(wire):
            calls.append(wire)
            return json.dumps({'model':MODEL,'choices':[{'finish_reason':'stop','message':{
                'role':'assistant','content':json.dumps({'queries':['cobalt kettle']})}}],
                'usage':{'prompt_tokens':10,'completion_tokens':10}}).encode()
        service=SemanticCueService(core,transport,enabled=True)
        self.addCleanup(service.cue_db.close)
        for user, text in [('u', 'The cobalt kettle return uses RMA-88213.'),
                           ('u', 'RMA-88213 destination corrected to Cedar Road.'),
                           ('foreign', 'RMA-88213 destination is private foreign address.')]:
            core.official_add({'user_id':user,'session_id':text,'request_id':text,
                               'messages':[{'role':'user','content':text}]})
        query={'user_id':'u','query':'cobalt kettle','top_k':100}
        baseline=service.official_search(query)['data']
        self.assertNotIn('Cedar Road','\n'.join(x['content'] for x in baseline))
        core.config.flags['experimental_reference_expansion']=True
        candidate=service.official_search(query)['data']
        self.assertEqual(len(calls),2)  # one existing search call per invocation
        self.assertEqual(candidate[:len(baseline)],baseline)
        self.assertIn('Cedar Road','\n'.join(x['content'] for x in candidate))
        self.assertNotIn('foreign address','\n'.join(x['content'] for x in candidate))
        for row in candidate:
            for span in row['source_spans']:
                self.assertTrue(row['content'][span['start']:span['end']])
        core.config.top_k_max=3
        small=service.official_search({**query,'top_k':3})['data']
        self.assertIn('Cedar Road','\n'.join(x['content'] for x in small))
        core.config.top_k_max=100
        capped=service.official_search({**query,'top_k':1})['data']
        self.assertLessEqual(len(capped),1)
        self.assertIn('Cedar Road','\n'.join(x['content'] for x in capped))
        core.config.flags['official_evidence_packing']=False
        self.assertNotIn('Cedar Road','\n'.join(x['content'] for x in service.official_search(query)['data']))
        service.delete_user('u')
        self.assertEqual(service.official_search(query),{'data':[]})

    def test_http_flag_configuration_and_erasure(self):
        server=HostedAMLServer(HostedConfig(credential='synthetic-reference-test',db_path=':memory:',port=0,
            retrieval_json=json.dumps({'flags':{'views':False,'official_evidence_packing':True,
                                                'experimental_reference_expansion':True}})),quiet=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(path,body):
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                data=json.dumps(body).encode(),headers={'Content-Type':'application/json',
                    'Authorization':'Bearer synthetic-reference-test'})
            with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)
        try:
            for i,text in enumerate(['The violet lantern ticket is LAMP-472.',
                                     'LAMP-472 pickup moved to the west desk.']):
                post('/add',{'user_id':'u','session_id':str(i),'request_id':str(i),
                             'messages':[{'role':'user','content':text}]})
            query={'user_id':'u','query':'violet lantern','top_k':100}
            rows=post('/search',query)['data']
            self.assertIn('west desk','\n'.join(x['content'] for x in rows))
            self.assertEqual(post('/search',{**query,'user_id':'foreign'}),{'data':[]})
            server.service.delete_user('u')
            self.assertEqual(post('/search',query),{'data':[]})
        finally:
            server.shutdown();server.server_close();server.service.close();thread.join(5)
