# FlowGrid Agent Memory 0.1.0

The first governed-memory release establishes an evidence-backed current-truth
layer for local AI agents.

Highlights:

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

The release remains alpha and supports one trusted local host. It does not claim
a hosted multitenant perimeter, production natural-language extraction quality,
or a new official AML score.
