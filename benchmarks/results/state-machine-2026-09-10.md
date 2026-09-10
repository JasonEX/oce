# 检索状态机整理与开发评测（2026-09-10）

本报告记录 `candidate-1/2` 快照。[后续关系证据修复](state-machine-followup-2026-09-10.md)记录当前实现、重复测量及对文件名特判回退的处理；以下保留原测量与当时判断。

本轮完成了请求实体与裁剪权限分离、限定名与调用链端点修复、失败实验分支清退、排序及关系预算职责拆分。问法与布局对照显著改善，短查询与项目用例头部护栏保住，但**当前实现没有通过全部默认检索效用容忍线**：主语义集 nDCG@10 下降约 1.42 个百分点，历史 heldout 语义集下降约 1.62 个百分点。两者都超过现行 1 个百分点语义容忍线。835 个单元测试通过不能替代这个产品质量判断。

这是一份开发变更评估，测量结束时尚未提交、打标签或发布。报告不把检索收益表述为下游 agent 成功率或 ACE 对比结论。

## 变更与取舍

- `application/retrieval.py` 协调固定阶段；领域层保留纯名字解析、排序、摘录和预算策略。调用链遍历接收明确的 scope、端点和边界，不共享整个可变 `RetrievalState`。必需 Protocol 方法直接调用，fake 同步补全。
- 完整名字的抽取不依赖反引号；提及实体与请求目标分开。参数类型、第三个中间名字、中文单字和说明性前言不再直接决定聚焦预算或链端点。无已识别目标的 reference 使用覆盖预算。
- reference 保留 dense，并要求返回候选带 SQL occurrence 或完整名字及作用域证据。只有所有请求定义分别解析成功或有可取内容的 SQL 路径证据时，才能释放等待 dense；同叶子的多个限定作用域分别核验。scope 和 occurrence 仍是有界静态证据，不是类型解析后的调用图。
- 删除 `index.ts`、`types.ts`、`__init__.py` 等普通源码文件名的额外降权；保护已验证定义、路径、链端点、traceback 和明确请求的使用/测试/覆写证据。先验只执行一次，结构头部键计算一次，普通源码头部不再覆盖模型的最终语义顺序。
- 通用源码头部与乘性先验分别消融；现有诊断不支持删除，保留默认 3 个槽位及一次先验。没有恢复文件名惩罚或加入针对某个仓库的包装函数规则。
- 删除 hub、未校准的 ambiguous-definition 分支和混用分数标尺的 `confidence_floor`。保留 `vector_threshold`（0 仍过滤负分）、多跳 callers、两个可选 reranker、query rewrite、审计迁移及 CQRS。
- 关系事实有界获取一次；只有新关系证据才裁剪主结果尾部，裁剪后在内存中重算摘录、去重和来源。被移除主片段独有的名字不继续扩展，避免二次查询和无效预算损耗。

## 配对设计与证据身份

基线是 `ac5c9e1a30f10fe07c52abe78c80d4aa64af14fc`。最终候选使用独立冻结源码快照，保留每个生产文件的 SHA-256；不能仅靠同一个 Git HEAD 标识未提交候选。冻结 evaluator 驱动两种服务源码，使用发布版 `oce-client 0.2.0` 与稳定 HTTP API。黑盒测量未 import `oce`、读取数据库或复制路由实现。

最终顺序为 `baseline-4 → candidate-1 → candidate-2 → baseline-5`；同一个已填充物理索引顺序启动服务，没有并发打开 Milvus Lite 数据目录。使用 Qwen3-Embedding-4B / 1024 维，两个 reranker、query rewrite 和 query vector cache 关闭。服务报告 dense 46,224 / path 12,965 条，索引 fingerprint 为 `2c7e083e241a59b6c3546a5eb7d802daf936838fb3aac069e743062a420eb6a8`。客户端 SHA-256 为 `ae27c49198e946730f7eccd5596b814f412cd0aa89b823ebb928561872b085fc`。

最终四轮均核验相同的 truth digest、schema、按序 case IDs、客户端、索引 profile、索引条数、evaluator 文件及运行开关；每轮结束核验源码与 truth 未变。主文使用 baseline-4 / candidate-1 的效用数值，重复差异另列，p50 使用两轮范围。


