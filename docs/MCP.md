# MCP v2 local stdio adapter

FlowGrid Agent Memory exposes an optional local MCP adapter built on the
official Python SDK v2. The base package remains dependency-free; MCP support
is installed explicitly:

```bash
python3 -m pip install 'flowgrid-agent-memory[mcp]'
```

The package constraint is `mcp>=2,<3`. The adapter uses the public v2
`MCPServer` API, its default stdio transport, and is tested with the official
`Client(server)` in-memory transport and `Client(stdio_client(...))` subprocess
transport. The SDK negotiates the 2026-07-28 protocol and supported older
protocols; this project does not implement JSON-RPC or MCP framing itself.

## Trusted local launch

The stdio process requires two explicit absolute paths. It never obtains a
database path, principal, authority, permissions, or user grants from MCP tool
arguments or environment variables.

```bash
flowgrid-memory-mcp \
  --db /absolute/local/path/agent-memory.db \
  --principal-config /absolute/local/path/mcp-principal.json
```

Example trusted-principal configuration:

```json
{
  "principal_id": "local-agent-host",
  "authority": "owner",
  "allowed_users": ["user-123"],
  "scopes": {"project": "example-project"},
  "permissions": ["memory:write", "memory:extract", "memory:read"]
}
```

The launcher or desktop host is the trust boundary. Restrict the database,
configuration file, and their parent directories to that operating-system
user. Stdio carries protocol frames on stdout; diagnostics use fixed messages
on stderr and do not include database paths, event bodies, identifiers, or
tracebacks.

## Exact tool surface

Only these tools are registered:

- `memory_ingest_events`: stores immutable raw events and returns opaque event
  identifiers and counts, never event bodies.
- `memory_extract_candidates`: runs only the built-in strict
  `@flowgrid.memory/v1` directive extractor. It cannot accept a callable,
  model, or code payload and can only create candidates.
- `memory_query_current`: returns the governed current-state `ContextPack`
  public-field allowlist. Candidate, rejected, superseded, deleted, conflict,
  and unknown states never become confirmed current memory.
- `memory_compile_context`: returns the same governed current-state envelope
  with an exact canonical character budget.

There are no MCP transition, confirmation, audit, raw-evidence, privacy erase,
or administrator tools. Resources, resource templates, and prompts are empty.
Owner confirmation must happen through a separately governed owner channel.

All four tools authorize the requested user and scope against the trusted
principal before the product facade is opened. Extra arguments are rejected;
they are not silently ignored. Tool failures and unknown tools return fixed
error codes without reflecting request data or exception text.

## Budget and disclosure behavior

`memory_query_current` and `memory_compile_context` return only the static
`ContextPack` representation: current items or structured unknown/conflict and
owner-gate signals. They do not return `CurrentStateResult`, state actors,
state reasons, creator fields, source-event arrays, raw bodies, or filesystem
paths.

`max_chars` is supported. `max_tokens` is explicitly rejected with
`token_budget_unsupported`, because the adapter does not ship a model- and
version-bound exact tokenizer. It never substitutes a heuristic token count.
Only a pack with `status: "ready"` is injectable memory. Unknown, conflict,
forbidden, and budget-exceeded packs are control results; their complete
safety envelope can be larger than an extremely small requested character
budget and must not be injected as memory text.

## Scope of this release

This is a local stdio integration, not a remote production MCP service, a
hosted multi-tenant boundary, or an administrator API. It does not claim an
official Agent Memory Leaderboard score or an AML score improvement. Those
claims require separate official evaluation artifacts.

Run one stdio server process per database. Schema bootstrap is transactional.
On POSIX hosts, cooperating product processes also serialize the compatibility
preflight and first open with a host-local advisory lock. The Windows fallback
is process-local. This coordination prevents partial first migrations; it does
not turn the local stdio adapter into a hosted multi-process service boundary.
