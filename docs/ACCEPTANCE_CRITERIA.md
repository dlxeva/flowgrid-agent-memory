# Release acceptance criteria

This document defines stable gates. Commit-specific counts, hashes, runner
versions, and artifact sizes belong in generated release evidence, not in this
file.

A releasable commit must satisfy all of the following:

1. Core tests pass on Python 3.11, 3.12, and 3.13.
2. The official MCP-SDK suite passes on Python 3.11.
3. Every transition into `confirmed` has durable same-user source evidence.
4. Resolver truncation remains explicit through `ContextPack.completeness`.
5. Owner review, current-state query, context compilation, transition, and
   privacy erasure preserve their documented authorization boundaries.
6. Context compilation respects record, item, aggregate, and final-pack limits.
7. A wheel and sdist build successfully from the tagged source.
8. The wheel installs in a fresh environment and exposes `flowgrid_memory` plus
   the three product command entry points.
9. The supported `cli` and `mcp` container targets both build and execute their
   documented smoke commands; the image exposes no REST port target.
10. Release assets include SHA-256 checksums, acceptance JSON, SPDX SBOM, and
    provenance JSON.
11. GitHub artifact attestations are created for the wheel and sdist.

The release workflow runs these gates before creating the tag and GitHub
Release. Generated assets are the evidence for one exact commit and version.
Local development can execute the main code gate with:

```bash
./scripts/run_tests.sh --with-mcp
```

Container verification runs in the repository CI and release workflow from the
same Dockerfile targets documented in [CONTAINER.md](CONTAINER.md).