生产源码清单 SHA-256：`c593ffb8533b6405782168ef1de784f6f2c6d675c3de1df4f8caa0501cead08d`。truth 清单 SHA-256：`f03a78b0cbc2cc755af3f04a832a2b5cfd3f8ebe303cf166911c564773ea6fb6`。evaluator 清单 SHA-256：`35ec0b7be1ace9e5cae0db66ab82a9905a35d557f17910c58a790225fc8e3867`。

## 必需四套与外部护栏

下表的百分点差值为候选值减去基线值，不是相对变化百分比；nDCG 和 MRR 也按百分制展示，各指标不合成总分。

| 指标 | 基线 | 候选 | 百分点差 |
| --- | --- | --- | --- |
| Short Top-1 | 100.00% | 100.00% | +0.00 |
| Short MRR | 100.00% | 100.00% | +0.00 |
| Project primary Hit@3 | 97.14% | 97.14% | +0.00 |
| Project primary Top-1 | 88.57% | 94.29% | +5.71 |
| Project relation recall | 94.33% | 93.76% | -0.57 |
| Project supporting recall | 93.81% | 90.95% | -2.86 |
| Project test recall | 100.00% | 100.00% | +0.00 |
| Project distractor head ↓ | 0.00% | 0.00% | +0.00 |
| Semantic nDCG@10 | 74.73% | 73.31% | -1.42 |
| Semantic primary Top-1 | 74.36% | 71.79% | -2.56 |
| Semantic relevant Top-1 | 82.05% | 82.05% | +0.00 |
| SWE development core Top-1 | 76.92% | 84.62% | +7.69 |
| SWE development edit Top-1 | 46.15% | 53.85% | +7.69 |
| SWE development nDCG@100 | 62.36% | 77.88% | +15.52 |
| SWE development core region recall@10 | 59.49% | 56.03% | -3.46 |
| SWE development weighted core coverage | 15.18% | 11.75% | -3.43 |
| CSN region Top-1 | 63.75% | 63.75% | +0.00 |
| CSN region Hit@10 | 83.75% | 86.25% | +2.50 |
| CSN region MRR | 72.86% | 73.50% | +0.63 |
| Upstream project Hit@3 | 83.33% | 83.33% | +0.00 |
| Upstream project relation recall | 81.94% | 83.33% | +1.39 |
| Upstream semantic nDCG@10 | 32.29% | 32.29% | +0.00 |
| Historical heldout project Hit@3 | 95.83% | 95.83% | +0.00 |
| Historical heldout project relation recall | 97.57% | 97.57% | +0.00 |
| Historical heldout distractor head ↓ | 0.00% | 0.00% | +0.00 |
| Historical heldout semantic nDCG@10 | 74.63% | 73.01% | -1.62 |

短查询 240 条来自 40 个锚点，不是 240 个独立需求。SWE 只运行 development 的 13 个 issue；其 nDCG 和 Top-1 提升伴随区域覆盖下降、返回字符增多，不能据此声称整体覆盖改善。主项目 supporting recall 下降约 2.86 点，relation recall 下降约 0.57 点；未超过当前约一个 case 的容忍量，但保留为未解决损失。

### 语义集按意图与代码语言

39 条主语义查询，13 个固定快照；nDCG 按原始 0–1 比值显示。

| 意图 | N | 基线 nDCG | 候选 nDCG | 百分点差 | 平均字符 |
| --- | --- | --- | --- | --- | --- |
| feature | 13 | 0.7508 | 0.7184 | -3.23 | 31792 → 31462 |
| overview | 13 | 0.6703 | 0.6464 | -2.39 | 29328 → 29870 |
| call_chain | 13 | 0.8209 | 0.8345 | +1.36 | 29053 → 29830 |

