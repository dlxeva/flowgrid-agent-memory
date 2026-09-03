# RawEvent → Candidate Extractor Contract v1

This contract defines a replaceable, auditable extraction boundary. It does
**not** claim that the built-in implementation understands ordinary natural
language, and it does not change the official AML Add/Search wire contract.

## 1. Non-negotiable state boundary

An extractor may propose only these payload fields:

- `memory_key`
- `memory_type`: `fact`, `preference`, `event`, `procedure`, or `judgment`
- `subject`
- `content`
- exact `evidence_spans` (host-callable path only)
- optional `confidence`, `valid_from`, and `valid_until`

The core, not the extractor, binds `user_id` and `scope`, derives authority
from same-user RawEvents, creates the record ID, records the extractor as
`created_by`, and forces the initial state to `candidate`. An extractor cannot
set or smuggle `status`, `authority`, `scope`, `user_id`, `record_id`,
`confirmed*`, `supersedes*`, or any undeclared field. A user-authored sentence,
`confidence: 1`, or a model output still produces only a candidate requiring
the existing owner/authority transition gate.

`subject: "$user"` is the only trusted subject alias. Immediately before
persistence the core resolves it to the service-bound `request.user_id`.
Extractors therefore do not need to hard-code a real user ID. Every other
subject is stored literally (useful for entity facts); a preference whose
literal subject is not the user remains unconfirmable under the governance
layer's direct-user preference rule.

## 2. Built-in deterministic directive

The zero-dependency default recognizes only a whole RawEvent body shaped as:

```text
@flowgrid.memory/v1
{"proposals":[{"memory_key":"profile.city","memory_type":"fact","subject":"user-profile","content":"杭州","confidence":0.99}]}
```

Leading/trailing whitespace around the whole message is tolerated. There may
be no surrounding prose, duplicate JSON object keys, non-finite JSON numbers,
unknown envelope fields, or unknown proposal fields. An ordinary message that
does not begin with the exact directive prefix is valid and deterministically
produces zero proposals.

Directive proposals do **not** accept source IDs or span fields. The core locks
each proposal's evidence to the complete immutable RawEvent carrying that
directive: `source_event_id = carrier.id`, `start = 0`, and
`end = len(carrier.content)`. Therefore a directive cannot cite another event
in the same batch to launder provenance.

The built-in `ExtractorIdentity` is marked `deterministic=true` and includes a
full SHA-256 `config_digest` covering its prefix, limit, and carrier-evidence
policy.

## 3. Host-injected callable

`CallableMemoryExtractor` accepts an explicit `ExtractorIdentity` and a host
callable. It adds no network client, model SDK, credentials, or environment
lookup. `MemoryService.compile_events` invokes it before opening the atomic
write transaction.

The callable receives an `ExtractionRequest` with an exact ordered RawEvent
snapshot and service-bound `trusted_scope`. Both scope mappings are frozen;
the canonical request digest is cached before invocation and recomputed after
invocation and again before persistence. Mutation attempts fail the entire
batch.

The callable may return `ProposalDraft` values, strict proposal mappings, a
`{"proposals": [...]}` envelope, or that envelope serialized as JSON. Each
callable proposal must include at least one evidence span:

```json
{
  "memory_key": "profile.city",
  "memory_type": "fact",
  "subject": "user-profile",
  "content": "杭州",
  "evidence_spans": [
    {
      "source_event_id": "raw_...",
      "start": 3,
      "end": 5,
      "quote": "杭州"
    }
  ]
}
```

Every source ID must be in the exact request batch, belong to the bound user,
and satisfy `raw.content[start:end] == quote` using Python Unicode codepoint
indices. Any bad proposal rejects the complete batch.

Exact duplicate proposals (the same full proposal SHA-256) are deterministically
collapsed to the first occurrence before record ordinals are assigned. This is
only byte-exact structural deduplication; the compiler performs no semantic
merge of merely similar proposals.

The core does not claim a hard timeout it cannot enforce. A model/process host
must perform real deadline enforcement, cancellation, rate limiting, and cost
control, then report a completed timeout as `TimeoutError`. The adapter returns
a safe `extractor invocation timed out` error. Timeout and other exception
messages never echo source bodies or raw model output, and failures write no
receipt, so the same key may be retried. A custom `MemoryExtractor` is an
untrusted boundary too: even if it deliberately raises one of FlowGrid's own
exception classes with source text in the message, `compile_events` replaces it
with the fixed `extractor invocation failed` category. Only exact built-in
framework implementations receive the framework's internal safe-error handling.
The custom extractor call boundary also converts control-flow exceptions such
as `SystemExit` and `KeyboardInterrupt` to that fixed category so an in-process
plug-in cannot terminate the service or use an exception message to disclose a
source body. Real cancellation and hard timeout remain responsibilities of the
host executor or process boundary; they must not rely on raising a control-flow
exception from inside the extractor.

