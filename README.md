<p align="center">
  <img src="web-vue/public/logo.svg" width="112" alt="GPTImage2API logo" />
</p>

<h1 align="center">GPTImage2API</h1>

<p align="center">将 ChatGPT 官网能力接入 OpenAI 兼容 API，并提供面向多账号、注册机、图片任务与自托管场景的管理控制台。</p>

<p align="center">
  <strong>简体中文</strong> · <a href="./README_EN.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v1.0.1-111827" alt="Version v1.0.1" />
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 18" />
  <img src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white" alt="Docker ready" />
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="License AGPL-3.0" /></a>
</p>

<p align="center">
  <span>v1.0.1</span>
  · <a href="./CHANGELOG.md">更新说明</a>
  · <a href="./docs/README.md">维护文档</a>
</p>

> [!IMPORTANT]
> `v1.0.1` 是 `gptimage2api` 的首个发布版本。镜像发布到 `ghcr.io/biubiubiu125/gptimage2api`。使用官方 Compose 时，受管容器控制台可一键在线更新。图片任务默认依赖独立 PostgreSQL 队列库，首次部署请使用 PostgreSQL Compose overlay。

> [!WARNING]
> 本项目通过逆向研究接入 ChatGPT 官网的文本、图片和文件生成能力，并非 OpenAI 官方服务。接口可能随上游变化失效，并可能导致账号受限、临时或永久封禁；请勿使用重要、常用或高价值账号。
>
> 使用者须自行了解技术、账号与合规风险，遵守 OpenAI 服务条款及当地法律法规。严禁用于批量滥用、恶意竞争、账号盗用、诈骗、骚扰，以及生成或传播违法、暴力、色情或涉及未成年人的内容；使用者自行承担全部风险与责任。

## 快速部署

### 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/biubiubiu125/gptimage2api/main/deploy/install.sh | sudo bash
```

安装向导默认中文和 Docker，回车即采用默认值并立刻显示已选项。数据库固定为 PostgreSQL 18 本地容器，代码固定拉 `main`。管理员登录密钥需输入两次且不会回显。摘要确认后才开始拉镜像或克隆。脚本会写入 `GPTIMAGE2API_GITHUB_REPOSITORY=biubiubiu125/gptimage2api`，供控制台检查 GitHub Release 并一键更新。

### Docker Compose

当前工作区直接构建并启动单体 App 与 PostgreSQL。PostgreSQL 同时承载
Application Database 和独立的 `gptimage2api_image_queue` 队列数据库：

```bash
cp .env.example .env
# 编辑 .env，为 GPTIMAGE2API_AUTH_KEY 和 POSTGRES_PASSWORD 设置私有值
test -f config.json || printf '{}\n' > config.json
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

| 入口            | 地址                       |
| :-------------- | :------------------------- |
| 管理控制台      | `http://localhost:3000`    |
| OpenAI 兼容 API | `http://localhost:3000/v1` |
| 数据目录        | `./data`                   |