| 语言 | N | 基线 nDCG | 候选 nDCG | 百分点差 | 平均字符 |
| --- | --- | --- | --- | --- | --- |
| python | 15 | 0.8257 | 0.7900 | -3.57 | 33992 → 33974 |
| typescript | 3 | 0.8089 | 0.7679 | -4.09 | 26093 → 27292 |
| javascript | 3 | 0.8942 | 0.9065 | +1.23 | 24432 → 25786 |
| rust | 3 | 0.6185 | 0.6185 | +0.00 | 26506 → 25960 |
| go | 3 | 0.6151 | 0.6151 | +0.00 | 21950 → 21950 |
| c | 3 | 0.7189 | 0.7189 | +0.00 | 31413 → 31413 |
| csharp | 3 | 0.5377 | 0.5636 | +2.60 | 20160 → 20219 |
| java | 3 | 0.7113 | 0.7344 | +2.31 | 31083 → 33442 |
| bash | 3 | 0.6824 | 0.6557 | -2.66 | 39152 → 39106 |

历史 heldout 语义集的 18 条查询已经参与本轮错误分析，现作为开发回归集使用；文件名及原标签不变，不再是未触碰的验证集。

| 意图 | N | 基线 nDCG | 候选 nDCG | 百分点差 | 平均字符 |
| --- | --- | --- | --- | --- | --- |
| feature | 6 | 0.6405 | 0.6405 | +0.00 | 24261 → 24261 |
| overview | 4 | 0.6660 | 0.6660 | +0.00 | 24485 → 24496 |
| call_chain | 4 | 0.8365 | 0.8365 | +0.00 | 27930 → 27650 |
| issue | 4 | 0.8951 | 0.8222 | -7.29 | 26499 → 22925 |

| 语言 | N | 基线 nDCG | 候选 nDCG | 百分点差 | 平均字符 |
| --- | --- | --- | --- | --- | --- |
| python | 5 | 0.9155 | 0.9174 | +0.19 | 26790 → 24448 |
| javascript | 5 | 0.5379 | 0.4798 | -5.81 | 24670 → 24293 |
| go | 4 | 0.7509 | 0.7509 | +0.00 | 21925 → 22544 |
| java | 4 | 0.7908 | 0.7883 | -0.26 | 29056 → 27992 |

## 问法鲁棒性：按需求、问法、仓库分别报告

`query_variants.json` 在最初基线检索前编写：12 个既有需求 × 6 种问法，共 72 条，覆盖 7 个仓库快照。标注区域直接继承原 project cases，未根据结果修改；这些是助理编写的开发对照，不是独立自然用户样本。同一需求所有问法始终在同一组。组均值先组内平均再等权平均；“最弱问法均值”先取每组最低值，再对 12 组等权平均。

| 需求等权指标 | 基线 | 候选 |
| --- | --- | --- |
| Hit@3 | 59.72% | 100.00% |
| 最弱问法 Hit@3 均值 | 25.00% | 100.00% |
| MRR | 54.89% | 100.00% |
| 最弱问法 MRR 均值 | 27.71% | 100.00% |
| Relation recall | 67.13% | 98.33% |
| 最弱问法 relation recall 均值 | 27.22% | 98.33% |

| 问法 | N | 基线 Top-1 | 候选 Top-1 | 基线 Hit@3 | 候选 Hit@3 | 基线 RelR | 候选 RelR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| context | 12 | 41.67% | 100.00% | 66.67% | 100.00% | 53.89% | 98.33% |
| imperative | 12 | 41.67% | 100.00% | 50.00% | 100.00% | 71.11% | 98.33% |
| noun | 12 | 25.00% | 100.00% | 41.67% | 100.00% | 57.78% | 98.33% |
| original | 12 | 91.67% | 100.00% | 100.00% | 100.00% | 100.00% | 98.33% |
| unquoted | 12 | 50.00% | 100.00% | 58.33% | 100.00% | 63.33% | 98.33% |
| zh | 12 | 25.00% | 100.00% | 41.67% | 100.00% | 56.67% | 98.33% |

