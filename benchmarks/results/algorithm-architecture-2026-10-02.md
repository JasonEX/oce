# 算法与架构改进验证（2026-10-02）

本轮改动以源码语义、阶段契约和资源所有权为依据。没有修改评测查询或 truth，也没有按
仓库名、case ID 调规则、权重或阈值。代码质量门禁、冻结回放和两轮四套黑盒配对已完成。
跨新索引的结果存在质量取舍，不能仅以总体排序分数提高宣称通过所有质量护栏。两轮同一
兼容索引的流程消融确认查询编排保持语义/SWE 指标，并在一个调用链 case 上改善关系
召回。本轮保留四项契约/正确性改动，不宣称重新索引后的全类别效用已提高。

## 改动与边界

1. **共享主要符号的命中事实。** dense 跳过、adaptive rerank 和 symbol 头部均读取
   `primary_definition_found`。只命中参数类型时仍允许语义补召回及已授权的重排，参数
   类型声明不占请求目标的保护槽位。没有把召回分数当作置信度。
2. **明确交接受保护的结构候选。** rank 把有界 `structural_heads` 交给 select；选择先
   按候选顺序接纳这些头部，再填文件覆盖。同文件的两个调用链端点不再因覆盖其他文件
   被延后，数量、每文件、重叠与字符预算仍然生效。源码偏好不因此成为保护头部。此次
   没有引入完整的证据覆盖优化器：现有融合结果没有保留足够的逐 facet 归属信息。
3. **结构查询先判断 SQL 答案。** 启用已有决定性跳过、scope 非空且 SQL operator 可用
   时，symbol 和显式 path 命中不创建 query embedding task；未命中或失败才启动原有
   模型车道。reference 和语义请求继续并行。已发送的 embedding 仍只释放等待者，不
   因提前答案或调用方取消而取消 provider。代价是结构未命中的请求先支付 SQL 耗时。
4. **补保守的 Python 使用点证据。** 唯一模块级具名导入别名可补源名称 call 行；保留
   调用处拼写，成员调用不继承裸名别名。参数、赋值、类型参数、类型别名、导入等冲突，
   通配导入或不完整语法会放弃推断；导入必须在调用前。常见名称的新增调用记录仅在
   Python 同文件声明和绑定检查通过时接纳。任意对象接收者及其他语言沿用原规则。

符号证据仍是名称使用点，不是唯一声明身份或运行时派发目标。scope、同名声明歧义和关系
预算不变。分析局限于单文件，不增加长生命周期状态；沿用 512 KiB tree-sitter 输入边界，
调用记录及待判断候选分别受 3,000 上限约束。

符号分析增加 CPU 工作。在相同 182 个 Python 文件（843,417 字节）、预热 grammar、文件
预先读入后各抽取 5 次的组件诊断中，中位耗时 **0.361→0.469 秒**，约增加 **30%**，平均
每文件增加约 **0.60 ms**。definition/import 等计数不变，call 计数 3972→3973。这是主机
背景评测仍在运行时的组件观察，包含 AST parsing，不是完整索引或检索的性能结论；不应把
省 query embedding 调用解释成索引 CPU 也降低。原始记录见
[符号分析成本](algorithm-architecture-2026-10-02-data/symbol-cost.json)。

## 索引兼容性

`SYMBOL_EXTRACTION_VERSION` 从 5 升到 **6**。旧索引启动被拒绝，已存 profile 不被覆盖，
上传/checkpoint 的 readiness 检查仍然关闭入口。需要新数据目录和完整客户端重同步；本轮
没有 schema 迁移，也没有修改既有用户索引。chunk、embedding 输入和 dense/path 语义不变。

## 代码验证

- 实际根环境为 Python **3.13.5**：108 个测试文件分别运行，**1027 passed、1 skipped**；
  真实 Container、SQLite 迁移、Milvus Lite、HTTP 上传/checkpoint/检索 smoke 的 5 项包含
  在该总数内。唯一跳过为未配置 PostgreSQL 的模型建表测试，其 SQLite 对应测试通过。
- Milvus Lite 集成：**6 passed**。Ruff check/format、mypy（165 个源文件）和 diff 检查通过。
- 隔离 uv 环境中的 Python **3.11.14**、**3.14.7** 各运行 8 个相关测试文件，均
  **150 passed**；根 `.venv` 未切换。这是针对性兼容验证，不是三版本全量 CI。
