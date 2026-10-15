"""Direct malformed-evaluation-input checks, no overlay or provider."""
import tempfile
from pathlib import Path
import unittest
from aml_retriever.evaluation import harness
from aml_retriever.evaluation.dataset import Dataset,Query,make_dataset


class EvaluationInputFixedTests(unittest.TestCase):
    def result(self,name='L5',flags=None,mrr=0.2):
        return harness.StageResult(name,{'exact':False} if flags is None else flags,overall={'mrr':mrr})

    def dataset(self,gold=None,distractors=None,user='u'):
        return Dataset(0,'synthetic','plain','classic',['u'],[{'user_id':'u','session_id':'s',
            'messages':[{'role':'user','content':'synthetic','timestamp':1760000000000}]}],
            [Query('q',user,'single_hop','plain','synthetic',['s#0'] if gold is None else gold,distractors or [])])

    def test_mismatched_stage_length_name_flags_order_and_duplicates_rejected(self):
        for rows in ([[self.result()],[self.result('L9')]],[[self.result()],[]],
                     [[self.result()],[self.result(),self.result('L9')]],
                     [[self.result()],[self.result(flags={'exact':True})]],
                     [[self.result(),self.result('L9')],[self.result('L9'),self.result()]],
                     [[self.result(),self.result()]]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):harness.aggregate_across_seeds(rows)

    def test_valid_aggregation_and_empty(self):
        result=harness.aggregate_across_seeds([[self.result()],[self.result(mrr=0.8)]])
        self.assertEqual(result[0]['overall']['mrr'],{'mean':0.5,'min':0.2,'max':0.8,'n':2})
        self.assertEqual(harness.aggregate_across_seeds([]),[])

    def test_missing_cross_user_and_duplicate_sources_rejected(self):
        duplicate=self.dataset();duplicate.sessions.append(dict(duplicate.sessions[0]))
        for dataset in (self.dataset(gold=['missing#0']),self.dataset(distractors=['s#99']),self.dataset(user='other'),duplicate):
            with self.assertRaises(ValueError):harness._validate_dataset_references(dataset)

    def test_valid_and_absent_queries_and_generated_suites(self):
        harness._validate_dataset_references(self.dataset());harness._validate_dataset_references(self.dataset(gold=[]))
        for seed in (1,20260806,31):
            for suite in ('classic','v11','relative_time'):
                harness._validate_dataset_references(make_dataset(seed=seed,scale='smoke',suite=suite))

    def test_invalid_stage_refused_before_file_creation(self):
        with tempfile.TemporaryDirectory(prefix='evaluation-input-fixed-') as temp:
            with self.assertRaises(ValueError):harness.run_stage('synthetic',{},self.dataset(gold=['missing#0']),workdir=temp)
            self.assertEqual(list(Path(temp).iterdir()),[])
