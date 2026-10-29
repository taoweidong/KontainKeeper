# 真实环境全流程 E2E（WSL + 真 Broker + 真 Agent + 真浏览器）

一条命令跑完 KontainKeeper 的端到端链路，**不使用任何 mock**：

- 真 Mosquitto（WSL 内独立实例，独立端口与持久化目录，可单独重启）
- 真 `kk-agent` 二进制 × 3（WSL 内 systemd 临时单元常驻，真 MQTT 上行/下行）
- 真 `kk-server`（Windows 侧独立进程 + 独立 SQLite + 托管真实 `web/dist`）
- 真 Chromium（Playwright 驱动真实页面，非 headless DOM 伪断言）
- 真文件落盘（命令输出下载、四类 CSV 导出、自更新二进制上传/下载）

代码在 `scripts/e2e/`，运行产物（截图/下载/DB/日志）落在 `artifacts-e2e/<run_id>/`（已 gitignore）。

> 历史背景与首版实施记录见 `docs/e2e-real-env-test-2026-09-13.md`；本文件是**可复用的运行手册**。

## 1. 为什么是这种拓扑

| 决策 | 原因 |
| --- | --- |
| Broker/Agent 在 WSL，服务端在 Windows | Agent 是 Linux 目标产物（psutil + paho、systemd 管理），服务端开发体验在 Windows；两边只靠 MQTT 与 HTTP 交互，天然可拆 |
| 专用 Broker 端口（默认 18831）+ 专用 topic 前缀 `kk/e2e` | 与开发机常驻的 1883、CI 的 18830 全部错开；前缀隔离避免与本地真实 Agent 抢主题 |
| 服务端独立 SQLite（`artifacts-e2e/<run_id>/kk-e2e.db`） | 每轮从零开始，`/api/containers` 数量可硬断言，不污染开发库 |
| 服务端监听 18443（可顺延） | 避开 8443 等常用开发端口；`pick_port()` 发现占用会自动顺延并把实际端口写进报告 |
| WSL 挂一个 `sleep` 保活进程 | WSL 发行版在没有前台进程时会自动关机，Broker/Agent 随之全灭（`Stack.start_keeper/stop_keeper`） |

端口顺延这件事有个坑：`Env.__post_init__` 里算的是**默认**端口，顺延后必须重算 `env.base`，否则服务端起来后所有轮询都打在一个没人监听的地址上直到超时。

## 2. 前置条件

Windows 侧：

- 仓库根有 `.venv`（`uv sync --all-packages`），且 `scripts/e2e` 用它跑
- Node + pnpm 可用（`where pnpm` 第一行若是无扩展名的 sh 脚本，`CreateProcess` 起不来，`core.pnpm_exe()` 会挑 `.cmd`/`.exe` 那一行）
- Playwright Chromium 已安装（`python -m playwright install chromium`）
- `web/mock/` 目录存在（`pnpm build` 的硬要求，空目录即可）

WSL 侧（默认 `Ubuntu-22.04`，可用 `--distro` 换）：

- `mosquitto` 可执行文件（`mosquitto -h` 能跑）
- 能用 systemd（`systemctl --user` 不需要；脚本用**系统级** transient unit，需要 root，脚本内部走 `wsl -u root`）
- `python3` 与 `psutil`/`paho-mqtt`（Agent 依赖，`preflight` 会实测 import）

`preflight` 会把上面每一项都实测一遍并打印结果；硬项不过直接中止并把原因写进报告，不会带着残缺环境继续跑。

## 3. 运行

```bash
# 全量（含 pnpm build，约 6–10 分钟）
.venv/Scripts/python.exe -m scripts.e2e.run

# 复用已有 web/dist（只改了后端/脚本时用这个，快 2 分钟）
.venv/Scripts/python.exe -m scripts.e2e.run --skip-build

# 只跑某些模块（调试单条用例最快）
.venv/Scripts/python.exe -m scripts.e2e.run --only UI --skip-build

# 结束后保留栈以便手工排查（下次运行会自动清掉上一次的残留单元/端口）
.venv/Scripts/python.exe -m scripts.e2e.run --only API --keep

# 看浏览器（默认 headless）
.venv/Scripts/python.exe -m scripts.e2e.run --only UI --headed
```

