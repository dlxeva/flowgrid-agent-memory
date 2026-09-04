# Context completeness contract

Governed reads expose an explicit completeness signal whenever `max_records`
limits a result. This prevents a safe partial result from being mistaken for an
exhaustive current-state answer.

## Resolver result

`CurrentStateResult.to_dict()` includes:

```json
{
  "completeness": {
    "matched_count": 120,
    "returned_count": 100,
    "truncated": true
  }
}
```

`matched_count` is the number of records eligible after lifecycle, validity,
scope, and conflict resolution but before `max_records`. `returned_count` is
the number of records returned by the resolver.

## ContextPack

REST and MCP return the same public `ContextPack` fields:

```json
{
  "status": "ready",
  "completeness": {
    "complete": false,
    "matched_count": 120,
    "returned_count": 80,
    "reason": "resolver_limit_and_budget"
  },
  "omitted": {
    "count": 40,
    "by_reason": {
      "resolver_limit": 20,
      "budget": 20,
      "policy": 0,
      "audit_evidence": 0
    }
  }
}
```

Possible completeness reasons are `resolver_limit`, `budget`,
`resolver_limit_and_budget`, and `not_evaluated`. A `ready` pack remains safe
to inject because every returned item passed governance and disclosure checks;
callers that require an exhaustive answer must also require
`completeness.complete == true`.
