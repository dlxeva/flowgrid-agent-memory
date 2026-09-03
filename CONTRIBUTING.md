# Contributing

FlowGrid Agent Memory is built around evidence provenance, explicit truth
states, current-state resolution, abstention, owner gates, and minimal authorized
disclosure. Changes must preserve those invariants rather than silently turning
retrieved or extracted text into confirmed truth.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
./scripts/run_tests.sh --with-mcp
```

## Pull requests

- Keep changes scoped and describe the trust-boundary impact.
- Add regression tests for state transitions, authorization, provenance, and
  disclosure behavior when relevant.
- Use only synthetic fixtures. Never commit real memory databases, credentials,
  private transcripts, host traces, or local project ledgers.
- Label synthetic, observed, inferred, and externally verified evidence
  honestly. Local evaluation is not an official AML score.
- Do not add a path that automatically promotes a candidate or inference to
  `confirmed` without an explicit authorized transition.

By contributing, you agree that your contribution is licensed under the MIT
License in [LICENSE](LICENSE).