**必须用 `-m` 形式**：包内是相对导入，`python scripts/e2e/run.py` 会在 `from . import ...` 处 ImportError。

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--distro` | `Ubuntu-22.04` | WSL 发行版 |
| `--port` | `18443` | 服务端端口（占用则顺延） |
| `--broker-port` | `18831` | 专用 Broker 端口（占用则顺延） |
| `--skip-build` | 关 | 跳过 `pnpm build` |
| `--headed` | 关 | 浏览器带界面 |
| `--keep` | 关 | 保留 Broker/服务端/Agent/保活会话 |
| `--only` | 全部 | `API,UI,UPG,RES` 逗号分隔 |
| `--root-wsl` | `$HOME/kk-e2e` | WSL 侧工作根目录 |

退出码：`0` = 全部用例 PASS 且无环境级故障；`1` = 有失败或环境故障；`2` = `--only` 传了未知模块。

## 4. 编排顺序（有硬约束）

`run.py` 的步骤顺序不是随手排的：

1. **先钉住 WSL 实例**（`start_keeper`）：preflight 里几次 `wsl.exe` 调用之间也有空窗，实例一旦被回收，后面 systemd 起的 Broker/Agent 全部作废。
2. **preflight 实测**：`.venv` Python、发行版、mosquitto、Agent 依赖、Chromium、`web/mock`、pnpm。
3. **构建前端**：`pnpm build` → `web/dist`，服务端直接托管它（`KK_WEB_DIR`）。产物不自动同步进 `server/src/kk_server/web/`，E2E 也不需要同步。
4. **起 Broker**（WSL，专用端口/前缀/持久化）。
5. **服务端先以空 `KK_PUBLIC_URL` 起来 → 实测 WSL 可达基址 → 重启一次注入**。Agent 的升级下载地址由服务端在下发帧里给（`bridge._download_url()`），读的是启动时配置；而「WSL 能否访问 Windows 的这个端口」取决于 WSL 网络形态（mirrored 可直连 127.0.0.1，NAT 必须走默认网关），只能实测。探测失败不中止，但升级用例会如实失败并在报告里记录 `Agent 可达基址=探测失败`。
6. **登录**（admin/admin123，独立库的首启默认）。
7. **部署并启动 3 台 Agent**（WSL 内 `wsl-agent-01/02/03`，`KK_INTERVAL=3`）。
8. **等机队就绪**：既要求 `online=true`（status 帧），又要求 `metrics.mem_mb` 非空（首帧心跳）。只看 status 会在指标为空时误判——这是 AGENTS.md 明示的坑。
9. **按 API → UI → UPG → RES 顺序跑模块**（顺序有依赖，见下节）。

失败语义：**单条用例失败不中断**（报告要一次看全）；只有环境级故障（preflight 硬项、栈起不来、机队超时）才中止，并把栈写进报告而不是只留 traceback。

## 5. 用例清单（32 条）

### API（13）— 后端全链路，纯 HTTP

| 编号 | 覆盖 |
| --- | --- |
| API-01 | 健康检查：服务端就绪 + Broker 已连接 + 3 台 Agent 在线 |
| API-02 | 鉴权：错口令 401 / 无 token 401 / 正确登录可读身份 |
| API-03 | 机队视图：3 台在线 + v4 元信息（分组/协议/IP/能力）齐全 |
| API-04 | **数据上报**：心跳指标持续更新（等 N 秒看 `last_seen` 前进）+ 标签落库 |
| API-05 | 筛选：关键字/分组/协议/仅在线/非法视图参数 |
| API-06 | 主机详情 + 指标序列（升序、hours 下限钳到 1）+ 404 语义 |
| API-07 | **数据采集**：5 个采集项 × 3 台全部成功 |
| API-08 | Shell 命令执行与输出提取（text / base64 / 非法格式） |
| API-09 | 安全红线：黑名单拦截 / 非法采集项 / 非法 kind / 不存在主机 / 空命令 |
| API-10 | 命令检索面：按批次/主机/状态/关键字、批次汇总、单条与 404 |
| API-11 | 审计：登录/下发/拦截全部留痕，可按动作与操作者筛选 |
| API-12 | **文件提取**：四类 CSV 导出（BOM + 中文文件名 + 落盘）+ 上限/必填校验 |
| API-13 | 系统统计：主机数 / 心跳明细行数 / Broker 连接 / 命令积压 |

### UI（8）— Playwright 真实页面

| 编号 | 覆盖 |
| --- | --- |
| UI-01 | 登录页：错口令被拒、正确登录进入总览（`#/welcome`） |
| UI-02 | 主机总览：读数带 / 关键字筛选 / 勾选 |
| UI-03 | 主机详情：曲线与指标区块（磁盘/网卡/最近命令） |
| UI-04 | **命令面板**：批量下发 2 台 + 结果抽屉含输出 + 下载输出文件 |
| UI-05 | **采集面板**：默认采集项 3 台下发 |
| UI-06 | 审计日志：严重度圆点 / 关键字 / 导出 CSV |
| UI-07 | 系统统计：链路状态与计数器 |
| UI-08 | **版本与更新**：空态 / 上传校验 / 回滚禁用 |

