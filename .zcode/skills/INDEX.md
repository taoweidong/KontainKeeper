# 已安装 Skills 索引（项目级）

> 本目录下的每个子目录（含 `SKILL.md`）是 ZCode 的项目级 skill，仅对本仓库生效。
> 以下 skills 于 2026-09-29 从主流 skill 市场（claudeskills.info 索引的 GitHub 源仓库）按
> 「安装量 / 源仓库权威性 / 与本仓库技术栈（FastAPI + async SQLAlchemy + pytest）匹配度」筛选安装。

| Skill | 来源仓库 | 市场热度（安装量级） | 用途 |
|---|---|---|---|
| `python-patterns` | [affaan-m/ecc](https://github.com/affaan-m/ecc) `.kiro/skills/python-patterns` | ~269K | 现代 Python 模式：Protocol、dataclass、context manager、装饰器、类型注解 |
| `fastapi-patterns` | [affaan-m/ecc](https://github.com/affaan-m/ecc) `.kiro/skills/fastapi-patterns` | ~269K | FastAPI 生产模式：异步 API、依赖注入、Pydantic 模型、安全、可测试性 |
| `python-testing` | [affaan-m/ecc](https://github.com/affaan-m/ecc) `skills/python-testing` | ~269K | pytest/TDD：fixture、mock、参数化、覆盖率策略 |
| `fastapi` | [fastapi/fastapi](https://github.com/fastapi/fastapi) 官方 `fastapi/.agents/skills/fastapi` | ~103K | FastAPI 官方最佳实践（含 SSE/依赖/Pydantic references 分册） |
| `blocking-io-guard` | [bytedance/deer-flow](https://github.com/bytedance/deer-flow) `.agent/skills/blocking-io-guard` | ~83K | 防止 async 事件循环被阻塞 IO 卡死（安装后需按本仓库路径适配其 SOP） |
| `async-python-patterns` | [wshobson/agents](https://github.com/wshobson/agents) `plugins/python-development/skills` | ~40K | asyncio 并发模式：TaskGroup、信号量、超时、后台任务 |
| `webapp-testing` | [anthropics/skills](https://github.com/anthropics/skills) 官方 `skills/webapp-testing` | ~165K | Python Playwright 端到端测试本地 Web 应用（含 with_server.py 服务器生命周期管理） |

## 筛选标准

1. **热度门槛**：市场安装量 ≥ 40K（claudeskills.info 索引 42,244 个 skill 中的头部）。
2. **来源权威**：优先官方仓库（fastapi、anthropics）、知名工程团队（bytedance/deer-flow）与高星社区集合（ecc / wshobson/agents）。
3. **技术栈匹配**：只装 Python 后端工程相关，与本仓库 server 端（FastAPI + SQLAlchemy 2 async + loguru + pytest）直接相关的；未装 Django/爬虫/MCP 等无关项。

## 注意

- 新安装的 skill 在**下一次会话启动时**才会被 ZCode 发现并进入可用列表。
- `blocking-io-guard` 是 deer-flow 仓库的 SOP 原文，其中 `tests/blocking_io/`、`backend/app/` 等路径为其仓库约定；
  借用其「运行时锚点 + 确定性扫描」方法论时需映射到本仓库的 `server/src/kk_server/` 与 `server/tests/`。
