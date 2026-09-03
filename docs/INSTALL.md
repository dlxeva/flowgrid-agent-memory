# Installation and offline verification

## Requirements

- CPython 3.11 or newer
- SQLite compiled with FTS5
- no third-party runtime dependency for the base package

The build step uses a PEP 517 frontend plus `setuptools`. It does not need the
network when those build tools are already installed locally. Do not enable an
online build-isolation download in a restricted environment.

## Install from a local checkout

```bash
python3 -m pip install --no-deps .
flowgrid-memory --version
flowgrid-memory doctor --ephemeral
flowgrid-memory demo --ephemeral
```

Every product CLI command that touches SQLite requires an explicit `--db PATH`
or `--ephemeral` choice.

## Build an offline wheel

Build in a disposable copy so build metadata does not alter the source tree:

```bash
python3 -m pip wheel \
  --no-index --no-deps --no-build-isolation \
  --wheel-dir /tmp/flowgrid-memory-dist \
  /tmp/flowgrid-memory-source-copy
```

Then create a fresh environment outside the repository and install without
contacting an index:

```bash
python3 -m venv /tmp/flowgrid-memory-venv
/tmp/flowgrid-memory-venv/bin/python -m pip install \
  --no-index --no-deps /tmp/flowgrid-memory-dist/flowgrid_agent_memory-*.whl
/tmp/flowgrid-memory-venv/bin/python -c \
  'import aml_retriever; print(aml_retriever.PRODUCT_VERSION)'
/tmp/flowgrid-memory-venv/bin/flowgrid-memory doctor --ephemeral
/tmp/flowgrid-memory-venv/bin/flowgrid-memory demo --ephemeral
```

The wheel includes the governance evaluation manifests, fixtures, and pinned
legacy baseline JSON files as package data. The packaging tests inspect the
archive and repeat the fresh-environment flow offline.

## Persistent local database

Choose an explicit path outside a synced folder and restrict its directory to
the operating-system user running the agent:

```bash
flowgrid-memory doctor --db /absolute/path/to/memory.db
flowgrid-memory demo --db /absolute/path/to/memory.db
```

`doctor --db` is strictly read-only. A missing database is reported as
`missing`; an existing legacy database is reported as `uninitialized`; neither
case causes creation or migration. Constructing `FlowGridMemory` or running
`demo --db` is a writable open and may initialize or additively migrate a
compatible database.

See [LOCAL_SECURITY.md](LOCAL_SECURITY.md) and
[DATA_LIFECYCLE.md](DATA_LIFECYCLE.md) before storing real data.

## Run the local acceptance suite

The base environment runs every core, CLI, AML HTTP, governed REST, packaging,
and governance check. Official MCP SDK tests are skipped when the optional
extra is absent:

```bash
./scripts/run_tests.sh
```

Run the same suite in an environment that has the MCP extra to require the
official stdio subprocess smoke:

```bash
PYTHON=/absolute/path/to/mcp-venv/bin/python \
  ./scripts/run_tests.sh --with-mcp
```

Both paths are offline and use disposable synthetic data. `--with-eval` adds
the slower legacy retrieval ablation and parameter scan. The product profile
keeps official AML evidence in a separate `unverified` gate; a local pass does
not claim a new official score.

## Optional MCP v2 adapter

The MCP adapter is the only optional runtime extra:

```bash
python3 -m pip install 'flowgrid-agent-memory[mcp]'
```

It installs the official Python SDK under the bounded constraint `mcp>=2,<3`.
Installing the base package does not install MCP, and
`import aml_retriever.mcp_adapter` remains safe without it; calling
`create_mcp_server` then raises the fixed `mcp_dependency_unavailable` error.

Run the local stdio server with explicit absolute paths:

```bash
flowgrid-memory-mcp \
  --db /absolute/local/path/agent-memory.db \
  --principal-config /absolute/local/path/mcp-principal.json
```

The package also installs the separate governed REST entry point as
`flowgrid-memory-rest`. REST and MCP do not reuse the legacy AML Add/Search
HTTP handler as a product security boundary.

See [MCP.md](MCP.md) for the trusted-principal format, exact four-tool surface,
candidate/owner-gate behavior, disclosure limits, and stdio trust boundary.

The governed REST example and exact request contract are documented in
[REST_V1.md](REST_V1.md). The server accepts only `127.0.0.1` and must not be
placed behind a public listener.
