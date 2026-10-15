"""Portable malformed metadata regression; temporary synthetic SQLite only."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from aml_retriever.facade import FlowGridMemory
from aml_retriever.migrations import inspect_schema


class LongSchemaVersionTests(unittest.TestCase):
    def test_integer_conversion_limit_is_structured_and_read_only(self):
        previous = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)
        try:
            with tempfile.TemporaryDirectory(prefix="flowgrid-schema-version-") as directory:
                for table in ("governance_meta", "extraction_meta"):
                    with self.subTest(table=table):
                        path = Path(directory) / (table + ".sqlite")
                        with FlowGridMemory(db_path=str(path)):
                            pass
                        with sqlite3.connect(path) as db:
                            db.execute("UPDATE " + table + " SET value=? WHERE key='schema_version'",
                                ("9" * 1000,))
                        before = hashlib.sha256(path.read_bytes()).hexdigest()
                        report = inspect_schema(str(path))
                        self.assertEqual(report.status, "corrupt")
                        self.assertFalse(report.compatible)
                        self.assertTrue(report.read_only)
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        finally:
            sys.set_int_max_str_digits(previous)