| 需求组（每组 6 问法） | 基线 Hit@3 | 候选 Hit@3 | 基线 RelR | 候选 RelR |
| --- | --- | --- | --- | --- |
| axum-into-response-status-code | 33.33% | 100.00% | 50.00% | 100.00% |
| flask-send-file-tests | 50.00% | 100.00% | 77.78% | 100.00% |
| gin-should-bind-json-method | 100.00% | 100.00% | 91.67% | 100.00% |
| gson-fromjson-jsonreader-overload | 16.67% | 100.00% | 16.67% | 100.00% |
| gson-type-adapter-factory-impls | 83.33% | 100.00% | 40.00% | 80.00% |
| pytest-get-unpacked-marks-uses | 100.00% | 100.00% | 91.67% | 100.00% |
| requests-extract-cookies-callers | 33.33% | 100.00% | 90.00% | 100.00% |
| requests-get-to-adapter-send | 50.00% | 100.00% | 53.33% | 100.00% |
| requests-merge-setting-uses | 100.00% | 100.00% | 91.67% | 100.00% |
| requests-session-get-method | 50.00% | 100.00% | 50.00% | 100.00% |
| rtk-configure-store-tests | 66.67% | 100.00% | 91.67% | 100.00% |
| rtk-create-slice-built-from-factory | 33.33% | 100.00% | 61.11% | 100.00% |

| 仓库快照 | 需求数 | 基线 Hit@3 | 候选 Hit@3 | 基线 RelR | 候选 RelR |
| --- | --- | --- | --- | --- | --- |
| axum-v0.7.9 | 1 | 33.33% | 100.00% | 50.00% | 100.00% |
| gin-v1.10.0 | 1 | 100.00% | 100.00% | 91.67% | 100.00% |
| gson-2.11.0 | 2 | 50.00% | 100.00% | 28.33% | 90.00% |
| pallets__flask-5014 | 1 | 50.00% | 100.00% | 77.78% | 100.00% |
| psf__requests-5414 | 4 | 58.33% | 100.00% | 71.25% | 100.00% |
| pytest-dev__pytest-10356 | 1 | 100.00% | 100.00% | 91.67% | 100.00% |
| redux-toolkit-v2.2.7 | 2 | 50.00% | 100.00% | 76.39% | 100.00% |

候选全部问法 Top-1/Hit@3/MRR 为 1，distractor head 从 18.06% 降至 0。但 original 问法的 relation recall 从 1 降到 0.9833，Gson 实现者需求仍少一个已标注实现；增加变体没有消除这个损失。

## 保持语义的布局对照

`layout_controls` 使用 Python / TypeScript 的相同配置读取任务，调整符号名、源码文件名（含 `__init__.py`、`index.ts`、`types.ts`）和声明前 0/80 行偏移；调用方 import 随文件名同步。20 个布局分别问定义、语义实现、调用方和测试，共 80 条，但只计 4 个需求。完整源码、查询和目标位置进入 fixture digest，目标位置在基线前冻结。

| 需求（各 20 个布局） | 基线 Top-1 | 候选 Top-1 | 基线 Hit@3 | 候选 Hit@3 |
| --- | --- | --- | --- | --- |
| definition | 40.00% | 100.00% | 100.00% | 100.00% |
| reference | 60.00% | 100.00% | 100.00% | 100.00% |
| semantic | 40.00% | 100.00% | 40.00% | 100.00% |
| tests | 0.00% | 100.00% | 40.00% | 100.00% |

总体 Top-1 0.35 → 1，Hit@3 0.70 → 1，MRR 0.5708 → 1。每个需求最弱布局的 Hit@3 再取均值为 0.50 → 1。此合成任务证明所测变化下的不变性，不代表大仓库布局泛化。测试框架机制与寻找测试位置的反例另由路由单元测试约束。

## 消融与中间失败

源码头部和乘性路径先验分开测试。`no-prior` 只在应用乘性先验时换用中性因子，头部资格仍用原始因子，避免把两个机制同时关闭。以下是冻结开发快照上的单轮诊断，不是独立或重复充分的效用资格证明；负向结果足以不采纳进一步删除，不能推断所有配置下的优劣。最终基线与实际采用的候选均进行了两次完整测量。

