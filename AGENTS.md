# AGENTS.md

面向 AI 编码代理的项目开发约束。使用说明见 [README](README.md) / [中文 README](README.zh-CN.md)，文档入口见 [docs](docs/README.md)。

## 项目

OpenContextEngine (`oce`) 是 ACE 兼容的代码检索服务，使用 FastAPI + DDD/CQRS。

依赖方向：`shared <- domain <- application <- api`。`infrastructure` 实现 shared/domain
协议，只能由 composition root 装配；API router 不编排业务流程。

### 数据与索引

- cAST/tree-sitter 语义切块；每个 chunk 带封闭作用域签名链 `context`（占位于 `blob_chunks`，不参与内容哈希），embedding 输入为 `File + Context + 正文`
- PostgreSQL/SQLite 存储元数据、`symbol_occurrences`（tree-sitter 整文件抽取 definition/endpoint/import/call/reexport/inherit，每行带 `enclosing` 所在定义名，regex 兜底）和 `chunk_lexical` 词法索引（SQLite FTS5 / PG tsvector，DDL 在 `persistence/lexical_index.py`）。只有 definition/endpoint 算 symbol 定义已命中的结构证据；call/import/reexport/inherit 可用于 reference 使用点、实现头部、导入头部识别及关系/调用链检索
- Milvus 3.0 仅做 dense 向量检索，BM25/sparse 不回 Milvus；路径索引独立维护，另有 SQL 精确路径后缀查找
- 向量维度只有一个来源 `EMBED_DIMENSIONS`：Milvus 两个 collection 与凭据校验都从它取值
- `index_profiles` 持久化不含密钥的 embedding/chunker/schema fingerprint；不兼容启动或热重载必须 fail closed，不得复用旧向量/切块
- 源码准入版本为 2，允许以 `-retrieval-eval` 结尾的普通目录；版本 1 索引需要新数据目录和完整客户端重同步
- Milvus 索引类型由 `MILVUS_DENSE_INDEX_TYPE` 配置（默认 HNSW）；显式更改本地索引类型时原地重建索引并保留向量。索引类型不可核验或构建失败时不得标记初始化成功
- SQL 元数据是 blob 与 checkpoint 的持久真值，Redis 是任务投递投影。SQLite 连接必须启用外键。GC 先标记 `DELETING`，dense/path 删除均成功后才删元数据；失败身份可重试，checkpoint 与索引写入不得复活它
- 上传路径最长 1024 字符；完整路径参与 embedding，Milvus 诊断字段按 UTF-8 容量裁剪，检索输出从 SQL 取完整路径

### 检索

改动检索前先读 [retrieval-pipeline.md](docs/retrieval-pipeline.md)，以其中的阶段职责、头部规则、车道门控和退休规则为准。

