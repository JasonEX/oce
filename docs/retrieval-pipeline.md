# 检索管线设计

本文说明当前检索编排、配置边界和保留这些规则的原因。源码位于
`src/oce/domain/services/retrieval/`，意图策略在 `retrieval_strategy.py`，配置默认值在
`shared/config/settings.py`。开发约束见 [AGENTS.md](../AGENTS.md)，产品启动与配置见
[README.zh-CN.md](../README.zh-CN.md)，评测方法见 [benchmarks/README.md](../benchmarks/README.md)。
后文的历史结果不代表当前部署的质量或延迟保证。

## 当前默认与作用域

默认启用 exact、lexical、语义路径索引、SQL 路径查找、句子级 query decomposition、源码先验、
coverage/focused 选择、相邻合并和按意图开放的关系小节。query rewrite 默认关闭；两种模型
重排默认也关闭，授权后才由逐查询 policy 决定是否执行。关闭某项能力会去掉对应车道或处理，
不会关闭整条检索管线。

生产请求必须声明 checkpoint 或 added blobs。应用层先计算
`declared = (checkpoint members | added) - deleted`，再只让元数据为 READY 的 blob 进入
`SearchScope.blob_names`。PENDING、ERROR、DELETING 或不存在的 blob 即使留有向量也不准入。
`deleted_blob_names` 同时保存用户删除项和 `declared - ready`：后者在请求进行中变成 READY，
也不能扩大这次已解析的 scope。原始 added 集合仍保留，供工作集先验判断增量大小。

这是本次请求的成员准入快照，不是跨 SQL 与 Milvus 的全局数据库快照；后续 SQL 仍检查 READY。
SQL 车道共用 `persistence/scope_filter.py`：优先用 checkpoint 关系和有限 delta 表达 scope，
关系版本变化或 delta 过大时回退到已解析成员的分批查询。dense 和语义路径使用同一准入集合。
空 scope 返回空结果；只有直接调用领域管线的测试允许 `scope=None`。

## 阶段与请求内记录

一次请求按固定顺序执行。前三个阶段的记录是冻结 dataclass，后续阶段只读取这些证据；
候选和输出列表则由所属阶段更新，expand 可以合并或裁去 selected 的尾部。

| 阶段 | 模块 | 写入 | 职责 |
| --- | --- | --- | --- |
| route | `route.py` | `QueryRoute`（冻结） | 提取标识符、限定名、traceback、短语、文件与词元；确定意图、策略、标题标识符及问测试/问实现标记 |
| plan | `plan.py` | `QueryPlan`（冻结）、embedding task | 可选改写；保留完整请求并增加句子级 facet；启动 query embedding |
| recall | `recall.py`、`recall_*.py` | `RecallEvidence`（冻结） | 汇总 exact、lexical、路径、锚点与 dense 结果 |
| fuse | `fuse.py` | `candidates` | 语义名次融合、按意图合并 exact、补入端点/锚点、路径 boost 与回填 |
| prior → rerank | `rank.py` | `candidates`、`decision` | 源码/工作集先验、有界头部、模型重排、头部复位 |
| select | `pipeline.py`、`selector/` | `selected` | focused/coverage 或 Top-K 选择，遵守代码字符预算 |
| expand | `expand.py`、`chain.py` | `selected`、`related` | 相邻合并、关系小节和调用链，必要时裁去主结果尾部 |

请求的结构证据和问法判断集中在 route，后续阶段不再从原文重新判断标题点名、测试或实现意图。
plan 仍处理搜索文本的句子边界，rewrite/rerank 模型仍会收到请求文本；这些操作不重写已确定的
`QueryRoute`。车道以返回值交出结果，例如 exact 返回 `ExactEvidence`，由 recall 汇总，
不直接改候选或其他车道的输出。

请求内缓存 rank 的 import-only 头部 key、实现关系 key，以及 expand 按选中 chunk 保存的
call 行、按名称和 SQL 上限保存的 definition 行。头部排序和关系小节重新生成，不缓存完整
答案，也不跨请求保存这些关系证据。

意图策略允许的能力如下，各项仍受对应配置开关、store 可用性和证据条件约束：

| 意图 | 选择 | rewrite（另需授权） | 关系小节 |
| --- | --- | --- | --- |
| symbol | focused | 允许 | 定义、调用方、实现/子类、测试、re-export |
| path | focused | 允许 | 无 |
| reference | coverage | 允许 | 定义、调用方、测试 |
| call_chain | coverage | 不使用 | chain、定义、调用方、测试 |
| feature | coverage | 允许 | 定义、测试 |
| overview | coverage | 不使用 | 定义 |
| compound | coverage | 允许 | 测试 |

