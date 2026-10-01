# 检索管线设计

本文记录 `RetrievalPipeline` 各阶段的设计决策与调优历史。约束（改动前必须遵守的规则）
在 `AGENTS.md`；这里回答"为什么是这样"。代码位于 `src/oce/domain/services/retrieval/`，
每个阶段一个模块。

## 状态机

一次检索是固定的状态转移。每个阶段产出一条记录（`state.py`），之后的阶段只读不改：

| 阶段 | 模块 | 产出 | 职责 |
| --- | --- | --- | --- |
| route | `route.py` | `QueryRoute`（冻结） | 只解析一次请求文本：`QueryEvidence`（标识符、traceback 帧、引号短语、文件名、词元）、意图、策略、限定名、`asks_tests` / `asks_implementors` |
| plan | `plan.py` | `QueryPlan`（冻结） | 可选 LLM 改写、句子级 facet 分解、启动（不等待）query embedding |
| recall | `recall.py` + `recall_*.py` | `RecallEvidence`（冻结） | dense ∥ exact ∥ 按意图 lexical ∥ path ∥ path lookup ∥ anchors |
| fuse | `fuse.py` | `candidates` | dense/lexical 按 RRF 融合 → 合并 exact → 端点/锚点补入 → 路径 boost/回填 |
| prior + rerank | `rank.py` | `candidates` | 源码/工作集先验 → 有界头部槽位 → 模型重排 → 头部复位 |
| select | `pipeline.py` | `selected` | focused/coverage 选择，字符预算为硬限制 |
| expand | `expand.py` + `chain.py` | `related` | 相邻合并；关系小节（被引用定义、调用方、实现、测试、转出、调用链） |

后续阶段不再读原始请求文本做判断：问测试、问实现等问法在 route 阶段算出，写进
`QueryRoute`。车道以返回值交出结果（例如 exact 车道返回 `ExactEvidence`），由 recall
编排汇总成一条记录，不在共享状态上留副作用。`RetrievalState` 另外只保存两类请求内缓存：
头部复位复用的实现关系 key，以及关系小节重渲染复用的 call / definition 原始行。

关闭对应开关时每个阶段退化为恒等变换。`lane_failed` 是车道失败的唯一出口：任何车道
抛出异常（包括 SQL 超时）时记录 `audit.lane_failures[lane] = ExceptionType`，落到
`retrieval_metrics.lane_failures`，请求照常从其余车道作答。存储层不吞超时：空结果只表示
索引里没有匹配，超时一律抛给车道记录。离线对比时若该列非空，排序变化不能归因于代码。

## 路由（route）

- 限定名 `Session.get` 整体保留为一个标识符；管线派生叶子 `get`，并记录 `get` 被钉在
  `Session` 作用域。钉住顺序：已记录的 `enclosing` → 同时点名作用域与叶子的声明行 →
  片段文本。路径证据须整段相等（`test/app.render.js` 不算 `app` 作用域）；use-site
  批次不用声明行阶段。
- 「哪些地方调用了 X」是 reference；「A 如何到达 B」两个符号是 call_chain；「X 在哪里定义」
  不论点名多少参数类型都是 symbol（多出的名字用于挑重载，不是新 facet）。
- 标识符超过 2 个或 planner 切出 ≥ 3 个 facet 的 issue 文本是 compound。
- 中文调用链信号按构式匹配：「从 A 到 B」（同一分句内两端都在）与「调到 / 到达」；单独的
  「到 / 路径 / 完整」不算（「在哪里能找到」「配置文件的路径」「完整实现」曾被路由成
  call_chain），「用到」与「使用」同为 reference 动词。

## 召回（recall）

- SQL 车道只依赖路由，在 embedding 往返前启动。symbol 的首个被问符号定义命中、path 的
  SQL 路径命中、reference 的被问符号 call/inherit 使用点一旦出现就不再等 embedding
  （`RETRIEVAL_DECISIVE_SKIPS_DENSE`，`dense_route` 记 `skip:<reason>`）。只有 import
  证据不算：使用点可能在抽取器归因不了的代码里。
