# Local owner review channel

`flowgrid-memory review` is the human-side governance channel for a trusted
local host. It lists exact-scope `candidate` and `inferred` records together
with their immutable source evidence, then applies an explicit owner
confirmation or rejection.

The REST and MCP adapters remain non-administrative. They do not gain a review,
transition, audit, evidence, or erase tool through this command.

## Trust boundary

The command assumes that the person who can open the SQLite database on the
host is an authorized owner. It does not provide OS-user authentication,
remote authentication, or a multi-tenant security perimeter.

A review command binds:

- one concrete `user_id`;
- one exact scope;
- one explicit actor identifier;
- the fixed `owner` transition authority;
- the fixed audit purpose `local owner review`.

Records from fallback/global or adjacent project/session/repository scopes are
not included in an exact-scope queue and cannot be transitioned through it.

## Inspect the queue

```bash
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha
```

The command intentionally prints candidate bodies and source-event bodies for
human review. Do not pipe this output into ordinary application logs. The
supplied database must already exist; a typo does not create a new empty
database.

Use `--record` to inspect one pending record and `--limit` to bound the queue:

```bash
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha \
  --record mem_123 \
  --limit 1
```

Each item reports `evidence_complete`. Confirmation requires all referenced raw
events to exist, belong to the same user, and preserve every scope field on the
derived record. Rejection remains available when evidence is incomplete.

## Confirm or reject

```bash
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha \
  --record mem_123 \
  --decision confirm \
  --reason "source evidence verified"
```

```bash
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha \
  --record mem_456 \
  --decision reject \
  --reason "claim is ambiguous"
```

Decision output is a minimal receipt. It contains the record ID, prior/current
status, decision, actor, and evidence-verification signal; it does not repeat
the memory or evidence body.

The underlying record payload remains immutable. A changed claim must be a new
candidate with its own source evidence. Existing supersession semantics remain
in force when a replacement candidate is confirmed.

## Limits

- Queue output is capped at 100 records per invocation.
- Review fails closed when the audit window reaches its 10,000-record safety
  ceiling.
- This initial channel is JSON/CLI based. It does not yet provide batch policy
  approval, editing, or a graphical interface.
