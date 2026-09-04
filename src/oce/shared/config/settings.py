"""应用配置。每个配置组使用独立环境变量前缀。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 所有配置组读同一组文件：.env.local 覆盖 .env，避免某些组读不到本地覆盖值。
_ENV_FILES = (".env", ".env.local")

# reranker 逐查询路由策略；authorization 由各自的 *_ENABLED 开关单独控制。
RerankPolicy = Literal["adaptive", "always"]


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
    # 长 issue 文本整段送去做 query embedding 既慢又会稀释向量；标题和描述通常
    # 位于前部。具体消融数据留在 benchmarks/results，避免配置代码固化实验快照。
    max_query_chars: int = Field(
        default=3_000, ge=0, description="query embedding 输入字符上限；0 不限制"
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
        description="是否启用专用 reranker",
    )
    # api：远端交叉编码器（外发 query 与候选源码）；local：进程内 ONNX 交叉编码器，
    # 不外发，需要 `uv sync --extra local-rerank` 与本地模型目录。
    provider: Literal["api", "local"] = Field(
        default="api", description="专用 reranker 的提供方式"
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
    # 交叉编码器对每个候选都要重读一遍 query；本项目的 0.6B 基准中，一段 25K
    # 字符的 issue 曾让单次调用接近 15 秒。截断保留开头的问题描述。
    max_query_chars: int = Field(
        default=2_400, ge=200, description="送入 reranker 的 query 字符上限"
    )
    # 本地 ONNX 交叉编码器窗口刻意小于远端默认值，限制 CPU 延迟。
    local_model_dir: str = Field(
        default="~/.cache/oce/models/jina-reranker-v2-base-multilingual",
        description="本地 reranker 模型目录（含 model_int8.onnx 与 tokenizer.json）",
    )
    local_model_file: str = Field(
        default="model_int8.onnx", description="模型目录内的 ONNX 文件名"
    )
    local_candidates: int = Field(
        default=16, ge=1, le=100, description="本地 reranker 打分的候选数"
    )
    local_max_doc_chars: int = Field(
        default=800, ge=100, description="每个候选送入本地 reranker 的字符上限"
    )
    local_max_tokens: int = Field(
        default=512, ge=64, le=8192, description="query+候选的 token 上限"
    )
    local_batch_size: int = Field(default=4, ge=1, le=64, description="推理批大小")
    # 混合大小核 CPU 上 onnxruntime 开满逻辑核可能反而更慢。
    local_threads: int = Field(
        default=0, ge=0, le=128, description="推理线程数；0 取物理核数的一半（上限 8）"
    )
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
    api_key: SecretStr | None = Field(default=None, description="LLM API Key")
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

    # 两种 reranker 只处理排序，不参与候选裁剪。RERANK_ENABLED / LLM_RERANK_ENABLED
    # 授权对应阶段（api/chat 会外发，local 不外发）；这里的策略只决定已启用的模型对哪些查询调用：adaptive 在
    # exact/path 等确定性证据已经回答问题时跳过，always 用于质量优先或可复现对照。
    rerank_policy: RerankPolicy = Field(
        default="adaptive",
        description="专用 reranker 调用策略",
    )
    llm_rerank_policy: RerankPolicy = Field(
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
    focused_max_context_chars: int = Field(
        default=12_000,
        ge=1,
        description="symbol/path focused 查询的字符预算（硬限制）",
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
    path_top_k: int = Field(
        default=20, ge=1, le=100, description="每个查询变体从路径索引召回的文件数"
    )
    # 路径证据只作为有界 boost 加到已融合候选上，不替换内容命中，避免挤掉正确 chunk。
    path_boost_weight: float = Field(
        default=0.5, ge=0.0, le=2.0, description="路径证据对同文件 chunk 的加权系数"
    )
    # 精确路径查找：请求里出现的文件名/路径（含 traceback 帧）在 scope 内做后缀匹配，
    # 不经 embedding；命中与路径索引共用同一 boost 权重。
    path_lookup_enabled: bool = Field(
        default=True, description="是否启用 SQL 精确路径/文件名后缀匹配"
    )

    # 词法召回：chunk 词元的 FTS 索引，覆盖报错文案、调用点等 dense 不敏感的线索。
    # 这是能力开关；symbol/path 仅在结构化证据缺失时补跑，
    # 语义类查询直接启用。
    lexical_enabled: bool = Field(
        default=True, description="是否允许按查询启用词法召回"
    )
    lexical_top_k: int = Field(default=30, ge=1, le=200, description="词法召回条数")
    lexical_weight: float = Field(
        default=1.0, gt=0.0, le=2.0, description="词法结果在 RRF 融合中的权重"
    )
    lexical_timeout_seconds: float = Field(
        default=2.0, gt=0.0, description="词法召回超时；超时后只用其他召回"
    )

    # 源码头部槽位：语义类查询的前 N 个结果优先给未被先验降权的源码文件。乘性
    # 先验在归一化 RRF 上过弱（同时进入 dense 和 lexical 的测试片段仍居首），
    # 而重排器又不一定启用；显式提到 test/测试 的查询不适用。0 关闭。
    source_head_slots: int = Field(
        default=3, ge=0, le=10, description="语义查询保留给源码文件的头部槽位数"
    )
    # 只含 import 证据的片段是文件头（use/import 行、license 注释、模块 docstring）：
    # 它点名了文件接触的所有模块，所以在向量空间里离"架构/流程"措辞很近，却不
    # 实现其中任何一个。这类片段让出头部槽位；没有任何符号证据的片段不受影响。
    # 2026-09-04 评测：三套 bench 上净效果为零（语义 nDCG@10 -0.1，两条 overview /
    # call-chain 用相关度更低文件的正文换掉了高相关文件的头部片段）。默认关闭。
    head_skips_import_headers: bool = Field(
        default=False, description="源码头部槽位是否跳过只含 import 证据的文件头片段"
    )
    # reference 查询：有 exact/lexical 使用证据的片段按先验分级填充头部槽位；
    # 使用点全在测试/示例/__init__ 里时，仍胜过没有证据的文档。
    reference_head_fallback: bool = Field(
        default=True, description="reference 头部槽位在源码层为空时是否按先验分级回退"
    )
    # compound（issue 文本）查询：正文点名且在工作集内定义不超过 3 处的标识符，
    # 其定义片段占据受保护的头部槽位；0 关闭。
    # 在 13 条 issue 上未观察到收益（一条因锚定 MVCE 里的 setup 调用而回退），
    # 默认关闭，保留为消融开关。
    compound_anchor_slots: int = Field(
        default=0, ge=0, le=5, description="compound 查询保留给点名标识符定义的槽位数"
    )

    # 工作集增量先验：请求 added_blobs 里的文件就是用户正在改的文件。增量过大
    # （首次全量同步）时先验没有区分度，直接跳过。
    working_set_boost: float = Field(
        default=1.15, ge=1.0, le=2.0, description="added_blobs 命中的乘性 boost"
    )
    working_set_boost_max_blobs: int = Field(
        default=50, ge=0, description="added_blobs 超过此数量时不应用 boost；0 关闭"
    )

    # 结果整形：同文件相邻片段合并成一段；二跳拉取被引用符号的定义摘要。
    merge_adjacent_enabled: bool = Field(
        default=True, description="是否合并同文件相邻/重叠片段"
    )
    related_definitions_enabled: bool = Field(
        default=True, description="是否允许语义关系查询附带相关定义摘要"
    )
    related_source_hits: int = Field(
        default=5, ge=1, le=50, description="从前多少条主结果里抽取被引用标识符"
    )
    related_max_symbols: int = Field(
        default=8, ge=1, le=50, description="最多附带多少个符号的定义"
    )
    related_max_definitions_per_symbol: int = Field(
        default=3, ge=1, le=20, description="scope 内定义数超过此值的符号视为过于常见"
    )
    related_snippet_lines: int = Field(
        default=12, ge=1, le=200, description="每个定义摘要最多多少行"
    )
    related_max_chars: int = Field(
        default=4_000,
        ge=1,
        description="定义摘要字符上限（同时受主结果剩余预算约束）",
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
