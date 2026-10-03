# AML Cycle 2 candidate: FlowGrid Agent Memory

Candidate product version: `0.1.1`. AML adapter version: `1.1.0`.

Public repository: <https://github.com/dlxeva/flowgrid-agent-memory>.
Track: Textual Memory. Division: Open-source Methods.
Official requirements reviewed on 2026-10-03:
<https://agentmemories.ai/api-guide> and
<https://agentmemories.ai/competition/>.

## Version freeze and evidence

The source commit is the full Git SHA of the reviewed candidate, recorded by
the release verifier after the public source is committed. Never substitute an
older HEAD for a build containing new source changes. Release evidence records
the source digest, artifact SHA-256 values, build environment and verification
results. The deployed service identity must match that evidence.

Use [RELEASE.md](RELEASE.md) for offline verification and
[AML_DEPLOYMENT.md](AML_DEPLOYMENT.md) for the dedicated service. A deployment
operator must supply the real HTTPS Add/Search/Health addresses and confirm
capacity before sending an evaluation request. No live endpoint is claimed by
this document.

## Attribution and method changes

The project derives from FlowGrid AML Retriever v1.1, originally published by
dlxeva at <https://github.com/dlxeva/flowgrid-aml-retriever>, baseline commit
`cdae7dbd38d73eda33793b30017559bdfb75eff5`, under the MIT license.
The standalone public repository uses a clean public history. Internal project
ledgers and runtime memory have never been required for reproduction.

The inherited AML path uses SQLite FTS5, deterministic message/session views,
lexical and evidence features, guarded supersession, deduplication and weighted
RRF. Search returns ranked evidence and never generates the final answer.
The default candidate uses no Add or Search model and no vector dependency.
The official guide states that open-source Add model use is expected to use
`gpt-4o-mini`; disclose the deterministic, no-model path in the application and
obtain organizer acceptance before starting a Full run. Do not silently add a
model provider or transfer local transcripts to satisfy this requirement.

The public product adds a stable `flowgrid_memory` facade, source-backed
governance records, explicit lifecycle transitions, current-state resolution,
authorized context compilation, owner review, extractor conformance and
resource-bounded local REST/MCP surfaces. Relative-time reranking remains
default-off under its existing non-regression evidence gate.

This candidate changes release verification, the dedicated AML transport and
Streaming/retry tests. It does not change retrieval weights, silently confirm
derived records, or reinterpret raw messages as owner-approved truth. The AML
compatibility path retrieves historical evidence; the governed product's
current-state and owner-review APIs are a separate surface. Passing the product
governance suite alone does not demonstrate that AML exercises those APIs.

## FLG memory invariants

Raw evidence and derived records remain separate. Candidate, inferred,
unknown, confirmed, superseded, rejected and deleted keep distinct meanings.
Confirmation requires explicit authority and source evidence. Ordinary governed
continuation suppresses invalid records, preserves uncertainty, and retains
permission and provenance boundaries within its context budget.

Every candidate must pass the AML contract/regression gate and the governed
product invariant gate. All new tests and rehearsals use synthetic data. Local
pass/fail results are implementation evidence, not an official score or a
retrieval comparison against another system.

## Operations and retention

The evaluation service exposes only Health, Add and Search, requires a dedicated
credential for writes/reads, and keeps administration local. TLS, durable disk,
restart policy, request timeouts and configured concurrency are operator
responsibilities described in the deployment guide. Capacity declarations must
use the actually deployed limits and measured evidence.

Use a dedicated database for AML. Request bodies, retrieved text, user/session
identifiers and credentials must not enter access logs, source archives or CI
artifacts. Organizer data and derived copies are used only for the evaluation;
delete them within 30 days of completion unless the organizer grants written
permission. Deletion of real evaluation data is an explicit operator action.

## Remaining external inputs

- Real deployment host, domain, dedicated storage and operator access.
- Private Memory System Key delivered through the organizer's approved channel.
- Contact details and information approved for public display.
- Organizer acceptance of the declared no-model Add/Search method.
- Evaluation Access Request approval and an issued Eval Key.
- Official Smoke, hosted stability evidence and then Full.

Materials are due 2026-10-31 23:59 (UTC+8), and evaluation closes
2026-11-04 23:59. Full runs are limited to two per key/track, with the second
unlocking 30 days after the first completes. Do not consume a Full merely to
discover an avoidable transport or packaging failure.
