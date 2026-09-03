# Data lifecycle, provenance, and erasure

## Runtime layers

| Layer | SQLite objects | Lifecycle meaning |
| --- | --- | --- |
| Original source | `messages` | Accepted message body, stored unchanged |
| Raw event locator | `raw_events` | Immutable locator and source authority; body is not copied |
| Retrieval compatibility | `views`, `fts`, `requests`, `sessions` | AML Add/Search evidence and idempotency surfaces |
| Governed memory | `memory_records` | Immutable proposal payload plus current projection fields |
| Truth history | `memory_state_events` | Append-only authoritative lifecycle transitions |
| Extraction proof | `extraction_receipts`, `proposal_origins` | Request/output fingerprints and source-span hashes; no second body copy |

Raw ingestion and extraction are separate actions. Extraction creates only
`candidate` records. A candidate becomes current only through an explicit
authorized transition. Rejection, supersession, and deletion are monotonic;
old records cannot be revived. A replacement must be a new record with its own
source and an explicit supersession relationship.

Governed REST binds the trusted effective scope into each raw event. Product
ingest retries are exact: the same `(user_id, request_id)` is idempotent only
when messages, roles, timestamps, session, and effective scope match. A
divergent retry conflicts without changing the database. Extraction rechecks
the immutable raw-event scope, so an event cannot be read in one project and
reissued as a candidate in another.

## Where data is stored

The product facade requires an explicit `db_path`. The product CLI requires
either `--db PATH` or `--ephemeral`; it never silently writes `./aml.db`.
Ephemeral CLI runs use a private temporary directory and remove it on exit.

Governed REST also requires an explicit database path in its strict product
configuration. Persistent paths must be absolute. Configuration, loopback bind,
authentication mode, principal policy, token source, limits, and the erase gate
are validated before the writable facade is opened. REST never falls back to
the legacy `AML_DB_PATH` or the compatibility handler's defaults.

A file-backed SQLite database may also create `<db>-wal` and `<db>-shm` in the
same directory. These files are part of the database lifecycle and may contain
pages with memory data.

The legacy AML configuration still supports its historical `AML_DB_PATH` and
`:memory:` defaults. That compatibility behavior is not the product CLI
contract.

## Current reads and audit reads

- `query_current` returns only confirmed, currently valid truth, or a structured
  unknown/conflict result. Candidate and inactive text is withheld.
- `query_audit` still requires trusted authorization and a permitted audit
  purpose. It may include lifecycle evidence and therefore must not be used as
  ordinary model context.
- `compile_context` applies the disclosure allowlist and exact output budget.
  Only a `ready` pack is injectable.
- REST returns an explicit `injectable` control flag. It is true only for a
  ready current-mode pack; audit packs are never injectable. Its
  zero-dependency v1 supports exact character limits and rejects token limits
  rather than estimating them.
- REST audit requires a concrete user, read+audit permissions, and a trusted
  purpose allowlist. Its public query/context shape omits raw event and state
  event bodies and reports their count; it may still contain authorized memory
  content and clients must not log the response.
- Historical `as_of` is audit-only in REST. Current query/context rejects it,
  preventing terminal records from being revived as ordinary model context.

## Tombstone versus privacy erasure

Transitioning one record to `deleted` creates an auditable tombstone. It does
not physically remove source evidence.

`FlowGridMemory.erase_user` is the privacy-erasure operation. It requires an
explicit actor, reason, and `user`/`owner`/`policy` authority, then removes the
user's messages, views, FTS rows, request/session state, raw-event locators,
governed records, state history, extraction receipts, and proposal origins.
Because the audit data itself is erased, the caller must keep any legally
required erasure receipt outside the memory database without copying memory
content.

The REST erase route adds two outer gates: `erase.enabled` is false by default,
and the principal needs `memory:erase`. Because the operation deletes the whole
user partition, a project/session/repository-scoped principal cannot invoke it.
Request JSON cannot choose the erase actor or authority.

The legacy compatibility command remains available:

```bash
python3 -m aml_retriever.cli delete-user --db /absolute/path/to/memory.db --user user-1
```

After closing all processes, physical destruction of a file-backed instance
must include the main file and its WAL/SHM companions. Use an operator-approved,
recoverability-aware deletion workflow appropriate to the host; this project
does not automatically delete a caller-supplied persistent database.

## Logs and command output

The product demo and doctor output only status, counts, versions, and invariant
checks. They do not print memory bodies, database paths, secrets, extractor
errors, or internal tracebacks. Context compilation can return memory content
only after authorization and disclosure policy; callers are responsible for
not logging that pack.

## Evaluation data

This repository contains deterministic synthetic fixtures and hand-written
tests, not private AML evaluation data. Generated local reports contain
aggregate metrics. Official or otherwise restricted evaluation data must not be
used for training or reconstruction and must be deleted according to its
current governing terms. Local synthetic metrics do not establish an official
leaderboard result.