- sdist/wheel 构建通过。wheel 的 183 个 Python 模块与候选源码逐文件一致；抽取后的
  wheel 实际执行导入别名 AST 抽取，确认新增模块和版本 6 被正确打包。
- 同一冻结 183 文件语料、43 个查询、5 种配置的 **215/215** 记录逐条一致，包括所有
  返回片段和审计字段。它证明这个回放范围内的保持，不证明新增场景无变化或产品效用提高。

针对性回归覆盖 SQL 未命中/异常/取消与 provider 释放，参数类型误命中的重排，受限预算下
同文件端点选择，别名/接收者遮蔽与错误 AST，实际 SQL reference/call 关系，以及旧 profile
的 fail-closed 行为。没有放宽既有护栏；慢 embedding 测试的期望改为结构命中根本不发送，
另有并行 reference 测试保留“已发送请求不取消”的原约束。

## 黑盒验证协议

四套既有护栏使用冻结的查询、truth、快照与同一个发布客户端，经正常准入、上传、
checkpoint 和稳定 HTTP API 执行。两级 reranker、rewrite 和 query cache 关闭；SQLite、
Milvus Lite、默认 HNSW、worker 关闭；embedding 为 `Qwen3-Embedding-4B`（1024 维）。
这验证此配置下的检索行为，不验证真实 opt-in reranker 的模型质量。

客户端为发布版 `oce-client 0.2.0`，SHA-256：
`ec7983848c3515e30774517f94c629289504cca43f974113a18b6b247545eb61`。
两个来源使用相同冻结黑盒 harness；基线归档为了 harness provenance 建立的本地 Git
snapshot `f4a2c50e7f9b66e432c11696617a8438e9513802` 不是项目提交，实际生产身份以
`082ad06` 和逐文件源摘要为准。

基线与候选各使用全新独立数据目录、客户端状态和实际索引；抽取版本不同，不能绕过
compatibility guard 共用旧索引。每个来源在自己的同一物理索引上复跑四套查询，检查结果
稳定性。独立 HNSW 建图可能产生差异，即使重复稳定，也不能把跨索引变化全部归因于
本轮代码。依现有指标向量审阅 quality、干扰项、字符数与 p50；顺序运行不证明延迟提高。

通过 `/admin/stats` 在各 suite 前后记录模型使用增量，摄入与查询分开。它是已观察调用
记录；usage 缺失或监控失败可以跳过采集，token 数不完整，不代表 provider 账单。已发出
但提前释放的 embedding 可能跨 suite 完成，单套增量是观察窗口计数，不能视为该套拥有的
请求数。例如基线复跑的 short 窗口为 237，随后 semantic 的外围窗口为 42（harness 查询
窗口为 39）；两套合计仍为 279，另 3 次完成跨越了边界。成本对比同时查看连续四套的总量，
不把这 3 次 spillover 当作 semantic 模型调度变化。

## 独立新索引的配对结果

每个来源每轮共 327 个 case（240 short、39 semantic、35 project、13 SWE development），
两轮各 654 次成功请求，没有查询 HTTP 错误。候选的两轮返回区域、指标和字符数
327/327 一致；基线为 326/327，差异在一个 Python overview 请求。每轮评分前正常客户端
同步上传数均为 0。摄入最终两边均为 9,676 个 ready blob，pending/error/staging 为 0。

| 指标 | 基线两轮 | 候选两轮 |
| --- | ---: | ---: |
| short Top-1 / MRR | 1.000 / 1.000 | 1.000 / 1.000 |
| semantic nDCG@10 | 0.73536 / 0.72703 | 0.73958 / 0.73958 |
| semantic relevant Top-1 | 0.87179 | 0.84615 |
| project primary Hit@3 | 0.97143 | 0.97143 |
| project relation recall | 0.93381 | 0.95286 |
| project test recall / distractor head | 1.000 / 0.000 | 1.000 / 0.000 |
| SWE core Top-1 | 0.69231 | 0.76923 |
| SWE nDCG@100 | 0.46779 | 0.62358 |
| SWE edit file / region recall@10 | 0.84615 / 0.44872 | 0.73077 / 0.33333 |
| SWE core file / region recall@10 | 0.69103 / 0.58846 | 0.65897 / 0.55641 |
| 连续四套观察到的 embedding calls | 376 / 376 | 206 / 206 |

