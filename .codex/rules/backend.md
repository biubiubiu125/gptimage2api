# 后端规则

## 分层职责

- `api/` 负责 HTTP 鉴权、输入解析、状态码和调用领域 Module，不复制领域规则。
- `contracts/` 负责稳定的输入、输出和展示投影；字段含义必须由契约测试固定。
- `services/` 负责领域行为、编排、状态转换和操作结果。
- `repository/` 与 `services/storage/` 作为持久化 Adapter，不向页面泄漏存储细节。
- 配置默认值、约束和运行时解析应从一个规范 Module 派生，不能维护多份漂移规则。

## 业务投影

- Upstream Credentials 的 AT/RT 状态、账号状态、额度结果、代理结果、任务状态、Call Record 诊断和批量事件都由后端生成。
- 批量操作返回可直接渲染的摘要和事件。前端不得在缺少事件时补造“成功、失败、删除”等业务文案。
- 单项和批量写入共享同一规范化与状态转换 Implementation；批量路径控制保存次数和并发，不能复制规则。
- Refresh Token 刷新 Access Token，与账号/额度同步是不同操作；命名、设置和事件不得混用。
- Proxy Reference 只有 `direct`、`group`、`custom`；继承是没有显式选择，不是第四种模式。Proxy Session 不提供默认出口。

## 并发和一致性

- 共享并发规则必须有一个权威实现，管理投影与实际执行不得分别限制。
- 远程批处理使用有界并发、超时和取消；本地原子批量修改不得机械拆成多次持久化。
- 锁内只做需要原子性的状态修改；网络和长耗时工作不得无理由持锁。
- 异步任务必须定义 queued、running 和终态、所有权、幂等、清理及进程重启后的行为。
- 写入后若还有缓存、索引或运行时状态，必须由拥有该不变量的 Module 一起收口。

## 安全

- 所有外部 URL、重定向和下载都必须限制协议、地址、响应大小、超时和重定向次数。
- SSRF 校验必须约束实际连接地址，不能只校验连接前的一次 DNS 结果。
- 上传与 base64 输入必须限制单项大小、数量和总大小，避免先完整读入内存后再判断。
- 路径安全、资源所有权和公开资产规则集中在拥有存储的 Module 内，不能在多个调用者复制。
- 管理端日志、账号详情、调用诊断、设置投影和 `/api/proxy/runtime` 保留明文凭证；对外 OpenAI / 公开队列错误仍剥离 Bearer、签名 URL 和本站管理员密码，形状保持 `Authorization: Bearer [redacted]`。`quota_unavailable` 阶段用「收口验活」。
- 注册给人看的失败四段阶段必须是中文，不能露出状态机英文 `failed`、`code_wait` 等；机器阶段 `account_create` 展示为「创建账号资料」，`token_exchange` 展示为「Token 换取」。原因必须是完整中文，带 URL、`continue=`、OAuth 或英文详情的混合串只进「原文」。缺回调即使原文带 `/oauth/` 也是「账号创建失败」，不能被通用 oauth 标记抢成授权失败。短串 `/backend-api/me 403` 仍是验活被拦。成功 payload 的 `stage`、失败 payload 的 `failed_from_stage`、暂存结果的 `register_stage` 也要中文。微软邮箱不支持无密码登录记成授权失败，阶段「继续授权」。`login_continue` HTTP 失败阶段固定「继续授权」，不能被状态机 `code_wait` 盖成「验证码等待」。Microsoft 登录的 `authorize/continue` 与 `passwordless/send-otp` 遇真挑战页必须先走 Cloudflare 检测/刷新，不能只抛 `login_continue_http_*` / `passwordless_send_otp_http_*`。微软无密码登录换 Token 失败（`微软无密码 Token 换取失败` / `token换取失败`）阶段固定「Token 换取」，即使 live 状态机还停在 `code_wait` 也不能写成「验证码等待」；这条路径必须在调用 `exchange_tokens_from_continue_url` 之前切到 `token_exchange`。`platform_authorize_http_*` / `authorize_continue_http_*` 和 `token换取失败` 即使 debug 带 `cf-ray` 头，仍分别是授权失败和 Token 换取失败。收口验活 `/backend-api/me` 403 即使原文带 `cf-ray` 头仍是验活被拦。只有 Just a moment / `cf-chl-` / Enable JavaScript and cookies to continue 这类真挑战才记 Cloudflare 拦截，真挑战页优先于 HTTP 前缀。拿到 token 后暂存、邮箱回写、额度 0 或暂存清理失败只黄字警告，必须 `format_log(kind="warning")` 前缀「注册警告」，仍继续入库。

## Python 变更

- 需要回归时，在 Git 忽略的本地 `tests/` 中补充或修改覆盖公共 Interface 的测试；测试源码不纳入提交。
- 新增生产文件后确认它被 Git 跟踪，并检查所有导入在干净工作区可用。
- 不向已弃用的 `test/` 目录新增测试；测试入口以 `pyproject.toml` 的 `testpaths` 为准。
- 修改公共契约、设置、持久化或并发时，先运行针对性测试，再运行完整 Python 测试。
