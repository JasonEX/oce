# AGENTS.md

面向 AI 编码代理的项目开发约束。用户文档见 `README.md`。

## 项目

OpenContextEngine (`oce`) 是 ACE 兼容的代码检索服务：

- cAST/tree-sitter 语义切块
- PostgreSQL/SQLite 存储元数据与 `symbol_occurrences` 精确标识符索引
- Milvus 3.0 仅做 dense 向量检索，BM25/sparse 已移除；路径索引独立维护
- 检索主链路：dense + exact + path → source priority / 召回过滤 → 专用 reranker → chat-LLM reranker → focused/coverage select；两种 reranker 必须保留候选集
- `RERANK_ENABLED` / `LLM_RERANK_ENABLED` 是数据外发授权，`RETRIEVAL_RERANK_POLICY` / `RETRIEVAL_LLM_RERANK_POLICY` 只做逐查询路由；两者共用 `retrieval_strategy.plan_rerank` 的确定性证据（intent、候选数、exact/path 命中），禁止用原始召回分数估置信度，也不新增 LLM 分类器；路由结果落 `retrieval_metrics.rerank_route`
- 模型凭据集中在 `model_credentials` 单表，按 kind（embed/rerank/llm_rerank/query_rewrite）+ status=active + 最小 priority 解析（`persistence/active_credential.py`），取不到回落各自环境变量
- 向量维度只有一个来源 `EMBED_DIMENSIONS`：Milvus 两个 collection 与凭据校验都从它取值
- 所有配置组统一读 `.env` 与 `.env.local`（后者覆盖前者）
- `index_profiles` 持久化不含密钥的 embedding/chunker/schema fingerprint；不兼容启动或热重载必须 fail closed，不得复用旧向量/切块
- 运维面 `/admin/*` 用独立 `ADMIN_API_KEY`（空则回落 `API_KEY`）：凭据 CRUD/热重载、队列、GC、监控与索引统计
- 监控子系统旁路采集调用/token/资源与检索阶段审计，落 metrics 表
- query vector 使用只保存 query 哈希与向量的进程内 TTL LRU；源码向量与 retrieval result 不缓存，凭据热重载后清空
- FastAPI + DDD/CQRS 分层

依赖方向：`shared <- domain <- application <- api`。`infrastructure` 实现 shared/domain
协议，只能由 composition root 装配；API router 不编排业务流程。

## 命令

个人模式（SQLite + Milvus Lite + worker 关闭，单机零依赖）：

```powershell
uv run oce init                  # 在 ~/.oce/data 生成个人模式 .env
uv run oce serve                 # 默认 127.0.0.1:8986；--data-dir/--env-file/--host/--port，-v/-vv 提日志级别
uv run oce version               # 打印版本（等价 oce --version）
```

服务模式（PostgreSQL + Milvus 3.0 + Redis）：

```powershell
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn oce.main:app --reload --port 8986
uv run python -c "from oce.main import app; print('OK')"
```

```powershell
uv run pytest tests/unit/application/test_service.py -q
uv run pytest tests/unit/infrastructure/test_milvus3.py -q
```

按文件粒度跑，让 Milvus Lite / tree-sitter 运行时在进程间释放；内存受限时勿在单进程里跑整个
`tests/unit/infrastructure`。

## 代码约束

- 依赖管理只使用 `uv`；新增依赖先修改 `pyproject.toml`。
- CI 同时跑 `uv run ruff check .` 与 `uv run ruff format --check .`；提交前先 `uv run ruff format .`。
- 时间戳使用 `datetime.now(timezone.utc)`，禁止 `datetime.utcnow()`。
- 禁止新增 `__all__`；直接 import 具体符号。
- 领域模型使用 dataclass，配置和 HTTP DTO 使用 Pydantic。
- Repository、SearchStore、Embedder、Reranker 使用 Protocol。
- 新业务编排进入 `application/`，FastAPI router 仅处理 DTO、鉴权和异常映射。
- 数据面用 `verify_api_key`、运维面用 `verify_admin_key`，两者分离；凭据明文只经 `model_credentials`，响应与日志一律只暴露末 4 位。
- 监控/指标为旁路且非阻塞：采集失败或 usage 字段缺失只跳过，不得影响检索主链路。
- 修改 chunking、embedding 输入/池化、symbol extraction 或 path-document 语义时，同步递增 `shared/index_profile.py` 中对应版本常量。
- 不保留未接入 production composition root 的占位实现或阶段性迁移注释。
- 单文件职责单一；注释解释约束和原因，不复述代码。
- 保持 ACE API 字段与错误语义兼容。

## 运行环境

- Python 3.13.5，虚拟环境为根目录 `.venv`。
- tree-sitter 锁定 `0.25.2`；`compat.py` 负责 API 快照和生命周期隔离。
- `docker-compose.dev.yml` 提供服务模式依赖：PostgreSQL、Redis 和 Milvus 3.0。
- 临时密钥不得写入仓库、日志或评测报告。