### UPG（6）— 真实自更新（二进制级）

| 编号 | 覆盖 |
| --- | --- |
| UPG-01 | 上传/回滚负例：无二进制不升级、非法版本/空文件/超 64MB 均被拒 |
| UPG-02 | 上传 0.4.0：清单 sha256/size 可核验、**下载字节一致**、版本切换 |
| UPG-03 | 推送升级 0.4.0：下载 → sha256/HMAC → 原子替换 → execv → 新版上线 |
| UPG-04 | 升级幂等与跳过原因：已最新 / 主机不存在 / 版本号非当前 / 空名单 |
| UPG-05 | 回滚待分发版本：两次互换回到原点，下载内容随版本切换 |
| UPG-06 | 升级失败留痕：替换目标不是独立二进制 → 台账 failed/bad_target + 审计 |

### RES（5）— 韧性（真掐真重启）

| 编号 | 覆盖 |
| --- | --- |
| RES-01 | 掉线判定：`kill -9` 后遗嘱 offline 生效，在线数/筛选/健康检查同步 |
| RES-02 | **离线排队补投**：Agent 离线时下发 → queued 不发帧，重连后补投并升态 |
| RES-03 | Broker 重启：REST 不受影响，3 台 Agent 自动重连（进程未重启） |
| RES-04 | 服务端重启：旧会话仍有效、数据不丢、心跳续传、伪造 token 仍 401 |
| RES-05 | 优雅退出：SIGTERM 由 Agent 自报 `reason=stopping`（区别于 LWT 空 reason） |

### 模块顺序的依赖

- **UI-08 断言「还没有待分发版本」**，UPG 上传 0.4.0 后这条前提就没了 → UI 必须在 UPG 之前。
- **RES 需要 UPG 之后的状态**：拿「已钉回的 0.4.0」与「尚未升级的主机」演离线排队与幂等。
- 所以顺序是硬编码的 `API → UI → UPG → RES`，用 `--only` 单跑 UPG/RES 时要注意这是「跨模块前提被跳过」。

## 6. 产物与报告

```
artifacts-e2e/<run_id>/
  report.json        # 机器读：cases[] 原样保留 detail，含 env/meta/汇总
  report.md          # 人读：编号/用例/前置/步骤/预期/实际/结论 + 截图相对链接
  screenshots/       # 每个涉及页面的用例一张 PNG
  downloads/         # 命令输出、CSV 导出、自更新二进制等真实落盘文件
  kk-e2e.db          # 本轮独立 SQLite（可直接翻表核对）
  agent_assets/      # 服务端侧自更新资产（上传/缓存的二进制）
  web-build.log      # pnpm build 的完整输出
  server.log         # 服务端 stdout（KK_LOG 留空，日志在这里）
```

Agent 侧日志在 WSL 的 `$HOME/kk-e2e/logs/<host>.log`（`KK_LOG`），Broker 日志在
`$HOME/kk-e2e/broker/mosquitto.log`。

报告必须能**自证**「这次跑的是哪套代码、哪个库」：`meta` 里写入服务端版本、协议版本、Agent 版本、Agent 可达基址、端口顺延说明。翻旧报告时据此判断结论是否还成立。

## 7. 已知坑与对策（都踩过）

