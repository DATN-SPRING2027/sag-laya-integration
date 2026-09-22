"""由 sag 配置装配 zleap-sag 0.8.2 的 `EngineConfig`。

支持信源级覆盖（`overrides`）——目前支持 `language`，未来可扩展 `entity_types` 等。
0.8.2 变更:`storage_mode` 必填;向量库改为显式 VectorConfig 家族
(Elasticsearch / PgVector / Qdrant / OceanBase),不再使用 vector_provider 字符串。
"""

from __future__ import annotations

from typing import Any

from zleap.sag import EngineConfig
from zleap.sag.config import (
    ElasticsearchVectorConfig,
    EmbeddingConfig,
    LLMConfig,
    OceanBaseConnectionConfig,
    OceanBaseVectorConfig,
    PgVectorConfig,
    PostgresConnectionConfig,
    RelationalConfig,
    RuntimeLimits,
)
from zleap.sag.core.ai.structured import StructuredOutputMode

from sag_api.core.config import Settings
from sag_api.sag.qdrant_store import QdrantVectorConfig

# LLM 未配置时的占位符：允许 EngineConfig 构造 / start() 建 schema（离线路径），
# 真正的 ingest / extract / search 会在运行时因缺少凭证而报错（服务层已前置守卫）。
_PLACEHOLDER = "not-configured"

def _build_vector(settings: Settings) -> Any:
    provider = settings.sag_vector_provider
    if provider == "es":
        # 0.8.2 要求 hosts;SAG 未提供 ES 地址配置时退回本地默认。
        # TODO(REQ-7/配置):新增 SAG_ES_HOSTS 设置项,生产显式配置。
        return ElasticsearchVectorConfig(hosts=["http://localhost:9200"])
    if provider == "qdrant":
        # zleap-sag 0.12's EngineConfig has no Qdrant model; the validated
        # config is replaced with QdrantVectorConfig after construction below.
        return ElasticsearchVectorConfig(hosts=[settings.sag_qdrant_url])
    if provider == "pgvector":
        return PgVectorConfig(
            connection=PostgresConnectionConfig(
                host=settings.sag_pg_host,
                port=settings.sag_pg_port,
                user=settings.sag_pg_user,
                password=settings.sag_pg_password,
                database=settings.sag_pg_database,
            )
        )
    if provider == "oceanbase":
        return OceanBaseVectorConfig(
            connection=OceanBaseConnectionConfig(
                host=settings.sag_pg_host,
                port=2881,
                user=settings.sag_pg_user,
                password=settings.sag_pg_password,
                database=settings.sag_pg_database,
            )
        )
    raise ValueError(f"不支持的向量后端: {provider}")


def _build_relational(settings: Settings) -> RelationalConfig | None:
    provider = settings.sag_relational_provider
    if not provider or provider == "sqlite":
        return None  # EngineConfig 从 data_dir 派生 SQLite
    return RelationalConfig(
        provider=provider,  # postgres / mysql / oceanbase
        host=settings.sag_pg_host,
        port=settings.sag_pg_port,
        user=settings.sag_pg_user,
        password=settings.sag_pg_password,
        database=settings.sag_pg_database,
    )


def _structured_output_mode(settings: Settings) -> StructuredOutputMode:
    """映射显式模式；auto 由 SAG LiteLLM seam 首选 schema 并按能力降级。"""
    mode = settings.llm_structured_output_mode
    if mode == "auto":
        return StructuredOutputMode.JSON_SCHEMA
    return StructuredOutputMode(mode)


def build_engine_config(settings: Settings, *, overrides: dict[str, Any] | None = None) -> EngineConfig:
    overrides = overrides or {}

    llm = LLMConfig(
        api_key=settings.llm_api_key or _PLACEHOLDER,
        model=settings.routed_llm_model,
        provider="litellm",
        base_url=settings.llm_base_url,
        temperature=settings.effective_llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=max(1, (settings.llm_timeout_ms + 999) // 1000),
        max_retries=settings.llm_max_retries,
        structured_output_mode=_structured_output_mode(settings),
    )
    embedding = EmbeddingConfig(
        model=settings.embedding_model,
        base_url=settings.effective_embedding_base_url,
        api_key=settings.effective_embedding_api_key or _PLACEHOLDER,
        # 当前引擎以该值预建向量 schema；未显式配置时保持既有 1024 维默认值。
        dimensions=settings.embedding_dimensions or 1024,
        timeout=settings.embedding_timeout,
    )

    config = EngineConfig(
        storage_mode="normal",
        llm=llm,
        embedding=embedding,
        relational=_build_relational(settings),
        vector=_build_vector(settings),
        data_dir=str(overrides.get("data_dir") or settings.data_dir),
        language=overrides.get("language", settings.sag_language),
        runtime_limits=RuntimeLimits(
            embedding_concurrency=settings.embedding_concurrency,
            acquire_timeout_seconds=float(max(30, settings.embedding_timeout)),
        ),
    )
    if settings.sag_vector_provider == "qdrant":
        return config.model_copy(
            update={
                "vector": QdrantVectorConfig(
                    url=settings.sag_qdrant_url,
                    api_key=settings.sag_qdrant_api_key,
                )
            }
        )
    return config
