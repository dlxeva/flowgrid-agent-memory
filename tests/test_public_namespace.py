"""Stable product namespace and compatibility boundary."""
import importlib
import tomllib
import unittest
from pathlib import Path

import aml_retriever
import flowgrid_memory


class TestPublicNamespace(unittest.TestCase):
    def test_facade_and_contracts_are_identity_preserving(self):
        self.assertIs(flowgrid_memory.FlowGridMemory, aml_retriever.FlowGridMemory)
        self.assertIs(flowgrid_memory.ContextPack, aml_retriever.ContextPack)
        self.assertEqual(flowgrid_memory.__version__, aml_retriever.PRODUCT_VERSION)

    def test_internal_database_and_aml_objects_are_not_public_exports(self):
        for name in ("RetrieverDB", "MemoryService", "Store", "RetrieverConfig"):
            self.assertFalse(hasattr(flowgrid_memory, name), name)
            self.assertNotIn(name, flowgrid_memory.__all__)

    def test_product_entrypoint_wrappers_import_without_side_effects(self):
        for module in ("flowgrid_memory.cli", "flowgrid_memory.rest", "flowgrid_memory.mcp"):
            loaded = importlib.import_module(module)
            self.assertTrue(callable(loaded.main))

    def test_distribution_discovers_both_namespaces(self):
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        include = project["tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertIn("flowgrid_memory*", include)
        scripts = project["project"]["scripts"]
        self.assertEqual(scripts["flowgrid-memory"], "flowgrid_memory.cli:main")


if __name__ == "__main__":
    unittest.main()
