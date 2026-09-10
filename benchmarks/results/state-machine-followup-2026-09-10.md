# 关系证据后续修复与复测（2026-09-10）

本轮修复两处请求与证据不匹配的通用缺口：没有点名符号的测试问题丢失被测实现，明确询问实现类却未补全已查到的继承关系。当前项目 relation recall 为 95.29%，高于上一候选的 93.76% 和原基线的 94.33%；头部干扰项仍为 0。两轮最终源码测量的逐 case 指标、返回区域顺序与字符数完全相同。

工程取舍是继续去除依赖特殊文件名的排序，不以恢复旧开发集分数为修复目标。原有语义下降仍公开保留；其中一部分与文件名降权撤除相伴，但不能把全部下降都断言为特调收益消失。本轮未改 truth、容忍线、索引或模型，也没有加入 Pylint、Fastify、Requests、Gson 专用规则。[前一报告](state-machine-2026-09-10.md)保留旧快照及消融证据。

## 修复边界

- 仅具名 reference 将相关定义限制到被问符号。没有具名目标的测试请求，可从最终保留片段的调用/文本证据补充被测定义；原有作用域、歧义、来源及预算约束继续生效。
- 显式实现者请求在排序前取得一批有界 inherit 事实，供头部资格和选择后的 implementation 小节复用。多接口、限定作用域与可选子类型仍约束该批事实；普通构造调用不能代替继承证据。一次请求不为这两个阶段重复查找实现者。
- dense 候选若有独立的 inherit 事实，可以取得实现者头部资格，即使它未进入另一条有界 exact 召回窗口。没有合并不同标尺分数，也没有按源码文件名给予例外。
- 新增回归用例使用缓存过期测试及 SocketDriver 本地/远程实现，不复刻真实项目名称；实现放在普通文件或 `__init__.py` 都必须保持行为。

## 逐 case 修复与成本

| Case | 上一候选 RelR | 当前 RelR | 返回字符 |
| --- | --- | --- | --- |
| requests-redirect-cookie-tests | 66.67% | 100.00% | 24965 → 29922 |
| gson-type-adapter-factory-impls | 80.00% | 100.00% | 32454 → 34594 |

Requests 补回 `resolve_redirects`，supporting recall 从 0 恢复到 1；原先返回的测试位置保留。Gson 补回 `ReflectiveTypeAdapterFactory`，primary recall 从 0.75 恢复到 1。项目集只有这两个 case 的效用/返回发生变化；问法集只有同一 Gson 需求的六种问法变化。新增证据增加字符成本，不能视为无成本提升。

| 指标 | 原基线 | 上一候选 | 当前 |
| --- | --- | --- | --- |
| Project primary Top-1 | 88.57% | 94.29% | 94.29% |
| Project primary Hit@3 | 97.14% | 97.14% | 97.14% |
| Project primary recall | 95.71% | 96.43% | 97.14% |
| Project supporting recall | 93.81% | 90.95% | 93.81% |
| Project relation recall | 94.33% | 93.76% | 95.29% |
| Project test recall | 100.00% | 100.00% | 100.00% |
| Project distractor head ↓ | 0.00% | 0.00% | 0.00% |
| Variants Top-1 | 45.83% | 100.00% | 100.00% |
| Variants Hit@3 | 59.72% | 100.00% | 100.00% |
| Variants MRR | 54.89% | 100.00% | 100.00% |
| Variants relation recall | 67.13% | 98.33% | 100.00% |

问法集仍为 12 个既有需求 × 6 种问法，覆盖 7 个仓库；不能当成 72 个独立需求。当前每种问法、每个需求组及每个仓库的 Hit@3、relation recall 均为 1，需求等权平均与最弱问法均值也均为 1。original 问法的 relation recall 已恢复到 1。布局对照仍为 4 个需求、20 个布局，Top-1/Hit@3/MRR 均为 1；这些是开发对照，未变成独立自然用户验证集。

## 四套必需评测与外部护栏

| 指标 | 原基线 | 上一候选 | 当前 |
| --- | --- | --- | --- |
| Short Top-1 | 100.00% | 100.00% | 100.00% |
| Short MRR | 100.00% | 100.00% | 100.00% |
| Semantic nDCG@10 | 74.73% | 73.31% | 73.31% |
| Semantic primary Top-1 | 74.36% | 71.79% | 71.79% |
| SWE core Top-1 | 76.92% | 84.62% | 84.62% |
| SWE edit Top-1 | 46.15% | 53.85% | 53.85% |
| SWE nDCG@100 | 62.36% | 77.88% | 77.88% |
| SWE core region recall@10 | 59.49% | 56.03% | 56.03% |
| SWE weighted core coverage | 15.18% | 11.75% | 11.75% |
| CSN Top-1 | 63.75% | 63.75% | 63.75% |
| CSN Hit@10 | 83.75% | 86.25% | 86.25% |
| Upstream project Hit@3 | 83.33% | 83.33% | 83.33% |
| Upstream project relation recall | 81.94% | 83.33% | 83.33% |
| Upstream semantic nDCG@10 | 32.29% | 32.29% | 32.29% |
| Historical heldout Hit@3 | 95.83% | 95.83% | 95.83% |
| Historical heldout relation recall | 97.57% | 97.57% | 97.57% |
| Historical heldout semantic nDCG@10 | 74.63% | 73.01% | 73.01% |

