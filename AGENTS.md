# AGENTS.md

本文件约束 `gptimage2api` 仓库中的 AI 和自动化开发行为，作用域为整个仓库。
本仓库把 ChatGPT 官网能力接入 OpenAI 兼容 API，并提供账号池、注册机、图片任务与自托管控制台。
用户当前指令优先；更深目录若存在 `AGENTS.md`，则在其作用域内补充或覆盖本文件。
领域术语和所有权以 `CONTEXT.md` 为准；可执行细则在 `.codex/rules/`；部署与运维步骤在 `docs/deployment.md` 和 `docs/runbooks/`，不写进本文件。

## 开始任务前

1. 先读取本文件，再完整读取与任务相关的 `.codex/rules/` 文件。
2. 先执行 `git status --short`，识别用户已有改动、未跟踪源码、生成物和两个仓库的独立状态。
3. 先读相关实现、测试、`CONTEXT.md`、接受的 ADR 和当前文档，以当前代码为准；不允许用以前的修复记忆、过期计划或旧仓库文档代替取证。
4. 非简单修改必须先分析，建立只覆盖当前任务的短期计划，向用户同步后再编辑，并在工作过程中更新状态。
5. Review、分析、解释和方案请求默认只读，直接开始；结束后按严重程度给出带文件行号的问题清单，不先确认、不顺手改代码。用户明确要求修复或实现后才可写代码。

过期计划不是事实来源。不要创建长期执行计划文件，除非用户明确要求；当前任务的计划保留在任务状态和对话中即可。

## 规则分发

| 任务范围 | 必读规则 |
| --- | --- |
| 所有任务 | `.codex/rules/workflow.md` |
| 架构、契约、跨层重构、存储 | `.codex/rules/architecture.md` |
| `api/`、`contracts/`、`services/`、`repository/`、Python 运行时 | `.codex/rules/backend.md` |
| `web-vue/`、页面、交互、样式、Nanocat 接入 | `.codex/rules/frontend.md` |
| 文档、PRD、架构地图、runbook、外部参考 | `.codex/rules/documentation.md` |
| Review、测试、回归、验收 | `.codex/rules/testing-and-review.md` |
| 暂存、提交、推送、版本、Nanocat 发布 | `.codex/rules/git-and-release.md` |

跨多个范围的任务必须读取全部相关规则，不能只读其中一个。

## 永久约束

- 保留用户已有修改；不回滚、不覆盖、不顺手整理无关文件。
- 不因“顺便优化”扩大任务范围。发现范围外问题时记录并汇报，不直接修改。
- 发生冲突时按当前代码、测试和公开契约，再 `CONTEXT.md`，再 accepted ADR，再 current 文档判断；计划只表示意图。
- 不在没有明确授权和新 ADR 的情况下改变持久化所有权或统一数据库。Application Database 与独立 PostgreSQL 图片队列库不得混成一个通用仓库。
- 后端输出业务语义和稳定 JSON 投影；Vue 负责传输校验、交互状态、布局和渲染。这不是后端 HTML 渲染。
- 通用 UI 能力优先评估 Nanocat；产品业务、页面组合和仅单页使用的实现留在本仓库。
- `D:\nanocat` 是独立 Git 仓库。两个仓库分别检查、测试、提交和发布。
- 不执行 `git push`、创建发布、打 tag 或 `npm publish`，除非用户明确授权该动作。
- 公共生图必须进入独立 PostgreSQL 图片队列，由 `ImageTaskService` 拥有生命周期；不得绕过队列直打上游。
- 出站 ChatGPT 浏览器身份以 `contracts/chrome146.json` 和 `services/browser_fingerprint.py` 为唯一来源；不得另写 User-Agent、Client Hints 或 impersonate。
- Cloudflare 清关只接受手动 Chrome146 `cf_clearance`，不得把 FlareSolverr 或其他浏览器 TLS 的 cookie 接到 chrome146 会话。
- 需要跑测试时用 WSL 里本仓库的 `.venv`，不要用宿主机 Python 直接下结论。`tests/` 只用于本地验证，不纳入提交。
- 用户反复纠正且具有长期价值的要求，应写入最相关规则，内容必须明确、可执行且不重复。
