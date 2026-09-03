"""Offline wheel, package-data, and isolated-install acceptance tests."""
from __future__ import annotations

import email
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.legacy_fixture import create_legacy_database


REPO = Path(__file__).resolve().parents[1]
FIXTURE_PATH = "aml_retriever/evaluation/fixtures/governance_v1.json"
BASELINE_PATH = "aml_retriever/evaluation/baselines/legacy_v11_small.json"
SDK_V2_ENV = Path("/tmp/flowgrid-mcp-sdk-v2")


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


class TestOfflineWheel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="flowgrid-wheel-test-")
        cls.root = Path(cls._temporary.name)
        cls.source = cls.root / "source-copy"
        cls.build_cwd = cls.root / "outside-cwd"
        cls.wheel_dir = cls.root / "wheelhouse"
        cls.build_cwd.mkdir()
        cls.wheel_dir.mkdir()
        shutil.copytree(
            REPO,
            cls.source,
            ignore=shutil.ignore_patterns(
                ".git",
                ".flg",
                "__pycache__",
                "*.pyc",
                "*.egg-info",
                "build",
                "dist",
                "eval_out",
                "*.db",
                "*.db-wal",
                "*.db-shm",
            ),
        )
        cls.env = os.environ.copy()
        cls.env.pop("PYTHONPATH", None)
        try:
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(cls.wheel_dir),
                    str(cls.source),
                ],
                cwd=cls.build_cwd,
                env=cls.env,
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic
            raise AssertionError(
                "offline wheel build failed\n" + exc.stdout + "\n" + exc.stderr
            ) from exc
        wheels = list(cls.wheel_dir.glob("flowgrid_agent_memory-*.whl"))
        if len(wheels) != 1:  # pragma: no cover - diagnostic
            raise AssertionError(f"expected one wheel, got {wheels}")
        cls.wheel = wheels[0]

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def test_metadata_has_only_extra_scoped_mcp_dependency(self):
        with zipfile.ZipFile(self.wheel) as archive:
            metadata_name = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            metadata = email.message_from_bytes(archive.read(metadata_name))
        self.assertEqual(metadata["Name"], "flowgrid-agent-memory")
        self.assertEqual(metadata["Version"], "0.1.0")
        requirements = metadata.get_all("Requires-Dist") or []
        self.assertEqual(len(requirements), 1)
        normalized = requirements[0].replace(" ", "").replace('"', "'")
        self.assertEqual(normalized, "mcp<3,>=2;extra=='mcp'")
        self.assertEqual(metadata.get_all("Provides-Extra"), ["mcp"])
        self.assertEqual(metadata["Requires-Python"], ">=3.11")

    def test_console_entry_points_are_exact(self):
        with zipfile.ZipFile(self.wheel) as archive:
            entry_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/entry_points.txt")
            )
            entries = archive.read(entry_name).decode("utf-8")
        self.assertIn("flowgrid-memory = aml_retriever.product_cli:main", entries)
        self.assertIn("flowgrid-memory-rest = aml_retriever.rest_v1:main", entries)
        self.assertIn("flowgrid-memory-mcp = aml_retriever.mcp_adapter:main", entries)

    def test_canonical_package_data_present_and_project_state_absent(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            self.assertIn(FIXTURE_PATH, names)
            self.assertIn(BASELINE_PATH, names)
            for relative in (FIXTURE_PATH, BASELINE_PATH):
                packaged = archive.read(relative)
                source = (REPO / relative).read_bytes()
                self.assertEqual(hashlib.sha256(packaged).digest(), hashlib.sha256(source).digest())
                json.loads(packaged)
            forbidden = [
                name
                for name in names
                if name.endswith((".db", ".db-wal", ".db-shm"))
                or "/.flg/" in name
                or name.startswith(".flg/")
                or Path(name).name
                in {"PROJECT_MASTER.json", "ORCHESTRATION.json", "SNAPSHOT.md", "PROGRESS.md"}
            ]
        self.assertEqual(forbidden, [])

    def test_fresh_venv_isolated_import_cli_and_demo(self):
        venv = self.root / "fresh-venv"
        _run([sys.executable, "-m", "venv", str(venv)], cwd=self.build_cwd, env=self.env)
        python = venv / "bin" / "python"
        cli = venv / "bin" / "flowgrid-memory"
        mcp_cli = venv / "bin" / "flowgrid-memory-mcp"
        rest_cli = venv / "bin" / "flowgrid-memory-rest"
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(self.wheel),
            ],
            cwd=self.build_cwd,
            env=self.env,
        )
        code = (
            "import json,aml_retriever\n"
            "from importlib.resources import files\n"
            "from aml_retriever.evaluation.governance_suite import ("
            "CANONICAL_MANIFEST_SHA256,DEFAULT_BASELINE_PATH,load_manifest)\n"
            "from aml_retriever.server import _Handler\n"
            "from aml_retriever.auth import TrustedPrincipal\n"
            "from aml_retriever.mcp_adapter import MCPDependencyError,create_mcp_server\n"
            "fixture=files('aml_retriever.evaluation').joinpath('fixtures/governance_v1.json')\n"
            "loaded=load_manifest()\n"
            "principal=TrustedPrincipal(principal_id='base-wheel',authority='owner',"
            "allowed_users=frozenset({'u'}),scopes={},permissions=frozenset())\n"
            "dependency_error=None\n"
            "try:\n create_mcp_server(db_path=':memory:',principal=principal)\n"
            "except MCPDependencyError as exc:\n dependency_error=str(exc)\n"
            "print(json.dumps({'file':aml_retriever.__file__,"
            "'product':aml_retriever.PRODUCT_VERSION,"
            "'adapter':aml_retriever.AML_ADAPTER_VERSION,"
            "'mcp_missing':__import__('importlib').util.find_spec('mcp') is None,"
            "'mcp_error':dependency_error,"
            "'fixture':json.loads(fixture.read_text(encoding='utf-8')) is not None,"
            "'manifest_attested':loaded.sha256==CANONICAL_MANIFEST_SHA256,"
            "'baseline_exists':DEFAULT_BASELINE_PATH.is_file(),"
            "'http_banner':_Handler.server_version}))\n"
        )
        imported = _run(
            [str(python), "-I", "-c", code],
            cwd=self.build_cwd,
            env=self.env,
        )
        info = json.loads(imported.stdout)
        self.assertEqual(info["product"], "0.1.0")
        self.assertEqual(info["adapter"], "1.1.0")
        self.assertTrue(info["mcp_missing"])
        self.assertEqual(info["mcp_error"], "mcp_dependency_unavailable")
        self.assertTrue(info["fixture"])
        self.assertTrue(info["manifest_attested"])
        self.assertTrue(info["baseline_exists"])
        self.assertEqual(info["http_banner"], "aml-retriever/1.1")
        self.assertIn(str(venv / "lib"), info["file"])
        self.assertIn("site-packages", info["file"])
        self.assertNotIn(str(REPO), info["file"])
        self.assertTrue(mcp_cli.is_file())
        self.assertTrue(rest_cli.is_file())

        checked = _run(
            [str(python), "-m", "pip", "check"], cwd=self.build_cwd, env=self.env
        )
        self.assertIn("No broken requirements found", checked.stdout)

        version = _run([str(cli), "--version"], cwd=self.build_cwd, env=self.env)
        self.assertIn("0.1.0", version.stdout)
        doctor = _run(
            [str(python), "-I", "-m", "aml_retriever.product_cli", "doctor", "--ephemeral"],
            cwd=self.build_cwd,
            env=self.env,
        )
        self.assertEqual(json.loads(doctor.stdout)["status"], "ok")
        demo = _run(
            [str(python), "-I", "-m", "aml_retriever.product_cli", "demo", "--ephemeral"],
            cwd=self.build_cwd,
            env=self.env,
        )
        result = json.loads(demo.stdout)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(result["checks"].values()))
        self.assertNotIn("concise and evidence-first", demo.stdout)

        # Generate the accepted legacy schema without predecessor Git history,
        # then let only the installed wheel migrate and reopen it twice.
        legacy_db = self.root / "legacy.db"
        create_legacy_database(
            legacy_db,
            content="legacy-wheel-sentinel",
            user_id="legacy-u1",
            session_id="legacy-s1",
        )
        migration_code = (
            "import json,sqlite3\n"
            "from aml_retriever import FlowGridMemory\n"
            "from aml_retriever.api import MemoryService\n"
            "from aml_retriever.config import RetrieverConfig\n"
            f"path={str(legacy_db)!r}\n"
            "FlowGridMemory(db_path=path).close()\n"
            "FlowGridMemory(db_path=path).close()\n"
            "with MemoryService(RetrieverConfig(db_path=path)) as svc:\n"
            " result=svc.official_search({'query':'legacy-wheel-sentinel',"
            "'user_id':'legacy-u1','top_k':10})\n"
            " con=sqlite3.connect(path)\n"
            " counts={'raw':con.execute('select count(*) from raw_events').fetchone()[0],"
            "'requests':con.execute('select count(*) from requests').fetchone()[0],"
            "'states':con.execute('select count(*) from memory_state_events').fetchone()[0]}\n"
            " con.close()\n"
            " print(json.dumps({'found':len(result['data']),**counts}))\n"
        )
        migrated = _run(
            [str(python), "-I", "-c", migration_code],
            cwd=self.build_cwd,
            env=self.env,
        )
        migrated_info = json.loads(migrated.stdout)
        self.assertGreaterEqual(migrated_info["found"], 1)
        self.assertEqual(migrated_info["raw"], 1)
        self.assertEqual(migrated_info["requests"], 1)
        self.assertEqual(migrated_info["states"], 0)

    @unittest.skipUnless(
        (SDK_V2_ENV / "bin" / "python").is_file(),
        "official MCP SDK v2 test environment is unavailable",
    )
    def test_wheel_only_official_sdk_stdio_outside_repository(self):
        """Install ``wheel[mcp]`` into an SDK-v2 env and use real stdio."""

        mcp_venv = self.root / "mcp-wheel-venv"
        shutil.copytree(SDK_V2_ENV, mcp_venv, symlinks=True)
        python = mcp_venv / "bin" / "python"
        server = mcp_venv / "bin" / "flowgrid-memory-mcp"
        wheel_with_extra = f"{self.wheel}[mcp]"
        installed = _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                wheel_with_extra,
            ],
            cwd=self.build_cwd,
            env=self.env,
        )
        self.assertIn("flowgrid-agent-memory", installed.stdout)
        checked = _run(
            [str(python), "-m", "pip", "check"],
            cwd=self.build_cwd,
            env=self.env,
        )
        self.assertIn("No broken requirements found", checked.stdout)
        self.assertTrue(server.is_file())

        db_path = self.root / "wheel-mcp.db"
        principal_path = self.root / "wheel-mcp-principal.json"
        stderr_path = self.root / "wheel-mcp-stderr.log"
        principal_path.write_text(
            json.dumps(
                {
                    "principal_id": "wheel-stdio-principal",
                    "authority": "owner",
                    "allowed_users": ["u1"],
                    "scopes": {"project": "wheel-stdio"},
                    "permissions": ["memory:write", "memory:extract", "memory:read"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        code = (
            "import asyncio,json,os\n"
            "from mcp import Client,StdioServerParameters,stdio_client\n"
            f"server={str(server)!r}\n"
            f"cwd={str(self.build_cwd)!r}\n"
            f"db={str(db_path)!r}\n"
            f"principal={str(principal_path)!r}\n"
            f"stderr_path={str(stderr_path)!r}\n"
            "sentinel='WHEEL-MCP-PRIVATE-SENTINEL'\n"
            "directive='@flowgrid.memory/v1\\n'+json.dumps({'proposals':["
            "{'memory_key':'wheel.preference','memory_type':'preference',"
            "'subject':'$user','content':sentinel}]},separators=(',',':'))\n"
            "async def run():\n"
            " with open(stderr_path,'w+',encoding='utf-8') as err:\n"
            "  params=StdioServerParameters(command=server,args=['--db',db,"
            "'--principal-config',principal],env=dict(os.environ),cwd=cwd)\n"
            "  transport=stdio_client(params,errlog=err)\n"
            "  process=None\n"
            "  async with Client(transport) as client:\n"
            "   frame=getattr(getattr(transport,'gen',None),'ag_frame',None)\n"
            "   process=frame.f_locals.get('process') if frame else None\n"
            "   listed=await client.list_tools()\n"
            "   resources=await client.list_resources()\n"
            "   templates=await client.list_resource_templates()\n"
            "   prompts=await client.list_prompts()\n"
            "   ingest=await client.call_tool('memory_ingest_events',"
            "{'request_id':'wheel-r','user_id':'u1','messages':["
            "{'role':'user','content':directive}],'scope':{'project':'wheel-stdio'}})\n"
            "   extract=await client.call_tool('memory_extract_candidates',"
            "{'user_id':'u1','raw_event_ids':ingest.structured_content['raw_event_ids'],"
            "'idempotency_key':'wheel-e','scope':{'project':'wheel-stdio'}})\n"
            "   current=await client.call_tool('memory_query_current',"
            "{'user_id':'u1','memory_key':'wheel.preference',"
            "'scope':{'project':'wheel-stdio'}})\n"
            "   context=await client.call_tool('memory_compile_context',"
            "{'user_id':'u1','memory_key':'wheel.preference',"
            "'scope':{'project':'wheel-stdio'},'max_chars':4096})\n"
            "   forbidden=await client.call_tool('WHEEL-UNKNOWN-SENTINEL',{})\n"
            "  err.seek(0); errors=err.read()\n"
            " assert [tool.name for tool in listed.tools]==["
            "'memory_ingest_events','memory_extract_candidates',"
            "'memory_query_current','memory_compile_context']\n"
            " assert resources.resources==[] and templates.resource_templates==[] "
            "and prompts.prompts==[]\n"
            " assert extract.structured_content['proposal_count']==1\n"
            " assert current.structured_content['status']=='unknown' "
            "and context.structured_content['owner_gate_required']\n"
            " assert sentinel not in json.dumps(current.model_dump(by_alias=True))\n"
            " assert forbidden.structured_content['error']['code']=='tool_not_available'\n"
            " assert sentinel not in errors and db not in errors and principal not in errors\n"
            " assert process is not None and process.returncode is not None\n"
            " return {'status':'ok','tools':4,'empty_discovery':True,"
            "'candidate_unknown':True,'orphan':False}\n"
            "async def bounded():\n"
            " return await asyncio.wait_for(run(),timeout=30)\n"
            "print(json.dumps(asyncio.run(bounded()),sort_keys=True))\n"
        )
        smoke = _run(
            [str(python), "-I", "-c", code],
            cwd=self.build_cwd,
            env=self.env,
        )
        result = json.loads(smoke.stdout)
        self.assertEqual(
            result,
            {
                "status": "ok",
                "tools": 4,
                "empty_discovery": True,
                "candidate_unknown": True,
                "orphan": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