`.env` 中的 `GPTIMAGE2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。Compose 使用独立运行时卷支持控制台在线更新；控制台设置、账号、用户密钥、调用日志和指标写入 Application Database，图片任务写入独立队列库。不要提交本地 `.env`、`config.json` 或 `data/`。

### 外部 PostgreSQL

连接已有 PostgreSQL 时设置 Application Database 和队列库：

```env
DATABASE_URL=postgresql://user:password@host:5432/gptimage2api_app
GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL=postgresql://user:password@host:5432/gptimage2api_image_queue
```

使用已发布镜像连接外部数据库时，加载不包含本地 PostgreSQL 的
`docker-compose.remote.yml`：

```bash
docker compose -f docker-compose.remote.yml up -d
```

完整的升级、备份、PostgreSQL 和故障排查说明见 [部署文档](./docs/deployment.md)。

## 核心能力

通用 UI 组件、主题和基础交互来自 [yukkcat/nanocat-ui](https://github.com/yukkcat/nanocat-ui)；本项目负责业务页面、后端状态投影和产品流程。

|       | 领域       | 能力                                                                                                           |
| :---: | :--------- | :------------------------------------------------------------------------------------------------------------- |
|   🔌   | API 网关   | Chat Completions、Responses、Messages、搜索、图片生成、图片编辑、PPT / PSD 与统一可编辑文件任务                |
|   💬   | 对话画图   | 文本对话、联网搜索、文生图、图生图、多图参考、局部编辑、Markdown、代码高亮、引用来源和推理强度                 |
|   👥   | 账号管理   | 手动添加、OAuth、Access Token、Session JSON、CPA、远程 CPA、Sub2API 导入，以及搜索、筛选、分组、导出和批量处理 |
|   📨   | 注册机     | 仅支持 `yyds_mail`、`remail`、`outlook_token`、`icloud_api`，成功注册自动进入上游账号池 |
|   🔑   | 凭证与额度 | 独立展示 AT / RT 状态，支持 RT 刷新 AT、同步套餐与额度、指定账号文本/画图测试和异常账号处置                    |
|   ⚙️   | 调度与并发 | 多账号选择、账号处理并发、单账号图片并发、多图并行、失败换号、额度与限流状态管理                               |
|   🌐   | 代理出口   | 账号代理、账号组代理、多出口代理组、节点图片并发、轮换间隔、默认出口、备用出口和连通性检测                     |
|   📊   | 日志与监控 | 调用日志、活跃请求、最近完成、慢请求、账号切换、出口信息、图片阶段时间线和原始上游诊断                         |
|   🖼️   | 图片与文件 | 本地 / WebDAV 存储、图库、标签、缩略图、下载、ZIP、压缩、清理、PPT / PSD 产物和可选图片放大                    |
|   ✨   | 提示词库   | 本地提示词资产、云端来源同步、分类选择和更新状态管理                                                           |
|   💾   | 数据与备份 | SQLite、PostgreSQL 18、R2 备份、保留策略，以及调用趋势、成功率和模型统计                                       |
|   🖥️   | 管理控制台 | 概览、账号、代理、日志、实时监控、图片、对话画图和系统设置，适配桌面与移动端                                   |

## 架构

```mermaid
flowchart LR
  Client["兼容 API 客户端"] --> API["/v1 兼容 API"]
  User["管理员 / Web 用户"] --> Console["Vue 管理控制台"]
  API --> Services["业务服务"]
  Console --> AdminAPI["/api 管理接口"]
  AdminAPI --> Services
  Services --> Scheduler["账号调度与代理出口"]
  Scheduler --> Upstream["ChatGPT Web"]
  Services --> AppDB["Application Database<br/>SQLite / PostgreSQL 18"]
  Services --> Queue["Persistent Image Queue<br/>PostgreSQL"]
  Services --> Assets["图片与生成文件<br/>本地 / WebDAV"]
  Services --> Monitor["进程内实时监控"]
  Services --> Backup["R2 备份"]
```

Application Database 保存账号、用户密钥、设置、日志和指标；图片任务使用独立 PostgreSQL 持久化队列，图片与生成文件使用独立文件存储。详见 [存储架构](./docs/storage-architecture.md)。

## API

所有 AI 接口使用 Bearer Key：

```http
Authorization: Bearer <auth-key>
```

| 接口                                | 方法         | 说明                                                 |
| :---------------------------------- | :----------- | :--------------------------------------------------- |
| `/health`                           | `GET`        | 部署健康检查，覆盖 Application Database 和 Image Queue Store |
| `/v1/models`                        | `GET`        | 返回本地目录与上游实时模型合并后的模型列表           |
| `/v1/chat/completions`              | `POST`       | 文本、搜索和图片场景的 Chat Completions 入口         |
| `/v1/responses`                     | `POST`       | 支持文本、搜索和图片工具调用的 Responses 入口        |
| `/v1/messages`                      | `POST`       | Anthropic Messages 兼容入口                          |
| `/v1/search`                        | `POST`       | 返回回答、引用来源和搜索结果                         |
| `/v1/images/generations`            | `POST`       | 图片生成，支持 `n=1..4`                              |
| `/v1/images/edits`                  | `POST`       | multipart、远程 URL、base64、data URL 和多参考图编辑 |
| `/v1/editable-file-tasks`           | `GET / POST` | 创建与查询 PPT / PSD 可编辑文件任务                  |
| `/v1/editable-file-tasks/{task_id}` | `DELETE`     | 删除当前密钥所属任务                                 |
| `/v1/ppt/generations`               | `POST`       | PPT 任务快捷入口                                     |
| `/v1/psd/generations`               | `POST`       | PSD 任务快捷入口                                     |
| `/files/{file_path}`                | `GET`        | 下载当前 API Key 所属任务的生成文件                 |

文件任务的创建、查询和删除按 API Key 隔离；下载 `/files/...` 需要携带当前 API Key。服务端会校验任务归属、存储路径及文件类型，拒绝路径穿越。

<details>
<summary>Chat Completions 示例</summary>

```bash
curl http://localhost:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{"model":"gpt-5","messages":[{"role":"user","content":"介绍一下这个项目"}],"stream":true}'
```

</details>

<details>
<summary>图片生成示例</summary>

```bash
curl http://localhost:3000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -H "Idempotency-Key: unique-request-id" \
  -d '{"model":"gpt-image-2","prompt":"一只漂浮在太空里的猫，电影感光影","n":1,"response_format":"b64_json"}'
