# Optional finite-budget semantic cue host

This entry point adds the fixed `gpt-4o-mini-2024-07-18` cue adapter to the
authenticated loopback AML Add/Search service. It is disabled unless explicitly
configured. It makes no quality or official evaluation claim.

## Boundaries

- The core remains dependency-free and model-neutral. Original messages remain
  evidence. Cue strings are untrusted retrieval aids and never confirmed memory.
- Add offers every message in whole-message chunks of at most twelve, each
  within the 16KiB model request limit. All chunks must pass before raw Add
  commits. Offering all messages does not guarantee useful model keywords.
- No automatic credential discovery, provider fallback, retry or budget reset.
- The private SQLite budget binds scope, runtime digest, retrieval profile,
  total estimated USD and attempt limit. Each call reserves 5,376 microUSD,
  then settles validated usage at conservative uncached input/output prices.
- The estimates use $0.15 per million input tokens and $0.60 per million output
  tokens. Recheck provider prices before authorizing a paid deployment. Estimates
  are not invoices and cannot guarantee the provider's final billed amount.
- An unresolved call keeps its entire reservation and blocks new calls. Provider
  or semantic failure persists a halt. Restarting does not remove either state.
- HTTP operations use a bounded FIFO (16 by default, including active work),
  while provider calls and source/cue operations remain serial. Queue saturation,
  waiting expiry and cancellation return 429 without starting provider work.
  Excess work is not retained for later execution. Shutdown cancels queued work.
- Waiting shares the original HTTP operation deadline. The handler propagates
  deadline/cancellation, and every model call checks it before reserving and
  before launching. Provider network/process limits are at most120/130 seconds,
  additionally clipped to remaining operation time, without automatic retries.
  A paid call with unknown outcome still retains its hold and halts the scope.
- Configure hosted operation/request limits as270/300 seconds, max_inflight at
  least queue_capacity and max_connections at least queue_capacity+2. This
  capacity includes waiting requests; it is not16 concurrent model calls. Very
  slow upstream calls or large multi-chunk Adds can exhaust the shared deadline.
  Synthetic concurrent tests do not establish real provider or Full capacity.
- This changes the runtime from fixed commit9bc6488. It is a local candidate
  until separately published, deployed and registered/disclosed to AML. Never
  silently replace the implementation of an already-bound evaluation version.

## Explicit configuration

Run `python -m aml_retriever.aml_semantic_host --config /absolute/config.json --check`
to check configuration shape without reading credential files, opening a
listener, creating a ledger or making a provider request. This does not verify
deployment readiness or grant spending authority.

Configuration has exactly four sections:

```json
{
  "api_key_file": "/private/service/api.key",
  "hosted": {
    "db_path": "/private/service/source.sqlite",
    "host": "127.0.0.1",
    "port": 8081,
      "max_inflight": 16,
      "max_connections": 32,
    "operation_timeout_seconds": 270,
    "request_timeout_seconds": 300,
    "require_build_identity": true
  },
  "semantic": {
    "model_key_file": "/private/service/openai.key",
    "cue_db_path": "/private/service/cues.sqlite",
    "budget_db_path": "/private/service/budget.sqlite",
      "enabled": true,
      "queue_capacity": 16
  },
  "policy": {
    "scope": "replace-with-approved-scope",
    "implementation_sha256": "replace-with-installed-runtime-sha256",
    "profile_sha256": "replace-with-approved-retrieval-profile-sha256",
    "budget_microusd": 100000,
    "max_attempts": 10
  }
}
```

The example contains deliberate invalid digest placeholders; it cannot be used
as an authorization. Set finite limits only within the approved provider scope.
All paths must be canonical, absolute and separate. Credential files must be
regular private files (0600); the budget directory must already exist and be
private (0700). No symlink or shared personal experiment ledger is accepted.

Actual startup omits `--check`. It requires matching runtime and retrieval
digests and a committed build manifest. Do not bind an active official task
to a changed implementation without organizer confirmation. Keep credentials
out of source control, logs, public configuration and official email bodies.

## Local evidence

Offline tests replace provider subprocesses with synthetic responses. They cover
finite-budget persistence, invalid receipts, concurrent admission, idempotent
Add, authentication, TopK100, user isolation, deletion and restart. These tests
cannot establish real provider latency, retrieval gains or official Smoke/Full
results. A separately built and installed candidate must pass release checks
before any scoped publication or deployment.

中文说明：这是可选模型服务入口。预算和调用次数会持久保存，异常即停；记忆治理边界不变。
配置检查不读取真实密钥或调用模型，真实启动另需固定版本和明确部署、费用范围。