- embedding 请求只释放、不取消：取消进行中的 httpx 请求会让连接不归还，约 20 次后连接池
  耗尽，之后每次 embed 都超时。
- 词法召回：symbol/path 只在结构证据缺失时补跑；reference 以标识符整体代理 token 为必要
  条件，避免 `get OR json` 被 `json` 密集的片段占满。
- compound 锚点：traceback 帧解析到该文件该行的声明、标题点名且限定名严格钉住的声明
  （定义 ≤ 3 处）。早先按「任何点名标识符」锚定曾锁定 MVCE 里的 setup 调用而回退。
- hub 车道（请求词拼出的已声明名字按被引用文件数排序）已删除：精选 overview nDCG@10
  67.8→74.3，但 held-out 语义集 overview 66.4→54.1、call-chain 85.6→78.2，两轮评测未成为
  默认值；扩展到 feature 问句的变体把实现函数挤开（feature nDCG@10 73.5→68.8、CSN Region
  Top-1 62.5→60.0）。评测记录保留在 `benchmarks/results/`。

## 融合与先验（fuse, rank）

- dense cosine、BM25/ts_rank 只按名次（RRF）融合；exact 按意图合并：symbol 的定义拼接在
  语义候选之前，call_chain 在窗口里给 exact 预留约三分之一，compound 把 exact 当作一路名次
  列表，其余意图按 key 取较高分。exact 的种类分（0.85–1.0，按同名定义数衰减）因此在窗口里
  领先大多数语义候选，这是有意的：结构证据必须留在头部规则可选的窗口里。anchors 与
  call_chain 端点按 key 补入窗口，由头部规则排序。
- 已评测并否决：所有意图统一按名次融合 exact（2026-10-01，生产同款 Qwen3-Embedding-4B，
  同一物理索引配对，基线复跑逐条一致）。纯名次融合会把只有 exact 车道命中的使用点挤出
  50 条窗口：short reference Top-1 92.5→88.8%、project test_mapping Hit@3 85.7→57.1%、
  distractor_head 0→2.9%。补上「结构列表不被窗口截断」后仍无任何套件改善（project Hit@3
  −1 例、semantic weighted R@5 −1 点），按发布规则不采用。
- 头部槽位：symbol 的定义按文件分散（每个声明文件先各占一槽），path 的 SQL 匹配每文件一槽，
  compound 的锚点按帧顺序，call_chain 的两个端点。语义查询保留 `RETRIEVAL_SOURCE_HEAD_SLOTS`
  给未降权的源码文件；只含 import 证据的文件头让出槽位（2026-09-08 在 project_cases 上
  复测：唯一的头部干扰项消失，其余四套逐 case 不变）。
- reference 头部按结构分层：call/inherit 使用点 → 文本提及 → 仅 import；点名限定符的
  片段优先；同时点名另一个符号的优先；他文件的使用点先于声明文件；以符号命名的文件先；
  离声明包更近的先。问测试的查询由测试文件领头，声明名最贴近符号的测试块在前。
- 文件命名与包内邻近是结构证据，不是个案拟合：`parser` 这类常见名在脚本里是无关的局部
  变量，邻近声明包近似了索引没有的名字解析；测试按被测单元命名是通行约定。2026-10-01 删除
  这三条平局规则的变体回退了 pytest `parser` reference、gin/rtk test_mapping 与 gson
  multi_impl，已恢复。
- 模型重排后头部规则复位：`always` 是评测策略，不是抹掉确定性答案的许可。overview 例外，
  允许语义重排把架构文档放回首位。
- 头部复位复用当前 `RetrievalState` 已取得的实现关系 key；排序仍重新应用，不重复查询 SQL。
  证据不跨请求保存，失败仍记入 `lane_failures`。
- 本地 jina 交叉编码器在结构化头部车道之上复测为净负（semantic nDCG@10 74.9→72.9、
  issue nDCG@100 74.3→62.0，每个向量请求多约 1.2 s），默认关闭。

