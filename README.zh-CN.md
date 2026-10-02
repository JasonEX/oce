<div align="center">

<img src="assets/opencontextengine-logo.svg" alt="OpenContextEngine" width="75%"/>

# OpenContextEngine

**自托管、ACE 兼容的代码检索服务，为 AI 编码代理提供精准上下文。**

Dense + exact + 词法 + 路径检索 · 语义切块 · 可选重排

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://img.shields.io/github/actions/workflow/status/JasonEX/oce/ci.yml?branch=master&logo=github&label=CI)](https://github.com/JasonEX/oce/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/JasonEX/oce/pkgs/container/oce)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Milvus](https://img.shields.io/badge/Vectors-Milvus%203.0-00A1EA.svg)](https://milvus.io/)
[![ACE](https://img.shields.io/badge/ACE-compatible-success.svg)](#api)

</div>

OpenContextEngine 是面向 AI 编码代理的自托管、ACE 兼容代码检索服务。它结合语义向量、精确
标识符、词法文本和文件路径，从客户端声明的工作集中返回源码上下文。

单机使用**个人模式**（SQLite + 内嵌 Milvus Lite，同步索引）；多人或多台机器共享索引时使用
**服务模式**（PostgreSQL + Milvus 3.0 + Redis，后台索引）。两种模式遵循同一套检索与索引合同。

服务端和客户端分别维护：

- 服务端：<https://github.com/JasonEX/oce>
- 客户端：<https://github.com/JasonEX/oce-client>

这是此前 ACE 服务的重构版本，相关背景见
[linux.do 讨论](https://linux.do/t/topic/2308140/125)。

## 特性

- **混合检索** —— 按查询意图组合独立的 dense、精确标识符、SQL 词法、路径索引与精确路径查找车道。
- **语义源码上下文** —— cAST/tree-sitter 切块保留代码边界和封闭作用域；结果可附带相关定义、调用者、实现、测试与重导出。
- **可选重排** —— API 或本地 ONNX reranker 与 chat LLM 可单独运行，也可级联；最终选择遵守各类任务的代码字符预算。
- **ACE 兼容 API 与 MCP 客户端** —— Bearer 鉴权保护上传、checkpoint 和检索，按工作集隔离。
- **运维与监控** —— 独立 admin 鉴权管理模型凭据、队列恢复、垃圾回收和索引/调用/token/资源统计。
- **[黑盒评测](benchmarks/README.md)** —— 通过发布版客户端和稳定 API 评测双语查询、人工复核的架构查询与固定源码版本的 issue 任务。

<details>
<summary><strong>目录</strong></summary>

- [环境要求](#环境要求)
- [个人模式](#个人模式)
- [服务模式](#服务模式)
- [可选模型](#可选模型)
- [运维](#运维)
- [客户端与 MCP](#客户端与-mcp)
- [API](#api)
- [架构与检索](#架构与检索)
- [测试](#测试)
- [许可](#许可)

</details>

## 环境要求

- Python 3.11 及以上
- [uv](https://docs.astral.sh/uv/)
- 默认启用向量时，需要可用的嵌入端点及其凭据

个人模式不需要单独部署数据库、向量或队列服务。服务模式使用 PostgreSQL 16、Milvus 3.0
和 Redis，开发用依赖编排见 `docker-compose.dev.yml`。

## 个人模式

安装 CLI 并生成配置：

```powershell
uv tool install "git+https://github.com/JasonEX/oce.git"
oce init                    # 生成 ~/.oce/data/.env
```

本 fork 不发布 PyPI。上面的命令安装当前 Git 源码，已发布的服务端镜像位于 GHCR。

编辑 `~/.oce/data/.env` 并填入嵌入 key。默认使用 SiliconFlow 和 Qwen3-Embedding-4B，
向量为 1024 维：

```dotenv
EMBED_API_KEY=你的嵌入服务密钥
# 使用其它端点或模型时修改这些配置。
EMBED_ENDPOINT=https://api.siliconflow.cn/v1/embeddings
EMBED_MODEL=Qwen/Qwen3-Embedding-4B
EMBED_DIMENSIONS=1024
```

然后启动服务：

```powershell
oce serve                   # http://127.0.0.1:8986
```

`oce serve` 自动执行数据库迁移，并补齐个人模式默认值：data 目录中的 SQLite 数据库与
Milvus Lite 文件，以及 `WORKER_ENABLED=false`。上传在请求内同步索引。生成的
`API_KEY=sk-opencontextengine` 与客户端默认值一致；服务暴露到本机以外时，请改用强随机
`API_KEY`，并在客户端设置相同的 `OCE_API_KEY`。

data 目录中的 `.env.local` 覆盖 `.env`，已有进程环境变量优先于两者。`${VAR}` 引用按这
两个文件的顺序展开；存在同名进程变量时使用进程值。常用参数：

| 参数 | 用途 |
| --- | --- |
| `--data-dir <path>` | 数据库、向量文件和配置目录，默认 `~/.oce/data` |
| `--env-file <path>` | 改为读取指定文件；该文件提供的值覆盖进程变量 |
| `--host <addr>` / `--port <n>` | 监听地址，默认 `127.0.0.1:8986` |
| `--reload` | 开启 Uvicorn 开发模式重载 |

`oce version` 与 `oce --version` 打印版本。`oce -v serve` 开启 INFO 日志，
`oce -vv serve` 开启 DEBUG；CLI 默认级别为 WARNING。

临时运行而不安装：

```powershell
uvx --from "git+https://github.com/JasonEX/oce.git" oce serve
```

## 服务模式

根目录 Compose 会启动应用、PostgreSQL、Redis 和 Milvus 依赖：

```powershell
git clone https://github.com/JasonEX/oce.git
Set-Location oce
Copy-Item .env.example .env
# 设置 API_KEY、ADMIN_API_KEY、EMBED_API_KEY、POSTGRES_PASSWORD 和 REDIS_PASSWORD。
docker compose up -d
```

只有应用 API 端口发布到宿主机，应用容器启动时执行迁移。真实凭据应保留在仓库以外。
Compose 会把 `.env` 注入应用进程环境；如需其它文件，请显式配置 Compose 的 `env_file`。

若要在源码目录运行应用、仅把依赖放在 Docker 中：

```powershell
uv sync --extra dev
docker compose -f docker-compose.dev.yml up -d
# 按开发编排的宿主端口和凭据设置 DB_URL 与 REDIS_URL。
# DB: 127.0.0.1:25432；Redis: 127.0.0.1:26379；Milvus: 127.0.0.1:19530。
uv run alembic upgrade head
uv run uvicorn oce.main:app --host 127.0.0.1 --port 8986
```

源码启动时，所有配置组按工作目录中的 `.env`、`.env.local` 顺序读取，进程变量优先。
可用配置及默认值见 [.env.example](.env.example)。

也可以在自己的编排中使用已发布镜像：

```powershell
docker pull ghcr.io/jasonex/oce:latest
```

固定部署请选择带版本号的镜像，并提供 `DB_URL`、`REDIS_URL` 和 `MILVUS_ENDPOINT`；
镜像监听容器内的 `8986` 端口。

## 可选模型

嵌入会把准入后的源码块和语义查询发送到 `EMBED_ENDPOINT`；API reranker 把查询与候选源码
发送到 `RERANK_ENDPOINT`；chat 重排把查询与候选片段发送到 LLM 端点，query rewrite
只发送查询。请使用获准接收这些数据的端点。`RERANK_PROVIDER=local` 在进程内重排，不发出外部调用。

两种重排默认都关闭。启用 API reranker：

```dotenv
RERANK_ENABLED=true
RERANK_PROVIDER=api
RERANK_API_KEY=你的 rerank 服务密钥
RERANK_ENDPOINT=https://provider.example.com/v1/rerank
RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B
RERANK_TOP_N=50
RERANK_MAX_QUERY_CHARS=2400
RETRIEVAL_RERANK_POLICY=adaptive
# provider 不支持 task instruction 时，将 RERANK_INSTRUCTION 置空。
```

chat LLM 可以单独运行，或接在专用 reranker 后面。下面的例子把第二阶段限制为 20 个候选，
配置默认值为 50：

```dotenv
LLM_RERANK_ENABLED=true
LLM_API_KEY=你的 LLM 服务密钥
LLM_BASE_URL=https://provider.example.com/v1
LLM_MODEL=你的 chat 模型
LLM_MAX_CANDIDATES=20
LLM_RERANK_TIMEOUT_SECONDS=15
RETRIEVAL_LLM_RERANK_POLICY=adaptive
```

`RERANK_ENABLED` 和 `LLM_RERANK_ENABLED` 授权相应阶段，`RETRIEVAL_*_POLICY` 再按查询
决定是否调用：`adaptive` 在确定性证据足够时跳过，`always` 对至少两个候选的结果运行。
两阶段均保留重排窗口之外的候选。查询改写单独通过
`RETRIEVAL_QUERY_REWRITE_ENABLED` 启用，默认值为 `false`。

使用本地 ONNX reranker 时，从源码目录安装可选依赖，并自行准备模型文件：

```powershell
uv sync --extra local-rerank
# 用该源码环境运行 uv run oce serve。
```

```dotenv
RERANK_ENABLED=true
RERANK_PROVIDER=local
RERANK_LOCAL_MODEL_DIR=/path/to/model-directory
# 默认读取目录中的 model_int8.onnx 与 tokenizer.json。
```

模型许可与服务端许可独立。评测使用的
[`jinaai/jina-reranker-v2-base-multilingual`](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)
模型采用 CC-BY-NC-4.0；请核对使用权或选择兼容的其它导出。

[第三轮报告](benchmarks/results/utility-round3-2026-09-09.md)记录了本地 reranker 在当时配置下
的质量回退与 CPU 延迟；选择可选模型前，请在自己的工作负载上测量。
按日期查阅对照结果，见[历史评测索引](benchmarks/results/README.md)。

## 运维

### Admin 面板与凭据

官方面板：<https://oce-ai.github.io/oce-admin>。设置独立 `ADMIN_API_KEY`，在面板填入服务
地址和 key，即可管理凭据、队列、GC 和统计。admin key 留空时回落到 `API_KEY`。
面板把 key 存在浏览器本地存储中，请勿放进 URL 或日志。默认放行官方面板来源；其它来源
用逗号分隔的 `CORS_ORIGINS` 配置，留空可关闭 CORS。

凭据按 kind（`embed`、`rerank`、`llm_rerank`、`query_rewrite`）选择，启用行中 `priority`
数值最小者优先。没有对应启用行时回落到 `EMBED_*`、`RERANK_*` 或 `LLM_*`；API 重排的
key 为空时回落到嵌入 key。凭据响应只暴露 key 的末四位。

修改凭据后调用 `POST /admin/credentials/reload`。兼容的 key、timeout 和批量限制变更无需
重启。成功响应为 `{"reloaded": true, "reason": null}`；请检查 `reloaded` 和 `reason`：
不兼容的 profile 会被拒绝，LLM 刷新失败可能报告部分重载，此时已切换的客户端继续生效。
通过环境配置启用或关闭模型阶段需要重启。reload 刷新数据库凭据，不重读环境文件；成功
重载只验证本地配置与索引兼容性，不探测远端 key 或 provider 可用性。

### 索引兼容与查询缓存

OCE 持久化不含密钥的 index profile，在接收索引任务前检查兼容性。改变嵌入身份、维度、
文档输入语义或切块/索引语义时，需要新 data 目录；服务模式则使用新的 SQL 存储与 Milvus
collection，再由客户端完整重同步。不兼容的启动或重载会 fail closed，保留旧数据。
`EMBED_DIMENSIONS` 同时决定两个向量 collection 的维度和凭据校验。

源码准入版本 2 允许以 `-retrieval-eval` 结尾的普通目录，版本 1 索引需要新存储及完整重同步。
显式改变 `MILVUS_DENSE_INDEX_TYPE` 会重建本地 dense 索引并保留向量，与改变嵌入身份不同。

重复语义查询使用进程内 query-vector LRU，默认容量 256、TTL 600 秒
（`EMBED_QUERY_CACHE_MAX_ENTRIES`、`EMBED_QUERY_CACHE_TTL_SECONDS`），任一设为 `0` 可关闭。
缓存只保存 query 哈希与向量；源码向量留在 Milvus，检索结果不缓存。兼容的嵌入凭据重载会
清空该缓存。监控保存查询原文是另一项配置，默认关闭（`MONITORING_STORE_QUERY_TEXT=false`）。

### 队列与垃圾回收

服务模式中，SQL pending blob 与 staging 源码是持久任务依据，Redis 承载投递。
`GET /admin/queue` 返回 `main_size`、`inflight`、`db_pending` 和 `worker_state`。
`inflight` 统计尚未确认的投递身份，包括排队中与处理中的项。worker 关闭时返回
`enabled=false` 和零队列计数；元数据数量请看 `/admin/index-stats`。

`EMBED_ENABLED=false` 时，上传在请求内完成切块，即使 `WORKER_ENABLED=true` 也不装配
worker 或连接 Redis。blob 保留 pending 与 staging 源码，仍可按正常 TTL 规则回收。

`POST /admin/queue/reset` 默认接受 `{"mode":"sync","requeue":true}`。`sync` 清掉过期
队列记录并补齐 pending 投递；`purge` 先清空队列再恢复 pending。`requeue=false` 只抑制本次
立即补队，worker 的周期 replay 仍可能恢复持久 pending 任务。reset 修改前会等待活动批次
结束。`POST /admin/queue/requeue-stale` 接受 `stale_hours`（默认 24）和 `limit`（默认 100），
用于重新投递有 staging 的长时间 pending blob。

GC 通过 admin 显式执行，删除前先预览：

```powershell
# ADMIN_API_KEY 未设置时，这里改用服务端 API_KEY。
$adminHeaders = @{ Authorization = "Bearer $env:ADMIN_API_KEY" }
$gc = @{ ttl_days = 30; dry_run = $true; limit = 1000 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8986/admin/gc `
  -Method Post -Headers $adminHeaders -ContentType application/json -Body $gc
# 确认后把 dry_run 改为 $false 再执行。
```

API 默认 `ttl_days=30`、`dry_run=true`、`limit=1000`，TTL 至少为一天。真实 GC 会等待 worker
活动批次结束、跳过 inflight 身份，并在标记删除前重新检查最近活动与 checkpoint 引用。
本轮被删除 checkpoint 释放的 blob 会留到后续 GC 回收。

向量清理失败时，blob 保留为 `deleting`，后续 GC 可以重试；该身份不进入检索，复用它的
上传或 checkpoint 请求返回可重试的 HTTP 503，直到删除完成。
`/admin/index-stats` 的 `metadata.blobs_deleting` 与 ready、pending、error 计数一起展示。
清理后同一份源码可以再次上传。状态转移与恢复细节见
[docs/runtime-lifecycle.md](docs/runtime-lifecycle.md)。

## 客户端与 MCP

独立 Rust 客户端扫描工作区、上传变更、维护 checkpoint 并检索代码上下文。
从[客户端 Releases](https://github.com/JasonEX/oce-client/releases)下载 Windows、Linux 或
macOS 压缩包，把 `oce-client` 放进 `PATH`，也可以从源码构建：

```powershell
cargo install --git https://github.com/JasonEX/oce-client --locked
$env:OCE_API_URL = "http://127.0.0.1:8986"
$env:OCE_API_KEY = "sk-opencontextengine"  # 服务模式改为服务端 API_KEY
$env:OCE_WORKSPACE = (Get-Location).Path

oce-client sync
oce-client retrieve "Where is request authentication implemented?"
```

PyPI 包 `opencontextengine-client` 是已被取代的 0.1 客户端。接入支持 MCP 的 AI 编码工具时，
用同一个二进制启动 stdio server：

```powershell
oce-client mcp --workspace C:\path\to\workspace
```

它在后台建立初始索引、监听变更，并暴露 `codebase-retrieval` 工具。多工作区可重复传入
`--workspace`，工具调用须指定对应的 `workspace_folder`。环境变量等价配置为
`OCE_API_URL`、`OCE_API_KEY` 和 `OCE_WORKSPACE`/`OCE_WORKSPACES`，凭据请放在进程环境
变量或 secret manager 中。

## API

- **公开：** `GET /health`、`GET /version` 无需鉴权；`/health` 只报告存活，不探测模型 provider。
- **数据面：** `Authorization: Bearer <API_KEY>`。
- **Admin（`/admin/*`）：** `Authorization: Bearer <ADMIN_API_KEY>`，留空时回落到 `API_KEY`。

### 数据面端点

| POST 路径 | 请求字段 | 响应字段 |
| --- | --- | --- |
| `/find-missing` | `mem_object_names` | `unknown_memory_names`、`nonindexed_blob_names` |
| `/batch-upload` | `blobs: [{path, content}]`、可选 `checkpoint_id` | `blob_names` |
| `/agents/codebase-retrieval` | `information_request`、`blobs`、可选 `chat_history` | `formatted_retrieval`、`codebase_retrieval_elapsed_ms` |
| `/agents/blob-status` | `blobs`（检查 `added_blobs` 和 `checkpoint_id`） | `unknown_blob_names`、`nonindexed_blob_names`、`checkpoint_not_found` |
| `/checkpoint-blobs` | `blobs` | `new_checkpoint_id` |

共用的 `blobs` payload 包含 `checkpoint_id`、`added_blobs` 和 `deleted_blobs`。
blob 名称为 UTF-8 `path + content` 的 SHA-256。新上传要求路径为 1–1024 个字符，SQLite
已有的更长路径仍可读取。SQL 和返回上下文保留完整路径，路径向量旁保存的有界字符串仅供诊断预览，embedding 使用完整路径文档。
上传准入跳过依赖/构建/缓存目录、敏感文件、二进制和非源码产物；`.env.example` 等安全模板、
项目清单和测试固件有豁免。跳过的上传保存为空 ready blob，避免反复上传。

后台上传返回 blob 身份时，索引可能尚未完成；用 `/find-missing` 或 `/agents/blob-status`
检查就绪。checkpoint 可以包含 pending 身份，但检索只准入 ready 元数据。

检索作用域为 `(checkpoint 成员 ∪ added_blobs) − deleted_blobs`，再限定为 ready blob。
必须提供有效 checkpoint 或非空 added 列表；解析出的工作集为空时返回空答案。
`deleted_blobs` 只收窄检索范围，或经 `/checkpoint-blobs` 更新 checkpoint 成员，物理清理由
GC 执行。检索缺少 scope 或 checkpoint token 格式错误时返回 HTTP 400，checkpoint 不存在
或版本过期时返回 404。

```powershell
$headers = @{ Authorization = "Bearer $env:API_KEY" }
$body = @{
  information_request = "Where is request authentication implemented?"
  blobs = @{
    checkpoint_id = ""
    added_blobs = @("<blob-name-from-batch-upload>")
    deleted_blobs = @()
  }
} | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:8986/agents/codebase-retrieval `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

### Admin 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` / `POST` | `/admin/credentials` | 列出脱敏凭据 / 创建凭据 |
| `PATCH` / `DELETE` | `/admin/credentials/{id}` | 更新 / 删除凭据 |
| `POST` | `/admin/credentials/{id}/duplicate` | 复制凭据，可覆盖部分字段 |
| `POST` | `/admin/credentials/reload` | 重载运行凭据 |
| `GET` | `/admin/queue` | 队列计数与 worker 状态 |
| `POST` | `/admin/queue/reset` | 同步或清空队列投递 |
| `POST` | `/admin/queue/requeue-stale` | 投递长时间 pending blob |
| `POST` | `/admin/gc` | 预览或回收过期 checkpoint 与 blob |
| `GET` | `/admin/stats` | 调用、token、检索与资源指标 |
| `GET` | `/admin/index-stats` | 元数据计数、dense/path 存储、查询缓存、运行配置与 index profile |

## 架构与检索

依赖向内收敛：`shared ← domain ← application ← api`。infrastructure 实现协议，由
`application/container.py` 装配；应用层负责用例和事务边界。SQL 存元数据、symbol 与词法
证据（SQLite FTS5 / PostgreSQL `tsvector`），Milvus 存 dense 和路径向量。

检索采用固定序列：route → plan → recall → fuse → prior → rerank → select → expand。
SQL 证据在 query embedding 往返前开始召回；决定性的 symbol、path 或使用点证据可以直接
作答，无需等待 dense。symbol/path 查询采用 focused 选择，其它任务采用 coverage 选择，
默认代码正文字符预算分别为 12,000 和 32,000；标题、行号和 `Context:` 等格式化内容另计。

关系结果依据已索引的名称、出现位置和封闭定义，并受歧义上限约束，不绑定动态接收者类型。
可选车道能独立失败，失败记录在检索审计指标中。意图门控、头部规则、关系预算、配置和历史
评测见[检索设计](docs/retrieval-pipeline.md)；索引、恢复和资源所有权见
[运行时生命周期](docs/runtime-lifecycle.md)。维护中的设计与运维说明汇总在
[文档索引](docs/README.md)。

## 测试

从源码目录安装开发依赖，按文件独立运行，让 Milvus Lite 和 tree-sitter 运行时在进程间释放：

```powershell
uv sync --extra dev
uv run pytest tests/unit/application/test_service.py -q
uv run pytest tests/unit/domain/test_retrieval.py -q
uv run pytest tests/unit/infrastructure/test_milvus3.py -q
uv run pytest tests/unit/test_smoke_personal_mode.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

冒烟测试使用真实容器、迁移、Milvus Lite 和 HTTP 路由，以及临时存储与进程内 embedding
端点。内存受限时，不要在一个进程中运行整个 `tests/unit/infrastructure`。
检索纯重构用 `benchmarks.internal.retrieval_equivalence` 验证行为保持；产品质量与延迟通过
[黑盒评测](benchmarks/README.md)测量。

## 许可

Apache-2.0。OpenContextEngine 与 Augment Code Inc. 相互独立。