除 project 与 variants 外，其他九套的逐 case 指标、返回区域顺序和字符数与上一候选相同；主语义和历史语义按意图/语言分组结果因此也与前一报告相同。历史 heldout 已参与开发分析；SWE 为 development 的 13 个 issue。这里不声称下游 agent 任务成功率或 ACE 优势。

## 对原有下降的判断

主语义 nDCG@10 相对原基线仍低 1.42 个百分点，历史语义低 1.62 点。Pylint 的 checker/run 问题在撤除 `__init__.py` 等文件名惩罚后的中间快照中，头部转向实际注册/启动包装入口；布局对照则显示旧惩罚使相同实现仅因换文件名而失分。这支持删除该文件名规则的工程判断，但中间快照并非全因素隔离实验，不能精确分解每一分变化。

Fastify 的 `exposeHeadRoute` issue 仍因正文限定名召回的 getter 片段而改变排序；SWE 的 Pylint 4604 和 pytest 5787 核心区域覆盖仍有损失。现有证据不足以将这些都归因于特调，也没有证明新的通用请求/证据错误。本轮保留观察，不添加仓库专用排序来追分。原语义容忍线没有数值通过；这与接受删除有布局依赖的特殊规则是两个需要分别记录的判断。

## 重复性、运行异常与工程验证

原基线 `baseline-4/5`、上一候选 `candidate-1/2` 和当前 `evidence-1/2` 使用同一已填充索引、冻结 evaluator、truth/schema/case 顺序、发布版 `oce-client 0.2.0` 与稳定 HTTP API。当前两轮每轮 11 套、613 条查询，共 1,226 条，零请求错误；逐 case 效用、返回区域顺序及字符数完全重复。该检查不包含墙钟时延或异步 model usage，不是显著性检验。黑盒没有 import `oce` 或读取数据库。

最终源码冻结前的单轮 `followup-1` 已包含两处关系修复，但缺少 dense 候选 inherit 头部资格的最后一处边界修复。该预跑的 `flask-wsgi-dispatch-chain` 恰有词法召回超时警告，头部干扰率升到 1/35；HTTP 仍成功，不能算作无质量退化。最终两轮均未复现该头部退化，且此调用链路径不受本次两处修复影响；超时根因未被证明。原预跑结果及服务器日志完整保留，没有调整超时阈值或删除该样本。

| 套件 | 每轮 N | 上一候选字符 | 当前字符 | 上一候选 p50 ms | 当前 p50 ms |
| --- | --- | --- | --- | --- | --- |
| project | 35 | 18969 | 19171 | 550–560 | 558–566 |
| variants | 72 | 18676 | 18854 | 479–506 | 568–604 |
| short | 240 | 10949 | 10949 | 274 | 277–279 |
| semantic | 39 | 30387 | 30387 | 1244–1307 | 1252–1263 |
| swe | 13 | 27249 | 27249 | 1670–1696 | 1627–1656 |
| csn | 80 | 25429 | 25429 | 1209 | 1220–1226 |
| upproject | 6 | 25006 | 25006 | 1186–1219 | 1213–1219 |
| upsemantic | 6 | 29088 | 29088 | 1287–1314 | 1250–1275 |
| heldout | 24 | 16677 | 16677 | 534–581 | 542–568 |
| heldsem | 18 | 24770 | 24770 | 1303–1309 | 1327–1341 |
| layout | 80 | 1132 | 1132 | 430–436 | 424–444 |

p50 范围来自两次完整测量，同一主机与远端 embedding 未做网络/负载隔离，不作延迟因果归因。当前正式两轮未与全量单测并行。返回的 token usage 为 0/缺失不能解释为无调用成本。

- 全量 unit 按 99 个文件逐进程运行：838 passed、1 skipped、0 failed，源码逐文件匹配当前候选；包括 25 条头部/关系离线回归。
- Ruff check/format、`uv lock --check`、`git diff --check` 通过。wheel/sdist 全部生产 Python 文件与工作树字节相同；旧 domain retrieval 模块不存在，解包导入和 OpenAPI 装配通过。
- 当前生产源码清单 SHA-256：`7a9d67dd4330db66a4be2d112f9b7ee4e2b9adaf4719968f46de367c853ef5e6`。
- wheel SHA-256：`51b0d592c90376ca86952a56a8373d26145d06e523fac86723fb416229d21529`。OpenAPI 与原基线相同，SHA-256：`25bfb1ebb58fbb6f15f98ec02e296a94b1f2d4c24d6cbc70940b8e341c3a7fad`。
- 索引 profile、切块/抽取语义、模型及开关与前一报告一致，没有重建或更改 truth。本文记录提交前的开发验证，未涉及打标签或推送。

原始资料位于 `/home/yuqian/.cache/oce/bench-runs/state-machine-2026-09-10/`：`evidence-{1,2}-<suite>.json`、`evidence-{1,2}-provenance.json`、`followup-comparison-proof.json`（66 份正式结果及 11 份预跑结果的 SHA-256、可比性与逐 case 差异）、`unit-checks-evidence/summary.json`、`package-proof-evidence.json` 和 `followup-1-server.log`。没有将临时凭据写入仓库或报告。
