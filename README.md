# FlowGrid Agent Memory

Evidence-first, governed local memory for AI agents.

> **Status:** alpha. This repository is suitable for local evaluation and
> integration experiments; its current security boundary is one trusted host.

[中文](README.zh-CN.md) · [Install](docs/INSTALL.md) ·
[Governed REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·
[Owner review](docs/OWNER_REVIEW.md) · [Public API](docs/PUBLIC_API.md) ·
[Container](docs/CONTAINER.md) · [Local security](docs/LOCAL_SECURITY.md) ·
[Data lifecycle](docs/DATA_LIFECYCLE.md) · [Evaluation](docs/EVAL.md) ·
[Acceptance](docs/ACCEPTANCE_CRITERIA.md)

FlowGrid Agent Memory is a general local memory core. It preserves raw evidence,
separates proposals from confirmed truth, resolves only current governed state,
and compiles a minimal authorized context pack for an agent. Its central rule
is simple: storing or extracting text does not make that text true.

The repository also contains the AML v1.1 Add/Search implementation that formed
the original code base. That API is now a compatibility and competition adapter,
not the product's trust or security boundary.

This standalone project was derived from AML Retriever v1.1 at commit
`cdae7dbd38d73eda33793b30017559bdfb75eff5`. FlowGrid governance semantics and
validation experience shaped the product layer; private project ledgers,
operator traces, real trial data, and competition planning are not part of the
public distribution.

## Product status

- Product version: `0.1.0`
- AML Add/Search adapter version: `1.1.0`
- Base runtime: Python 3.11+, standard library only, SQLite with FTS5
- Product surfaces: Python facade, local CLI, authenticated loopback REST v1,
  and an optional official MCP SDK v2 stdio adapter
- Supported deployment boundary: local and controlled by one trusted host;
  `user_id` is a logical partition within that boundary
- Not included: hosted multitenancy, a public network service, or a production
  remote-security perimeter

## Why it is different

The product keeps distinct layers that ordinary vector retrieval often merges:

```text
immutable raw event
        ↓ explicit extraction
candidate / inferred proposal
        ↓ user, owner, or policy transition
confirmed current state
        ↓ trusted AccessContext + disclosure policy
minimal ContextPack for the agent
```

This preserves the FlowGrid memory philosophy:

- **Provenance before fluency.** Derived memory points to immutable source
  events and auditable lifecycle events.
- **Explicit truth states.** `candidate`, `inferred`, `unknown`, `confirmed`,
  `superseded`, `rejected`, and `deleted` are not interchangeable.
- **Current-state resolution.** Rejected, superseded, deleted, or merely
  proposed content cannot reappear in ordinary agent context.
- **Unknown is a valid result.** Missing or conflicting truth causes abstention
  and can require an owner gate.
- **Minimal authorized disclosure.** Reads require a trusted `AccessContext`;
  audit mode is closed unless permission and purpose are explicitly allowed.

## Install and verify

From a local checkout:

```bash
python3 -m pip install .
flowgrid-memory --version
flowgrid-memory doctor --ephemeral
flowgrid-memory demo --ephemeral
```

The product CLI never silently creates `./aml.db`. `doctor` and `demo` require
either `--db PATH` or `--ephemeral`. `review` requires an existing `--db PATH`,
so a typo cannot create an empty database and falsely report an empty queue.
`doctor --db PATH` is read-only: it reports a missing or incompatible database
without creating or migrating it.

For an offline wheel build and fresh-environment verification, see
[docs/INSTALL.md](docs/INSTALL.md).

## Python facade

`FlowGridMemory` is the stable transport-neutral boundary shared by the CLI,
REST, and MCP adapters. The database path is mandatory and the underlying
database object is not exposed. New product integrations should import from
`flowgrid_memory`; `aml_retriever` remains the implementation and AML
compatibility namespace.

```python
from flowgrid_memory import AccessContext, FlowGridMemory, PERMISSION_READ

access = AccessContext(
    principal_id="trusted-local-owner",
    authority="owner",
    scopes={"project": "demo"},
    permissions=frozenset({PERMISSION_READ}),
    purpose="agent context",
    allowed_users=frozenset({"user-1"}),
)

with FlowGridMemory(db_path=":memory:") as memory:
    # ingest_raw_events -> extract_candidates/propose_memory ->
    # explicit transition_memory -> query_current/compile_context
    pass
```

The default zero-dependency extractor recognizes only a strict whole-message
`@flowgrid.memory/v1` directive. Ordinary natural language correctly produces
zero proposals. A host can inject its own extractor, but the core still binds
scope and authority, verifies source spans, and persists candidate-only output.

## Local CLI

```bash
# Explicit temporary database, automatically removed
flowgrid-memory demo --ephemeral

# Read-only inspection; does not create or migrate this path
flowgrid-memory doctor --db /absolute/path/to/memory.db

# Persistent governed demo; intentionally writes to the supplied database
flowgrid-memory demo --db /absolute/path/to/memory.db

# Human owner queue; this intentionally prints candidates and source evidence
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha
```

Use the same `review` command with `--record`, `--decision confirm|reject`, and
`--reason` to apply one explicit lifecycle transition. Decision receipts do not
repeat memory or evidence bodies. See [Local owner review](docs/OWNER_REVIEW.md).

The demo proves this state chain without printing the memory body, database
path, or internal traceback:

`raw event → candidate → unknown/owner gate → owner-confirmed current → authorized ContextPack`

## Container image

The default OCI target is a non-networked CLI image and runs
`flowgrid-memory doctor --ephemeral`. A separate `mcp` build target supports
stdio use. The verified container contract intentionally provides no REST port
target; governed REST remains a host-local loopback service. See
[Container contract](docs/CONTAINER.md).

## Governed local adapters

The REST adapter accepts only the literal `127.0.0.1` bind address and requires
either a trusted local-process boundary or bearer authentication. Start it with
an explicit configuration file:

```bash
flowgrid-memory-rest --config /absolute/path/to/product.json
```

The optional MCP adapter exposes exactly four non-administrative tools over
stdio. It has no transition, audit, raw-evidence, erase, or administrator tool:

```bash
python3 -m pip install 'flowgrid-agent-memory[mcp]'
flowgrid-memory-mcp \
  --db /absolute/path/to/memory.db \
  --principal-config /absolute/path/to/mcp-principal.json
```

Both adapters bind identity, authority, permissions, users, and scopes from
trusted startup configuration. Request or tool arguments cannot replace those
values. See [Governed REST v1](docs/REST_V1.md) and
[MCP v2 local stdio](docs/MCP.md) for their exact trust boundaries.

## AML compatibility adapter

The existing `aml_retriever.cli`, HTTP Add/Search wrapper, deterministic
retriever, and official contract tests remain available for AML compatibility.
They preserve synchronous Add, immediate Search, `user_id` isolation, and the
original official response shapes. They do not use the governed facade as a
remote authentication boundary.

The historical AML v1.0 entry `FlowGrid_AML_Retriever` scored 43.98 (#8 in the
first public academic/text snapshot). The current product and v1.1 changes do
not have a new official score. Local synthetic and governance evaluations are
development evidence only; see [docs/EVAL.md](docs/EVAL.md).

## Honest boundaries

This release does **not** claim:

- general natural-language extraction in the zero-dependency path;
- a new official AML score or improvement on hidden official data;
- semantic PII detection inside otherwise allowed memory content;
- tenant-grade hosted isolation, distributed storage, or remote production
  security;
- that a non-ready ContextPack is safe to inject. Callers must inject only a
  pack whose `status` is `ready`.

See [docs/LOCAL_SECURITY.md](docs/LOCAL_SECURITY.md) before placing real user
data in a persistent database.

## Release evidence

The release workflow rebuilds and tests the package, performs a fresh-wheel
install, publishes checksums, acceptance JSON, SPDX SBOM, and provenance JSON,
and creates GitHub Sigstore attestations for the wheel and sdist. Stable
criteria are documented in [Acceptance criteria](docs/ACCEPTANCE_CRITERIA.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change. Please report
security issues privately as described in [SECURITY.md](SECURITY.md), and never
attach real memory databases, credentials, or private transcripts to an issue.