For reproducibility, `ExtractorIdentity` records:

- stable `name`, `version`, and `implementation`;
- full lowercase SHA-256 `config_digest` (model, prompt, tokenizer, sampling,
  or other behavior-affecting configuration belongs in its preimage);
- a `deterministic` boolean;
- an exact full SHA-256 identity fingerprint over all fields.

## 4. Atomic compilation and idempotency

`MemoryService.compile_events` is an internal API. Its `trusted_scope` must be
constructed by an authenticated host adapter, never copied from extractor or
request-body proposal fields. Official `/add` does not invoke it automatically.

The request digest is canonical SHA-256 over the complete exact request:
user, idempotency key, trusted scope, ordered RawEvent metadata and bodies, and
the full extractor identity/config fingerprint. Only the digest, not a second
copy of those bodies, is stored in the receipt.

The idempotency key is `(user_id, idempotency_key)`:

- same key + same exact digest returns the original record IDs and does not
  invoke the callable again;
- same key + different digest fails with `ExtractionConflict` before invoking
  the callable;
- zero-proposal success is also receipted and idempotent;
- concurrent same-digest calls may both finish external extraction, but a
  single `BEGIN IMMEDIATE` check/persist section writes exactly one candidate
  set and all callers receive the winning IDs.

The complete output is schema- and evidence-validated before any derived
insert. Inside `BEGIN IMMEDIATE`, the compiler re-reads every source through
`raw_events JOIN messages` and exactly compares content, role, authority,
timestamps, scope, locator, and message ID with the pre-extraction snapshot.
It repeats request-integrity and live-source verification immediately after
defensively cloning proposal objects, closing mutation through a hostile
`ProposalDraft` subclass. Authority and `observed_at` are then derived only
from that transaction-local sealed source map, never from caller-owned request
objects. This also closes deterministic-ID reuse after a concurrent privacy
erase/re-Add and rejects forged lower-level RawEvent snapshots. The transaction
then writes all candidate records, initial candidate state events, proposal
origins, and the success receipt. Any failure rolls all four back while leaving
the currently accepted RawEvents untouched.

`proposal_origins` stores the exact proposal fingerprint and evidence locators
with `quote_sha256`; it does not duplicate evidence text. Both origins and
receipts are update-immutable and are removed by privacy erase/purge.

## 5. Schema gate and claim boundary

Extraction storage has an independent `extraction_meta.schema_version`.
Initialization is serialized and rejects malformed or future versions. For v1
it compares the exact ordered table layouts (declared types, `NOT NULL`,
defaults, and primary-key ordinals, including the receipt's composite key) and
the normalized `sqlite_master.sql` of both immutability triggers. A trigger with
the expected name but the wrong table/event/body, or without the specified
`RAISE(ABORT, ...)`, is therefore not accepted. This gate does not alter the
governance schema version.

This v1 proves the local directive/callable boundary, provenance validation,
candidate-only governance, atomicity, idempotency, and deletion behavior. It
does not prove general free-text extraction quality, hosted multitenant
isolation, semantic sensitive-data classification, or an official AML score.
In particular, `authority=user` is derived from a RawEvent whose role is
`user`; a hosted adapter would still need to authenticate and bind that role
rather than trusting an arbitrary public request body.

## 6. Quote-anchored model adapter

`QuoteAnchoredModelExtractor` is a provider-neutral adapter for ordinary text.
It builds a canonical prompt from the exact RawEvent batch and an explicit,
versioned key catalog, then passes that prompt to a host-injected runner. The
library still contains no provider SDK, network client, credential lookup, or
unenforceable timeout. Provider/model/sampling configuration belongs in the
extractor identity digest, while the host owns real deadlines, cancellation,
rate limits, and cost controls.

The model returns `evidence_quotes`, not offsets. The adapter accepts a quote
only when it occurs exactly once in the cited immutable event, computes Python
Unicode-codepoint offsets locally, and then emits the existing `EvidenceSpan`.
Missing, repeated, or cross-batch quotes reject the complete output. The model
may use only catalogued keys and their fixed memory types; unknown fields,
including every governance field, are rejected. Output is still persisted only
as `candidate`, so a fluent sentence or high confidence never becomes current
truth without the existing authority transition.

The prompt directs the model to abstain on ambiguity, inference, stereotypes,
hypotheticals, negations, examples, third-party claims, quoted instructions,
credentials, tokens, and secrets. This is a model instruction plus a structural
gate, not a proved semantic sensitive-data classifier. Extraction quality must
therefore be evaluated per pinned provider configuration, and synthetic tests
must not be described as production behavior or an official AML result.