## 扩展（expand）

- `evidence_pack` 按意图组装独立小节，各自槽位与字符上限，去重后以 role 标注。主结果预算
  不预扣：只有出现新的关系证据时才裁掉主结果最低优先级的尾部，关系上限随上下文预算缩放。
- 被引用定义：被调用的名字优先；symbol/reference 在具名车道之后填充；限定名按作用域钉住
  （"where is `Flask.make_response` defined" 不能附上 `helpers.make_response`）。
- call_chain 两端点：沿 `symbol_occurrences` 的 call 边有界 BFS，只跟随声明处 ≤ 2 的名字，
  每跳先放声明头部，交接调用离头部远时再补一段止于调用行的窗口。单端追踪取两层被调用者。
- 主结果裁尾后重新生成关系小节，只复用本次请求中按选中 chunk 保存的原始 call 记录和
  按名称、SQL 上限保存的 definition 记录；重新计算顺序、去重、来源与字符预算，不缓存小节
  成品。已裁掉的来源不能继续贡献关系证据。
- 普通关系总上限为 0 时不执行普通关系 SQL，也不输出普通关系小节；`chain` 使用独立的
  `RETRIEVAL_CALL_CHAIN_MAX_CHARS` 预算。两类预算都受请求总字符上限约束。

## 实验开关退休规则

设置项中默认关闭、且注释写明"实测净负"或"待校准"的开关，是可测量的实验，不是产品功能。
处置规则：

1. 一个开关自加入起经过两轮配对评测仍未成为默认值，删除开关及其代码路径；评测记录保留
   在 `benchmarks/results/`。
2. 已判定净负（所有套件回退）的变体立即删除，不保留为可选项。
3. "待校准"开关必须附带离线标签的获取计划；没有计划的按第 1 条处理。
4. 已成为默认值的消融开关，若关闭分支既不在生产也不在评测编排中使用，删除开关、保留行为。

当前状态：

| 开关 | 状态 | 依据 |
| --- | --- | --- |
| `RETRIEVAL_HUB_HEAD_SLOTS` / `RETRIEVAL_HUB_MAX_DEFINITIONS` | 已删除（规则 1） | 精选集正向、held-out 负向，两轮未成为默认值 |
| `RETRIEVAL_HUB_FEATURE_ENABLED` | 已删除（规则 2） | feature 上净负 |
| `RETRIEVAL_RERANK_AMBIGUOUS_DEFINITIONS` | 已删除（规则 3） | 无同名定义离线标签的获取计划 |
| `RETRIEVAL_CONFIDENCE_FLOOR` | 已删除 | 默认 0 为恒等；非 0 时以融合后的混合标尺分数过滤，违反「不混排」 |
| `RETRIEVAL_HEAD_SKIPS_IMPORT_HEADERS` | 已删除（规则 4），行为保留 | 2026-09-08 起为默认值 |
| `RETRIEVAL_REFERENCE_HEAD_FALLBACK` | 已删除（规则 4），行为保留 | 默认开启 |

## 等价性验证

纯结构重构（不改变默认排序）用 `benchmarks/internal/retrieval_equivalence.py` 证明：
它把一个目录索引进内存 SQLite，用确定性词频向量替代 embedding，对固定查询集在五种配置档
下跑管线并记录每条结果与审计字段。重构前后两份 dump 必须逐条一致：

```bash
uv run python -m benchmarks.internal.retrieval_equivalence dump --corpus src/oce --out before.json
# ... refactor ...
uv run python -m benchmarks.internal.retrieval_equivalence dump --corpus src/oce --out after.json
uv run python -m benchmarks.internal.retrieval_equivalence compare before.json after.json
```

语料必须冻结（重构会改动 `src/oce` 本身），用 `git archive HEAD src/oce` 解出一份。这只
证明两个版本计算同一函数，不代表产品效用；效用仍由 `benchmarks/blackbox/` 配对复跑判定。
