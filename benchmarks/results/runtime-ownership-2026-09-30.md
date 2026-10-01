# Runtime ownership 优化验证（2026-09-30）

基线为 `c607235e993c4109e8413562e3495bedab115d46`。本次按任务所有权、持久状态与资源
上限改造运行时，不新增查询问法、项目名或数据集规则。设计见
[运行时生命周期](../../docs/runtime-lifecycle.md)。

## 改动边界

- 应用层删除通用消息总线和纯转发的凭据 handler，组合根显式绑定类型明确的读写用例。
- 重复上传只更新 `last_seen`；DB pending 是持久任务来源，worker 有界补偿 Redis 投递。
- worker 串行管理启停与维护；维护排空活动批次，Redis 列表和 sentinel 原子更新。
- 缺少已验证 profile 时，上传和 checkpoint 在任何持久写入前返回既有 HTTP 503；补齐
  凭据、成功重载后恢复消费。显式关闭 embedding 的只切块模式保留。
- embedding 的文档/查询批次，以及热重载的新旧 delegate，共用 runtime 并发额度。
  相同 query 的活动请求合并，按 generation 隔离，等待者取消不取消共享 provider 请求。
- 检索仅在单次请求内复用原始实现/调用/定义证据，裁尾后重算来源与预算；普通关系上限
  为 0 时不查询普通关系，调用链保留独立预算；SQL 路径读取独立于向量路径开关。
- 新迁移 `a7b8c9d0e1f2` 用 `(status, blob_name)` 替换 status 单列索引；不改变切块、
  向量或 index-profile 语义，无需重建语料索引。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 整仓 unit，102 个测试文件独立进程 | 886 passed，1 skipped，0 failed |
| Milvus Lite 集成 | 6 passed |
| 真实 Container 装配（纳入 unit） | 4 passed：上传/checkpoint/HTTP 检索；缺凭据 503、重载启动与 reset；关闭 embedding 仍切块；关闭向量路径仍 SQL 检索 |
| 冻结语料结构等价性 | 基线源码 179 文件，43 查询 × 7 配置，301/301 结果与审计字段一致 |
| 实际本机 embedding HTTP | 并发配置 2，文档/查询混合峰值 2；12 个同 query 只发 1 次请求 |
| 实际本机 HTTP + SQLite 凭据轮换 | 两代 delegate 重叠，峰值 2；5 个新旧请求全部成功 |
| Redis 7.4.2，私有 UNIX socket | 24,000 项恢复、12,000 项保留；60 轮并发、4,800 次 enqueue、240 次维护、177 个原子快照，未发生 sentinel 分歧或重复投递 |
| SQL 成本与迁移 | SQLite keyset 查询使用复合索引，无临时 ORDER BY 排序；旧库升级/回滚保留记录，完整迁移链可往返 |
| 静态与打包 | Ruff check/format、mypy、uv lock、diff whitespace、sdist/wheel 均通过；wheel 源文件与工作树逐字节一致，删除模块未残留，新迁移已包含；从解包 wheel 导入并幂等迁移通过 |

关系刷新故障回归保留原来的证据、头部、字符预算与失败审计断言；故障注入点从已消除的
第二次 SQL 查询移至第二次关系刷新，另外断言 SQL 只读一次。

等价性以 `git archive c607235 src/oce` 冻结语料，在改动前后分别执行
`benchmarks.internal.retrieval_equivalence dump` 并 `compare`。这里验证结构、行为边界和
并发可靠性，不作为检索产品效用、生产延迟或多进程共享队列的评测结论。

服务模式升级需执行 `uv run alembic upgrade head`；个人模式由 `oce serve` 自动迁移。
本次不包含部署或发布。
