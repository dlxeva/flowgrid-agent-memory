# Reproducible release verification

The release verifier freezes an explicit public source allowlist, builds twice
offline, and requires identical wheel and normalized sdist bytes. It then
rebuilds the wheel from the sdist and requires the same wheel SHA256. A fresh
venv outside the checkout installs each artifact with `--no-index --no-deps
--no-build-isolation` and exercises isolated public namespace imports, CLI
doctor, the governed lifecycle demo, the fixed governance fixture and the AML
hosted entry point. The base package has no third-party runtime dependencies.

中文说明：这个验收检查的是同一份公开源码能否重复打出相同发行包，以及脱离仓库后的真实离线安装和治理流程。它不代表 AML 平台评分或公网部署已通过。

## Offline prerequisites and invocation

Use CPython 3.11 or newer with pip and locally installed `setuptools>=77`.
No build frontend or index access is needed. For the sdist installation, the
verifier copies the caller's exact installed setuptools distribution into a
new venv as a build-only tool. The fresh wheel installation does not receive
that tool. Both installs disable indexes and build isolation.

Choose an existing temporary parent and an empty output directory outside the
checkout. For large local work, use an already-mounted external volume.

```bash
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \
python scripts/verify_release.py \
  --output /tmp/flowgrid-release-artifacts \
  --work-parent /tmp \
  --require-committed
```

`--require-committed` is required for CI and public release candidates. It
checks every frozen public input against the same HEAD revision. A local
work-in-progress rehearsal can omit the flag; its embedded identity then
reports `source_state=uncommitted` and `source_commit=null`. An old HEAD is never
used to identify changed source code.

The default epoch is fixed at `1788307200`; releases normally provide the
candidate commit's timestamp. Wheels already respect `SOURCE_DATE_EPOCH`.
The verifier additionally normalizes the sdist gzip timestamp, tar timestamps,
owner IDs, names, modes and entry order. Byte reproduction requires the same
source digest, epoch and build-tool versions recorded in
`release-build-tools.json`. The project's normal build dependency range stays
compatible with supported Python versions.

## Source and data boundary

`scripts/verify_release.py` contains the reviewed `SOURCE_ALLOWLIST`, and
`MANIFEST.in` mirrors its permitted source distribution contents. The runtime
file set is fixed in `aml_retriever/release_integrity.py`. The two included JSON
resources are synthetic `governance_v1.json` and the pinned
`legacy_v11_small.json`. A generated `release_identity.json` is the only
additional runtime JSON. Future source modules or resources need an explicit
allowlist change.

The source freeze never reads unlisted files. Local `.flg`, databases, data,
reports, caches, credentials, real host data and project control files have no
route into an accepted artifact. Both built archives are inspected against
the exact allowlists and compared with the frozen source bytes. Symlinks,
unexpected archive members and path traversal fail the gate.
The release workflow also runs `check_public_boundary.py` against every actual
Git index blob; checking the packaging subset alone is insufficient for a
public source repository. The same portable text scanner checks frozen source
blobs, generated identity, archive metadata and the verification report.

## Embedded identity and output

`aml_retriever/release_identity.json` uses schema
`flowgrid.agent-memory.release-identity/v1` and contains product/adapter
versions, the frozen source digest, the runtime digest and reviewed runtime
file list, plus the verified source commit or an explicit uncommitted state.
Runtime hashing uses a sorted JSON list of relative path, file-content SHA256
and byte length, encoded with sorted keys, two-space indentation, UTF-8 and a
final newline. The hosted adapter recomputes this digest at startup and rejects
runtime drift. This integrity check supplements release provenance and does
not replace a signed supply-chain attestation.

The output directory contains wheel, sdist, `checksums.txt`,
`release-verification.json`, `source-manifest.json` and
`release-build-tools.json`. Reports contain relative source names, artifact
names, hashes and boolean checks. They omit local paths, memory contents and
subprocess logs.

The existing full test, MCP stdio, container, SPDX SBOM and GitHub attestation
gates remain separate requirements. `generate_release_evidence.py` retains
that evidence flow. A successful local verifier does not authorize publication
or deployment and does not claim an official AML result.