| 现象 | 根因 | 对策 |
| --- | --- | --- |
| 填了命令行却弹「请填写命令行」且无 POST | 命令面板（shell/collect）首次进入后约 **0.5s** 主内容被整块重建一次（实测输入框 517ms REMOVE+ADD、复选框 705ms），`shellForm` 重新初始化，先写进旧节点的值在 v-model 层已丢 | `ui_cases._fill_stable()`：写值后等观察窗，用 DOM stamp 校验「元素没被换掉 且 值还在」，否则重写；只读断言用 `_settle()` |
| `strict mode violation: .el-drawer resolved to 2 elements` | 命令面板常驻两个抽屉（HostPicker「选择目标主机」+ 输出抽屉） | 用 `filter(has=locator("pre.kk-out"))` 按内容挑 |
| 第二轮报 `Unit kk-e2e-broker.service already exists` | `--keep` 留下的是**运行中**的 transient unit，`reset-failed` 只清失败态 | `start_broker`/`_run_unit` 先 `systemctl stop ... || true` 再 reset-failed |
| 第二轮报端口被占 | 同进程内 `restart_server` 停旧进程需要几秒 | 端口守卫先等最多 10s 释放再报错 |
| `FileNotFoundError: [WinError 2]` 起 pnpm | `where pnpm` 第一行是无扩展名 sh 脚本，CreateProcess 不认 | `core.pnpm_exe()` 取 `.cmd`/`.exe` 行 |
| `UnicodeDecodeError: 'gbk' codec` | Windows 子进程默认按 GBK 解码，pnpm/utf-8 输出直接炸 reader 线程 | 所有 `subprocess.run(text=True)` 钉 `encoding="utf-8", errors="replace"` |
| WSL 里 curl 不到服务端 | NAT 形态下 127.0.0.1 不通，要打默认网关 | `detect_public_url()` 两个候选都实测，都失败则如实记录而非假装通过 |
| 磁盘告警数恒为 3 | WSL 根分区使用率超过 `DISK_ALERT_PCT=85`，告警是**真实的** | 断言以 API 真值为准，不写死 0 |
| UPG-03 台账 `failed/exec_error`，且 RES-01/02/03 连锁红灯 | 上传给服务端的启动器由 `Path.write_text()` 在 Windows 生成，默认 `newline=None` 写出 **CRLF**；下到 WSL 后首行是 `#!/bin/sh\r`，内核找不到解释器 → `execv` ENOENT。sha256/size 全对，最像「Agent 有 bug」 | `write_text(..., newline="\n")`；`deploy_agents` 写完立刻自检「首行 `#!/bin/sh\n` 且全文无 `\r`」，污染就按环境级故障中止 |
| UPG-02 其余字段全对却红灯 | uvicorn 发出的响应头是**小写** `content-disposition`，`dict(r.headers).get("Content-Disposition")` 恒为 None | 一律用 `core.hdr(headers, name)` 做大小写无关查找 |
| RES-01 采到「在线 1/3」 | UPG-06 收尾重启 host_c 后只等 MainPID，状态帧还没落地；RES 基线的「3 台在线」不成立 | UPG-06 finally 里等该主机 `online=true`；RES 模块开跑前再等一次全机队在线 |
| 直接 `python scripts/e2e/run.py` | 相对导入 | 一律 `-m scripts.e2e.run` |

## 8. 排障

- **preflight 报某项不过**：按打印的 detail 修环境；WSL 侧依赖用 `wsl -d Ubuntu-22.04 -- python3 -c "import psutil, paho.mqtt"` 验证。
- **机队 120s 未就绪**：看 `artifacts-e2e/<run_id>/agents/*.log`；常见是 Broker 端口/前缀不一致，或 Agent 连不上（`KK_MQTT_URL` 里的 localhost 在 WSL 内指向 WSL 自己，这是对的）。
- **升级用例全红**：先看报告 `meta` 里的「Agent 可达基址」。为空 = WSL 访问不到服务端，Agent 下载 404/超时，属环境问题而非功能缺陷。
- **`--keep` 之后想清理**：`wsl --terminate Ubuntu-22.04`（连保活会话一起没），再按提示 `taskkill` 占端口的服务端进程。
- **只想重跑某个模块**：`--only UPG --skip-build`（注意依赖顺序，见 §5 末）。

## 9. 与单元测试门禁的关系

E2E **不替代** AGENTS.md 的提交门禁。提交前仍必须：

```bash
# 后端：failures/errors = 0，skipped 恰好 5（无 Broker 时）
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml

# 前端
cd web && pnpm typecheck && pnpm test
```

E2E 的定位是**端到端功能的最终裁决**：单测锁语义、E2E 锁「真实组件拼起来还能不能干活」。