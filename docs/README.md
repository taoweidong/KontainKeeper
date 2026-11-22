# KontainKeeper 文档索引

**从这里开始 → [项目手册 manual.md](manual.md)**：综合指南，覆盖项目概述、快速开始、本地开发、部署、
详细使用手册（Web 界面逐页）、客户端/服务端配置与 API 参考、二次开发、运维排障。

## 现行指南（行为依据）

| 文档 | 定位 |
|---|---|
| [manual.md](manual.md) | **项目手册**（综合入口） |
| [design.md](design.md) | 总体架构设计（含 QR1125 双通道说明） |
| [development.md](development.md) | 开发环境搭建（uv/pnpm/Broker/WSL PG18/测试） |
| [deployment.md](deployment.md) | 生产部署权威指南（compose/离线/PG18/TLS/安全自检/Agent 部署） |
| [design-transport.md](design-transport.md) | QR1125 上报/采集机制 redesign（HTTP push + SSE，Phase 0 已实施） |
| [ci-jenkins.md](ci-jenkins.md) | CI/CD 流水线（Jenkins） |
| [e2e-real-env.md](e2e-real-env.md) | 真环境 E2E 操作手册（WSL + 真 Broker + 真 Agent + Playwright） |
| [../proto/messages.md](../proto/messages.md) | 双端协议契约 v4（含 §8 HTTP/SSE 传输绑定） |
| [../AGENTS.md](../AGENTS.md) | 面向 AI 协作的工程约束（承重红线） |

## 历史账本 / 评审（按日期参考，非行为依据）

- 架构与代码评审：`architecture-review.md`、`code-review-2026-09-06.md`
- 质量评估：`quality-assessment-2026-09-12.md`、`quality-assessment-2026-11-03.md`、`quality-review-*.md`
- 优化/修复方案：`optimization-plan-2026-09-12.md`、`service-optimization-plan.md`、`remediation-plan-*.md`、
  `frontend-optimization-plan-2026-10-07.md`、`frontend-layout-analysis-2026-10-15.md`
- 路线图：`completion-plan-mqtt.md`、`v4-evolution-plan-hosts-docker.md`、`mqtt-transport-assessment-2026-09-13.md`
- E2E 历史快照：`e2e-real-env-test-2026-09-13.md`
- 设计源头：`superpowers/specs/*`、`superpowers/plans/*`

> 文档与代码冲突时，以代码与 `git` 历史为准。最新特性口径：QR1124 动态 IP 白名单、
> QR1125 Phase 0 HTTP/SSE 双通道、QR1126 PG18 调优。