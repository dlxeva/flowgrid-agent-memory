# Extractor conformance kit

`flowgrid_memory.run_extractor_conformance` gives host integrators a
zero-dependency, machine-readable check for an injected extractor and the
production extraction boundary around it. Pass either an
`ExtractorIdentity` plus callable, or an object implementing
`MemoryExtractor`:

```python
from flowgrid_memory import ExtractorIdentity, run_extractor_conformance

identity = ExtractorIdentity(
    name="example.extractor",
    version="1",
    implementation="host-callable",
    deterministic=True,
)

report = run_extractor_conformance(identity, extract)
print(report.to_json())
```

The `security_contract` profile runs the supplied extractor through the real
`CallableMemoryExtractor`/compilation path and uses controlled probes to verify:

- accepted output is persisted only as `candidate`;
- source IDs, offsets, and quotes are checked against the exact input batch;
- undeclared governance fields such as `status` and `authority` are rejected;
- trusted request scope is immutable;
- invocation failures do not expose exception payloads;
- the production proposal limit is enforced;
- exact replay is idempotent and a changed request conflicts before invocation.

An extractor that safely returns no proposal is security-conformant. Extraction
coverage is a quality decision, not an authorization requirement. Set
`include_behavioral_quality=True` to add the separate
`behavioral.negative_text` probe, which expects abstention on one synthetic
hypothetical third-party statement. Hosts can use that optional signal as a
starting point, but should maintain their own domain-specific quality suite.

The report has a stable `flowgrid.extractor-conformance/v1` schema. Each case
contains only `id`, `status`, and a fixed `code`; neither source bodies, proposal
content, exception messages, nor tracebacks are emitted.

## Claim and execution boundary

Passing finite synthetic probes does not certify an extractor as trusted,
accurate, non-malicious, or safe for every input. The conformance kit grants no
permission and does not change candidate/owner governance. It invokes the
extractor in the current process, so it cannot terminate an infinite loop or
contain arbitrary process, filesystem, network, or cost side effects. Run
untrusted or model-backed implementations in a host-managed subprocess or
other isolation boundary with real timeout, cancellation, resource, network,
and credential controls before calling this harness.

All probe statements are synthetic. A pass is evidence about these contract
checks only, not production extraction quality or an official AML evaluation.