### route 与 plan

- 限定名 `Session.get` 保留整体，并派生叶子 `get` 与限定作用域 `Session`。匹配优先用
  已记录的 enclosing，其次是同时点名作用域和叶子的声明行，最后才用片段文本；use-site
  批次不使用声明行判定。路径组件须完整匹配，`test/app.render.js` 不能证明 `app` 作用域。
- 「哪里定义 X」是 symbol，参数类型用于挑重载；「哪里使用 X」是 reference；「A 如何到达 B」
  是 call_chain。多标识符、多明确句子条件的 issue 文本可以成为 compound。
- 中文调用链按「从 A 到 B」（同一分句内两端都在）、「调到 / 到达」等构式判断；单独的
  「到 / 路径 / 完整」不触发调用链。「用到」和「使用」都可表达 reference。
- 默认 decomposition 最多产生 4 个搜索文本（完整请求加 facet），facet 至少 8 字符。
  单个有效片段不额外分解。多 facet 时每个召回 20 条，facet 的 RRF 权重为 0.75；
  单文本召回窗口默认 50 条。
- query rewrite 授权且意图允许时，LLM 返回原文加改写；失败退回原文。语义路径搜索同时保留
  原文与改写，避免跨语言文件描述失去原来的文件名证据。

### recall 的门控

SQL 任务在 plan 启动 embedding 之前创建，允许 SQL 与模型往返重叠。当前 recall 先汇总
exact、path lookup、按意图开放的 lexical 和 compound anchors，再判断结构证据是否足够；
判定通过后不等待 embedding 完成，并停止尚未完成的向量车道编排。

默认 `RETRIEVAL_DECISIVE_SKIPS_DENSE=true`，决定性证据是二元事实：

| 意图 | 可以跳过等待 dense 的条件 |
| --- | --- |
| symbol | 首个被问符号的定义命中；仅参数类型命中不算 |
| path | SQL 路径命中，且有内容 store 可以回填展示片段 |
| reference | 有 call/inherit 使用点，且至少一个使用 chunk 不是该符号的声明 chunk |

只有 import 证据不算使用点。跳过时 `dense_route` 记 `skip:<reason>`，不使用相似度阈值猜测
置信度。已发出的 embedding 请求只释放等待、不取消，让它自行结束并消费任务结果；取消
HTTP 响应曾导致池连接不能归还。调用者取消或 plan 失败时，SQL 任务会取消并 drain，
embedding 仍按这项纪律释放。

词法车道使用 SQL FTS5/tsvector。reference 以完整标识符的代理 token 为必要条件，例如
`getJson` 的整体 token，避免宽泛的 `get OR json` 召回；多个标识符的必要 token 之间是 OR。
symbol/path 通常只在 exact/SQL path 缺失时补跑；引号短语可以使词法车道提前执行。
call_chain、feature、overview、compound 默认提前执行词法召回。

语义路径索引服务于找文件的请求，SQL 路径查找处理显式文件名、路径及 traceback；两个车道
都以 blob 交出证据。路径回填只为内容车道遗漏的文件取代表 chunk，不替换已经命中的正文。
traceback 的路径证据通常只 boost，避免把文件首部 import 回填成故障位置。

compound 锚点来自 traceback 文件/行定位的声明，或 route 保存的标题标识符：后者须严格满足
限定名约束且同名声明不超过 3 处。仅在 issue 正文或最小复现中提到的 helper 不成为锚点。

## 融合、头部与重排

### 不同证据的合并边界

多路 dense cosine 与 lexical BM25/ts_rank 按名次做 RRF，单路保留其顺序与分数；
默认 `RRF_K=60`、词法权重 1.0。
exact 按意图合并：symbol 的定义位于语义候选前；call_chain 在候选窗口中给 exact-only
保留约三分之一；compound 把 exact 作为一路名次列表；其他意图按 key 保留较高分的命中。
exact 种类分（约 0.85–1.0，按同名定义数衰减）会在部分窗口里领先 RRF 候选，这是当前
有意保留的行为，使结构证据进入可被头部规则保护的窗口。

anchors 与 call_chain 端点按 key 补入，之后由头部规则排序。路径证据按配置权重对同 blob
的候选做 boost；它不能作为重排置信度。这里没有把所有存储分数统一校准到一个数值尺度。

