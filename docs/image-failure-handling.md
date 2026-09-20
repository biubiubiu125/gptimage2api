# 图片失败处理

状态：当前

## 单一分类来源

`services/image_failure.py` 中的 `ImageFailure` 是图片执行期间的统一分类对象。它携带失败码、作用域、能力、是否可重试、HTTP 状态、错误类型、原始诊断和对外文案。执行、账号处理和 API 响应使用当前分类结果；调用日志、实时监控和历史尝试则消费其持久化的结构化失败字段，并为旧记录做兼容投影。任何一层都不应再按错误文本重新分类。

## 文本结果与失败

HTTP 400 的图片结果被分类为文本结果：`content_policy_violation`、`invalid_image_input`、`upstream_text_reply`、`conversation_mode_blocked` 和 `unsupported_model`。前三类会保留可展示的上游文本，不将账号切换或账号记失败。`conversation_mode_blocked` 与 `unsupported_model` 对外失败使用四段中文 `对话生图失败【阶段】：原因 原文「原文」`，管理端保留完整明文。额度提交失败、幂等冲突、幂等键/client_task_id 校验、任务确认冲突、请求参数校验、备份恢复、参考图无效、可编辑文件校验对外 HTTP 返回四段中文。`/v1` 未匹配路由 404/405 同样四段；`/v1` 与 `/api/image-tasks*` 的 401/403/429 对外阶段分别为认证失败、权限不足、请求过快。非 `/v1` 仅 `/api/image-tasks*` 走四段，`/api/logs` 等管理端路径保持短中文。`/api/image-tasks` 创建与编辑路径不再把 `ValueError` 原文直接回给调用方；`unsupported image model` 与 `InvalidImageArtifact` 走公开中文。幂等冲突与任务状态冲突的英文原文只留在管理端异常信息里。额度提交失败、队列不可用、幂等冲突和模型不支持时，管理端调用日志保留底层原文，并与对外栏分栏。图片队列资源压力的公开 503 / SSE 和管理端图库维护 503 都只回通用中文，不附带 `resource_cpu` 这类英文 reason。图片任务 SSE 里任务不存在、结果不可用走对应四段中文，未知异常仍走内部错误，原文只进管理端日志。订阅超时仍用短中文心跳，不等于任务失败；/v1 等待超时才走「任务进行中」四段。公开结果不可用四段原文不带产物路径或 checksum，管理端 `raw_error` 仍保留明文；`/v1` 读产物失败、公开 409、SSE 与失败快照一致。交付失败四段同样不把内部 URL、checksum 或英文诊断放进原文。resume-poll 任务不存在走 404「任务不存在」，与 GET / cancel / ack / stream 一致。列表单条产物不可用时隔离该项，不把整页打成 409；该项对外投影为失败和「结果不可用」，不把成功结果留给调用方。公开任务快照只保留 `public_error`，不再把对外文案写入 `error`。公开 `/v1` 心跳不再带英文 `wait_reason`。备份、设置保存和队列表结构校验的管理端可见错误使用「应用数据库」「图片队列存储」等中文名称，不再夹带 `Application Database` / `Image Queue Store`。设置保存冲突、环境变量锁定的公开访问地址、保存失败 500、代理管理、图库存储、提示词来源、账号导入冲突和 GenBox 图库 404 的管理端可见错误同样用中文；设置保存 500 不再把底层 `OSError` 原文拼进 HTTP。图片队列资源压力的异常文本也不再夹带 `resource_cpu`。CPA / Sub2API 导入校验、改账号时 access token 冲突、缺少 access token、代理测试失败和通行状态测试失败的管理端可见错误也用中文；代理测试与通行状态的底层异常只进日志，不拼进 HTTP。提示词来源 `last_error` 有汉字时保留原文，否则用中文兜底，不再回网络英文。

其他 `ImageFailure` 的 `outcome` 是失败，允许执行账号切换与账号验证流程。是否实际切换还取决于账号池、尝试上限和当前设置；分类对象只给出一致的切换资格，不保证一定能找到下一个账号。

常见类别包括：

| 类别 | 典型含义 | 对外状态 |
| --- | --- | --- |
| `auth_invalid` | 上游鉴权无效 | 401 |
| `upstream_rate_limited` / `image_quota_exhausted` | 限流或图片额度耗尽 | 429 |
| `image_poll_timeout` | 等待图片结果超时 | 502 |
| `image_stream_timeout` / `image_stream_interrupted` | SSE 超时或中断 | 502 |
| `image_tool_error` | 上游图片工具终态异常 | 502 |
| `image_download_failed` | 已生成但交付下载失败 | 502 |
| `conversation_mode_blocked` | 当前会话无法调用图片工具 | 400 |
| `no_available_account` | 当前账号池无法选择账号 | 503 |

## 诊断字段

日志和尝试详情区分三种信息：

- **对外错误**：返回给 API 调用方的安全文案。
- **上游错误**：上游结构化错误或异常摘要。
- **上游文本**：上游 assistant 返回的原始可读文本。

结构化 JSON 不会被误当作用户可读文本直接展示。终态 assistant 普通文本会保留为文本结果；当终态 JSON / 结构化字段明确表示图片工具失败时，后端归为 `image_tool_error` 或更具体的失败码。

## 图片任务与对外投影

`ImageTaskService` 持久化的任务状态是 `queued`、`running`、`success` 或 `error`。`/api/image-tasks` 的视图投影会根据原始状态、请求数量、成功数量和失败分类对外呈现 `success`、`partial_success`、`failed` 或 `text_review`。`partial_success` 用于多张请求中已有结果但未全部完成；`text_review` 是文本结果，不是前端把 400 临时改名后的失败。

更改图片失败策略时，先调整 `ImageFailure` 的策略和契约测试，再检查 API、账号切换、持久化诊断、日志、监控和 Studio 投影是否一致。