| 快照/运行 | 变化 | 语义 nDCG | SWE core Top-1 | SWE nDCG100 | SWE core region R | 历史语义 nDCG |
| --- | --- | --- | --- | --- | --- | --- |
| ranked-3 | 早期排序快照，3 槽 | 0.7195 | 76.92% | 0.7019 | 52.18% | 0.7296 |
| ranked-soft | 同一早期快照，0 槽 | 0.7160 | 69.23% | 0.6249 | 52.18% | 0.6746 |
| candidate-1 | 最终候选，3 槽及先验 | 0.7331 | 84.62% | 0.7788 | 56.03% | 0.7301 |
| head-1 | 候选前一快照，1 槽 | 0.7313 | 84.62% | 0.7788 | 56.03% | 0.7019 |
| no-prior | 同一快照，3 槽、无乘性先验 | 0.6535 | 84.62% | 0.7748 | 47.69% | 0.6826 |

除所测开关外，1 槽/无先验快照与最终候选的生产差异仅为无符号 reference 的覆盖预算修复；这里三套查询的该路径不受影响。不同阶段的快照不能用来计算单一机制的因果增益。保留 3 槽是当前开发证据下的保守选择，最初希望撤掉通用源码头部的目标未采纳。

`baseline-1`（缺少 runner 的 httpx 环境）和 `baseline-2`（非 Git evaluator）在设置阶段失败，保留日志，不计为有效基线。`baseline-3` 运行时布局 fixtures 尚未填充，物理索引为 46,178 / 12,919 条；因此最终改用同一已填充索引的 baseline-4/5。baseline-3 → baseline-4 主语义 nDCG 漂移约 +0.078 个百分点，其他主要效用率稳定。`route-1`、`ranked-3`、`final-1`、`final-2` 的误路由、无关使用点、覆写、测试标题和模块作用域问题均保留原结果，未计入最终候选分数。

## 仍未解决的损失与错误分类

以下保持原标注；对原因的解释来自源码和返回区域检查，不把相关性当作完整消融。

| 用例 | 基线 → 候选 | 观察与限制 |
| --- | --- | --- |
| pylint-checker-messages | nDCG 0.9571 → 0.5505 | 注册包装函数排到 message_definition_store/pylinter 前；普通 __init__.py 不再降权后暴露。 |
| pylint-run-overview | nDCG 0.9737 → 0.5753 | run_pylint 入口包装函数排在 Run 等核心实现前。 |
| fastify-head-route-issue | nDCG 0.7872 → 0.4966 | 正文 fastify.get 的 SQL 名字证据提高 fastify.js getter 区域，lib/route.js 的目标由第一位移至后面。 |
| requests-redirect-cookie-tests | relation recall 1 → 0.6667 | 正确测试仍第一；辅助实现区域在关系预算/装配后的尾部缺失，supporting recall 1 → 0。 |
| gson-type-adapter-factory-impls | relation recall 1 → 0.8 | 前列实现不变，少一个已标注实现；自动错误类别仍为 redundant，故不能只看错误类别总数。 |
| gin-servehttp-handler-chain | relation recall 0.6667 → 1 | 上下游交接区域进入结果；收益保留在完整指标向量中。 |

| Project 35 cases | none | exact_miss | distractor | test_missing | relation_missing | redundant |
| --- | --- | --- | --- | --- | --- | --- |
| baseline-4 | 12 | 1 | 0 | 0 | 3 | 19 |
| candidate-1 | 12 | 1 | 0 | 0 | 4 | 18 |

最终仍被标为主要缺失的用例为 `flask-wsgi-dispatch-chain`、`requests-redirect-cookie-tests`、`pytest-parser-reexport`、`pytest-approx-tests`、`xarray-dataarray-to-compatible-data`。除 requests 本轮回退外，其余主要缺失沿用基线。原始逐 case 返回位置、细分 recall 与分类均在原始 JSON 中，不能用 redundant 分类掩盖局部漏召回。

## 字符成本、延迟与重复性

字符为每查询平均值；p50 范围来自两次完整运行。使用同一主机和远端 embedding，但没有隔离网络/主机负载，早期消融还与单元验证重叠，因此不声称延迟因果改善。主项目 reference 不再提前跳过 dense，项目与历史关系集的 p50 增长也必须计入取舍。

