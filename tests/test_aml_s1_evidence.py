"""S1 candidate boundary probes, not a QA gain claim."""
import json
import threading
import unittest
import urllib.request
from aml_retriever.api import MemoryService, ApiError
from aml_retriever.config import RetrieverConfig
from aml_retriever.aml_hosted import HostedConfig, HostedAMLServer
from aml_retriever.retriever import RetrieverDB


def fixture():
    return {'request_id':'source-fixture','user_id':'fixture-user','session_id':'fixture-session',
        'messages':[
        {'role':'user','content':'Alex likes mint tea, not coffee.', 'timestamp':1704067200000},
        {'role':'assistant','content':'Alex likes mint tea, not coffee.', 'timestamp':1704153600000},
        {'role':'user','content':'Update: Alex no longer likes mint tea.'}]}


class S1EvidenceTests(unittest.TestCase):
    def service(self, **flags):
        s = MemoryService(RetrieverConfig().with_flags(**flags))
        self.addCleanup(s.close)
        s.official_add(fixture())
        return s

    def test_default_wire_unchanged(self):
        s = self.service()
        q = {'query':'mint tea','user_id':'fixture-user','top_k':100}
        raw = s.search(user_id=q['user_id'], query=q['query'], top_k=100)
        self.assertEqual([r['content'] for r in s.official_search(q)['data']],
                         [r.content for r in raw.results])

    def test_same_text_different_sources_are_preserved(self):
        records = [{'id':str(i),'content':'Same words','view':'message', 'source_ids':[str(i)]}
                   for i in range(2)]
        self.assertEqual(len(RetrieverDB._dedup(records)), 1)
        self.assertEqual(len(RetrieverDB._dedup(records, source_aware=True)), 2)

    def test_exact_aggregate_duplicates_do_not_fill_slots(self):
        rows = [{'id':'w1','content':'user: tea','view':'window','source_ids':['m1']},
                {'id':'s1','content':'user: tea','view':'session-segment','source_ids':['m1']},
                {'id':'w2','content':'user: tea','view':'window','source_ids':['m2']}]
        self.assertEqual([r['id'] for r in RetrieverDB._dedup(rows,source_aware=True)], ['w1','w2'])

    def test_labels_preserve_role_time_negation_and_unknown(self):
        s = self.service(official_source_labels=True, source_aware_dedup=True)
        data = s.official_search({'query':'mint tea','user_id':'fixture-user','top_k':100})['data']
        content = '\n'.join(r['content'] for r in data)
        for value in ('role="user"','role="assistant"','2024-01-01','2024-01-02',
                      'time="unknown"','not coffee','no longer likes mint tea'):
            self.assertIn(value, content)
        self.assertEqual(s.official_search({'query':'mint tea','user_id':'another-user','top_k':100}), {'data':[]})

    def test_erased_source_cannot_be_rendered_from_cached_evidence(self):
        s = self.service(official_source_labels=True)
        evidence = s.search(user_id='fixture-user',query='mint tea',top_k=100).results[0]
        s.db.delete_user('fixture-user')
        with self.assertRaisesRegex(ApiError, 'source unavailable'):
            s._evidence_to_official(evidence)

    def test_real_http_combined_profile(self):
        cfg = HostedConfig(credential='synthetic-s1-key',db_path=':memory:',port=0,
            retrieval_json=json.dumps({'flags':{'official_source_labels':True,'source_aware_dedup':True}}))
        server = HostedAMLServer(cfg,quiet=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def post(route, body):
            req = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+route,
                data=json.dumps(body).encode(),headers={'Authorization':'Bearer synthetic-s1-key',
                                                       'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=5) as response:
                return json.load(response)
        try:
            p=fixture()
            self.assertTrue(post('/add',p)['success'])
            self.assertTrue(post('/add',p)['success'])
            result=post('/search',{'query':'mint tea','user_id':'fixture-user','top_k':100})
            self.assertLessEqual(len(result['data']),100)
            self.assertIn('role="assistant"','\n'.join(r['content'] for r in result['data']))
            self.assertEqual(post('/search',{'query':'mint tea','user_id':'other-user','top_k':100}), {'data':[]})
            with server.service.db.connection() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM memory_records').fetchone()[0],0)
        finally:
            server.shutdown(); server.server_close(); server.service.close(); thread.join(5)
            self.assertFalse(thread.is_alive())
