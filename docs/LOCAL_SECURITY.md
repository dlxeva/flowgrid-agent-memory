# Local security boundary

FlowGrid Agent Memory `0.1.0` is a local library, CLI, and authenticated
loopback REST adapter, not a remotely exposed service. Its verified security
boundary is one trusted local deployment with an explicit SQLite path.

## What the product enforces

- ordinary reads require a trusted `AccessContext` and explicit user grant;
- audit requires a concrete user grant, `memory:audit`, a purpose, and a
  disclosure-policy purpose allowlist;
- candidate, inferred, rejected, superseded, and deleted content is not emitted
  as current truth;
- owner-sensitive transitions require `user`, `owner`, or `policy` authority;
- extractor output cannot choose user, trusted scope, authority, record ID,
  lifecycle state, or confirmation;
- product CLI errors are fixed envelopes without memory content, database path,
  credentials, or internal traceback;
- future or corrupt governance schema metadata is rejected before a writable
  database connection is opened.
- governed REST accepts only literal `127.0.0.1`, rejects unauthenticated mode,
  binds a recursively immutable trusted principal before reading request JSON,
  and emits fixed `no-store` error envelopes;
- REST transitions prove the opaque target record's exact immutable scope
  through an authorized audit-facade lookup before applying the owner gate;
  optional related records must resolve in the same scope and slot;
- REST privacy erase is disabled by default, requires `memory:erase`, and is
  rejected for project/session-scoped principals because erasure is user-wide.
- REST handler shutdown drains active requests before closing SQLite, and
  partial request-body reads have a fixed local socket timeout.

## Operator responsibilities

- derive `AccessContext` only from trusted local policy or the governed REST
  principal binder; never deserialize it directly from an untrusted request;
- inject only `ContextPack.status == "ready"`; unknown, conflict, forbidden, and
  budget-exceeded packs are control states;
- protect the database directory with OS permissions and keep it outside cloud
  sync unless the data owner has explicitly accepted that replication;
- back up and delete the main SQLite file together with its `-wal` and `-shm`
  companions;
- treat the actor and authority arguments on local mutating calls as trusted
  host inputs. REST derives them only from its fixed trusted principal.
- do not log successful query/context/audit bodies; fixed denial envelopes are
  safe to record, but authorized responses can contain memory content.
- for `QuoteAnchoredModelExtractor`, pin provider/model/prompt behavior in the
  extractor identity, enforce the actual process or network deadline outside
  the library, and authorize data egress before sending any non-synthetic
  RawEvent to a remote runner. A quote anchor proves textual provenance, not
  that the proposed interpretation is safe or correct.

## Boundaries not provided

- governed REST is loopback-only; no public-network or hosted production
  security boundary is claimed;
- MCP, if installed separately, is an optional adapter with its own boundary;
- no network listener in the legacy AML adapter is a supported product security
  boundary;
- `user_id` isolation is not tenant-grade physical multitenancy;
- there is no semantic PII scanner inside an otherwise allowed memory body;
- there is no encryption-at-rest or key-management layer beyond what the host
  operating system and storage provide;
- model-backed invocation, hard timeouts, process isolation, rate limits,
  cost controls, egress approval, and credentials belong to the injecting host
  and are not supplied here. The included quote-anchored adapter supplies only
  the prompt and strict output-to-candidate validation boundary.

Do not bind the legacy AML HTTP server to an untrusted network and describe it
as the governed product. Governed REST is a separate handler and still must not
be placed behind a public listener or reverse proxy and presented as a verified
remote/multitenant service.