### 有界头部

源码先验降权文档、测试和 barrel 文件；明确找文件或问测试的短查询使用中性先验。
小规模 added delta 可以获得工作集 boost，首次全量同步超过增量上限时不会被当成编辑线索。
先验只准备模型之前的候选顺序。

结构头部按被问对象保留有限槽位：symbol 最多 3 个（也受最终主结果数限制），每个声明文件
先占一槽再补重载；path 每个 SQL 匹配文件一槽；compound 最多 3 个锚点，按帧再标题的顺序；
call_chain 保护已解析的一到两个端点。语义与 reference 默认另外优先放 3 个源码片段，
import-only 头部优先让位给实现，根 README 不消耗源码槽位。

reference 头部须有 exact/lexical 发生证据，声明 chunk 通常让位给使用点。问实现时已记录的
目标实现块优先，随后比较 call/inherit → 文本提及 → 仅 import、限定符共现、其他被问符号
共现、是否位于声明文件、文件是否按符号命名、与声明包的邻近关系。源码候选为空时允许测试、
示例或包入口中的真实使用点领头。明确问测试时优先匹配命名和包邻近的测试文件，再比较测试
声明名与符号的距离、使用点及提及证据。

模型重排后恢复结构头部和源码偏好，同时保留模型的尾部顺序；overview 不再恢复源码偏好，
允许模型把架构文档放回首位。实现关系与 import-only key 在本次请求内复用，不重复查 SQL。

### 模型授权与 policy

`RERANK_ENABLED`、`LLM_RERANK_ENABLED` 是部署者的能力授权；`RETRIEVAL_RERANK_POLICY`、
`RETRIEVAL_LLM_RERANK_POLICY` 是授权后的逐查询路由，不能打开一个未授权模型。
专用 reranker 先执行，chat LLM 后执行；两者只重排，不删除其输入候选。
`RERANK_PROVIDER=api` 和 chat LLM 会外发请求/候选源码，`local` 在进程内运行。

两者共用 `plan_rerank`，当前只读 intent、候选数、exact 命中、SQL path 命中、dense 是否
已被结构证据跳过，以及授权和 policy。候选少于 2 条时不重排。默认 `adaptive` 对有任何
exact 命中的 symbol、SQL path 命中的 path 和已跳过 dense 的结构答案跳过模型；无对应命中
的 symbol/path 允许两者。这里的 symbol exact 条件比 dense 的决定性条件宽：可能只命中参数
类型，不能把 `skip:exact_definition` 解读为首个请求符号已经找到。reference 保留专用重排、
跳过 chat LLM 以保留发生点覆盖；其他意图允许两者。`always` 在候选足够时运行对应已授权
模型，但仍恢复结构头部。

`exact_definitions`、`definition_sites` 和 `head_slots` 都是审计字段，不是当前 policy 输入；
任何原始或融合分数也不用于估计是否重排。

## 选择、扩展与字符预算

symbol/path 使用 focused，保留相关性顺序、默认每文件最多 4 个主片段、代码预算 12,000 字符。
其他意图使用 coverage，先覆盖不同文件再填第二个片段、默认每文件最多 2 个、预算 32,000 字符。
两者去重并抑制高度重叠的源码区间，默认选择最多 10 个主片段；关系小节另计条数。
关闭 coverage selector 时使用 Top-K，也遵守当前意图的代码预算。

硬上限按 `sum(len(hit.content))` 计算，含 primary 和 relation 的正文及合并后正文中的换行。
它不是 token、UTF-8 字节或整个 `formatted_retrieval` 的长度：formatter 添加的标题、Path、
Lines、Context、Hop、行号与分节分隔符不计入代码字符预算，所以 HTTP 文本可以更长。
首个主片段超限时截成摘录，优先保留完整源码行；单行过长时截该行并同步 end_line。后续
片段只有整体放得下才选入。相邻合并增加换行后若超限，保留合并前的片段。

`evidence_pack` 按意图与各车道开关组装相关定义、调用方、实现/子类、测试和 re-export，
去重并赋 role。feature 未点名符号时，从主结果已声明的名字寻找关系。被调用的名字优先于
一般正文提及；symbol/reference 的具名关系车道先填，普通定义补充在后；限定名始终钉住
作用域，不能把 `Flask.make_response` 的关系接到 `helpers.make_response`。

