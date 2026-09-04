# Governed REST v1

Governed REST v1 is a local, single-tenant adapter over `FlowGridMemory`. It is
not the legacy AML Add/Search server and does not call that handler. The only
accepted bind address is the literal IPv4 loopback address `127.0.0.1`.

## Startup contract

Run with an explicit product configuration:

```bash
export FLOWGRID_MEMORY_BEARER_TOKEN='replace-with-a-local-secret'
flowgrid-memory-rest --config /absolute/path/to/product.json
```

The configuration is schema-checked before the writable database is opened.
It must contain an absolute database path (or the explicit test-only spelling
`:memory:`), a literal `127.0.0.1` bind, a fixed trusted principal, and either:

- `local`: no wire credential, for a trusted local process boundary only; or
- `bearer`: a non-empty token resolved from the explicitly named environment
  variable. The token is compared with `hmac.compare_digest`.

`none`, `0.0.0.0`, hostnames, non-loopback IPs, missing bearer secrets,
unknown permissions, and malformed policies fail startup. The product example
contains only an environment-variable name, never a token.

## Trusted identity

The adapter constructs one immutable `TrustedPrincipal` from configuration.
Request JSON cannot set or replace its `principal_id`, `authority`, users,
scopes, permissions, purpose, or audit-purpose allowlist. Unknown request
fields are rejected.

Permissions are independent:

| Permission | Operation |
| --- | --- |
| `memory:write` | append original events |
| `memory:extract` | run the built-in directive extractor |
| `memory:read` | current query and current context |
| `memory:audit` | audit query/context, together with `memory:read` and a trusted purpose |
| `memory:transition` | request a governed transition |
| `memory:erase` | user-wide privacy erasure when separately enabled |

Every operation validates the concrete user and requested scope before calling
the facade. Audit rejects wildcard user grants. A REST transition additionally
requires `memory:read` + `memory:audit`, a permitted trusted audit purpose, and
an exact `memory_key`: the adapter performs an audit-authorized direct
primary-key metadata lookup and proves the target's immutable scope before
mutation. The lookup has no `max_records` window and loads no memory content,
raw evidence, or state-event body. This avoids trusting a request-declared scope
for an opaque record ID. Transition and erase
also require a principal authority of `user`, `owner`, or `policy`; granting the
permission to an agent/system/external/unknown principal still returns a fixed
denial.

## Endpoints

`GET /v1/health` is anonymous and returns only `product_version` and `ready`.

All other routes are strict `POST application/json`:

- `/v1/events`: `request_id`, `user_id`, `messages`, optional `session_id` and
  `scope`. Messages require a role and non-empty content. The trusted effective
  scope is stored with each raw event. An exact retry of the same
  `(user_id, request_id)` returns the prior receipt; any role, content,
  timestamp, session, or effective-scope difference returns `409` without a
  partial write.
- `/v1/extractions`: `user_id`, `raw_event_ids`, `idempotency_key`, optional
  `scope`. It always uses the built-in whole-message
  `@flowgrid.memory/v1` directive extractor. A request cannot provide code,
  a callable, an extractor identity, lifecycle state, or authority.
- `/v1/memories/query`: current or explicitly authorized audit state rendered
  through the same fixed public field allowlist and opaque source locators as
  `ContextCompiler`. Internal withheld/conflict IDs, actors, state reasons, raw
  event bodies, and state-event bodies are not returned; audit evidence is
  represented by an omitted count.
- `/v1/context/compile`: current or audit `ContextPack`, plus an `injectable`
  boolean. `injectable` is true only for a current-mode `status == "ready"`;
  audit is never injectable even when its pack status is ready.
- `/v1/memories/transition`: requires `user_id`, `record_id`, `memory_key`,
  `target_status`, `reason`, and optional exact `scope`/`related_record_id`.
  Actor and authority always come from the principal. A related record is
  resolved by the same direct metadata lookup and must share the target's exact
  user, key, type, subject, and scope. The success receipt contains only
  `record_id` and
  `current_status`; it does not disclose internal provenance, actors, reasons,
  confirmation metadata, or relation IDs.
- `/v1/admin/erase-user`: user-wide privacy erasure. It requires both the
  startup `erase.enabled=true` gate and `memory:erase`. It rejects principals
  or requests restricted to a narrower project/session scope.

`max_chars` is supported for context compilation. `max_tokens` is rejected
with a fixed `unsupported_token_budget` response because this zero-dependency
server has no exact model tokenizer; it does not substitute an estimate.
`as_of` is audit-only: current query/context rejects it so a historical
deleted, rejected, or superseded record cannot be revived as ordinary context.

## HTTP and error behavior

The adapter requires a single Content-Type and Content-Length, rejects transfer
encoding, duplicate JSON keys, invalid UTF-8/JSON, non-finite numbers, bodies
over the configured cap, query strings, unsupported methods, and unknown
fields. HTTP/0.9, its two-word request-line form, malformed versions, and
unsupported later protocol versions are rejected with one fully framed HTTP/1.1
error response. Responses include `Cache-Control: no-store` and never emit a
wildcard CORS header. Every response closes its connection,
`Expect: 100-continue` is rejected, and a bounded socket-read timeout prevents
an authenticated partial body from blocking shutdown indefinitely. Shutdown
stops acceptance, drains active handlers, and only then closes the facade and
SQLite pool.

Errors have one shape:

```json
{"error":{"code":"forbidden","message":"operation not allowed"}}
```

Codes and messages are fixed. Denials do not echo user IDs, request bodies,
record IDs, database paths, tokens, exception messages, or tracebacks. The
server suppresses BaseServer traceback logging. Successful memory/query/audit
responses may contain data explicitly authorized for that operation and must
not be copied into access logs.

## Verified boundary

This v1 boundary is a local process/server for one trusted deployment. User IDs
are logical partitions, not physical hosted multitenancy. It does not provide
remote production exposure, TLS termination, tenant-grade storage isolation,
encryption at rest, semantic PII detection, rate limiting, or a distributed
authorization service. Put no reverse proxy or public listener in front of it
or describe it as a verified hosted service.
