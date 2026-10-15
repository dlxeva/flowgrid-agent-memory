# Installation and offline verification

## Requirements

- CPython 3.11 or newer
- SQLite compiled with FTS5
- no third-party runtime dependency for the base package

The build step uses a PEP 517 frontend plus `setuptools`. It does not need the
network when those build tools are already installed locally. Do not enable an
online build-isolation download in a restricted environment.

JSON configuration flags and Python flag overrides accept only true boolean values. String values such as false or 0 are rejected; environment-variable strings retain their documented conversion convention.

中文说明：JSON/Python开关必须是真正的布尔值，字符串关闭值会被拒绝，避免误开实验；环境变量仍按原有约定转换。

Read-only extraction is rejected before invoking an injected extractor or reading its evidence batch. A writable integration still owns provider authorization, limits and cancellation.

中文说明：只读提取在调用提取器与读取该批证据前拒绝；可写宿主仍负责模型调用授权、限额和取消。

## Install from a local checkout

Use a reviewed checkout. A local install is not proof of a published release,
and this working tree must not be assumed byte-identical to a frozen candidate.
The command below may fetch build tools unless build isolation is disabled;
use the explicit offline wheel flow below in restricted environments.

中文说明：工作区安装与冻结候选属于不同对象；默认源码安装可能下载构建工具。离线使用按下方禁用索引与 build isolation 的流程操作，不把一次安装成功当正式发布证明。

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
  'import flowgrid_memory; print(flowgrid_memory.PRODUCT_VERSION)'
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

The total suite below requires the complete reviewed source checkout, including
its test helpers. The minimal sdist does not contain that whole test tree or all
acceptance scripts. A fresh wheel install and isolated build verification are
separate evidence; neither substitutes for the full checkout's acceptance gate.
The total runner must refuse missing helpers rather than report a partial pass.

中文说明：下面的总验收命令须在完整且已审阅的源码仓库执行。精简源码包不包含全量测试及所有辅助脚本，安装成功不能代替总验收；缺文件时应明确失败，不静默跳过。

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
python3 -m pip install '.[mcp]'
```

It installs the official Python SDK under the bounded constraint `mcp>=2,<3`.

Run this from the reviewed source checkout. It can download dependencies and
does not provide a fully pinned environment. An index install using only the
package name cannot identify the local candidate. For an offline candidate,
install its exact wheel and reviewed MCP dependency wheels with indexes disabled.

中文说明：从本地源码安装可选模块仍可能联网下载依赖；离线候选须绑定精确 wheel 与已核对的依赖，不复用未核验的同名索引包。
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

## Container targets

The default image is the non-networked `cli` target. The optional `mcp` target
installs the MCP extra and communicates over stdio. No container target exposes
the governed REST adapter because its verified boundary is a literal loopback
listener on one trusted host. See [CONTAINER.md](CONTAINER.md).