普通关系总上限默认 6,000 字符，还受活跃车道上限之和及上下文规模限制。主预算不预扣，
只有新关系证据出现时才从最低优先级的主结果尾部腾出空间，保留领头答案；裁尾后重新计算
关系的来源、顺序、去重和预算，已被裁掉的来源不能继续贡献证据。总上限为 0 时不查普通
关系 SQL，也不输出普通关系小节。

call_chain 的 `chain` 小节有独立 3,600 字符上限，同时受请求剩余总预算限制。两端点链沿
已记录 call 边做有界 BFS，默认最深 4 跳，只跟随同名声明不超过 2 处的名字；同文件声明
优先。每跳先放声明头部，交接调用离头部较远时补止于调用行的窗口；单端追踪取两层被调用者。
限定端点先以 enclosing 约束 SQL，再检查歧义，不能用全 scope 的同名数量误拒绝已钉住端点。

## 失败与审计

召回、头部证据和关系车道被管线吸收的异常都经 `lane_failed` 记录异常类型到
`retrieval_metrics.lane_failures`，同时写日志。存储层不把超时改成空结果，空结果只表示无匹配。
其余车道有答案时可以降级返回；dense 失败且最终没有任何候选时仍抛出错误，plan 失败或
调用者取消也不被伪装成成功。rewrite 自身失败退回原文，这是模型客户端的容错，不能把它当作
一次完整改写实验。HTTP 200 且 `lane_failures` 非空只证明降级作答，评测必须单独标出，
不能把差异直接归因于排序调整。

| 字段 | 当前含义 |
| --- | --- |
| `dense_route` | `dense`、结构跳过原因或 dense 异常类型 |
| `rerank_route` | 计划运行 `dedicated`、`llm` 或级联；无模型时为 `skip:<reason>` |
| `head_slots` | rank 计算的受保护 `structural_heads` 数量；不统计所有源码偏好，也不保证这些片段最后都被选入 |
| `exact_definitions` | 请求不同叶子名在 ready scope 内已记录的声明处数之和 |
| `definition_sites` | 上述各名计数的最大值；多个名字各一处不会被误写成一个名字多处 |
| `relation_hits`、`relation_chars` | 最终附加关系摘录数量及正文字符数；审计 collector 另按 role 计数 |
| `stages`、`lane_failures` | 分阶段耗时（同名累加）及被吸收的车道异常类型 |

声明计数只对带 audit 的 exact 请求执行一次按名称范围的 SQL 查询，不受召回 chunk 去重和
截断影响；限定名审计仍按叶子名在整个 ready scope 中计数。计数查询失败只标记
`definition_counts` 车道失败，保留已经取得的 exact 答案。当前 symbol projection 会合并
同 chunk、同 enclosing、同名的记录，所以指标描述的是已记录位点，不能宣称完整源码声明数。
旧版本两个字段统计召回 chunk 数，不能与修复后的记录混合做歧义校准。

## 实验管理与保留的决策

模型能力授权与消融实验分开处理：默认关闭的 reranker 授权控制部署成本和数据外发，不能
因一个模型配置的负结果删除通用 API/local 能力；具体模型、路由或召回变体仍须按配对结果判断。
默认启用能力的关闭分支只有在生产配置或评测消融中有用途才保留。

实验退休规则：

1. 默认关闭的实验自加入起经过两轮配对评测仍未成为默认值，删除开关及其代码路径，保留结果。
2. 已判定净负的变体立即删除。
3. 待校准实验必须有独立真值的标签计划，不能用服务端路由或返回结果反向生成答案。
4. 已成为默认值的消融开关，其关闭分支不再用于生产或评测时，删除开关并保留默认行为。

当前待评估的 query rewrite 默认关闭。标签计划是从黑盒查询与真实项目用例中分别整理明确文件
定位、跨语言功能描述及普通查询，人工按仓库源码确认目标路径/区间；既有开发集用于第一轮，
另留未参与调参的项目或查询用于第二轮。两轮均配对关闭/开启 rewrite，固定客户端、模型、
索引与其他配置，记录改写失败、各类别召回/排序、字符数和延迟；若目标类别没有稳定改善，
或其他套件超过容忍回退，两轮后移除。当前文档不把该计划写成已完成的评测证据。