- 固定流程：route → plan → recall（dense ∥ exact ∥ 按意图 lexical ∥ path ∥ path lookup）→ fuse → prior/rerank → select → expand。`domain/services/retrieval/` 每阶段一个模块；`QueryRoute`、`QueryPlan`、`RecallEvidence` 冻结，后续只读这些证据，按阶段交出候选、选择与扩展结果
- 请求文本只在 route 解析一次，问法、标题标识符等进 `QueryRoute`，后续不得再对原文跑正则；车道以返回值交出结果，不在共享状态上留副作用
- SQL 车道在 plan 前启动；可用 SQL operator 的 symbol/显式 path 请求先判断结构答案，命中不创建 embedding task，未命中才启动；其他请求保留并行。已发出的 embedding 请求只释放不取消（`shared/aio.wait_released`，不用 `asyncio.shield`）
- 任何车道失败通过 `lane_failed` 记入 `retrieval_metrics.lane_failures`，不得静默吞掉；存储层不吞超时，空结果只表示无匹配
- 新召回证据只能作为独立车道进入（固定槽位、必要条件门控或按意图开关），不得把不同标尺的分数直接混排；reference 词法召回以标识符整体代理 token 为必要条件
- 现有 exact 按意图窗口优先与 path 有界 boost 的合并策略见检索文档；`SearchHit.score` 是阶段内排序值，不是跨车道校准分数或置信度，不能用它为新车道增加阈值
- scope 只接纳已物化的 ready 身份，并冻结当前非 ready 身份的排除集合；词法、精确、路径查找共用 `persistence/scope_filter.py`，不得自行展开 `IN (...)` 全集。symbol 在已连接的 ready blob 上提前应用 scope；词法 deadline 由调用方持有
- `RERANK_ENABLED` / `LLM_RERANK_ENABLED` 授权对应阶段，policy 只做逐查询路由。两者共用 `retrieval_strategy.plan_rerank`（intent、候选数、主要请求符号的定义/SQL path 命中、dense 是否被结构证据跳过），symbol 与 dense 门控共用 `primary_definition_found`；禁止以原始召回分数估置信度或新增 LLM 分类器
- 同名声明数与头部槽位仅用于审计，不参与当前路由。`exact_definitions` 是 ready scope 内请求叶子名的已记录声明数之和，`definition_sites` 是最大单名计数，均在 chunk 去重/截断前计数，不宣称抽取了全部源码声明。路由、槽位和关系字符等写入 `retrieval_metrics`，用于离线校准
- 默认关闭的实验开关两轮配对评测未成为默认值即删除，净负变体立即删除，待校准开关必须附带标签计划
- rank 交出有界 `structural_heads`，select 先保留其候选顺序，再执行文件覆盖；保护不绕过条数、每文件、重叠和字符上限。选择与扩展共用代码正文的硬字符预算；HTTP 格式化标题、路径等不计入该预算。相邻合并须计入分隔符开销
- 限定名调用链端点先按已记录的 `enclosing` 约束 SQL 查询，再检查同名声明数；作用域隔离与歧义上限仍然生效

### 运行时、配置与数据外发

生命周期边界见 [runtime-lifecycle.md](docs/runtime-lifecycle.md)。

- `Container(settings, session_factory)` 装配同一张生产依赖图并拥有资源；`build_*` 按子系统构建，`start()` / `close()` 是唯一启动与释放入口，ASGI lifespan 只调用二者。worker 是显式 `WorkerState` 状态机，新增状态或转移须同步生命周期文档
- worker 关闭时上传/检索在请求内同步索引，不创建 Redis 队列；两种路径共享应用层短事务编排，embedding 往返不占元数据事务
- `EMBED_ENABLED=false` 收敛为请求内切块，不装配 Redis/worker；PENDING 与 staging 保留，不反复投递或通过后台处理延长 TTL
- 模型凭据集中在 `model_credentials`，按 kind（embed/rerank/llm_rerank/query_rewrite）+ active + 最小 priority 解析（`persistence/active_credential.py`），取不到回落各自环境变量。一次热重载持有 runtime 锁；LLM 部分失败沿用 `reloaded=false`/`reason`，不能宣称跨模型全局原子更新
- 所有配置组统一读 `.env` 与 `.env.local`，后者覆盖前者。CLI 默认保留进程变量优先级，显式 `--env-file` 保留其覆盖语义
- embedding 发送源码到配置的模型端点；重排仅 `RERANK_PROVIDER=api` 与 chat LLM 外发，`local` 为进程内 ONNX。`uv sync --extra local-rerank` 安装运行依赖，模型由部署者提供并单独核对许可证
- 运维面 `/admin/*` 使用 `ADMIN_API_KEY`（空则回落 `API_KEY`），包括凭据/热重载、队列、GC、监控与索引统计
- 监控旁路、非阻塞采集调用/token/资源与检索审计；采集失败或 usage 缺失只跳过，不影响主链路
- query vector 只缓存 query 哈希与向量，使用进程内 TTL LRU；源码向量与 retrieval result 不缓存，凭据热重载后清空
- 启动预热通过仓储接口获取有限个 ready blob，在接收请求前对 dense/path/lexical 做有界探测；失败记录日志并保留冷状态，不把后台预热视为首个请求已就绪的保证

### 评测与验证

