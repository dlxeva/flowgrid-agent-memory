# Optional candidate-only host writer

LifecycleCandidateWriter in aml_retriever.host_lifecycle_writes is disabled by
default. It requires a trusted writable memory facade, fresh same-actor
refresh_principal, serialize_user, is_user_active and is_source_active callbacks.
No authentication backend, LLM transport or grant is installed.

中文说明：宿主须提供真实身份、最新权限和所有 writer/撤销共同使用的锁。代码不会从模型 JSON 或 role=user 推断真人批准。

Only ingest_raw_events and extract_candidates are provided. Supply the
host-authenticated principal, user_id and optional requested_scope. Ingestion
also takes request_id, session_id and native JSON messages. Extraction takes
raw_event_ids, idempotency_key and an optional host-selected extractor; no
request field should choose executable code.

The writer canonicalizes user identity for locks and storage, authorizes the
operation, checks lifecycle, and seals bounded native messages. Ingestion writes
raw evidence, not confirmed truth. Extraction wraps the extractor with a stable
guard-specific identity, verifies source scope, materializes and validates the
bounded proposal array, then rechecks grants/lifecycle before core persistence.
The core still validates evidence and stores candidate only. Idempotent receipt
returns also require live checks; an unguarded receipt cannot silently become
a guarded extraction because their fingerprints differ.

Named operations do not forward proposal confirmation, transitions, erasure
or SQL. Trusted Python retaining the underlying memory can bypass this adapter:
it is not a sandbox. All grant/source/user writers must use the same lock
domain. This is not a distributed transaction or automatic rollback after an
already committed ingest; a non-coordinating backend violates the contract.

中文说明：正常结果继续是 candidate，治理字段注入不会变成确认。撤权后的旧输入/回执不能当成功。锁域与回调真实性没有实现就不能声称生产安全。

REST/MCP read-gated modes remain read-only until a separately reviewed trusted
writer factory is wired. This module alone does not enable transport writes,
schema management, confirmation or deletion. Existing profiles are unchanged.
LLM extraction requires separate data-egress, cost, retention and consent
controls; writer permissions do not authorize provider calls.

中文说明：安装模块不等于启用真实宿主。模型外传与费用有独立批准门，局部合成测试不证明真实身份或任务收益。

## Trusted MCP candidate writer configuration

create_mcp_server accepts lifecycle_writer_factory only when a trusted
lifecycle_reader_factory is also provided. Both receive the same deferred
candidate facade. Their refresh_principal, serialize_user, is_user_active and
is_source_active callback objects must match; accidental policy/lock mismatch
is refused, not replaced by direct SDK access. This configuration check does
not certify a real identity issuer or distributed locking.

Read-only MCP configuration remains read-only when the writer factory is
absent. Explicitly configured candidate mode routes the existing ingest/extract
tools through the live writer and current/context tools through the live reader.
No confirmation/admin tool or JSON/CLI code-injection field is added.

The trusted configured path must already contain a ready compatible database.
Before the normal writable open, a read-only preflight refuses missing,
uninitialized or migration-requiring input. The host must coordinate schema and
filesystem writers and keep the configured path stable; this is not a forensic
file-swap guarantee. No schema-management RPC is offered.

中文说明：写入工厂需要显式配置且和读取共享回调，原只读模式不改变。只接 candidate ingest/extract，不放开确认/管理操作。已有库预检与路径稳定仍是宿主责任，不把配置匹配当生产认证证明。

## Trusted REST candidate writer configuration

GovernedRestAdapter accepts lifecycle_writer_factory only with a trusted
reader factory. Each receives the same deferred candidate facade and the
authenticated principal. Enabled read/write gates must use matching identity,
user/source lifecycle and serialization callbacks. Incorrect binding or a
disabled pair fails closed before storage access, with no direct SDK fallback.

Explicit paired mode routes events and extractions through the live candidate
writer while retaining bearer/body/scope contracts. Current/audit/context reads
stay guarded. Transition and erase-user RPCs remain refused even with static
Owner/admin grants. Reader-only configuration remains read-only. No JSON/CLI
writer flag or user-controlled extractor is introduced.

As in MCP, the configured file must pass a read-only ready-schema preflight
before a normal writable open; path/schema writers must be coordinated by the
host. Existing profiles/services and real identity backends are not activated
by installing these modules.

中文说明：REST 双工厂模式仅接原文和 candidate 提取，原鉴权/请求契约保留，确认和管理删除继续拒绝。生产认证、路径稳定、共享锁和模型外传许可仍须独立落实。
