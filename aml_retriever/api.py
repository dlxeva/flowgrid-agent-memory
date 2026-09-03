"""AML Retriever — 核心 API 与官方契约 wrapper（分层）。

分层关系：
    retriever.RetrieverDB   核心引擎（存储 + 检索，与赛事无关）
        ↑
    api.MemoryService       领域服务（校验 + 生命周期 + 隐私）
        ↑
    api.official_*          官方 Add/Search 契约的字段映射（唯一对外形状）
        ↑
    server.py               HTTP 传输层

官方字段严格依据 docs/API_CONTRACT.md（2026-08-06 抓取核对官方 api-guide）：
  Add    请求 {request_id, messages[{role,content,timestamp?}], user_id, session_id}
         响应 {success:true, request_id, user_id, session_id}
  Search 请求 {query, options?, user_id, top_k}
         响应 {data:[{id, content, score?, created_at?}]}

校验口径（2026-08-07 收紧，见 docs/API_CONTRACT.md §校验矩阵）：
  * role 必填且非空字符串（此前误作可选）。
  * top_k 必填且必须是真整数；bool / float / 数字字符串一律 422，不做静默转换。
  * timestamp 可选；给了就必须是整数毫秒，小数不截断而是 422。
  * 官方未声明的额外字段一律忽略，不报错。

核心与 wrapper 分离：核心层不认识 HTTP，也不认识官方字段名；
wrapper 只做字段映射与校验，不参与打分与排序。
"""
from __future__ import annotations

import os

from . import compiler as extracted
from .compiler import CompilationReceipt
from .access import AccessContext, DisclosurePolicy, authorize_memory_read
from .extraction import (
    CallableMemoryExtractor,
    DirectiveMemoryExtractor,
    ExtractionError,
    ExtractionRequest,
    ExtractionValidationError,
    ExtractorIdentity,
    ExtractorInvocationError,
    MemoryExtractor,
    ProposalDraft,
    validate_proposals,
)
from .config import RetrieverConfig
from .context import ContextCompiler, ContextPack, TokenCounter
from .governance import CurrentStateResult, MemoryRecord, RawEvent
from .retriever import AddResult, Evidence, RetrieverDB, SearchResult


class ApiError(Exception):
    """带 HTTP 语义的接口错误。响应体格式对齐官方 {"detail":{"reason":...}}。"""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = int(status)
        self.reason = str(reason)

    def to_body(self) -> dict:
        return {"detail": {"reason": self.reason}}


_MISSING = object()


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(422, f"'{key}' is required and must be a non-empty string")
    return value


