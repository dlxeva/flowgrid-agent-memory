"""Deterministic relevance-bounded diversity; no gold read by ranker."""
import copy
import unittest
from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig
from aml_retriever.api import MemoryService


def row(name, score, sources):
    return {'id':name,'score':score,'source_ids':sources,'flags':[],
            'content':'unchanged evidence '+name,'view':'window'}


class ComplementaryTests(unittest.TestCase):
    def test_complementary_source_beats_repeated_window(self):
        records=[row('first',1,['a','b']),row('repeat',.99,['a','b']),
                 row('new',.95,['c']),row('irrelevant',.2,['d'])]
        result=RetrieverDB._complementary_order(copy.deepcopy(records))
        self.assertEqual([r['id'] for r in result],['first','new','repeat','irrelevant'])
        self.assertEqual(sorted(r['score'] for r in result),sorted(r['score'] for r in records))
        self.assertEqual({r['id']:r['content'] for r in result},{r['id']:r['content'] for r in records})

    def test_low_relevance_is_never_promoted(self):
        records=[row('first',1,['a']),row('repeat',.99,['a']),row('below',.89,['b'])]
        self.assertEqual([r['id'] for r in RetrieverDB._complementary_order(records)],['first','repeat','below'])

    def test_empty_sources_and_nonpositive_scores_are_safe(self):
        for records in ([],[row('one',0,['a'])],[row('one',-1,['a']),row('two',-2,['b'])]):
            self.assertEqual(RetrieverDB._complementary_order(records),records)
        self.assertEqual(len(RetrieverDB._complementary_order([row('a',1,[]),row('b',.99,[])])),2)

    def test_prefix_bound_and_complete_candidate_set(self):
        records=[row(str(i),1,['shared']) for i in range(201)]
        records[-1]['source_ids']=['outside']
        result=RetrieverDB._complementary_order(copy.deepcopy(records))
        self.assertEqual(result[-1]['id'],'200')
        self.assertEqual({r['id'] for r in result},{r['id'] for r in records})

    def test_real_search_contract_retains_raw_text_and_user_scope(self):
        config=RetrieverConfig().with_flags(complementary_sources=True)
        with MemoryService(config) as service:
            for i in range(3):
                service.official_add({'request_id':str(i),'user_id':'scope-user','session_id':'s'+str(i),
                    'messages':[{'role':'user','content':'Comet tea evidence '+str(i)}]})
            q={'query':'comet tea','user_id':'scope-user','top_k':100}
            response=service.official_search(q)
            self.assertTrue(response['data'])
            self.assertLessEqual(len(response['data']),100)
            self.assertEqual(response,service.official_search(q))
            self.assertEqual(service.official_search({**q,'user_id':'other'}),{'data':[]})
            self.assertEqual(service.db.delete_user('scope-user')['deleted_messages'],3)
            self.assertEqual(service.official_search(q),{'data':[]})
