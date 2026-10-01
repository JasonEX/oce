# 结构化重构与检索修正的配对评测（2026-10-01）

本轮在纯结构重构（`8414d36`，冻结语料 dump 逐条一致）之后，评测三项会改变默认检索编排的
修正，决定哪些进入 master。结论：**只采纳中文调用链构式路由（`9255c8d`）**；所有意图统一按
名次融合 exact、以及删除文件名/包内邻近/测试名平局规则，两项均经实测回退，已否决并恢复原
实现。

## 协议与身份

- 服务端：个人模式（SQLite + Milvus Lite，worker 关闭），两级 reranker、查询改写与 query
  vector cache 均关闭；embedding 为生产同款 `Qwen3-Embedding-4B`（1024 维，OpenAI 兼容端点）。
- 客户端：发布版 `oce-client 0.2.0`（Rust，Linux release 归档，SHA-256 前缀 `ec7983848c35`；
  历史报告的 `ae27c49198e9` 是本地构建，Rust 构建不可逐字节复现）。
- 所有变体顺序独占同一物理索引；index profile fingerprint 前缀 `fc022d040dc8`。
- 基线 `5160dc0`（结构重构 + 超时/等待修正，检索输出与重构前一致）。基线在同一索引上复跑，
  short / semantic / project_cases 返回区域逐条一致，本轮差异全部归因于代码。
- 索引状态 A：初始 13 个精选快照 + SWE development + CSN，dense 32,894 / path 11,067。
  加入 upstream 快照与 layout 夹具后为状态 B，dense 41,408 / path 12,064。HNSW 近似检索
  的结果随图结构变化，**只比较同一索引状态内的运行**。

## 统一按名次融合 exact（状态 A）

`cand` = 统一 RRF；`cand2` = 统一 RRF + 结构列表不被 50 条窗口截断 + 恢复三条平局规则。

| 变体 | short Top-1 | short ref Top-1 | semantic nDCG@10 | semantic W-R@5 | project Hit@3 | project RelR | test_mapping Hit@3 | distractor head | SWE / CSN |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base-r1 | 96.7 | 92.5 | 74.6 | 64.6 | 97.1 | 93.4 | 85.7 | 0.0 | 基线 |
| base-r2 | 96.7 | 92.5 | 74.6 | 64.6 | 97.1 | 93.4 | 85.7 | 0.0 | 逐条一致 |
| cand-r1 | 95.4 | 88.8 | 74.4 | 63.6 | 91.4 | 90.7 | 57.1 | 2.9 | 一致 |
| cand2-r1 | 96.7 | 92.5 | 74.4 | 63.6 | 94.3 | 92.8 | 71.4 | 0.0 | 一致 |

- 纯名次融合把只有 exact 车道命中的使用点、测试与实现挤出候选窗口（redux `configureStore`、
  flask 中文 reference、rtk/gin test_mapping、gson multi_impl），distractor_head 上升。
- 删除平局规则回退 pytest `parser`（脚本里同名的 argparse 局部变量领头）与 gin
  `default_validator_test.go`（以符号命名的测试文件）。包内邻近近似了索引没有的名字解析，
  测试按被测单元命名是通行约定。
- `cand2` 修回大部分回退，但没有任何套件改善（project −1 例、semantic W-R@5 −1 点），按
  发布规则不采纳。原实现中 exact 种类分在窗口里领先语义候选，是让结构证据留在头部规则可选
  范围内的有意设计。

## 中文路由（状态 B）

`cand3` = 移除「从 / 到 / 路径 / 完整 / 如何被 / 如何从」；`cand4` = 在 `cand3` 上按构式匹配
「从 A 到 B」与「调到 / 到达」，并把「用到」加入 reference 动词。三者的五个主套件、upstream
semantic 与 layout controls 均与基线逐条一致。

| 变体 | variants primary R | variants RelR | variants distractor head | variants distractor any | truth share | chars | upstream RelR | upstream hop R | chain closed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base-x3 | 68.6 | 68.7 | 12.5 | 25.0 | 24.2 | 18,956 | 81.9 | 81.9 | 50.0 |
| cand3-x1 | 67.2 | 68.2 | 12.5 | 22.2 | 24.5 | 18,027 | 66.7 | 68.1 | 33.3 |
| cand4-x1 | 67.2 | 68.2 | 11.1 | 20.8 | 25.4 | 18,026 | 81.9 | 81.9 | 50.0 |

- 单字匹配曾把「找到 X 的定义」「在哪里能找到…」「配置文件的路径…」路由成 call_chain；
  `cand3` 修正了这些，却让 upstream 里两条真正的流程问句（「从前端登录 API 到 Tauri 注册…的
  路径」「…经 API 封装和 Tauri 调到 Rust 的 enable_prompt」）失去 call_chain。
- `cand4` 恢复这两条，variants 的干扰项与字符数下降；唯一的 primary 回退是
  `rtk-create-slice-built-from-factory--zh`：「找到 createSlice 的定义」现在与英文
  unquoted 问法（`Where is createSlice defined?`）一样路由为 symbol，结果也与之相同。

## 未覆盖

- 只跑了 SWE development profile；`standard` 53 题未跑。
- 每个变体单次运行；embedding 端点确定，基线复跑无差异，但外部模型不保证跨日一致。
- 原始结果 JSON 与日志保留在评测机器上，未入库。
