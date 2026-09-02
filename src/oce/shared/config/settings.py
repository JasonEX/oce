"""应用配置。每个配置组使用独立环境变量前缀。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 所有配置组读同一组文件：.env.local 覆盖 .env，避免某些组读不到本地覆盖值。
_ENV_FILES = (".env", ".env.local")


def _settings_config(env_prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DatabaseSettings(BaseSettings):
    """数据库配置（PostgreSQL / SQLite 元数据存储）"""

    model_config = _settings_config("DB_")

    url: str = Field(
        default="postgresql+asyncpg://oce:oce@localhost:5432/oce",
        description="数据库连接 URL",
    )
    pool_size: int = Field(default=5, ge=1, le=100, description="连接池大小")
    max_overflow: int = Field(default=5, ge=0, le=100, description="连接池溢出上限")
    echo: bool = Field(default=False, description="是否打印 SQL 日志")

    @property
    def is_sqlite(self) -> bool:
        """是否是 SQLite"""
        return self.url.startswith("sqlite")


class MilvusSettings(BaseSettings):
    """Milvus 3.0 配置（dense 向量存储）。向量维度取自 EmbeddingSettings.dimensions。"""

    model_config = _settings_config("MILVUS_")

    # 连接
    endpoint: str = Field(
        default="http://localhost:19530",
        description="Milvus 端点：HTTP 服务地址，或本地 Milvus Lite 文件路径",
    )
    token: str | None = Field(default=None, description="认证 token（Zilliz Cloud）")

    # Collection
    collection_name: str = Field(default="oce_chunks", description="Collection 名称")
    path_collection_name: str = Field(
        default="oce_paths_v1",
        description="路径索引 Collection 名称",
    )

    # 索引
    dense_index_type: str = Field(default="HNSW", description="密集向量索引类型")
    dense_metric_type: str = Field(default="COSINE", description="密集向量距离度量")

    # HNSW 参数
    hnsw_m: int = Field(default=16, ge=4, le=64, description="HNSW M 参数")
    hnsw_ef_construction: int = Field(
        default=256, ge=8, le=512, description="HNSW efConstruction"
    )
    hnsw_ef_search: int = Field(
        default=64, ge=8, le=2048, description="HNSW ef（搜索时）"
    )


class EmbeddingSettings(BaseSettings):
    """嵌入模型配置"""

    model_config = _settings_config("EMBED_")

    enabled: bool = Field(default=True, description="是否启用嵌入(关闭时只切块不嵌入)")
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/embeddings",
        description="OpenAI 兼容的 embedding 端点",
    )
    api_key: SecretStr | None = Field(default=None, description="Embedding API 密钥")
    model: str = Field(default="Qwen/Qwen3-Embedding-4B", description="嵌入模型")
    dimensions: int = Field(
        default=1024,
        ge=1,
        description="向量维度；同时是 Milvus collection 的向量维度",
    )
    max_batch_size: int = Field(default=32, ge=1, le=256, description="单请求文本数")
    max_batch_chars: int = Field(
        default=32_000,
        ge=1,
        description="单请求 input 数组总字符预算",
    )
    max_input_chars: int = Field(
        default=8_000, ge=1, description="单条模型输入字符上限"
    )
    input_overlap_chars: int = Field(
        default=400, ge=0, description="长输入分段重叠字符数"
    )
    max_concurrency: int = Field(default=4, ge=1, le=32, description="最大请求并发")
    timeout_seconds: float = Field(default=60.0, gt=0, description="请求超时秒数")
    proxy: str | None = Field(default=None, description="可选 HTTP 代理")
    query_instruction: str = Field(
        default="",
        description="Query-side instruction（添加到 query 前，为空则不添加）",
    )
    query_cache_max_entries: int = Field(
        default=256,
        ge=0,
        le=10_000,
        description="进程内 query vector LRU 容量；0 禁用",
    )
    query_cache_ttl_seconds: float = Field(
        default=600.0,
        ge=0,
        description="query vector 缓存 TTL 秒数；0 禁用",
    )


class RerankSettings(BaseSettings):
    """重排模型配置。"""

    model_config = _settings_config("RERANK_")

    enabled: bool = Field(
        default=False,
        description="是否启用专用 rerank API",
    )
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/rerank",
        description="Rerank 端点",
    )
    api_key: SecretStr | None = Field(
        default=None, description="空值时复用 embedding key"
    )
    model: str = Field(default="Qwen/Qwen3-Reranker-0.6B", description="重排模型")
    top_n: int = Field(
        default=50,
        ge=1,
        le=100,
        description="专用 reranker 提升到候选队首的最大条数",
    )
    min_score: float = Field(default=0.05, ge=0.0, le=1.0, description="最低重排分")
    timeout_seconds: float = Field(default=60.0, gt=0, description="请求超时秒数")
    # Qwen3-Reranker 模型卡报告：instruction-aware 任务中常见 1%~5% 提升，
    # 且多语言场景建议用英文；其他 provider 不支持时可置空。
    instruction: str = Field(
        default=(
            "Given a code search query, judge whether the code snippet implements, "
            "defines, or directly answers what the query asks for"
        ),
        description="随每次请求发送的任务说明；置空则不发送",
    )


class ChunkingSettings(BaseSettings):
    """Source chunking composition."""

    model_config = _settings_config("CHUNKING_")

    semantic_enabled: bool = Field(
        default=True,
        description="启用 cAST 与专用结构化 chunker；关闭时统一使用 recursive chunker",
    )
    semantic_max_chunk_chars: int = Field(
        default=1500,
        gt=0,
        description="cAST semantic chunk 的 non-whitespace 字符预算",
    )
    recursive_chunk_size: int = Field(
        default=6000,
        gt=0,
        description="recursive fallback 目标字符数",
    )
    recursive_chunk_overlap: int = Field(
        default=200,
        ge=0,
        description="recursive splitter 的边界搜索重叠字符数",
    )


class LLMSettings(BaseSettings):
    """LLM 功能共享的环境变量 fallback 配置。

    LLM 语义重排和查询改写分别按 kind 构造客户端；未配置对应
    model_credentials 行时，共同回落到这里的 LLM_* 设置。
    """

    model_config = _settings_config("LLM_")

    rerank_enabled: bool = Field(
        default=False,
        description="是否允许 chat LLM 参与语义重排",
    )
    model: str = Field(default="Qwen/Qwen2.5-7B-Instruct", description="LLM 模型")
    api_key: SecretStr = Field(default="", description="LLM API Key")
    base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="LLM API Base URL",
    )
    proxy: str | None = Field(default=None, description="LLM API HTTP 代理")
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="单次 LLM HTTP 请求超时秒数",
    )
    rerank_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="chat LLM 重排的端到端延迟上限；超时保留原排序",
    )
    max_candidates: int = Field(
        default=50, ge=10, le=100, description="LLM 重排最大候选数"
    )
    output_top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="LLM 提升到候选队首的最大条数",
    )
    # 实测 chunk 中位长度约 1560 字符，99% 超过 400；截断过短会让 LLM 只看到片段开头
    snippet_chars: int = Field(
        default=1600, ge=200, le=4000, description="每个候选送入 LLM 的代码字符上限"
    )
    # 单次 rerank 可达 16k token，不限流会在十几个查询后连续 429 并静默退回原始顺序
    tpm_limit: int = Field(
        default=60_000,
        ge=1_000,
        description="LLM 接口 TPM 上限，客户端按滑动窗口排队",
    )


class RetrievalSettings(BaseSettings):
    """检索配置"""

    model_config = _settings_config("RETRIEVAL_")

    # 向量检索
    default_top_k: int = Field(default=50, ge=1, le=200, description="向量召回条数")
    vector_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Milvus dense 相似度过滤阈值；默认不预过滤",
    )
    final_select_k: int = Field(default=10, ge=1, le=50, description="最终返回条数")

    # 多查询融合
    rrf_k: int = Field(default=60, ge=1, description="多查询结果融合平滑常数")

    # 置信度门槛
    confidence_floor: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="进入模型重排前的召回置信度门槛",
    )

    # 两种 reranker 只处理排序，不参与候选裁剪。RERANK_ENABLED / LLM_RERANK_ENABLED 是
    # 数据外发授权；这里的策略只决定已授权的模型对哪些查询调用：adaptive 在
    # exact/path 等确定性证据已经回答问题时跳过，always 用于质量优先或可复现对照。
    rerank_policy: Literal["adaptive", "always"] = Field(
        default="adaptive",
        description="专用 reranker 调用策略",
    )
    llm_rerank_policy: Literal["adaptive", "always"] = Field(
        default="adaptive",
        description="chat LLM 重排调用策略",
    )

    # 精确标识符召回
    exact_timeout_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="SQL 精确标识符召回超时；超时后回退向量检索",
    )
    exact_enabled: bool = Field(
        default=True,
        description="是否启用 SQL exact identifier recall",
    )

    source_priority_enabled: bool = Field(
        default=True,
        description="是否对文档、测试和 barrel 文件应用 source priority",
    )
    coverage_selection_enabled: bool = Field(
        default=True,
        description="是否使用 focused/coverage selector；关闭时使用纯 Top-K",
    )

    # 仓库级多意图召回
    query_decomposition_enabled: bool = Field(
        default=True, description="是否分解多句检索请求"
    )
    query_max_queries: int = Field(
        default=4, ge=1, le=8, description="原查询和子查询总数上限"
    )
    query_min_facet_chars: int = Field(default=8, ge=1, description="子查询最少字符数")
    query_facet_weight: float = Field(
        default=0.75, gt=0.0, le=1.0, description="子查询融合权重"
    )
    per_query_top_k: int = Field(
        default=20, ge=1, le=100, description="多子查询融合时每个子查询的召回条数"
    )

    # 上下文剪枝与覆盖度（字符预算为硬限制，final_select_k 为软上限）
    max_chunks_per_path: int = Field(
        default=2, ge=1, le=20, description="单文件最多返回片段数"
    )
    focused_max_chunks_per_path: int = Field(
        default=4,
        ge=1,
        le=20,
        description="focused 模式单文件最多返回片段数",
    )
    max_context_chars: int = Field(
        default=32_000, ge=1, description="返回代码总字符预算（硬限制）"
    )
    overlap_threshold: float = Field(
        default=0.6, ge=0.0, le=1.0, description="同文件片段重叠抑制阈值"
    )

    # Query rewrite (LLM-based query expansion for better recall)
    # 默认关闭：仅跨语言文件名等特殊场景有明显增益，通用检索收益有限
    query_rewrite_enabled: bool = Field(
        default=False, description="是否启用 LLM 查询改写"
    )
    query_rewrite_model: str = Field(
        default="Qwen/Qwen2.5-7B-Instruct", description="查询改写使用的 LLM 模型"
    )
    query_rewrite_num: int = Field(
        default=3, ge=1, le=5, description="生成改写查询的数量"
    )

    # Path index (独立路径索引用于文件名查询)
    path_index_enabled: bool = Field(
        default=True, description="是否启用路径索引（文件名查询增强）"
    )
    # 路径分数与内容分数同为 COSINE 量纲，加权相加而非替换，避免挤掉正确 chunk
    path_boost_weight: float = Field(
        default=0.5, ge=0.0, le=2.0, description="路径索引命中对同文件 chunk 的加权系数"
    )


class RedisSettings(BaseSettings):
    """Redis 配置（任务队列）"""

    model_config = _settings_config("REDIS_")

    url: str = Field(default="redis://localhost:6379/0", description="Redis 连接 URL")
    queue_name: str = Field(default="oce:embed_queue", description="嵌入队列名称")


class WorkerSettings(BaseSettings):
    """Worker 配置（后台嵌入消费者）"""

    model_config = _settings_config("WORKER_")

    enabled: bool = Field(default=True, description="是否启用后台 worker")
    concurrency: int = Field(default=2, ge=1, le=32, description="并发消费协程数")
    blob_batch_size: int = Field(
        default=16,
        ge=1,
        le=256,
        description="单个消费协程一次处理的最大 blob 数",
    )
    max_retries: int = Field(default=3, ge=1, le=10, description="失败重试上限")


class LogSettings(BaseSettings):
    """日志配置"""

    model_config = _settings_config("LOG_")

    file_enabled: bool = Field(default=False, description="是否启用日志落盘")
    file_path: str | None = Field(
        default=None, description="日志文件路径（None 时自动推断）"
    )
    rotation: str = Field(
        default="100 MB", description="轮转策略：'1 day' 按天 / '100 MB' 按大小"
    )
    retention: str = Field(
        default="30 days", description="保留时长：'30 days' / '10 files'"
    )
    format_json: bool = Field(
        default=False, description="是否使用 JSON 格式（便于日志采集）"
    )
    level: str = Field(default="INFO", description="日志级别（WARNING/INFO/DEBUG）")


class MonitoringSettings(BaseSettings):
    """监控配置（调用 / token / 资源采集与落库）"""

    model_config = _settings_config("MONITORING_")

    enabled: bool = Field(default=True, description="是否启用监控采集与落库")
    flush_interval_seconds: float = Field(
        default=5.0, gt=0, description="缓冲区批量写库间隔秒数"
    )
    flush_max_buffer: int = Field(
        default=500, ge=1, description="单类指标缓冲上限，超出立即 flush"
    )
    resource_sample_interval_seconds: float = Field(
        default=60.0, gt=0, description="资源采样间隔秒数"
    )
    retention_days: int = Field(
        default=30, ge=1, description="监控数据保留天数（GC 清理阈值）"
    )
    cleanup_interval_seconds: float = Field(
        default=3600.0, gt=0, description="监控数据清理任务运行间隔秒数"
    )
    retrieval_audit_enabled: bool = Field(
        default=True, description="是否记录检索各阶段耗时与空回审计"
    )
    store_query_text: bool = Field(
        default=False, description="检索审计是否存储 query 原文（默认关，隐私安全）"
    )


class Settings(BaseSettings):
    """全局配置 - 聚合所有配置组"""

    model_config = _settings_config()

    # API
    api_key: str = Field(
        default="sk-opencontextengine",
        description="API 认证密钥；个人模式用与客户端约定的固定值，服务模式须改为强随机值",
    )
    admin_api_key: str = Field(
        default="",
        description="Admin 接口密钥；空则回落 API_KEY，一旦配置则 admin 接口只认此 key",
    )
    cors_origins: str = Field(
        default="https://oce-ai.github.io",
        description="允许访问 API 的浏览器来源，多个来源用逗号分隔；默认放行官方 admin 面板，设为空则关闭 CORS",
    )

    # 子配置组
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（缓存）"""
    return Settings()
