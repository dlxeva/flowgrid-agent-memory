"""Portable regression for exact boolean configuration values."""
import os
import unittest
from unittest.mock import mock_open, patch
from aml_retriever.config import RetrieverConfig


class ConfigFlagTypesTests(unittest.TestCase):
    def test_defaults_and_valid_updates(self):
        cfg = RetrieverConfig()
        for key in ("relative_time", "temporal_intent", "preference_role_boost"):
            self.assertIs(cfg.flags[key], False)
            self.assertIs(cfg.merge({"flags": {key: True}}).flags[key], True)
            self.assertIs(cfg.with_flags(**{key: True}).flags[key], True)
            self.assertIs(cfg.flags[key], False)

    def test_non_boolean_values_rejected_everywhere(self):
        for value in ("false", "0", "true", 0, 1, None, [], {}, 0.0):
            for action in (
                lambda: RetrieverConfig(flags={"relative_time": value}),
                lambda: RetrieverConfig().merge({"flags": {"relative_time": value}}),
                lambda: RetrieverConfig().with_flags(relative_time=value),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "^flag_boolean_required$"
                ):
                    action()

    def test_invalid_mapping_and_failed_merge_do_not_mutate(self):
        for value in (None, [], False, "false", 0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^flags_mapping_required$"):
                    RetrieverConfig(flags=value)
                with self.assertRaisesRegex(ValueError, "^flags_mapping_required$"):
                    RetrieverConfig().merge({"flags": value})
        cfg = RetrieverConfig()
        before = cfg.to_dict()
        with self.assertRaises(ValueError):
            cfg.merge({"flags": {"views": False, "relative_time": "false"}})
        self.assertEqual(cfg.to_dict(), before)

    def test_constructor_detaches_caller_mapping(self):
        flags = {"relative_time": False}
        cfg = RetrieverConfig(flags=flags)
        flags["relative_time"] = True
        self.assertIs(cfg.flags["relative_time"], False)

    def test_environment_string_convention_preserved(self):
        for value, expected in (("false", False), ("0", False), ("true", True), ("1", True)):
            with patch.dict(os.environ, {"AML_FLAG_RELATIVE_TIME": value}, clear=True):
                self.assertIs(RetrieverConfig.from_env().flags["relative_time"], expected)

    def test_file_boolean_string_cannot_be_hidden_by_environment(self):
        with patch.dict(os.environ, {"AML_FLAG_RELATIVE_TIME": "0"}, clear=True), patch(
            "aml_retriever.config.os.path.exists", return_value=True
        ), patch("builtins.open", mock_open(read_data='{"flags":{"relative_time":"false"}}')):
            with self.assertRaisesRegex(ValueError, "^flag_boolean_required$"):
                RetrieverConfig.from_env("synthetic.json")
