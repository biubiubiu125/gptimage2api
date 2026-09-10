# 存储架构

状态：当前

## 核心边界

结构化控制面数据共享一个 **Application Database**，默认位于
`data/gptimage2api.db`，也可以通过 `DATABASE_URL` 使用 PostgreSQL 18。同一个
物理数据库不等于同一个 Repository：业务规则、事务边界和读写 Interface 仍由
各领域 Module 独立拥有。持久化图片任务使用独立的
`gptimage2api_image_queue` PostgreSQL 数据库，不与 Application Database 混用。

| 数据 | 当前所有者 | 主要持久化位置 / 适配器 |
| --- | --- | --- |
| 上游账号与 User Key | Account Repository | Application Database |
| 系统配置与代理设置 | `ConfigStore` 的配置 Repository | Application Database |
| 调用日志 | `LogService` 的 Call Record Repository | Application Database |
| 概览指标 | `DashboardMetricsService` 的 Dashboard Metric Projection | Application Database 中的状态、小时和模型小时三张表，保留 30 天 |
| 提示词、远程导入和清理协调状态 | 各自领域 Repository | Application Database |
| Editable File Task 元数据 | `EditableFileTaskService` 的任务 Repository | Application Database |
| 图片任务 | `ImageTaskService` 与 `services/image_queue/` | Image Queue Store：`gptimage2api_image_queue` |
| 图片索引与删除恢复状态 | `ImageStorageService` | `data/image_index.json`，实际图片可在本地或 WebDAV |
| 图片标签和缩略图 | 图片领域 Module | `data/` 下的图片关联文件 |

## 数据库选择与切换

`DATABASE_URL` 选择 Application Database；未设置时使用本地 SQLite。队列通过
`GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL` 单独配置，生产部署应使用 PostgreSQL。
数据库 URL、连接池参数和 `GPTIMAGE2API_AUTH_KEY` 属于启动基础设施配置，不存入应用设置。

切换数据库不会自动导入旧 JSON、JSONL、Git 或旧账号 SQLite 数据，也不会双写。
目标数据库一经选中即为唯一事实来源，空库按当前 Schema 初始化。Schema 升级与
应用数据导入是两类不同操作，不能由启动流程静默混在一起。

## 访问与并发规则

- 账号写入必须通过账号服务和 Account Repository。
- 日志分页、筛选、删除和保留策略通过 Call Record Repository 执行。
- Dashboard 正常同步只读取状态序号之后新增的 Call Record，并原子累加受影响的小时行；用户在日志页删除记录和自动保留清理不会旋转 Call Record 游标，因此不收缩已经形成的 30 天投影；账号卡片、当前并发和运行环境不建立 Dashboard 持久化副本。
- 所有领域 Repository 共享一个进程级 Engine，但不共享可变业务状态。
- SQLite 使用 WAL、外键和 busy timeout；PostgreSQL 使用有界连接池。
- `ImageStorageService` 统一图片保存、删除、压缩、清理和同步；调用方不能直接改图片索引。
- 图片删除先记录可恢复的删除状态，再执行本地或 WebDAV 操作，避免中断后旧操作误删新版本。

备份需要同时覆盖 Application Database、Image Queue Store 与图片/文件资产位置。
只备份数据库不能恢复本地或 WebDAV 图片和生成文件；只备份 `data/` 也不能恢复
外部 PostgreSQL 或队列数据库。
内置 R2 备份始终生成一致的数据库快照：SQLite 归档为
`data/application-database.sqlite3`，PostgreSQL 归档为
`data/application-database.pgdump`。PostgreSQL 部署镜像包含 `pg_dump`。启用图片资产
备份时，归档还会包含本地图片以及图片索引中登记的 WebDAV-only 图片；未登记的
WebDAV 对象不属于 Application Database，仍由部署方独立备份。
