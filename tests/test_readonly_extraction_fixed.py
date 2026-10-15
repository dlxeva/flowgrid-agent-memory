"""Portable synthetic file regression; no external extractor/provider."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aml_retriever.facade import FlowGridMemory
from aml_retriever.extraction import DirectiveMemoryExtractor


class ReadOnlyExtractionTests(unittest.TestCase):
    def test_all_write_surfaces_refuse_before_extractor_and_preserve_file(self):
        with tempfile.TemporaryDirectory(prefix="flowgrid-readonly-extract-") as directory:
            path = Path(directory) / "synthetic.sqlite"
            with FlowGridMemory(db_path=str(path)) as writer:
                receipt = writer.ingest_raw_events(
                    request_id="synthetic", user_id="synthetic-user", session_id="synthetic",
                    messages=[{"role": "user", "content": "SYNTHETIC_BODY"}],
                )
                record = writer.propose_memory(
                    user_id="synthetic-user", memory_key="synthetic.key", content="SYNTHETIC_BODY",
                    source_event_ids=receipt.raw_event_ids, status="candidate",
                    authority="user", created_by="synthetic",
                )
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with FlowGridMemory.open_read_only(db_path=str(path)) as reader:
                operations = (
                    lambda: reader.propose_memory(
                        user_id="synthetic-user", memory_key="synthetic.other", content="NO_WRITE",
                        source_event_ids=receipt.raw_event_ids, status="candidate",
                        authority="user", created_by="synthetic"),
                    lambda: reader.transition_memory(user_id="synthetic-user", record_id=record.id,
                        target_status="deleted", actor="synthetic", actor_authority="owner",
                        reason="Synthetic test only"),
                    lambda: reader.erase_user(user_id="synthetic-user", actor="synthetic",
                        actor_authority="owner", reason="Synthetic test only"),
                    lambda: reader.extract_candidates(user_id="synthetic-user",
                        raw_event_ids=receipt.raw_event_ids, idempotency_key="no-invocation",
                        extractor=DirectiveMemoryExtractor()),
                )
                with patch.object(DirectiveMemoryExtractor, "extract",
                    side_effect=AssertionError("extractor must not run")) as callback:
                    for index, operation in enumerate(operations):
                        with self.subTest(operation=index), self.assertRaises(PermissionError):
                            operation()
                    callback.assert_not_called()
                class IdentityTrap:
                    @property
                    def identity(self):
                        raise AssertionError("identity must not be touched")
                with self.assertRaises(PermissionError):
                    reader.extract_candidates(user_id="synthetic-user",
                        raw_event_ids=receipt.raw_event_ids, idempotency_key="no-identity",
                        extractor=IdentityTrap())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
