"""FlowGrid Agent Memory with an AML Add/Search compatibility adapter.

分层：
  store.py      P0 词法基线（保留，作为回归基准）
  retriever.py  多视图混合证据检索引擎
  api.py        领域服务 + 官方契约 wrapper
  server.py     官方 Add/Search HTTP 传输层
"""
from .store import Store, tokenize  # P0 基线（回归保护，签名不得变更）
from .config import RetrieverConfig, DEFAULT_FLAGS
from .retriever import RetrieverDB, AddResult, SearchResult, Evidence
from .api import MemoryService, ApiError
from .compiler import CompilationReceipt
from .extraction import (
    DIRECTIVE_PREFIX,
    CallableMemoryExtractor,
    DirectiveMemoryExtractor,
    EvidenceSpan,
    ExtractionConflict,
    ExtractionError,
    ExtractionRequest,
    ExtractionValidationError,
    ExtractorIdentity,
    ExtractorInvocationError,
    MemoryExtractor,
    ProposalDraft,
)
from .model_extraction import (
    MODEL_EXTRACTOR_SCHEMA,
    MODEL_PROMPT_VERSION,
    QuoteAnchoredModelExtractor,
    build_model_extraction_prompt,
    quote_anchored_identity,
)
from .access import (
    PERMISSION_AUDIT,
    PERMISSION_EVIDENCE,
    PERMISSION_READ,
    AccessContext,
    AccessDecision,
    DisclosurePolicy,
    authorize_memory_read,
)
from .context import (
    CONTEXT_STATUSES,
    ContextCompiler,
    ContextPack,
    TokenCounter,
    canonical_json,
)
from .governance import (
    ALLOWED_TRANSITIONS,
    AUTHORITIES,
    CONFIRM_AUTHORITIES,
    GOVERNANCE_AUTHORITIES,
    GOVERNANCE_SCHEMA_VERSION,
    MEMORY_STATES,
    MEMORY_TYPES,
    CurrentStateResult,
    GovernanceConflict,
    GovernanceError,
    MemoryRecord,
    MemoryStateEvent,
    RawEvent,
)
from .facade import (
    AuthorizedMemoryResult,
    FlowGridMemory,
    IngestReceipt,
    PrivacyEraseReceipt,
)
from .migrations import SchemaReport, inspect_schema
from ._version import AML_ADAPTER_VERSION, PRODUCT_VERSION

__version__ = PRODUCT_VERSION

__all__ = [
    "Store",
    "tokenize",
    "RetrieverConfig",
    "DEFAULT_FLAGS",
    "RetrieverDB",
    "AddResult",
    "SearchResult",
    "Evidence",
    "MemoryService",
    "ApiError",
    "DIRECTIVE_PREFIX",
    "ExtractionError",
    "ExtractionValidationError",
    "ExtractionConflict",
    "ExtractorInvocationError",
    "ExtractorIdentity",
    "EvidenceSpan",
    "ExtractionRequest",
    "ProposalDraft",
    "MemoryExtractor",
    "DirectiveMemoryExtractor",
    "CallableMemoryExtractor",
    "MODEL_EXTRACTOR_SCHEMA",
    "MODEL_PROMPT_VERSION",
    "QuoteAnchoredModelExtractor",
    "build_model_extraction_prompt",
    "quote_anchored_identity",
    "CompilationReceipt",
    "PERMISSION_READ",
    "PERMISSION_AUDIT",
    "PERMISSION_EVIDENCE",
    "AccessContext",
    "AccessDecision",
    "DisclosurePolicy",
    "authorize_memory_read",
    "CONTEXT_STATUSES",
    "TokenCounter",
    "ContextPack",
    "ContextCompiler",
    "canonical_json",
    "MEMORY_STATES",
    "MEMORY_TYPES",
    "AUTHORITIES",
    "CONFIRM_AUTHORITIES",
    "GOVERNANCE_AUTHORITIES",
    "GOVERNANCE_SCHEMA_VERSION",
    "ALLOWED_TRANSITIONS",
    "GovernanceError",
    "GovernanceConflict",
    "RawEvent",
    "MemoryRecord",
    "MemoryStateEvent",
    "CurrentStateResult",
    "FlowGridMemory",
    "IngestReceipt",
    "AuthorizedMemoryResult",
    "PrivacyEraseReceipt",
    "SchemaReport",
    "inspect_schema",
    "PRODUCT_VERSION",
    "AML_ADAPTER_VERSION",
    "__version__",
]
