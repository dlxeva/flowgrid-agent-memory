"""Direct baseline provenance-field checks; Git-present branch is mocked."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from aml_retriever.evaluation import governance_suite as suite


class GovernanceProvenanceFixedTests(unittest.TestCase):
    def execute(self):
        fixture=suite.load_manifest().data['fixtures']['legacy_floor']
        with tempfile.TemporaryDirectory(prefix='governance-provenance-fixed-') as temp:
            return suite._op_legacy_floor(fixture,suite.EvaluationRuntime(repo_root=Path(temp),manifest_path=suite.DEFAULT_MANIFEST_PATH))

    def test_actual_git_absence_reports_reviewed_embedded_baseline(self):
        result=self.execute();self.assertEqual(result['status'],'pass')
        self.assertEqual(result['baseline_comparison']['source_verification'],'reviewed_embedded_attestation')
        self.assertIs(result['baseline_comparison']['source_commit_object_verified'],False)

    def test_mocked_git_present_branch_is_reported(self):
        with mock.patch.object(suite,'_git',side_effect=['',suite.LEGACY_BASELINE_TREE]):result=self.execute()
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['baseline_comparison']['source_verification'],'local_git_object')
        self.assertIs(result['baseline_comparison']['source_commit_object_verified'],True)
