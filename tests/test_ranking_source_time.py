"""Source chronology must not be manufactured from an ingestion clock."""
from datetime import datetime
import unittest
from unittest.mock import patch
from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB, _iso_from_ms

class RankingSourceTimeTests(unittest.TestCase):
    def pair(self, stamp, ingest='2040-01-01T00:00:00+00:00'):
        s=MemoryService(RetrieverConfig().with_flags(views=False,official_evidence_packing=True))
        self.addCleanup(s.close)
        for i,m in enumerate([
            {'role':'user','content':'Cedar project budget is 100 dollars.','timestamp':1767225600000},
            {'role':'user','content':'Cedar project budget updated to 200 dollars.','timestamp':stamp}]):
            with patch('aml_retriever.retriever._now_iso',return_value=ingest):
                s.official_add({'user_id':'u','session_id':str(i),'request_id':str(i),'messages':[m]})
        return s

    def test_missing_and_unrepresentable_times_have_no_temporal_rank_assertion(self):
        for stamp in (None,253402300800000):
            for ingest in ('2000-01-01T00:00:00+00:00','2040-01-01T00:00:00+00:00'):
                with self.subTest(stamp=stamp,ingest=ingest):
                    s=self.pair(stamp,ingest)
                    rows=s.search(user_id='u',query='Cedar project budget now',top_k=10).results
                    self.assertEqual(len(rows),2)
                    for r in rows:
                        self.assertIn('possible_conflict',r.evidence_flags)
                        self.assertFalse({'latest_state','supersedes_earlier','superseded'} & set(r.evidence_flags))
                        if 'updated' in r.content:self.assertNotIn('recency',r.evidence_flags)
                    packed=s.official_search({'user_id':'u','query':'Cedar project budget now','top_k':10})['data']
                    missing=next(r for r in packed if 'updated' in r['content'])
                    self.assertEqual(missing['source_spans'][0]['message_time'],'unknown')

    def test_valid_update_still_has_order_and_unique_latest(self):
        s=self.pair(1767312000000)
        rows=s.search(user_id='u',query='Cedar project budget now',top_k=10).results
        newer=next(r for r in rows if 'updated' in r.content)
        older=next(r for r in rows if 'updated' not in r.content)
        self.assertTrue({'latest_state','supersedes_earlier','recency'} <= set(newer.evidence_flags))
        self.assertIn('superseded',older.evidence_flags)

    def test_equal_instants_in_different_offsets_have_no_unique_latest(self):
        stamps=[int(datetime.fromisoformat(t).timestamp()*1000) for t in
                ['2026-01-01T00:00:00+00:00','2026-01-01T08:00:00+08:00']]
        self.assertEqual(stamps[0],stamps[1])
        s=self.pair(stamps[1])
        rows=s.search(user_id='u',query='Cedar project budget now',top_k=10).results
        self.assertEqual(len(rows),2)
        for r in rows:
            self.assertIn('possible_conflict',r.evidence_flags)
            self.assertFalse({'latest_state','supersedes_earlier','superseded'} & set(r.evidence_flags))

    def test_epoch_accepts_only_representable_integer_source_milliseconds(self):
        for value in (None,True,0.0,'0',253402300800000,-62135596800001,2**63-1):
            self.assertIsNone(RetrieverDB._epoch_of({'ts_ms':value,'created_at':'2040-01-01T00:00:00Z'}))
        self.assertEqual(RetrieverDB._epoch_of({'ts_ms':-1}),-.001)
        self.assertEqual(RetrieverDB._epoch_of({'ts_ms':0}),0)

    def test_aggregate_uses_first_original_timestamp_not_created_at(self):
        for stamp in (None,1767225600000):
            with self.subTest(stamp=stamp), MemoryService(RetrieverConfig()) as s:
                with patch('aml_retriever.retriever._now_iso',return_value='2040-01-01T00:00:00+00:00'):
                    s.official_add({'user_id':'u','session_id':'s','request_id':'r','messages':[
                        {'role':'user','content':'Cedar project budget phase one.','timestamp':stamp},
                        {'role':'user','content':'Cedar project budget phase two.','timestamp':1767312000000}]})
                with s.db.connection() as con:
                    views=con.execute('SELECT view_id,view_type FROM views WHERE user_id=?',('u',)).fetchall()
                    self.assertTrue(views)
                    loaded=s.db._load_records(con,'u',[(v[0],v[1],-1) for v in views])
                    source_times=dict(con.execute('SELECT id,ts_ms FROM messages WHERE user_id=?',('u',)))
                self.assertTrue(loaded)
                for row in loaded:
                    first_stamp=source_times[row['source_ids'][0]]
                    self.assertEqual(row['ts_ms'],first_stamp)
                    self.assertEqual(s.db._epoch_of(row),None if first_stamp is None else first_stamp/1000)
                # Wrong tenant cannot supply the first source timestamp.
                with s.db.connection() as con:
                    self.assertEqual(s.db._load_records(con,'other',[(v[0],v[1],-1) for v in views]),[])

    def test_created_at_conversion_keeps_exact_millisecond_boundaries(self):
        self.assertEqual(_iso_from_ms(253402300799999),'9999-12-31T23:59:59.999000+00:00')
        self.assertEqual(_iso_from_ms(-62135596800000),'0001-01-01T00:00:00+00:00')
        for value in (None,True,0.0,'0',253402300800000):
            self.assertIsNone(_iso_from_ms(value))
        with MemoryService(RetrieverConfig()) as s:
            s.official_add({'user_id':'u','session_id':'s','request_id':'r','messages':[
                {'role':'user','content':'Calendar upper boundary','timestamp':253402300799999}]})
            with s.db.connection() as con:
                stored=con.execute('SELECT created_at FROM messages').fetchone()[0]
            self.assertEqual(stored,'9999-12-31T23:59:59.999000+00:00')
