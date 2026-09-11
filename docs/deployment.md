# 部署与升级

状态：当前

发布镜像为 `ghcr.io/biubiubiu125/gptimage2api`。标准 Compose 将服务暴露在 `3000` 端口，使用
`gptimage2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地
`data/` 和 `config.json`。受管容器控制台从 GitHub Release 下载 `gptimage2api-app.tar.gz`
后可一键更新。图片任务必须配置独立 PostgreSQL 队列库；运行时配置和
数据不应提交到 Git。

## Docker 部署

```bash
# 当前目录应为 gptimage2api 工作区
cp .env.example .env
# 将 .env 中的 GPTIMAGE2API_AUTH_KEY 和 POSTGRES_PASSWORD 替换为私有值。
test -f config.json || printf '{}\n' > config.json
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

镜像中的 `/opt/gptimage2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，并复制镜像内已经验证过的 Python 虚拟环境；正常重启不会联网同步依赖。业务数据始终留在独立的 `data/` 挂载中。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后由一次性 `image-queue-init` 服务幂等创建队列数据库，再启动应用，并将数据持久化到 `gptimage2api-postgres-data` 命名卷。Application Database 固定为 `gptimage2api_app`，Image Queue Store 固定为 `gptimage2api_image_queue`；PostgreSQL 健康检查只负责报告可用性，不执行写入操作。数据库端口默认不暴露到宿主机。应用、PostgreSQL 和队列初始化都加入独立网 `gptimage2api-network`，不和其他 Compose 项目共用默认桥接。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。数据库名不可通过 `POSTGRES_DB` 改名。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
curl http://localhost:3000/health
```

`.env` 中的 `GPTIMAGE2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

一键安装向导固定使用 PostgreSQL 18 本地容器，不再询问 SQLite 或外部数据库 URL，也不再询问 Git 分支。直接回车使用默认值，并立刻打印已选项。管理员登录密钥必须手工输入两次，输入过程隐藏。摘要确认后才开始拉镜像或克隆。Docker 模式启动应用与 PostgreSQL；Python 源码模式仍会启动 PostgreSQL 容器，并把 `127.0.0.1:5432` 映射到宿主机，不对外网开放。若安装目录已是旧 Git 仓库且无法快进到新的 `main`，需要备份后删除该目录再装。重复运行会复用已有 `POSTGRES_PASSWORD`。`GPTIMAGE2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量；图片任务另由 `GPTIMAGE2API_IMAGE_QUEUE_GENERATION_CONCURRENCY` 控制。

## 本地开发

后端：

```bash
# 当前目录应为 gptimage2api 工作区
# 必须先准备 PostgreSQL，并填写独立的 Image Queue Store 连接：
export GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL='postgresql://user:password@localhost:5432/gptimage2api_image_queue'
uv sync
uv run main.py
```

Application Database 可以使用本地 SQLite；Image Queue Store 不能回退到
SQLite，未配置可用的 PostgreSQL 队列库时，应用会拒绝启动，避免把异步图片任务
误当成内存任务或文件任务。

图片协议请求优先使用客户端提供的 `Idempotency-Key`、`X-NewAPI-Request-Id`、
`X-OneAPI-Request-Id` 或 `client_task_id`。如果这些字段都没有，服务端会生成
`request:<uuid>` 形式的请求标识并通过响应头返回；需要跨重试复用同一任务时，应保存
并主动发送该标识。

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/gptimage2api.db`。图片任务由 `GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL` 选择独立
PostgreSQL 队列库，不能与 Application Database 混用。Application Database 支持
SQLite 与 PostgreSQL 18；队列库只支持 PostgreSQL，不再通过 `STORAGE_BACKEND` 选择
JSON、Git 或账号专用数据库。

选择数据库不会自动导入 JSON、JSONL、Git 或旧账号 SQLite 文件。需要从旧
`chatgpt2api` checkout 迁移时，先停止旧实例并备份旧数据，再在新项目根目录执行
一次迁移命令；迁移脚本只读旧目录，不会修改旧项目：

```bash
python scripts/migrate_legacy_data.py /path/to/chatgpt2api/data \
  --database-url "$DATABASE_URL" \
  --target-data-dir data
```

脚本会导入账号、User Keys、四种受支持 provider 的注册配置、邮箱运行时状态，以及
`images/`、`files/` 和 `image_tags.json`（目标存在且内容不同会直接失败，不会覆盖）。
不会迁移活动图片任务、租约、Worker 或 Cluster 状态。图片文件及其相关索引仍按图片
存储边界管理。完整边界见 [`storage-architecture.md`](storage-architecture.md)。

### 注册配置首次导入

如果 `data/register.json` 存在，首次创建 Application Database 注册配置时会将其中
的四种受支持 provider 导入数据库，并丢弃旧 provider 与运行时租约。使用上方迁移脚本
时，注册配置会在账号写入前完成加密预检；导入完成后，
数据库是唯一事实来源；之后再修改 `data/register.json` 不会覆盖数据库中的配置。
`outlook_token_used.json`、`register_core_results_pending.json` 和
`remail_dead_mailboxes.json` 仍是注册运行时兼容状态，升级和备份时要一并保留。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`；启用 Image Queue Store 备份时还会包含
`data/image-queue.pgdump`。注册配置在 Application Database 内，邮箱池运行时状态
按备份设置选择；启用“图片资产”时，图片索引中登记的 WebDAV-only 图片也会下载
到归档。未登记的 WebDAV 对象仍需由部署方独立备份。

备份归档可能包含账号凭据、注册 provider 凭据和队列任务数据。生产环境应在系统
设置中启用备份加密并设置强口令；加密口令不会写入日志、API 响应或备份元数据。

在系统设置的备份历史中可以直接恢复归档。恢复前会校验归档版本、数据库类型、
路径和归档内容；恢复会覆盖归档中包含的 Application Database、Image Queue Store
以及文件/图片资产，完成后必须重启服务。在恢复期间不要提交新的图片任务或修改
系统设置。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/gptimage2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/gptimage2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/gptimage2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

升级已有 PostgreSQL 数据卷时，先确认 `gptimage2api_app` 和
`gptimage2api_image_queue` 两个数据库均存在；PostgreSQL overlay 的健康检查会在
权限允许时自动创建缺失的队列库。如果健康检查持续失败，应使用 PostgreSQL 管理员
权限手动创建队列库，再重新执行上述启动命令。

镜像部署固定或回退版本时，在 `.env` 设置
`GPTIMAGE2API_IMAGE=<published-image>:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f gptimage2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。
