# Context compilation limits

Context compilation has explicit product-level resource ceilings in addition to
caller-supplied character or token budgets. They protect a trusted local host
from an accidental large query, malformed integration, or runaway agent.

## Hard ceilings

| Boundary | Limit |
| --- | ---: |
| Records accepted by one compiled result | 1,000 |
| Canonical UTF-8 bytes for one public atomic item | 512 KiB |
| Canonical UTF-8 bytes across all public items | 6 MiB |
| Canonical UTF-8 bytes for the final ContextPack | 8 MiB |
| REST caller-supplied `max_chars` | 4 Mi characters |
| MCP caller-supplied `max_chars` | 1,000,000 characters |

`MemoryService.compile_context` rejects a `max_records` value above 1,000 before
running governed retrieval. The standalone `ContextCompiler` independently
bounds materialization to 1,001 probes, then fails closed, so a forged or custom
`CurrentStateResult` cannot bypass the service limit.

Oversized public items or aggregate input produce a non-injectable
`budget_exceeded` pack with `reason=context_input_limit_exceeded`. An unexpected
final envelope above the hard pack ceiling produces
`reason=context_output_limit_exceeded`. Failure envelopes never repeat memory
or evidence bodies.

The byte limits use canonical UTF-8 output, while `max_chars` continues to count
Python Unicode code points exactly as documented by `ContextPack.budget`.

## Prefix selection complexity

With no budget, or when all items fit, the compiler finalizes one candidate.
For a character-only budget, it probes the full deterministic prefix once and
then binary-searches the largest fitting prefix. Candidate finalizations are
therefore `O(log N)` rather than a descending `N, N-1, ...` scan.

An injected exact token counter is arbitrary host code. The current protocol
requires determinism and exact final-JSON counting, but does not promise that
token counts are monotonic as JSON prefixes change. Token-budget selection
therefore retains the conservative descending scan so it still returns the
largest exact fitting prefix. The 1,000-record and byte ceilings bound that
fallback. REST and MCP do not expose generic token budgeting without a bound
model tokenizer.

## Atomicity and accounting

Items remain atomic. The compiler includes or omits a complete item and never
truncates a field. `budget.used_chars` still equals `len(pack.to_json())`, and a
result containing governed records never reports `ready` after every item was
removed by a budget.

`ContextPack.to_json()` memoizes its canonical rendering after first use. The
pack remains recursively immutable, so repeated size checks and transport
serialization reuse identical bytes without reopening a mutation path.
