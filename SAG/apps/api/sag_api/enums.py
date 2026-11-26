"""跨层共享的枚举（模型 / schema / 服务均可导入，无副作用）。"""

from __future__ import annotations

import re
import unicodedata
from enum import IntEnum, StrEnum
from typing import Literal

SearchStrategy = Literal["vector", "multi", "multi_es_fast"]
SEARCH_STRATEGIES = frozenset({"vector", "multi", "multi_es_fast"})

# 策略对底层引擎能力的依赖。缺失能力的部署会在 UI 灰置该选项；后端调用则拒绝。
SEARCH_STRATEGY_REQUIREMENTS: dict[str, frozenset[str]] = {
    "vector": frozenset(),
    "multi": frozenset(),
    "multi_es_fast": frozenset({"lexical_search"}),
}


def normalize_search_strategy(value: str) -> str:
    """把已下线的原子检索迁移到精确检索；其他值交给调用方校验。"""
    return "multi" if value == "atomic" else value


class SourceType(StrEnum):
    DOCUMENT = "document"
    WEB = "web"
    MESSAGE = "message"
    AUDIO = "audio"


class ConnectorKind(StrEnum):
    FILE_UPLOAD = "file_upload"
    WEB = "web"
    # 预留：NOTION = "notion"; S3 = "s3"; CONFLUENCE = "confluence"; ...


# 连接器 → 默认信源类型
CONNECTOR_SOURCE_TYPE = {
    ConnectorKind.FILE_UPLOAD: SourceType.DOCUMENT,
    ConnectorKind.WEB: SourceType.WEB,
}


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class DocumentStatus(StrEnum):
    PENDING = "pending"        # 已登记，待处理
    LOADING = "loading"        # ingest 中（解析 → 分块 → 入库 → 向量）
    EXTRACTING = "extracting"  # extract 中（事件 / 实体抽取）
    PAUSING = "pausing"        # 已收到停止命令，等待在途分块完成
    PAUSED = "paused"          # 抽取已暂停，可从 chunk 断点继续
    DELETING = "deleting"      # 已收到删除命令，后台正在停止并清理
    DELETE_FAILED = "delete_failed"
    READY = "ready"            # 处理完成，可检索
    FAILED = "failed"


class JobType(StrEnum):
    PROCESS_DOCUMENT = "process_document"
    REPROCESS_DOCUMENT = "reprocess_document"
    DELETE_DOCUMENT = "delete_document"
    SYNC_SOURCE = "sync_source"
    INDEX_UNIVERSE = "index_universe"
    REBUILD_UNIVERSE_TREE = "rebuild_universe_tree"
    RECONCILE_SEARCH_INDEX = "reconcile_search_index"
    OCTX_PREFLIGHT = "octx_preflight"
    OCTX_IMPORT = "octx_import"
    OCTX_EXPORT = "octx_export"
    OCTX_GC_INSTALLATION = "octx_gc_installation"
    OCTX_GC_TRANSFER = "octx_gc_transfer"
    CONFLICT_RESOLUTION = "conflict_resolution"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BindingTargetType(StrEnum):
    SOURCE = "source"
    MCP_SERVER = "mcp_server"  # Phase C：挂载 MCP server 作为工具来源


class OctxAssetOwnership(StrEnum):
    LOCAL = "local"
    IMPORTED = "imported"


class OctxReleaseOrigin(StrEnum):
    IMPORT = "import"
    EXPORT = "export"