| 套件 | 每轮 N | 基线字符 | 候选字符 | 基线 p50 ms | 候选 p50 ms |
| --- | --- | --- | --- | --- | --- |
| project | 35 | 19080 | 18969 | 349–356 | 550–560 |
| short | 240 | 11007 | 10949 | 258–259 | 274 |
| semantic | 39 | 30057 | 30387 | 1349–1357 | 1244–1307 |
| swe | 13 | 25627 | 27249 | 1878–1882 | 1670–1696 |
| csn | 80 | 24701 | 25429 | 1242–1259 | 1209 |
| upproject | 6 | 26328 | 25006 | 1231–1317 | 1186–1219 |
| upsemantic | 6 | 28266 | 29088 | 1277–1382 | 1287–1314 |
| heldout | 24 | 17326 | 16677 | 288–293 | 534–581 |
| heldsem | 18 | 25623 | 24770 | 1294–1309 | 1303–1309 |
| variants | 72 | 18631 | 18676 | 1012–1015 | 479–506 |
| layout | 80 | 1460 | 1132 | 629–648 | 430–436 |

| 套件 | 基线两轮逐 case 效用/区域/字符相同 | 候选两轮逐 case 效用/区域/字符相同 |
| --- | --- | --- |
| project | True | True |
| short | True | True |
| semantic | True | True |
| swe | True | True |
| csn | True | True |
| upproject | True | True |
| upsemantic | True | True |
| heldout | True | True |
| heldsem | True | True |
| variants | True | True |
| layout | True | True |

每轮 11 套共 613 条查询，最终四轮均为零请求错误。重复性检查包含顺序化返回区域、逐 case 指标和字符数，不包含墙钟时延或异步 model usage；它不是统计显著性检验。供应端 token usage 返回为 0/缺失时不能当成免费调用或准确 token 成本。

## 工程验证与复查入口

- 全量 unit 按 99 个文件逐进程执行：835 passed、1 skipped、0 failed；生产源码清单与最终候选逐文件相同，测试期间源码未变。包括 SQL 端到端、25 条检索头部/关系离线回归、作用域隔离、改写不变性、预算来源及必需协议 fake。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv lock --check`、`git diff --check` 通过。
- `uv build` 的 wheel 包含新的 application 协调器及领域策略，不含已删除的旧 domain retrieval 模块；每个生产 Python 文件与工作树字节一致，解包导入与 API 装配通过。wheel SHA-256：`f3db11aaa2aa7fda52e556924808d95d77c8ba561463c8ff73a6edd30ec97b5a`。
- 新旧 OpenAPI 规范正规化后相同，SHA-256：`25bfb1ebb58fbb6f15f98ec02e296a94b1f2d4c24d6cbc70940b8e341c3a7fad`。这证明 wire schema 保持，不扩张为所有外部消费者的运行验证。
- 切块、embedding 输入、抽取及索引文档语义未改动，未修改 index profile 版本或已发布迁移。本轮没有用真实可选 reranker 模型重新量化效用；可选模型的候选保留与结构顺序由单元测试覆盖。

原始资料位于本机 `/home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/`：`baseline-{4,5}-<suite>.json`、`candidate-{1,2}-<suite>.json`、各轮 `*-provenance.json`、`comparison-proof.json`（含 44 份原始结果的 SHA-256）、`unit-checks-candidate/summary.json`、`package-proof-current.json`、`api-contract-proof.json` 及中间失败日志。原始结果不含凭据；没有将临时环境文件写入仓库。

例如复查主项目和问法比较（不启动服务）：

```bash
uv run python -m benchmarks.blackbox.project_cases compare /home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/baseline-4-project.json /home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/candidate-1-project.json
uv run python -m benchmarks.blackbox.project_cases compare /home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/baseline-4-variants.json /home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/candidate-1-variants.json
```

后续默认采用的剩余条件是恢复真实语义任务中的核心实现排序，同时保住问法/布局收益和关系覆盖。现有结果不足以批准“所有默认质量门槛通过”，本报告不降低原容忍线，也不通过改标签、恢复特殊文件名惩罚来隐藏回退。
