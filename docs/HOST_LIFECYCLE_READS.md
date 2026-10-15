# Optional host lifecycle read gate

`aml_retriever.host_lifecycle_reads.LifecycleMemoryReader` is disabled by default.
It requires trusted `memory`, `refresh_principal`, `serialize_user`,
`is_user_active` and `is_source_active` callbacks. No authentication backend,
permissions, network service or default transport integration is installed.

中文说明：宿主须提供真实身份刷新、实时撤销和所有 writer 共享的 user 锁；类型和 Owner 字段不能充当认证。需要显式接入调用路径，旧 SDK/SQL、REST/MCP 不会自动获得保护。

Supported calls are `query_current`, `query_audit`, `compile_context` and
`compose_numeric_evidence`. Supply the host-authenticated `principal` and
`user_id`; query options are `scope`, `memory_key`, `query`, `as_of`,
`max_records`, with `max_chars`, `max_tokens`, `token_counter` for compilation.
Audit/evidence privileges and purpose checks remain required. Callers cannot
override access contexts or disclosure policies in request kwargs.

Source-candidate extension: `read_source_events(user_id=..., principal=...,
source_event_ids=[...], scope=...)` reads an exact batch of 1–32 unique raw
source IDs without requiring derived memory records. It still requires explicit
read/audit permissions and an allowed audit purpose, fresh grant and lifecycle
checks, and exact user/scope binding. Missing, deleted, out-of-scope or revoked
sources refuse the whole batch. Raw evidence never becomes a confirmed decision.
This source extension is included in the locally validated source-evidence
candidate. That candidate is uncommitted and is not a published release.
The extension adds no REST endpoint or MCP tool automatically.

中文说明：该接口用于“有原文、尚无候选”的事实问答，权限不自动增加；来源用途由可信宿主指定，审计批准仍必需。返回是证据快照，不能直接变成当前行动依据；须另行应用字符/披露预算，并在使用前复核授权。已进入本地验证候选包，尚未正式发布，也未新增 REST/MCP 原文工具。

```python
reader = LifecycleMemoryReader(
    memory=memory,
    refresh_principal=trusted_host.refresh_principal,
    serialize_user=trusted_host.serialize_user,
    is_user_active=trusted_host.is_user_active,
    is_source_active=trusted_host.is_source_active,
    enabled=True,
)
pack = reader.compile_context(
    user_id=authenticated_user,
    principal=authenticated_principal,
    scope=trusted_scope,
    memory_key="decision.plan",
    max_chars=4096,
)
```

All bindings above are supplied by trusted host code, not model JSON. The host
callbacks must perform fresh authenticated lookup. All lifecycle/event/source
writers must share the serialization domain. Callback code is not sandboxed.

## Trusted MCP factory integration

`create_mcp_server(db_path=..., principal=...,
lifecycle_reader_factory=trusted_factory)` accepts a Python host configuration
hook, not a request field or CLI JSON option. The factory receives a deferred
read facade and must return a `LifecycleMemoryReader` bound to that exact facade.
It performs live checks before the facade opens an existing SQLite file in
read-only mode. An unavailable, disabled or wrongly bound gate fails closed;
it never falls back to direct SDK reads.

Reader-only MCP configuration rejects ingest/extract operations even when
static grants contain write permissions. An explicit paired reader/writer
configuration supports candidate-only ingest/extract as documented in
[HOST_LIFECYCLE_WRITES.md](HOST_LIFECYCLE_WRITES.md).
Its four-tool discovery contract remains unchanged. Existing default MCP mode
does not gain lifecycle protection automatically. The factory is trusted code,
not a model-generated callback or a production identity implementation.

中文说明：宿主通过 Python 配置注入读取门，MCP 请求不能启用它。仅配置读取工厂时打开已有只读库并拒绝写入/提取；显式匹配双工厂才允许候选写入。默认模式保留原契约，真实身份后端仍需宿主接线。

## Trusted REST factory integration

`GovernedRestAdapter(config, lifecycle_reader_factory=trusted_factory)` calls
the Python factory with a deferred read facade and the authenticated principal.
The reader must bind to that exact facade. Bearer/local authentication, exact
request fields, audit purposes, historical/current separation and public
response allowlists remain in force. Request/config JSON cannot provide code.

Reader-only configuration defers an existing-file read-only open until live
checks pass and refuses events, extractions, transitions and erase operations.
Explicit paired reader/writer configuration permits only events and candidate
extractions; transitions and erase operations remain refused. See
[HOST_LIFECYCLE_WRITES.md](HOST_LIFECYCLE_WRITES.md). Query and context responses check
lifecycle before returning; shutdown waits for request leases and closes the
lazily opened facade. Default REST mode does not acquire this guard by itself.

中文说明：REST 保留原鉴权及字段契约，只通过可信 Python 配置接线。仅读取工厂的模式保持只读；显式匹配双工厂才允许原文及 candidate 写入，确认和管理删除继续拒绝。没有自动启用既有服务。缓存、最终发送窗口和真实认证仍需各自验证。

The gate authorizes before storage access, checks user and result source
lifecycle, checks related withheld/conflict metadata, and rechecks the grant
and lifecycle before returning. A revoked result is refused as a whole; it is
not filtered to revive an older decision. Unrelated source queries remain
usable. Errors carry no memory body. `supported` reviews still do not confirm
memories or authorize execution.

Returned objects are read snapshots, not future disclosure permits. Cached
plaintext does not disappear after revocation. Read again or reauthorize before
sending, displaying or acting, and independently apply transport/data-use
consent. Physical erasure, provider copies, budgets and signed authentication
remain outside this adapter.

中文说明：读取门拒绝受撤销影响的整次结果，不回退旧决定；返回前复查也不等于永久授权。发送、展示、执行和外部费用各有独立约束。局部合成测试不能证明真实账户后端、全 writer 协调或真实任务收益。