class OctxInstallationStatus(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"
    RETAINED = "retained"
    GC = "gc"
    FAILED = "failed"


class OctxTransferDirection(StrEnum):
    IMPORT = "import"
    EXPORT = "export"


class OctxImportAction(StrEnum):
    UPDATE = "update"
    NEW = "new"
    CANCEL = "cancel"


class OctxExportAction(StrEnum):
    EXPORT_READY_ONLY = "export_ready_only"
    CANCEL = "cancel"


class OctxTransferStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    DECISION_REQUIRED = "decision_required"
    QUEUED = "queued"
    IMPORTING = "importing"
    INDEXING = "indexing"
    SWITCHING = "switching"
    EXPORTING = "exporting"
    PACKAGING = "packaging"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AuthorityTier(IntEnum):
    POLICY_BINDING = 1      # "Chính thức" (1 = highest)
    OFFICIAL_SPEC = 2       # "Quy chuẩn"
    OPERATIONAL_GUIDE = 3   # "Hướng dẫn"
    INFORMAL_DRAFT = 4      # "Nháp"


AUTHORITY_TIER_LABELS: dict[int, str] = {
    1: "Chính thức",
    2: "Quy chuẩn",
    3: "Hướng dẫn",
    4: "Nháp",
}

_AUTHORITY_TIER_ALIASES: dict[str, int] = {
    # Tier 1
    "1": 1,
    "tier_1": 1,
    "t1": 1,
    "policy": 1,
    "policy_binding": 1,
    "binding": 1,
    "legal": 1,
    "executive": 1,
    "chinh_thuc": 1,
    "chính thức": 1,
    # Tier 2
    "2": 2,
    "tier_2": 2,
    "t2": 2,
    "spec": 2,
    "official_spec": 2,
    "sop": 2,
    "standard": 2,
    "adr": 2,
    "quy_chuan": 2,
    "quy chuẩn": 2,
    # Tier 3
    "3": 3,
    "tier_3": 3,
    "t3": 3,
    "guide": 3,
    "operational_guide": 3,
    "handbook": 3,
    "runbook": 3,
    "minutes": 3,
    "huong_dan": 3,
    "hướng dẫn": 3,
    # Tier 4
    "4": 4,
    "tier_4": 4,
    "t4": 4,
    "draft": 4,
    "informal_draft": 4,
    "memo": 4,
    "chat": 4,
    "notes": 4,
    "nhap": 4,
    "nháp": 4,
}

_SCOPE_TAG_WS_RE = re.compile(r"\s+")
_SCOPE_TAG_RE = re.compile(r"^[\w.:/-]{1,64}\Z")
_MAX_SCOPE_TAGS = 16


def normalize_authority_tier(value: int | str | AuthorityTier | None) -> int | None:
    """Normalize an authority tier input into canonical int 1..4 or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"authority_tier cannot be a boolean: {value!r}")
    if isinstance(value, AuthorityTier):
        return int(value)
    if isinstance(value, int):
        if 1 <= value <= 4:
            return value
        raise ValueError(f"authority_tier must be between 1 and 4, got {value}")
    if isinstance(value, str):
        normalized = " ".join(unicodedata.normalize("NFC", value).strip().casefold().split())
        if not normalized:
            return None
        resolved = _AUTHORITY_TIER_ALIASES.get(normalized)
        if resolved is not None:
            return resolved
        raise ValueError(f"unknown authority_tier value: {value!r}")
    raise ValueError(f"unsupported authority_tier type: {type(value).__name__}")


def authority_tier_label(tier: int | None, *, compact: bool = False) -> str | None:
    """Return a citation-safe display label (never emits '[' or ']')."""
    if tier is None or isinstance(tier, bool) or not isinstance(tier, int):
        return None
    tier_int = int(tier)
    label = AUTHORITY_TIER_LABELS.get(tier_int)
    if label is None:
        return None
    return f"T{tier_int}" if compact else label


def normalize_scope_tags(raw: object) -> list[str]:
    """Validate and normalize document-version scope tags (<= 16 sorted unique NFC tokens)."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError(
            f"scope_tags must be a list, tuple, or set of strings, got {type(raw).__name__}"
        )
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"scope_tags items must be strings, got {type(item).__name__}")
        normalized = unicodedata.normalize("NFC", item).strip().casefold()
        normalized = _SCOPE_TAG_WS_RE.sub("-", normalized)
        if not _SCOPE_TAG_RE.match(normalized):
            raise ValueError(f"invalid scope tag token: {item!r}")
        seen.add(normalized)
    result = sorted(seen)
    if len(result) > _MAX_SCOPE_TAGS:
        raise ValueError(f"scope_tags cannot exceed {_MAX_SCOPE_TAGS} unique tags")
    return result


class ConflictSubtype(StrEnum):
    DIRECT_SUPERSEDES = "DIRECT_SUPERSEDES"
    HARD_CONTRADICTION = "HARD_CONTRADICTION"
    SCOPED_EXCEPTION = "SCOPED_EXCEPTION"
    DIFFERENT_PERSPECTIVE = "DIFFERENT_PERSPECTIVE"
    SUPPORTS_OR_EQUIVALENT = "SUPPORTS_OR_EQUIVALENT"


class ConflictResolutionStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACTIVE = "ACTIVE"
    DISMISSED_SCOPED = "DISMISSED_SCOPED"
    REVOKED_WINNER = "REVOKED_WINNER"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
    SUPERSEDED_BY_UPDATE = "SUPERSEDED_BY_UPDATE"


class ConflictResolutionMode(StrEnum):
    AUTO_POLICY = "AUTO_POLICY"
    MANUAL_HITL = "MANUAL_HITL"
    UNRESOLVED = "UNRESOLVED"
    KEEP_BOTH_SCOPED = "KEEP_BOTH_SCOPED"