| 已退休开关 | 原因 |
| --- | --- |
| `RETRIEVAL_HUB_HEAD_SLOTS` / `RETRIEVAL_HUB_MAX_DEFINITIONS` | 两轮未成为默认值，精选集正向、held-out 负向 |
| `RETRIEVAL_HUB_FEATURE_ENABLED` | feature 变体净负 |
| `RETRIEVAL_RERANK_AMBIGUOUS_DEFINITIONS` | 没有同名定义离线标签计划 |
| `RETRIEVAL_CONFIDENCE_FLOOR` | 默认恒等；非零分支用混合标尺分数过滤 |
| `RETRIEVAL_HEAD_SKIPS_IMPORT_HEADERS` | 已成为默认行为，关闭分支退休 |
| `RETRIEVAL_REFERENCE_HEAD_FALLBACK` | 默认开启，关闭分支退休 |

保留的历史结果（完整记录见 `benchmarks/results/`）：

- hub 召回按请求词拼已声明名字，再按被引用文件数排序。精选 overview nDCG@10 67.8→74.3，
  held-out overview 66.4→54.1、call-chain 85.6→78.2；扩展到 feature 的变体使 feature
  nDCG@10 73.5→68.8、CSN Region Top-1 62.5→60.0。代码已删除，不能拿精选收益推断默认效用。
  见 [round 3 的 hub 消融](../benchmarks/results/utility-round3-2026-09-09.md#hub-lane-retrieval_hub_head_slots2-cycles-r3br3d)。
- 2026-09-08 的 import-only 头部让位早期观察曾消除唯一头部干扰项；后续 round 3 没有复现
  这项单轮降幅，不能承诺当前 distractor_head 为零。默认保留该规则，退休关闭分支。
- 本地 jina 交叉编码器在当时结构头部之上复测为负：semantic nDCG@10 74.9→72.9、issue
  nDCG@100 74.3→62.0，每个向量请求增加约 1.2 s。它支持默认不启用该配置的决策，不代表
  其他模型或部署环境必然相同。见 [round 3 的本地模型消融](../benchmarks/results/utility-round3-2026-09-09.md)。
- 2026-10-01 统一 exact RRF 的变体使用同款 Qwen3-Embedding-4B 和同一物理索引，基线复跑
  逐条一致。纯名次融合把 exact-only 使用点挤出 50 条窗口：short reference Top-1
  92.5→88.8%、project test_mapping Hit@3 85.7→57.1%、distractor_head 0→2.9%。补上
  结构列表免截断后仍没有套件改善（project Hit@3 −1 例、semantic weighted R@5 −1 点），
  因而保留当前按意图合并。
- 同日删除符号文件命名、声明包邻近、测试声明名平局规则，回退了 pytest `parser`
  reference、gin/rtk test_mapping 和 gson multi_impl。规则已恢复；它们在缺少完整类型与
  名称解析时提供结构近似，后续替换仍需独立真值和配对证据。两项否决的完整配对协议与限制见
  [2026-10-01 评测](../benchmarks/results/principled-refactor-2026-10-01.md)。

## 变更验证

纯结构重构用 `benchmarks.internal.retrieval_equivalence`：真实应用 indexing handler 把冻结
语料写进内存 SQLite，保留真实切块、symbol、lexical 和 path lookup，以确定性词频向量代替
embedding，固定查询在五个配置档中运行，比较结果摘录与审计字段。

```bash
corpus_dir=$(mktemp -d)
git archive HEAD src/oce | tar -x -C "$corpus_dir"
uv run python -m benchmarks.internal.retrieval_equivalence dump --corpus "$corpus_dir/src/oce" --out before.json
# 修改实现，继续使用同一份 corpus。
uv run python -m benchmarks.internal.retrieval_equivalence dump --corpus "$corpus_dir/src/oce" --out after.json
uv run python -m benchmarks.internal.retrieval_equivalence compare before.json after.json
```

语料、查询和配置须同时冻结；仅对纯结构重构要求 dump 逐条一致。预算或审计语义修正应逐条
解释预期差异，不能称为完整 dump 等价。此工具证明函数行为保持，不证明模型效用；默认行为
变更仍按 AGENTS.md 要求配对运行四套黑盒护栏，关系改动以真实项目关系用例为主裁判。
`tests/unit/infrastructure/test_retrieval_regression.py` 固定头部顺序与关系小节的离线回归，
也不能替代发布客户端驱动的产品评测。

2026-10-01 可靠性修正的预期预算/审计差异、四套黑盒护栏和验证边界见
[对应记录](../benchmarks/results/reliability-simplification-2026-10-01.md)。
