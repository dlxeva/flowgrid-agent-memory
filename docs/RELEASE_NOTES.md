# FlowGrid Agent Memory 0.1.1

This candidate adds a dedicated AML evaluation service and reproducible offline
release verification to the governed-memory core for local AI agents.

Highlights:

- a dedicated, authenticated AML Add/Search service with bounded concurrency,
  explicit retryable errors and no network administration endpoints;
- synthetic Streaming checkpoints, retry and isolation rehearsal;
- source-bound identity, reproducible archives and fresh offline installation;
- public-source and distribution checks that exclude local memory databases,
  transcripts, project control files and credentials;

- confirmed records require durable same-user source evidence;
- resolver and budget truncation are explicit through completeness metadata;
- a local Owner Review CLI closes the human governance loop;
- REST transitions use exact authorized primary-key metadata lookup;
- ContextCompiler character budgeting is logarithmic and resource-bounded;
- `flowgrid_memory` is the stable public Python namespace;
- the OCI contract defaults to a non-networked CLI image with a separate MCP
  stdio target;
- release archives ship with checksums, acceptance evidence, SPDX SBOM,
  provenance JSON, and GitHub Sigstore attestations.

The governed product remains alpha and supports one trusted local host. The
separate AML service requires a TLS proxy and a dedicated evaluation database.
Public deployment, official AML Smoke/Full and a new score remain unverified
until their live evidence is available.
