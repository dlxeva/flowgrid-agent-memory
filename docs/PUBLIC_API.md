# Stable public Python API

The distribution name is `flowgrid-agent-memory`. New integrations import the
product API from `flowgrid_memory`:

```python
from flowgrid_memory import AccessContext, FlowGridMemory, PERMISSION_READ
```

The public namespace contains the facade, request/result data contracts,
disclosure and access policies, governed lifecycle types, extraction protocols,
context contracts, version constants, and schema inspection. It intentionally
does not export `RetrieverDB`, `MemoryService`, `Store`, raw SQL helpers, or
migration implementation functions.

`aml_retriever` remains importable for AML Add/Search compatibility and existing
0.1 integrations. It is an implementation/compatibility namespace and may add
deprecation warnings in a later major compatibility cycle.

The installed command entry points also route through the product namespace:

- `flowgrid-memory` -> `flowgrid_memory.cli`
- `flowgrid-memory-rest` -> `flowgrid_memory.rest`
- `flowgrid-memory-mcp` -> `flowgrid_memory.mcp`

The wrappers contain no policy logic. They delegate to the internal adapters so
there remains one implementation of each trust boundary.