调用减少 170 次，约 **45.2%**；这支持观察到的 query 调用成本降低，不证明总体质量
提高或真实 provider 账单降低。short 符号与路径各 80 个 case 可不发送 query embedding，
reference 仍保留模型车道。semantic 模型调用不变。

semantic 必须拆开看：feature 的 nDCG@10 **0.74869→0.72815**，overview
**0.65003→0.65404**，call-chain **0.80736→0.83655**。各语言第一轮结果如下；每个
非 Python 语言只有 3 个 case，样本较小，不能扩大为语言整体能力结论。

| 语言 | case 数 | 基线 nDCG@10 | 候选 nDCG@10 |
| --- | ---: | ---: | ---: |
| Python | 15 | 0.79783 | 0.79551 |
| TypeScript | 3 | 0.80676 | 0.80676 |
| JavaScript | 3 | 0.89418 | 0.89418 |
| Rust | 3 | 0.60819 | 0.65091 |
| Go | 3 | 0.65518 | 0.53161 |
| C | 3 | 0.71949 | 0.75344 |
| C# | 3 | 0.51541 | 0.55041 |
| Java | 3 | 0.68896 | 0.76735 |
| Bash | 3 | 0.68235 | 0.68235 |

Go、feature 与 SWE edit recall 的退步不能被总体 nDCG 提高抵消。此独立建图对比本身
**没有通过“所有分类保持”的结论**。返回顺序跨来源改变的 case 数分别为 short 11/240、
semantic 23/39、project 7/35、SWE 12/13；头部指标相同不等于所有返回内容相同。

| 套件 | 基线 p50 ms（两轮） | 候选 p50 ms（两轮） | 基线平均字符（两轮） | 候选平均字符 |
| --- | ---: | ---: | ---: | ---: |
| short | 37 / 34 | 61 / 36 | 11021 / 11021 | 10976 |
| semantic | 390 / 380 | 437 / 388 | 30539 / 30364 | 30470 |
| project | 58 / 58 | 86 / 65 | 19113 / 19113 | 19085 |
| SWE development | 1164 / 1138 | 1325 / 1270 | 24765 / 24765 | 25108 |

没有观察到一致的 p50 改善。结构查询未命中增加 SQL 前置等待，不能把省调用解释成
所有请求更快。候选摄入有一次外部 embedding 连接错误，返回 HTTP 500；保留原兼容新
索引，经正常 find-missing/上传重试恢复。没有绕过 profile、替代向量或改写 ready 状态。
该中断也可能影响独立建图轨迹，已单独记录，未混入查询错误计数。

原始 16 份黑盒报告以 gzip JSON 归档；[汇总](algorithm-architecture-2026-10-02-data/blackbox-summary.json)
保留分组指标和重复稳定性，完整逐 case 前后变化在同名压缩汇总中。

## 同一兼容索引的查询流程消融

在临时候选源码副本中，仅把 `retrieval/`、`selector/` 和 `retrieval_strategy.py` 换回
`082ad06`；实际有 9 个文件改变，逐文件与基线一致。符号抽取器、版本 6 profile、
infrastructure、container 和其他模块保持候选原样。该诊断来源记为
`legacy_retrieval_on_symbols6`，不是“版本 5 服务启动在版本 6 索引上”。

两个查询流程使用同一物理 candidate HNSW/vector 数据和同一版本 6 符号事实；profile
始终 compatible，ready blob/chunk/link/symbol/lexical 与 dense/path 数量在每轮前后不变。
没有重索引或覆盖持久 profile。发布客户端和冻结 harness、truth、case 顺序沿用原配对。
旧流程另跑两轮，共 654 次成功评分请求，正常同步上传数均为 0；两轮 327/327 的返回
区域、指标和字符数一致。

