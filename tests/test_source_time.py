import unittest
from aml_retriever.source_time import source_message_time
from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.aml_semantic_cues import source_text

class SourceTimeTests(unittest.TestCase):
    def test_exact_millisecond_calendar_boundaries(self):
        self.assertEqual(source_message_time(0),'1970-01-01T00:00:00+00:00')
        self.assertEqual(source_message_time(-1),'1969-12-31T23:59:59.999000+00:00')
        self.assertEqual(source_message_time(-62135596800000),'0001-01-01T00:00:00+00:00')
        self.assertEqual(source_message_time(253402300799999),'9999-12-31T23:59:59.999000+00:00')
        for x in (None,True,'0',0.0,253402300800000,-62135596800001,2**63-1):
            self.assertEqual(source_message_time(x),'unknown')
    def test_cue_never_uses_created_at_as_source_time(self):
        row={'id':'m','role':'user','content':'original','created_at':'2040-01-01','ts_ms':253402300800000,'session_id':'s','seq':0}
        self.assertIn('"time": "unknown"',source_text(row))
        self.assertNotIn('2040',source_text(row))
        row['ts_ms']=0
        self.assertIn('1970-01-01',source_text(row))
    def test_real_packing_and_labels_unknown_for_unrenderable_original(self):
        for packing in (False,True):
            with MemoryService(RetrieverConfig().with_flags(official_source_labels=True,official_evidence_packing=packing)) as s:
                s.official_add({'user_id':'u','request_id':'r','session_id':'s','messages':[
                    {'role':'user','content':'Quartz clock evidence','timestamp':253402300800000}]})
                rows=s.official_search({'user_id':'u','query':'Quartz clock','top_k':5})['data']
                self.assertTrue(rows)
                for r in rows:
                    self.assertIn('="unknown"',r['content'])
                    if packing:self.assertTrue(all(x['message_time']=='unknown' for x in r['source_spans']))