```

`Idempotency-Key`（幂等键）必须在每次新任务中使用唯一值；网络重试时复用原值，
会返回同一个持久化 Image Task，不会重复提交生图。

</details>

实际可用模型以上游账号和 `/v1/models` 返回值为准。

## 关键配置

| 配置                             | 默认值       | 用途                                                                                     |
| :------------------------------- | :----------- | :--------------------------------------------------------------------------------------- |
| `GPTIMAGE2API_AUTH_KEY`          | 必填         | 管理员和默认 API Key，环境变量优先于 `config.json`                                       |
| `DATABASE_URL`                   | PostgreSQL   | Application Database 连接；本地默认使用 `data/gptimage2api.db`                          |
| `GPTIMAGE2API_BASE_URL`          | 当前服务地址 | 生成对外可访问的图片和文件 URL                                                           |
| `GPTIMAGE2API_THREAD_TOKENS`     | `120`        | 后端同步工作线程并发容量，只要求正整数且不设固定最高值；账号、代理和上游仍有各自并发限制 |
| `GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL` | 必填      | 独立 Image Queue Store PostgreSQL 连接                                                   |
| `GPTIMAGE2API_IMAGE_QUEUE_GENERATION_CONCURRENCY` | `4` | 单体队列生图 Worker 并发容量                                                          |
| `GPTIMAGE2API_IMAGE_QUEUE_MAX_BACKLOG` | `256` | 队列最大待处理任务数                                                                  |
| `account_processing_concurrency` | `30`         | 账号导入、刷新、同步和批量处理容量                                                       |
| `image_account_concurrency`      | `1`          | 单账号图片并发上限，可设置为 1–3                                                         |
| `image_stream_timeout_secs`      | `80`         | 图片上游 SSE / HTTP 流最长等待时间                                                       |
| `image_poll_timeout_secs`        | `60`         | 图片结果解析与轮询最长等待时间                                                           |
| `log_retention_hours`            | `24`         | 调用日志自动保留小时数                                                                   |

其余设置通过控制台维护。配置项的权威默认值与约束以当前接口投影为准。

## 效果展示

<table width="100%">
  <tr><td width="50%"><img src="docs/images/1.png" alt="控制台截图 1"></td><td width="50%"><img src="docs/images/2.png" alt="控制台截图 2"></td></tr>
  <tr><td width="50%"><img src="docs/images/3.png" alt="控制台截图 3"></td><td width="50%"><img src="docs/images/4.png" alt="控制台截图 4"></td></tr>
  <tr><td width="50%"><img src="docs/images/5.png" alt="控制台截图 5"></td><td width="50%"><img src="docs/images/6.png" alt="控制台截图 6"></td></tr>
</table>

## 本地开发

```bash
# 后端：Python 3.13 + uv
uv sync
uv run main.py

# 前端：Node.js + npm
cd web-vue
npm install
npm run dev
```

默认开发地址为 `http://localhost:5173`，后端接口由 Vite 开发代理转发。

## 文档

| 文档                                               | 内容                                 |
| :------------------------------------------------- | :----------------------------------- |
| [文档索引](./docs/README.md)                       | 当前架构与维护文档入口               |
| [部署与运维](./docs/deployment.md)                 | Docker、PostgreSQL、升级、备份与排障 |
| [存储架构](./docs/storage-architecture.md)         | Application Database 与文件存储边界  |
| [控制台架构](./docs/control-panel-architecture.md) | 前后端职责、业务投影与交互状态       |
| [图片失败处理](./docs/image-failure-handling.md)   | 图片失败分类、重试与账号处置         |
| [上游 SSE](./docs/upstream-sse-conversation.md)    | 会话与流式解析边界                   |

文档与实现冲突时，以当前代码、测试和公开接口契约为准。

## 许可证

本仓库当前版本以 [GNU Affero General Public License v3.0](./LICENSE)（`AGPL-3.0-only`）发布。修改后通过网络提供服务时，须按协议向服务用户提供对应源码。

## 贡献者

<p align="center">
  <a href="https://github.com/biubiubiu125">
    <img src="https://github.com/biubiubiu125.png?size=96" width="96" alt="biubiubiu125" />
  </a>
  <br />
  <a href="https://github.com/biubiubiu125">biubiubiu125</a>
</p>
