"""Fixed candidate tests: only synthetic storage, no function overlay."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aml_retriever import FlowGridMemory, governance

class FixedMetadataClockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='fg-fixed-clock-')
        self.addCleanup(directory.cleanup)
        self.memory = FlowGridMemory(db_path=str(Path(directory.name)/'synthetic.sqlite'))
        self.addCleanup(self.memory.close)
        self.memory.ingest_raw_events(user_id='synthetic', request_id='synthetic-source',
            session_id='synthetic-session', messages=[{'role':'user','content':'Synthetic evidence'}],
            trusted_scope={'project':'synthetic'})
        self.ids = [e.id for e in self.memory._active().list_raw_events('synthetic')]

    def create(self):
        options = dict(user_id='synthetic', source_event_ids=self.ids, status='candidate',
            created_by='fixture', authority='user', scope={'project':'synthetic'})
        self.memory.propose_memory(memory_key='decision.a', content='Synthetic a', **options)
        return self.memory.propose_memory(memory_key='decision.b', content='Synthetic b', **options)

    def lookup(self, record, *, user='synthetic', scope=None):
        return self.memory._active().transition_target_matches(user_id=user,
            record_id=record.id, memory_key=record.memory_key,
            scope=record.scope if scope is None else scope)

    def test_stopped_clock_target_remains_available(self):
        with patch.object(governance, 'utc_now', return_value='2026-01-01T00:00:00+00:00'):
            self.assertTrue(self.lookup(self.create()))

    def test_backward_clock_target_remains_available(self):
        with patch.object(governance, 'utc_now', return_value='2026-01-02T00:00:00+00:00'):
            record = self.create()
        with patch.object(governance, 'utc_now', return_value='2026-01-01T00:00:00+00:00'):
            self.assertTrue(self.lookup(record))

    def test_no_user_or_project_expansion(self):
        with patch.object(governance, 'utc_now', return_value='2026-01-01T00:00:00+00:00'):
            record = self.create()
            self.assertFalse(self.lookup(record, user='other', scope={'project':'synthetic'}))
            self.assertFalse(self.lookup(record, scope={'project':'other'}))