- 产品效用评测在 `benchmarks/blackbox/`，只能用发布版 `oce-client` 与稳定 HTTP API，禁止 import `oce`、直读数据库或复制服务端状态机；`benchmarks/internal/` 只作实现诊断，不作产品效用结论
- 关系改动以 `project_cases` 为主裁判（primary Hit@3、relation/test recall、distractor_head、逐 case 错误分类），公共基准为护栏；`csn_queries` 是 CodeSearchNet 函数语义护栏
- 改动默认检索编排前，配对复跑 `short_queries`（Top-1/MRR/p50）、`semantic_queries`（分意图/语言 nDCG@10/字符数）、`project_cases`、`swe_explore --profile development`（Top-1/nDCG@100/字符数），均位于 `benchmarks.blackbox`
- `tests/unit/infrastructure/test_retrieval_regression.py` 是头部与关系离线护栏；纯结构重构用 `benchmarks.internal.retrieval_equivalence` 在同一冻结语料上证明 dump 逐条一致。正确性修复导致的预算/审计差异必须逐项解释，不伪称全量等价
- 检索效用判断看指标向量：目标类别改善、其他套件不超容忍回退、distractor_head 不升；字符数与 p50 单独看，不合成总分。可靠性/结构变更须证明行为边界与质量保持，不能以保持分数宣称效用提高
- 命令与 truth 合同见 [benchmarks/README.md](benchmarks/README.md)，历史证据见 [评测归档](benchmarks/results/README.md)

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
uv run pytest tests/unit/test_smoke_personal_mode.py -q
uv run mypy
```

按文件粒度跑，让 Milvus Lite / tree-sitter 运行时在进程间释放；内存受限时勿在单进程里跑整个
`tests/unit/infrastructure`。

## 代码约束

- 依赖管理只使用 `uv`；新增依赖先修改 `pyproject.toml`。
- CI 同时跑 `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run mypy`；提交前先 `uv run ruff format .` 并保证 mypy 零报错。新函数必须完整注解（`disallow_untyped_defs`）。
- 时间戳使用 `datetime.now(timezone.utc)`，禁止 `datetime.utcnow()`。
- 禁止新增 `__all__`；直接 import 具体符号。
- 领域模型使用 dataclass，配置和 HTTP DTO 使用 Pydantic。
- Repository、SearchStore、Embedder、Reranker 使用 Protocol。
- 新业务编排进入 `application/`，FastAPI router 仅处理 DTO、鉴权和异常映射。
- 数据面用 `verify_api_key`、运维面用 `verify_admin_key`，两者分离；凭据明文只经 `model_credentials`，响应与日志一律只暴露末 4 位。
- 修改 chunking、embedding 输入/池化、symbol extraction、lexical 文档或 path-document 语义时，同步递增 `shared/index_profile.py` 中对应版本常量。
- 测试文件的判定只在 `domain/services/test_paths.py` 一处；先验降权与测试关系车道共用。
- 不保留未接入 production composition root 的占位实现或阶段性迁移注释。
- 单文件职责单一；注释解释约束和原因，不复述代码。代码内注释与 docstring 统一英文；中文只出现在 `AGENTS.md`、`docs/`、`README.zh-CN.md`、`.env.example` 和作为数据的查询样本里。
- 测试替身集中在 `tests/fakes/`（检索、索引、embedding），测试文件不再各自定义同名 Fake；`tests/unit/test_smoke_personal_mode.py` 用真实 Container、迁移、Milvus Lite 和进程内 embedding 端点走完上传、checkpoint、HTTP 检索，是装配错误的护栏。
- 保持 ACE API 字段与错误语义兼容。

## 运行环境

- Python 3.14（开发版本），虚拟环境为根目录 `.venv`；CI 覆盖 3.11（`requires-python` 下限）、3.13（Docker 镜像运行时）与 3.14。
- tree-sitter 锁定 `0.25.2`；`compat.py` 负责 API 快照和生命周期隔离。
- `docker-compose.dev.yml` 提供服务模式依赖：PostgreSQL、Redis 和 Milvus 3.0。
- 临时密钥不得写入仓库、日志或评测报告。
