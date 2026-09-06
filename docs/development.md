# KontainKeeper 开发环境搭建指南

本文面向**第一次接触本项目的开发者**，在一台开发机上跑起全部三个工程
（服务端 / Agent / 前端）并具备测试与联调能力。

> 生产部署（安全加固、镜像制作、TLS 等）请看[生产环境部署指南](deployment.md)，
> 两者职责不重叠：开发环境用匿名 Broker 与默认口令换取「开箱即起」。

## 0. 工程结构总览

本仓库是 **uv workspace**（根 `pyproject.toml` 声明 `members = [server, agent]`），
外加一个独立的 pnpm 前端工程：

| 工程 | 路径 | 技术 | 说明 |
|---|---|---|---|
| kk-server | `server/src/kk_server/` | FastAPI + SQLAlchemy async | REST `/api/*` + MQTT 桥接 + 三库支持；MVC 分层（models / services / controllers / web 托管） |
| kk-agent | `agent/src/kk_agent/` | paho-mqtt + psutil | 主机端采集器；`transport`(MQTT) / `collector` / `executor` / `updater`；可编译单文件二进制 |
| web | `web/` | Vue3 + Element Plus + Vite + Pinia + ECharts | 独立 pnpm 工程（pure-admin-thin 底座）；产物同步给服务端托管 |

```
agent/src/kk_agent/    transport.py(MQTT) / collector.py(psutil) / executor.py /
                       updater.py(自更新) / plugin_loader.py / main.py
server/src/kk_server/  models/(store,tables,version) / services/(mqtt_bridge,security) /
                       controllers/(auth,containers,commands,audit,stats,agent_update,health) /
                       main.py(create_app 工厂，python -m kk_server 运行)
web/src/               api/(业务 API 层) / views/(四个业务页) / router/modules/kk.ts(静态路由)
proto/messages.md      双端通信协议契约（v3 MQTT：匿名 Broker + IP 白名单），改协议必读
scripts/               build.sh(管理镜像) / mqtt_e2e.py(Broker 冒烟) / bench_agent.py / loadtest.py
deploy/mosquitto/      匿名 Broker 配置（mosquitto.conf，开发/生产同源）
```

