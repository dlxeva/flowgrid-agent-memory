# FlowGrid Agent Memory v0.1 acceptance

The previous version of this page embedded mutable test counts, artifact hashes,
local Docker versions, and historical review claims. Those values could drift
away from the commit displayed by GitHub.

Stable gates now live in [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md). The
`v0.1.0` GitHub Release contains commit-bound generated evidence:

- `acceptance.json`
- `checksums.txt`
- `sbom.spdx.json`
- `provenance.json`
- wheel and sdist artifacts

GitHub artifact attestations provide the signed build-provenance and SBOM
records for the published archives.