新客户端通过正常 HTTP 同步产生 checkpoint，chains 从 49 到 98、成员关系从 31,827
到 63,654；第二轮保持不变。这不是物化源码索引变化。独立核对 49 组客户端清单的
有序 path/blob/committed/status 摘要全部一致；每个状态已有 checkpoint，没有待提交或
删除 delta。发布版客户端的请求构造与服务端 scope 路径确认，这些清单推得同一 ready
源码范围、空 added/deleted，并不让其他 chain 参与查询或关系证据。这里是输入与源码
契约核对，没有声称抓取了线上 HTTP payload。详细证据见
[checkpoint scope 核对](algorithm-architecture-2026-10-02-data/ablation/checkpoint-scope-control.json)。

| 同索引对比：旧查询流程 → 候选查询流程 | 两轮结果 |
| --- | --- |
| short | 240/240 的区域、指标和字符数完全相同 |
| semantic | 39/39 的质量指标相同；38/39 的区域和字符数相同 |
| SWE development | 13/13 的区域、全部质量指标和字符数完全相同 |
| project | 34/35 的区域、指标和字符数相同；primary Hit@3/test recall/distractor head 保持 |
| project relation recall | 0.94333 → 0.95286 |
| 连续四套观察到的 embedding calls | 376 → 206，每轮相同 |

唯一 semantic 区域变化是 Flask WSGI 调用链：保护同文件头部后减少 321 个字符，质量
指标相同。唯一 project 变化是 Gin 的 ServeHTTP 调用链：接纳受保护的实现端点，替换
原覆盖阶段选中的测试片段，primary recall 从 0.5 到 1、relation/hop recall 从 2/3 到 1、
chain_closed 从 0 到 1。两者都由通用头部契约解释；没有增加 Flask/Gin 专用规则。

这个对照把查询流程**整组**改动与固定物化索引分开，没有逐项隔离 selector、门控与
模型调度。它也没有把版本 6 的符号事实与独立向量生成/HNSW 建图分开。因此，跨新索引
观察到的 Go/feature/SWE recall 退步仍保留，成因不能进一步独占归于某一组件；同索引
实验排除了新查询流程作为这些已观察退步的原因，范围仅限本候选索引和已公开开发用例。
opt-in reranker 的实际模型质量与下游 agent 任务成功率均不在此次结论中。

保留四项改动的依据是明确的源码/阶段正确性契约、失效与取消边界测试、冻结回放保持，
以及同索引两轮查询流程护栏。本轮没有新增实验开关、调召回权重、修改字符预算或更新
评测数据。符号版本升级后的广泛效用仍受独立重建与语义抽取的混合影响，不能用组件
正确性测试或总体 nDCG 代替这个尚未证明的结论。

消融的 [汇总](algorithm-architecture-2026-10-02-data/ablation/ablation-summary.json)、8 份
原始压缩报告、4 份官方比较表、运行 provenance 和完整源码清单也归档。

## 源码身份与证据

基线是项目 `082ad06`；候选为其上的未提交工作树。生产 Python 文件摘要如下，构建生成的
忽略项 `egg-info` 不属于此源码身份：

| 来源 | Python 文件数 | SHA-256 |
| --- | ---: | --- |
| 基线 | 182 | `2d35242eceba04f057d5ed6f2116f8af848950daf9f9dbeb207d887919224b76` |
| 候选 | 183 | `cbcf1763cde9177e65e0574c2cd63491c7fdae8bc3015ba40efeb1b679bab046` |
| 同索引旧查询流程消融 | 183 | `cb7a532a6b200f2c49b058be76caf6f8d5bc514bd153dfd50de618cd42264a2e` |

[质量门禁](algorithm-architecture-2026-10-02-data/quality-summary.json)、
[冻结回放摘要](algorithm-architecture-2026-10-02-data/frozen-summary.json)、
[基线源码逐文件摘要](algorithm-architecture-2026-10-02-data/baseline-source.json)、
[候选源码逐文件摘要](algorithm-architecture-2026-10-02-data/candidate-source.json) 已归档。
冻结回放的前后 dump 也保存在同目录。源码摘要是有序 `src/oce/**/*.py` 相对路径与
逐文件 SHA-256 清单的紧凑、键排序 JSON 的 SHA-256；忽略生成元数据和 pycache。
最终工作树的 183 个生产模块与评分候选逐文件一致，评分后仅补测试注解和文档/证据。
全部归档文件的 [SHA-256 清单](algorithm-architecture-2026-10-02-data/sha256.json) 可用于核验。