## 1. 前置条件

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | ≥ 3.12 | 后端两个工程的运行时 |
| [uv](https://docs.astral.sh/uv/) | 最新 | 依赖管理与 workspace |
| Node.js | ^20.19.0 或 ≥ 22.13.0 | 前端构建（`web/.nvmrc`） |
| pnpm | ≥ 9 | 前端包管理（`preinstall` 强制 only-allow pnpm） |
| Docker | 任意新版 | 起开发用 Mosquitto；跑集成测试 |
| git | 任意新版 | — |

> **Windows 开发机**完全受支持（psutil 跨平台、测试直读真机指标），
> 但有一条铁律见 §9.1：用 `.venv` 直调而不是 `uv run`。

## 2. 安装后端依赖

```bash
git clone https://github.com/taoweidong/KontainKeeper.git
cd KontainKeeper
uv sync --all-packages        # 服务端 + Agent + dev 组（pytest/httpx 等）
```

可选 extras（仅生产/对接真实库时需要）：`--extra postgres` / `--extra mysql`。

## 3. 启动开发用 Mosquitto（匿名，开箱即起）

方式 A：单独跑 Broker（推荐，最轻量）：

```bash
docker run -d --name mosquitto -p 1883:1883 \
  -v $PWD/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf \
  eclipse-mosquitto:2
```

方式 B：docker compose 起整套（Broker + 服务端，适合纯体验）：

```bash
docker compose up --build      # http://localhost:8443
```

方式 C（无 Docker 的 Windows 机）：WSL 内装原生 Mosquitto，
Windows 侧经 WSL2 localhost 转发直连 `127.0.0.1:1883`：

```bash
wsl -d Ubuntu-22.04 -- sudo apt-get install -y mosquitto mosquitto-clients
```

v3 起 Broker 统一匿名开放（`allow_anonymous true`，无 passwordfile / ACL），
开发与生产同一份配置；接入管控由服务端 `KK_AGENT_IPS` 白名单承担
（开发环境不配白名单 = 放行全部，见 §6）。

## 4. 启动服务端

```bash
.venv/Scripts/python.exe -m kk_server     # Windows
# .venv/bin/python -m kk_server           # Linux/macOS
```

- 监听 `http://127.0.0.1:8443`，浏览器打开即管理界面；
- 默认账号 `admin / admin123`（**连续输错 5 次锁定 300 秒**，别被自己锁了）；
- 未配 `KK_MQTT_URL` 时服务端仍能起（只读管理 + 审计导出），日志会警告
  不连 Broker——要联调 Agent 就先起 Mosquitto 再配环境变量：

```bash
KK_MQTT_URL=mqtt://127.0.0.1:1883 .venv/Scripts/python.exe -m kk_server
```

- 默认数据库为当前目录 SQLite（`kk-server.db`），开发无需任何配置。

## 5. 前端开发环境

```bash
cd web
pnpm install
pnpm dev          # http://localhost:8848，/api/* 代理到 127.0.0.1:8443
```

| 命令 | 用途 |
|---|---|
| `pnpm dev` | Vite 开发服务器（8848 端口，代理已配好，先起后端再起前端） |
| `pnpm typecheck` | TS 类型检查（`tsc` + `vue-tsc`） |
| `pnpm build` | 产物输出到 `web/dist/` |

**产物同步**（改前端后要让自己看到效果，必须做这一步）：

```bash
pnpm build
rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/
# 重启 kk-server（或 docker 重建镜像）后生效
```

前端开发注意事项：

- `web/dist/` 被 `.gitignore` 忽略，产物需人工同步到 `server/src/kk_server/web/`；
- `pnpm build` 要求 `web/mock/` 目录存在（空目录即可，prod 构建的 fake server 依赖）；
- 菜单**完全静态**（`getAsyncRoutes()` 返回 `[]`，路由在 `src/router/modules/kk.ts`），
  不要引入动态菜单，否则 prod 下菜单空白；
- 轮询定时器统一用 `setPoll()` / `clearPolls()` 管理，不要散落 `setInterval`。

## 6. 运行 Agent（源码方式）

v3 起 Agent 零凭据接入——只需要 Broker 地址。任意目录执行（Linux/macOS）：

```bash
KK_SERVER=mqtt://127.0.0.1:1883 \
KK_HOST_NAME=demo-host \
KK_INTERVAL=5 \
uv run kk-agent
```

Windows 用 `.venv` 直调（理由见 §9.1）：

```powershell
$env:KK_SERVER="mqtt://127.0.0.1:1883"; $env:KK_HOST_NAME="demo-host";
$env:KK_INTERVAL="5";
.venv\Scripts\python.exe -m kk_agent
```

也可以直接传位置参数（最简形式）：

```bash
uv run kk-agent mqtt://127.0.0.1:1883
```

验证链路：管理界面「主机总览」出现 `demo-host` → 详情页看曲线（**指标要等
首帧心跳**，不是上线即有）→ 命令中心下发命令 → 结果回传、审计留痕。

> 接入管控说明：Agent 上行帧携带自报 `ip`（自动探测，`KK_ADVERTISE_IP` 可覆盖），
> 服务端按 `KK_AGENT_IPS` 白名单校验。开发环境不配白名单即放行全部；
> 生产环境必须配置（`KK_ENV=production` 未配会拒绝启动），见部署指南 §4.1。

自定义采集插件：往 `KK_PLUGIN_DIR`（缺省 `<包目录>/plugins/`）放任意 `*.py`，
实现 `collect() -> dict` 即随心跳自动上报，mtime 变化热加载无需重启：

```python
def collect():
    return {"extension_count": 42}
```

## 7. 测试

```bash
# 全量（201 条）
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q
# 分包
.venv/Scripts/python.exe -m pytest agent/tests -q      # Agent 单测
.venv/Scripts/python.exe -m pytest server/tests -q     # Server 单测 + 端到端集成
# 前端
cd web && pnpm typecheck && pnpm build
```

- **Broker 可达**时 201 passed；**不可达**时 197 passed + 4 skipped
  （端到端集成用例自动跳过，不会误报失败）——跑全量前先起 §3 的 Mosquitto。
- `agent/tests` 覆盖 MQTT 传输（主题/QoS/retain/离线队列）、psutil 采集、
  命令执行、插件热加载、自更新；Windows 上直接读真机指标，无需伪造 /proc。
- `server/tests` 用 `httpx.AsyncClient` + `ASGITransport` 直打 `create_app`，
  不依赖真实端口；集成用例起真实 uvicorn 线程 + 真实 Agent 线程。
- 真实 Broker 冒烟（补单测证不到的 paho 排队/LWT/持久会话语义）：

```bash
python scripts/mqtt_e2e.py                     # 默认连 127.0.0.1:1883，退出码 0 = 通过
```

- 压测工具：`scripts/bench_agent.py`（Agent 资源占用）、`scripts/loadtest.py`。

## 8. Agent 二进制构建

制作管理镜像或验证自更新时才需要；日常源码开发不用：

```bash
cd agent && ./build/build_binary.sh     # 产出 agent/dist/kk-agent（Windows 为 .exe）
```

脚本自动安装 PyInstaller 并带上 paho/psutil 的 hidden-import。
完整的管理镜像叠加流程（叠加进 vscode-server 基础镜像）见部署指南 §7。

## 9. 开发约定与常见坑

### 9.1 Windows：务必 `.venv` 直调

`uv run` 在 Windows 开发机会尝试下载 Python 3.12 而失败。统一用：

```bash
.venv/Scripts/python.exe -m pytest ...      # 而非 uv run pytest
.venv/Scripts/python.exe -m kk_server       # 而非 uv run kk-server
```

### 9.2 协议改动四件套

改 `proto/messages.md` 协议时必须同步：

1. `agent/src/kk_agent/config.py` 的 `PROTO_VER`；
2. `server/src/kk_server/__init__.py` 的 `PROTO_VER`；
3. 协议文档本身；
4. 双端测试。

### 9.3 其他高频坑

| 坑 | 说明 |
|---|---|
| 指标不出现 | Agent 上线（status）≠ 有指标；**指标要等首帧心跳**，测试等待条件须同时检查 `metrics.mem_mb` 非空 |
| 心跳间隔下限 | `KK_INTERVAL` 下限 1s，别调回去——集成测试依赖数秒内积累多个序列点 |
| 插件热加载失效 | Windows 文件时间粒度粗，测试写插件后需显式 `os.utime` 递增 mtime |
| 三库方言 | 方言差异只允许收在 `Store._upsert` / `Store._ensure_schema`；新增列须登记 `tables._ADD_COLUMNS`；分批删按主键 `IN`，不要 `DELETE...LIMIT` |
| 命令 argv | 命令下发优先 argv 数组；`cmdline` 走 shlex.split，Windows 含空格路径会拆坏 |
| 服务端入口 | 是工厂 `create_app`，没有模块级 `app`；运行 `python -m kk_server` |
| 安全红线 | 命令黑名单（`KK_CMD_BLACKLIST`）+ 审计（`store.add_audit`）不能绕过 |
| 配置来源 | 双端全部走 `KK_*` 环境变量，不引入配置文件 |

### 9.4 提交规范

- 提交信息用**中文**，`feat/fix/docs/test/chore` 前缀，按模块分批提交；
- 信息里标注覆盖的缺陷编号（P0-x / P1-x / R-x，台账见
  [architecture-review.md](architecture-review.md)）。

## 10. 提交前检查清单

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q   # 全绿（或 197+4 skip）
cd web && pnpm typecheck && pnpm build                          # 前端通过
```

- [ ] 后端全量测试通过
- [ ] 前端 typecheck + build 通过
- [ ] 改过协议则 §9.2 四件套齐了
- [ ] 改过前端则产物已同步 `server/src/kk_server/web/`
- [ ] 提交信息符合 §9.4

---

- 总体设计与取舍：[design.md](design.md)；协议契约：[proto/messages.md](../proto/messages.md)
- 面向 AI 协作的工程约束：仓库根 [AGENTS.md](../AGENTS.md)
- 本文档与代码冲突时，以代码与 `git` 历史为准。
