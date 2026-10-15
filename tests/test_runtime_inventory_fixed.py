"""Direct package-inventory regressions; all data is disposable synthetic."""
import os
from pathlib import Path
import shutil
import sysconfig
import tempfile
import unittest
from aml_retriever import release_integrity as runtime


class RuntimeInventoryFixedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='runtime-inventory-fixed-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        site=Path(runtime.__file__).resolve().parent.parent
        for name in runtime.RUNTIME_SOURCE_FILES:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(site/name,target)

    def digest(self):return runtime.runtime_source_digest(self.root)

    def test_clean_inventory_matches_content_digest(self):
        paths,digest=self.digest()
        self.assertEqual(set(paths),runtime.RUNTIME_SOURCE_FILES)
        self.assertEqual(digest,runtime.runtime_digest_from_bytes({n:(self.root/n).read_bytes() for n in paths}))

    def test_native_extension_rejected(self):
        extension=sysconfig.get_config_var('EXT_SUFFIX');self.assertIsInstance(extension,str)
        (self.root/'aml_retriever'/('governance'+extension)).write_bytes(b'SYNTHETIC-NOT-EXECUTABLE')
        with self.assertRaises(ValueError):self.digest()

    def test_all_unreviewed_types_rejected(self):
        for name in ('extra.pyd','extra.dll','extra.pyc','extra.txt','extra.py','extra.json'):
            path=self.root/'flowgrid_memory'/name;path.write_bytes(b'SYNTHETIC-NOT-EXECUTABLE')
            with self.subTest(name=name),self.assertRaises(ValueError):self.digest()
            path.unlink()

    def test_normal_cache_and_generated_identity_excluded(self):
        before=self.digest();cache=self.root/'aml_retriever/__pycache__';cache.mkdir()
        (cache/'example.pyc').write_bytes(b'SYNTHETIC-NOT-EXECUTED')
        (self.root/'aml_retriever/release_identity.json').write_text('{}')
        self.assertEqual(self.digest(),before)

    @unittest.skipIf(os.name=='nt','Windows symlink privileges require separate host validation')
    def test_native_link_rejected(self):
        (self.root/'aml_retriever/extra.so').symlink_to(self.root/'aml_retriever/governance.py')
        with self.assertRaises(ValueError):self.digest()

    @unittest.skipIf(os.name=='nt','Windows symlink privileges require separate host validation')
    def test_directory_cache_and_identity_links_rejected(self):
        for name in ('linked','__pycache__','release_identity.json'):
            path=self.root/'aml_retriever'/name;path.symlink_to(self.root/'flowgrid_memory')
            with self.subTest(name=name),self.assertRaises(ValueError):self.digest()
            path.unlink()

    def test_content_change_detected(self):
        before=self.digest()
        path=self.root/'aml_retriever/governance.py';path.write_bytes(path.read_bytes()+b'\n# synthetic drift\n')
        self.assertNotEqual(self.digest(),before)

    def test_missing_file_rejected(self):
        (self.root/'aml_retriever/governance.py').unlink()
        with self.assertRaises(ValueError):self.digest()