def _coerce_timestamp(value, *, field: str) -> int:
    """严格解析 Unix 毫秒时间戳。

    只接受 int，或数值上等价于整数的 float（如 1.7e12）。
    显式拒绝 bool / 字符串 / 小数 —— 宁可 422 也不静默截断，
    因为静默截断会让上游误以为写入的时间戳被原样接受。
    """
    if isinstance(value, bool):
        raise ApiError(422, f"{field} must be an integer (unix milliseconds), got boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ApiError(422, f"{field} must be a finite integer (unix milliseconds)")
        if not float(value).is_integer():
            raise ApiError(
                422,
                f"{field} must be an integer (unix milliseconds); "
                "fractional values are rejected instead of being truncated",
            )
        return int(value)
    raise ApiError(422, f"{field} must be an integer (unix milliseconds)")


def validate_add_payload(payload: dict) -> dict:
    """校验官方 Add 请求。

    官方声明的必填字段：request_id / user_id / session_id / messages[]。
    每条 message 必须带非空 role 与非空 content；timestamp 可选。
    对官方未声明的额外字段一律忽略（不报错），以免真实评测里
    因为对方多传一个字段就整批 422。
    """
    if not isinstance(payload, dict):
        raise ApiError(422, "request body must be a JSON object")
    request_id = _require_str(payload, "request_id")
    user_id = _require_str(payload, "user_id")
    session_id = _require_str(payload, "session_id")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError(422, "'messages' is required and must be a non-empty array")

    normalized: list[dict] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise ApiError(422, f"messages[{index}] must be an object")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError(422, f"messages[{index}].content must be a non-empty string")
        # role 在官方 schema 里与 content 同级必填，这里按必填非空字符串校验。
        role = item.get("role")
        if isinstance(role, bool) or not isinstance(role, str) or not role.strip():
            raise ApiError(422, f"messages[{index}].role must be a non-empty string")
        timestamp = item.get("timestamp")
        if timestamp is not None:
            timestamp = _coerce_timestamp(timestamp, field=f"messages[{index}].timestamp")
        normalized.append({"role": role, "content": content, "timestamp": timestamp})

    return {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
        "messages": normalized,
    }


def validate_search_payload(
    payload: dict, *, top_k_max: int, top_k_default: int | None = None
) -> dict:
    """校验官方 Search 请求。

    官方声明的必填字段：query / user_id / top_k（正式评测固定 top_k=100）。
    top_k 必须是真整数：显式拒绝缺失、bool、float、数字字符串，
    避免 "100" / 100.7 这类输入被静默转换成一个我们自己编出来的值。
    `top_k_default` 参数仅为向后兼容保留，官方路径上不再使用。
    """
    if not isinstance(payload, dict):
        raise ApiError(422, "request body must be a JSON object")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ApiError(422, "'query' is required and must be a non-empty string")
    user_id = _require_str(payload, "user_id")

    top_k = payload.get("top_k", _MISSING)
    if top_k is _MISSING or top_k is None:
        raise ApiError(422, "'top_k' is required and must be an integer")
    if isinstance(top_k, bool):
        raise ApiError(422, "'top_k' must be an integer, got boolean")
    if isinstance(top_k, str):
        raise ApiError(422, "'top_k' must be an integer, got string")
    if isinstance(top_k, float):
        raise ApiError(
            422,
            "'top_k' must be an integer; fractional values are rejected "
            "instead of being truncated",
        )
    if not isinstance(top_k, int):
        raise ApiError(422, "'top_k' must be an integer")
    if top_k < 0:
        raise ApiError(422, "'top_k' must be >= 0")
    # 钳制到服务端上限，但不改变"必填"语义：客户端仍必须显式声明 top_k。
    top_k = min(top_k, int(top_k_max))

    options = payload.get("options")
    if options is not None:
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            raise ApiError(422, "'options' must be an array of strings")

    return {"query": query, "user_id": user_id, "top_k": top_k, "options": options}


class MemoryService:
    """领域服务层：校验、隔离、幂等、生命周期。不感知 HTTP。"""

    def __init__(self, config: RetrieverConfig | None = None, db: RetrieverDB | None = None):
        self.config = config or RetrieverConfig()
        self.db = db or RetrieverDB(self.config)
        self.include_provenance = (
            os.environ.get("AML_INCLUDE_PROVENANCE", "1").strip().lower()
            not in ("0", "false", "no", "off")
        )

    # -- 核心操作（内部形状）------------------------------------------------
    def add(
        self,
        *,
        request_id: str,
        user_id: str,
        session_id: str | None,
        messages: list[dict],
        trusted_scope: dict[str, str] | None = None,
        exact_replay: bool = False,
    ) -> AddResult:
        return self.db.add(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            trusted_scope=trusted_scope,
            exact_replay=exact_replay,
        )

    def search(
        self, *, user_id: str, query: str, top_k: int, options: list[str] | None = None
    ) -> SearchResult:
        return self.db.search(user_id=user_id, query=query, top_k=top_k, options=options)

    def list_raw_events(self, user_id: str, *, limit: int = 100) -> list[RawEvent]:
        return self.db.list_raw_events(user_id, limit=limit)

    def propose_memory(self, **kwargs) -> MemoryRecord:
        """Create a candidate/inferred record; never an implicit confirmation."""
        return self.db.propose_memory(**kwargs)

    def transition_memory(
        self,
        *,
        record_id: str,
        target_status: str,
        actor: str,
        actor_authority: str,
        reason: str,
        user_id: str,
        related_record_id: str | None = None,
    ) -> MemoryRecord:
        """Apply a governed state transition with explicit actor authority."""
        return self.db.transition_memory(
            record_id=record_id,
            target_status=target_status,
            actor=actor,
            actor_authority=actor_authority,
            reason=reason,
            user_id=user_id,
            related_record_id=related_record_id,
        )

    def search_governed(
        self,
        *,
        user_id: str,
        memory_key: str | None = None,
        query: str | None = None,
        mode: str = "current",
        scope: dict | None = None,
        as_of: str | None = None,
        max_records: int = 100,
    ) -> CurrentStateResult:
        """Internal Core API for current-state or audit retrieval.

        The official AML ``official_search`` wrapper remains byte-for-byte in
        its existing response shape; governed fields are exposed only here.
        """
        return self.db.search_governed(
            user_id=user_id,
            memory_key=memory_key,
            query=query,
            mode=mode,
            scope=scope,
            as_of=as_of,
            max_records=max_records,
        )

    def compile_events(
        self,
        *,
        user_id: str,
        raw_event_ids: list[str] | tuple[str, ...],
        idempotency_key: str,
        trusted_scope: dict | None = None,
        extractor: MemoryExtractor | None = None,
    ) -> CompilationReceipt:
        """Compile an exact RawEvent batch into candidate-only records.

        This is an internal, transport-neutral API.  ``trusted_scope`` must be
        bound by the authenticated host adapter; it is never accepted from an
        extractor proposal.  The extractor runs before the atomic write
        transaction.  With no injection, the zero-dependency strict directive
        extractor is used and ordinary free text legitimately yields zero
        proposals plus a durable success receipt.

        The official AML Add/Search paths never call this method implicitly.
        """

        active_extractor = extractor if extractor is not None else DirectiveMemoryExtractor()
        try:
            identity = active_extractor.identity
            if not isinstance(identity, ExtractorIdentity) or not callable(
                getattr(active_extractor, "extract", None)
            ):
                raise TypeError
            # Do not invoke a potentially overridden ``to_dict`` on an injected
            # identity subclass.  Snapshot only the declared base fields.
            identity_snapshot = {
                "name": identity.name,
                "version": identity.version,
                "implementation": identity.implementation,
                "config_digest": identity.config_digest,
                "deterministic": identity.deterministic,
            }
            sealed_identity = ExtractorIdentity(**identity_snapshot)
        except Exception:
            raise ExtractionValidationError("extractor identity is unavailable") from None

        events = self.db.raw_events_for_extraction(
            user_id=user_id,
            event_ids=raw_event_ids,
        )
        request = ExtractionRequest(
            user_id=user_id,
            idempotency_key=idempotency_key,
            raw_events=events,
            trusted_scope=trusted_scope if trusted_scope is not None else {},
            extractor=sealed_identity,
        )
        scope_snapshot = dict(request.trusted_scope)

        existing = extracted.resolve_existing_receipt(
            self.db.extraction_receipt(
                user_id=request.user_id,
                idempotency_key=request.idempotency_key,
            ),
            request,
        )
        if existing is not None:
            return existing

        # No DB write transaction is held across this call.  A model-backed
        # host callable may therefore time out or fail without leaving partial
        # candidate, state, origin, or receipt rows.
        trusted_framework_extractor = type(active_extractor) in {
            DirectiveMemoryExtractor,
            CallableMemoryExtractor,
        }
        try:
            if type(active_extractor) is DirectiveMemoryExtractor:
                # Call the framework implementation directly so an instance
                # attribute cannot shadow it with an untrusted callable.
                output = DirectiveMemoryExtractor.extract(active_extractor, request)
            elif type(active_extractor) is CallableMemoryExtractor:
                output = CallableMemoryExtractor.extract(active_extractor, request)
            else:
                output = active_extractor.extract(request)
        except ExtractionError:
            if trusted_framework_extractor:
                raise
            # Custom protocol implementations are untrusted even when they
            # deliberately raise one of our exception classes with source text.
            raise ExtractorInvocationError("extractor invocation failed") from None
        except TimeoutError:
            raise ExtractorInvocationError("extractor invocation timed out") from None
        except BaseException:
            # In-process plug-ins must not be able to smuggle source text or
            # terminate a long-lived service with control-flow exceptions such
            # as SystemExit or KeyboardInterrupt. Host cancellation belongs at
            # the executor/process boundary, not inside an extractor result.
            raise ExtractorInvocationError("extractor invocation failed") from None
        proposals = validate_proposals(request, output)
        # Persist with a fresh detached request that the callable never saw.
        # The digest equality check proves it is the same exact input snapshot.
        sealed_request = ExtractionRequest(
            user_id=request.user_id,
            idempotency_key=request.idempotency_key,
            raw_events=events,
            trusted_scope=scope_snapshot,
            extractor=ExtractorIdentity(**identity_snapshot),
        )
        if sealed_request.digest != request.digest:
            raise ExtractionValidationError("trusted extraction request was mutated")
        request = sealed_request

        # A concurrent same-digest caller may have completed while extraction
        # was running.  Avoid entering a write transaction when its receipt is
        # already durable; persist_extraction repeats this check under
        # BEGIN IMMEDIATE to close the final race.
        existing = extracted.resolve_existing_receipt(
            self.db.extraction_receipt(
                user_id=request.user_id,
                idempotency_key=request.idempotency_key,
            ),
            request,
        )
        if existing is not None:
            return existing
        return self.db.persist_extraction(request=request, proposals=proposals)

    def compile_context(
        self,
        *,
        user_id: str,
        access_context: AccessContext,
        memory_key: str | None = None,
        query: str | None = None,
        mode: str = "current",
        scope: dict | None = None,
        as_of: str | None = None,
        max_records: int = 100,
        max_chars: int | None = None,
        max_tokens: int | None = None,
        token_counter: TokenCounter | object | None = None,
        disclosure_policy: DisclosurePolicy | None = None,
    ) -> ContextPack:
        """Compile a governed result under trusted access and exact budgets.

        ``access_context`` must be constructed by an authenticated adapter; a
        request-body dictionary is deliberately rejected.  Authorization and
        scope binding happen before current-state resolution, sorting, and
        budget accounting.  The official AML Add/Search wrappers are not
        involved and retain their existing wire shape.
        """

        policy = disclosure_policy if disclosure_policy is not None else DisclosurePolicy()
        if not isinstance(policy, DisclosurePolicy):
            compiler = ContextCompiler(DisclosurePolicy(), token_counter)
            return compiler.forbidden(
                reason="access_denied",
                max_chars=max_chars,
                max_tokens=max_tokens,
            )
        compiler = ContextCompiler(policy, token_counter)
        decision = authorize_memory_read(
            access_context,
            user_id=user_id,
            requested_scope=scope,
            mode=mode,
            disclosure_policy=policy,
        )
        if not decision.allowed:
            return compiler.forbidden(
                reason=decision.reason,
                max_chars=max_chars,
                max_tokens=max_tokens,
            )
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records <= 0
        ):
            return compiler.failure(
                reason="invalid_max_records",
                max_chars=max_chars,
                max_tokens=max_tokens,
            )
        result = self.search_governed(
            user_id=user_id,
            memory_key=memory_key,
            query=query,
            mode=mode,
            scope=dict(decision.effective_scope),
            as_of=as_of,
            max_records=max_records,
        )
        return compiler.compile(
            result,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )

    def delete_user(self, user_id: str) -> dict:
        if not user_id:
            raise ApiError(422, "'user_id' is required")
        return self.db.delete_user(user_id)

    def health(self) -> dict:
        # 健康检查不返回任何记忆内容
        return {"status": "ok", "service": "aml-retriever"}

    def stats(self) -> dict:
        return self.db.stats()

    def close(self) -> None:
        self.db.close()

    # -- 官方契约 wrapper ---------------------------------------------------
    def official_add(self, payload: dict) -> dict:
        """官方 Add。

        幂等语义（本实现的选择，官方未声明）：以 (request_id, user_id) 为幂等键，
        首次写入生效；同键重复提交即使 messages 内容不同，也**不会**覆盖或追加，
        直接回显成功。理由是官方把 request_id 定位为一次写请求的标识，
        重试比"改写历史"更可能是真实场景；静默追加会在重试时污染记忆库。
        该行为由 tests/test_api_contract.py 锁定。
        """
        data = validate_add_payload(payload)
        try:
            self.add(
                request_id=data["request_id"],
                user_id=data["user_id"],
                session_id=data["session_id"],
                messages=data["messages"],
            )
        except ValueError as exc:
            raise ApiError(422, str(exc)) from exc
        # 官方要求：写入完成且立即可检索后才返回；三个 ID 必须原样回显
        return {
            "success": True,
            "request_id": data["request_id"],
            "user_id": data["user_id"],
            "session_id": data["session_id"],
        }

    def official_search(self, payload: dict) -> dict:
        data = validate_search_payload(payload, top_k_max=self.config.top_k_max)
        try:
            result = self.search(
                user_id=data["user_id"],
                query=data["query"],
                top_k=data["top_k"],
                options=data["options"],
            )
        except ValueError as exc:
            raise ApiError(422, str(exc)) from exc
        return {"data": [self._evidence_to_official(e) for e in result.results]}

    def _evidence_to_official(self, evidence: Evidence) -> dict:
        item = {
            "id": evidence.id,
            "content": evidence.content,
            "score": float(evidence.score),
        }
        if evidence.created_at:
            item["created_at"] = evidence.created_at
        if self.include_provenance:
            # 官方声明：未声明字段会被忽略。这些字段仅用于本地审计与可追溯性。
            item["view"] = evidence.view
            item["source_message_ids"] = evidence.source_message_ids
            item["evidence_flags"] = evidence.evidence_flags
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = [
    "MemoryService",
    "ApiError",
    "validate_add_payload",
    "validate_search_payload",
    "AddResult",
    "SearchResult",
    "Evidence",
    "RawEvent",
    "MemoryRecord",
    "CurrentStateResult",
    "AccessContext",
    "DisclosurePolicy",
    "TokenCounter",
    "ContextPack",
    "ContextCompiler",
    "ExtractorIdentity",
    "ExtractionRequest",
    "ProposalDraft",
    "MemoryExtractor",
    "DirectiveMemoryExtractor",
    "CallableMemoryExtractor",
    "CompilationReceipt",
    "ExtractionError",
    "ExtractionValidationError",
    "ExtractorInvocationError",
]
