# AGENTS.md

面向 AI 编码代理的项目开发约束。用户文档见 `README.md`。

## 项目

OpenContextEngine (`oce`) 是 ACE 兼容的代码检索服务：

- cAST/tree-sitter 语义切块；每个 chunk 带封闭作用域签名链 `context`（占位于 `blob_chunks`，不参与内容哈希），embedding 输入为 `File + Context + 正文`
- PostgreSQL/SQLite 存储元数据、`symbol_occurrences`（tree-sitter 整文件抽取 definition/endpoint/import/call/reexport/inherit，每行带 `enclosing` 所在定义名，regex 兜底；call/import/reexport/inherit 提供 reference/call-chain 使用证据与关系车道，不充当定义/endpoint 证据）和 `chunk_lexical` 词法索引（SQLite FTS5 / PG tsvector，DDL 在 `persistence/lexical_index.py`）
- Milvus 3.0 仅做 dense 向量检索，BM25/sparse 不回 Milvus；路径索引独立维护，另有 SQL 精确路径后缀查找
- 检索由 `application/retrieval.py` 在 `RetrievalState` 上协调固定状态机：route → plan → recall → fuse → prior → rerank → select → expand。`QueryEvidence` 只提取实体，`QueryRoute` 记录请求目标和所需证据；提到符号不自动获得 focused 预算或跳过 dense 的权限。完整标识符与限定名不依赖反引号，参数类型与请求函数分开；单字“到/从”、标识符数量和说明性前言不决定调用链/复合意图。
- recall 并行运行 dense、exact、按意图 lexical、path 与 SQL path lookup。SQL 车道在 embedding 往返前启动；只有全部请求定义命中或 SQL 路径命中且可取内容时，`RETRIEVAL_DECISIVE_SKIPS_DENSE` 才允许不等向量召回。reference 保留 dense，主候选须有 SQL occurrence 或完整被问标识符证据。embedding 请求只释放不取消，避免耗尽 httpx 连接池；`dense_route` 落 `retrieval_metrics`。
- `domain/services/symbol_resolution.py` 负责限定名与声明签名解析；定义先按 enclosing，再按同时点名作用域与叶子的声明行，最后按片段中的完整限定名钉住；路径组件须整段相等。SQL use-site 已有叶子 occurrence，不用声明行阶段，实例使用局部名时可用片段中提到的作用域；dense 使用候选须有完整名字、enclosing 或完整模块路径证据。这些不是类型解析后的接收者证明。无法验证定义作用域时，SQL 不冒充精确定义，语义召回保留。同叶子的多个请求作用域必须分别命中，重载参数名可以出现在函数名前。
- `domain/services/ranking.py` 负责纯排序：source/工作集先验只执行一次，普通源码文件名（包括 `index.ts`、`types.ts`、`__init__.py`）不代表实现能力。精确定义/路径、调用链端点与 compound 的 traceback/标题锚点占有界头部；reference 的使用证据先于声明，覆写请求优先同名方法声明；测试请求先看完整测试标题与声明/调用证据，再看路径。结构头部只计算一次，用于模型窗口准备及重排后的恢复；源码先验不覆盖模型的最终语义顺序。hub、`confidence_floor` 与未校准的 ambiguous-definition rerank 分支已退役，实验记录保留在历史报告。
- `application/call_chain.py` 接收显式 scope、端点、已选片段与遍历界限，沿 call 边有界 BFS；只跟随声明处 ≤ 2 的名字。`chain` 小节按 `Hop:` 返回声明头部与必要的交接调用窗口；单端向下追踪、双端路径与可选向上 callers 分开。限定端点先按已记录 enclosing 约束 SQL，再检查歧义上限。
- 明确问实现者时，头部与实现关系小节复用一批有界 inherit 事实；可选被问子类型和限定作用域仍约束这批事实。没有具名目标的测试请求可从已选片段的调用/文本证据扩展定义；仅具名 reference 将相关定义限制为被问符号。
- select 先使用完整主结果预算；expand 取得新关系证据后才裁剪最低优先级尾部，`RETRIEVAL_RELATION_RESERVE_CHARS` 是支出上限。定义候选有界获取一次，裁剪后在内存中重算摘录、去重和来源，不重复查 SQL；已移除主片段独有的名字不能继续扩展。`evidence_pack` 分别装配被引用定义、调用方、实现/子类、测试与转出入口，各有槽位和字符上限；`formatter` 固定小节顺序。
- `RERANK_ENABLED` / `LLM_RERANK_ENABLED` 授权对应阶段；只有 `RERANK_PROVIDER=api` 和 chat LLM 会外发数据，`local` 不外发。策略只做逐查询路由，统一使用 `retrieval_strategy.plan_rerank` 的 intent、候选数与完整 exact/path 证据，禁止用原始召回分数估置信度，不新增 LLM 分类器。reference 保留专用 reranker，adaptive 下不用 chat LLM 压缩覆盖；`always` 仍可运行。路由、定义计数、头部与关系字符等证据持续落 `retrieval_metrics`；已有迁移不回写。
- 新召回证据只能作为独立「车道」进入（固定槽位、必要条件门控或按意图开关），不得把不同标尺的分数直接混排；reference 意图的词法召回以标识符整体代理 token 为必要条件
- `RERANK_PROVIDER=local` 是不外发的进程内 ONNX 交叉编码器（`uv sync --extra local-rerank`）；模型文件由部署者提供，必须单独核对模型许可证
- 产品效用评测位于 `benchmarks/blackbox/`，只能通过发布版 `oce-client` 与稳定 HTTP API 驱动服务，禁止 import `oce`、直读数据库或复制服务端路由状态机；`benchmarks/internal/` 仅做实现级微基准，不作为产品效用结论
- 真实项目关系用例 `benchmarks.blackbox.project_cases` 是关系类改动的主裁判（primary Hit@3、relation/test recall、distractor_head、逐 case 错误分类），公共基准是护栏；问法集 `query_variants.json` 按需求组统计，布局集 `layout_controls` 检查重命名/文件名/行号不变性，均为开发诊断；`benchmarks.blackbox.csn_queries` 是 CodeSearchNet 函数级语义检索护栏。发布判断看指标向量：目标类别改善、其他套件不超容忍回退、distractor_head 不升、字符数与 p50 单独看，不合成总分
- 改动默认检索编排前，必须在 `benchmarks.blackbox.short_queries`（Top-1/MRR/p50）、`benchmarks.blackbox.semantic_queries`（分意图/语言 nDCG@10/字符数）、`benchmarks.blackbox.project_cases` 和 `benchmarks.blackbox.swe_explore --profile development`（Top-1/nDCG@100/字符数）上配对复跑，且 `tests/unit/infrastructure/test_retrieval_regression.py` 是头部顺序与关系小节的离线回归护栏
- 模型凭据集中在 `model_credentials` 单表，按 kind（embed/rerank/llm_rerank/query_rewrite）+ status=active + 最小 priority 解析（`persistence/active_credential.py`），取不到回落各自环境变量
- 向量维度只有一个来源 `EMBED_DIMENSIONS`：Milvus 两个 collection 与凭据校验都从它取值
- 所有配置组统一读 `.env` 与 `.env.local`（后者覆盖前者）
- `index_profiles` 持久化不含密钥的 embedding/chunker/schema fingerprint；不兼容启动或热重载必须 fail closed，不得复用旧向量/切块
- 运维面 `/admin/*` 用独立 `ADMIN_API_KEY`（空则回落 `API_KEY`）：凭据 CRUD/热重载、队列、GC、监控与索引统计
- 监控子系统旁路采集调用/token/资源与检索阶段审计，落 metrics 表
- query vector 使用只保存 query 哈希与向量的进程内 TTL LRU；源码向量与 retrieval result 不缓存，凭据热重载后清空
- FastAPI + DDD/CQRS 分层
- Milvus 索引类型由 `MILVUS_DENSE_INDEX_TYPE` 配置（默认 HNSW）；显式更改本地索引类型时原地重建索引并保留向量。索引类型不可核验或构建失败时不得标记初始化成功
- 限定名调用链端点先按已记录的 `enclosing` 约束 SQL 查询，再检查同名声明数；作用域隔离与歧义上限仍然生效
- 启动预热通过仓储接口获取有限个 ready blob，在接收请求前对 dense/path/lexical 做有界探测；失败记录日志并保留冷状态，不把后台预热视为首个请求已就绪的保证

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
- 修改 chunking、embedding 输入/池化、symbol extraction、lexical 文档或 path-document 语义时，同步递增 `shared/index_profile.py` 中对应版本常量。
- 测试文件的判定只在 `domain/services/test_paths.py` 一处；先验降权与测试关系车道共用。
- 词法/精确/路径查找三类 SQL store 共用 `persistence/scope_filter.py` 应用 scope；新增 SQL 召回不得自行展开 `IN (...)` 全集。
- symbol 查询在已连接的 `BlobModel.blob_name` 上应用同一 scope，使工作集过滤先于 occurrence 探测；超时保留空结果降级并记录 warning。词法查询的 deadline 由调用方持有，避免嵌套取消干扰连接归还。
- 不保留未接入 production composition root 的占位实现或阶段性迁移注释。
- 单文件职责单一；注释解释约束和原因，不复述代码。
- 保持 ACE API 字段与错误语义兼容。

## 运行环境

- Python 3.13.5，虚拟环境为根目录 `.venv`。
- tree-sitter 锁定 `0.25.2`；`compat.py` 负责 API 快照和生命周期隔离。
- `docker-compose.dev.yml` 提供服务模式依赖：PostgreSQL、Redis 和 Milvus 3.0。
- 临时密钥不得写入仓库、日志或评测报告。
