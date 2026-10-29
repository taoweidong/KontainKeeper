---
name: kk-e2e
description: 运行 KontainKeeper 真实环境全流程 E2E（WSL 真 Broker + 3 台真 Agent + 真服务端 + Playwright 真浏览器），覆盖页面操作、自更新升级、心跳指标上报、数据采集与文件提取。当用户要求跑端到端/全流程/真实环境验证、排查 E2E 用例失败、或新增端到端用例时使用。
---

# KontainKeeper 真实环境 E2E

一条命令跑完 32 条端到端用例（API 13 / UI 8 / UPG 6 / RES 5），**全程无 mock**：
WSL 内真 Mosquitto + 3 台真 `kk-agent`，Windows 侧真 `kk-server` + 独立 SQLite，
Playwright 驱动真 Chromium 操作真实页面。

完整运行手册（拓扑理由、前置条件、参数、32 条用例明细、踩过的坑与对策、排障）
在 `docs/e2e-real-env.md`——**动手前先读它**，尤其 §4 编排顺序与 §7 已知坑。

## 什么时候用

- 用户要求「跑一遍 E2E / 全流程验证 / 真实环境验证」
- 改了协议、Agent 传输/自更新、命令或采集链路、前端七个业务页中任意一处
- 排障：某条 E2E 用例红了，要先判断是功能缺陷还是环境问题

## 什么时候不用

- 只是跑单元测试门禁 → 直接用 AGENTS.md 里的 pytest / `pnpm test`
- 只改文档、注释、纯样式且不涉及行为

## 怎么跑

全部命令在**仓库根目录**执行（`-m` 形式是硬要求，包内是相对导入）：

```bash
# 全量（含 pnpm build，约 6–10 分钟）
.venv/Scripts/python.exe -m scripts.e2e.run

# 复用 web/dist（只改后端/脚本时，快 2 分钟）
.venv/Scripts/python.exe -m scripts.e2e.run --skip-build

# 只跑某模块（调试最快）
.venv/Scripts/python.exe -m scripts.e2e.run --only UI --skip-build

# 保留栈手工排查（下次运行会自动清残留单元/端口）
.venv/Scripts/python.exe -m scripts.e2e.run --only API --keep

# 看浏览器
.venv/Scripts/python.exe -m scripts.e2e.run --only UI --headed

# 长跑建议加 -u，否则重定向到文件时 stdout 会整块缓冲、看不到进度
.venv/Scripts/python.exe -u -m scripts.e2e.run
```

判定：退出码 `0` = 32 条全 PASS 且无环境级故障；`1` = 有失败或环境故障；`2` = `--only` 模块名非法。

## 产出在哪

```
artifacts-e2e/<run_id>/report.md         # 人读：编号/前置/步骤/预期/实际/结论 + 截图链接
artifacts-e2e/<run_id>/report.json       # 机器读：cases[] 原样 detail + env/meta/汇总
artifacts-e2e/<run_id>/screenshots/      # 每个涉及页面的用例一张 PNG
artifacts-e2e/<run_id>/downloads/        # 命令输出、CSV 导出、自更新二进制等真实落盘文件
artifacts-e2e/<run_id>/kk-e2e.db|agent_assets|server.log|web-build.log
```

`report.json` 的 `meta` 含服务端版本、协议版本、Agent 版本、**Agent 可达基址**、端口顺延说明——
升级类用例失败时先看这里：基址为空 = WSL 访问不到服务端，属环境问题而非功能缺陷。

## 代码地图（`scripts/e2e/`）

| 文件 | 职责 |
| --- | --- |
| `run.py` | 入口与编排顺序（顺序有硬约束，见 doc §4） |
| `setup.py` | Stack：WSL 保活、Broker/服务端/Agent 的起停与 preflight |
| `core.py` | 底座：HTTP、WSL 命令、`Env`、用例登记、等待原语 |
| `api_cases.py` | API-01..13（纯 HTTP） |
| `ui_cases.py` | UI-01..08（Playwright） |
| `upgrade_cases.py` | UPG-01..06（二进制级真自更新） |
| `resilience_cases.py` | RES-01..05（真 kill/真重启） |
| `report.py` | 同时落 JSON 与 Markdown |

## 改用例时必守的规矩

1. **失败要带证据**：断言失败时把接口响应、页面文本、浮层内容、最近若干条 console 一起写进
   `detail`。只留一句「断言失败」等于让下一个人从头复现。
2. **只断言 API 真值**，不写死与环境相关的数字（如磁盘告警数取决于 WSL 根分区使用率）。
3. **页面输入一律走 `_fill_stable()`**，只读断言前先 `_settle()`：命令面板首次进入后约 0.5s
   主内容会被整块重建一次，先写进旧节点的值在 v-model 层会丢（UI-04 的原始根因）。
4. **抽屉/对话框要按内容挑**：命令面板常驻两个 `.el-drawer`，用
   `.filter(has=page.locator("pre.kk-out"))` 之类收窄，否则 strict mode violation。
5. **不新增 mock / skip**：E2E 的价值全在「真组件拼起来还能干活」。环境缺件就让 preflight 明确失败，
   并在报告里如实记录，不假装通过。
6. **用例顺序有依赖**：`API → UI → UPG → RES`（UI-08 依赖「还没有待分发版本」；RES 依赖 UPG 钉回 0.4.0）。
   新增跨模块前提必须写进 doc §5 末。扰动过机队的用例（如 UPG-06 重启主机）收尾必须等该主机
   `online=true` 再交棒，否则下一个模块的基线不成立，红灯会指向错误的位置。
7. **Windows 侧生成的 fixture 要防换行符污染**：凡是要喂给 WSL 当可执行文件的文本（如自更新启动器），
   必须 `write_text(..., newline="\n")`——默认会把 `\n` 翻成 CRLF，`#!/bin/sh\r` 会让 `execv` 报 ENOENT，
   症状是「Agent 升级失败」，实际是测试夹具坏了。
8. **取响应头一律用 `core.hdr()`**：uvicorn 发的是小写头，`headers.get("Content-Disposition")` 恒为 None。

## 与其他门禁的关系

E2E **不替代** AGENTS.md 的提交门禁。提交前仍必须：后端
`pytest agent/tests server/tests` 的 failures/errors 为 0、skipped 恰好 5（解析 junitxml，不许 `| tail`），
前端 `pnpm typecheck && pnpm test` 全绿。E2E 是端到端功能的最终裁决。