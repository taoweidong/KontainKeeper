# Jenkins CI/CD 流水线说明

本文面向**搭建与维护这条流水线的人**（以及被它卡住时来排障的人）。
流水线定义在仓库根 [Jenkinsfile](../Jenkinsfile)；手工部署流程见[生产部署指南](deployment.md)，
两者用的是同一份 `docker-compose.prod.yml` 与同一套 `KK_*` 配置。

> 一句话：**测试 → Agent 二进制 → 服务端镜像 → 镜像冒烟 → 推送 → 目标机部署 → 部署验证**，
> 每道关卡都复用仓库既有脚本，CI 里不另写一份构建逻辑。

## 1. 流水线全景

| # | Stage | 复用仓库里的什么 | 卡住的是什么问题 |
|---|---|---|---|
| ① | 检出与元信息 | — | 记录提交 SHA / 镜像 tag，后续每次部署可追溯到具体提交 |
| ② | 工具链自检 | — | 节点缺 docker/node/pnpm 时**立刻**说清缺什么，而不是跑到一半报 `command not found` |
| ③ | 后端测试 | `pytest agent/tests server/tests` | 起一个真 Broker，把 4 条集成用例从 **skipped** 变成**真跑**（见 §3） |
| ④ | Broker 端到端冒烟 | `scripts/mqtt_e2e.py` | 单测证不到的语义：retain 只落 status、LWT 触发、离线命令由 Broker 排队、大输出分块重组 |
| ⑤ | 前端构建与产物同步 | `pnpm typecheck && pnpm build` | 产物同步进 `server/src/kk_server/web/`（镜像靠它带 UI），并检查**产物漂移** |
| ⑥ | Agent 二进制 | `agent/build/build_binary.sh` | PyInstaller 单文件二进制，供镜像内置与冒烟使用 |
| ⑦ | 构建服务端镜像 | `server/Dockerfile` | 构建上下文是仓库根（uv workspace 锁在根），并打上 git 修订标签 |
| ⑧ | 镜像部署冒烟 | `scripts/ci_smoke.sh` | **真起容器 + 真跑 Agent 二进制**走完整链路；跑不过就不许推送、不许部署 |
| ⑨ | 推送镜像 | — | 仅当配了 `IMAGE_REGISTRY`；production 额外推 `latest` |
| ⑩ | 部署 | `docker-compose.prod.yml` | 目标机 `git reset --hard` 到本次提交 → `pull` → `up -d`；生产需人工确认 |
| ⑪ | 部署验证 | `GET /api/health`、`/api/login`、`/api/system/stats` | 断言「真起来了且连得上 Broker」，而不是「容器在跑」 |
| ⑫ | 离线包（可选） | `deploy/offline/pack.sh` | 内网无网部署用，勾选 `PACK_OFFLINE` 才跑 |

**⑧ 是这条流水线的价值核心**。只做「构建成功 + 容器起来了」的 CI 会漏掉本项目最容易坏的地方——
服务端起来了但连不上 Broker、Agent 上线了但自报 IP 不在白名单、命令发得出去但结果回不来。
`scripts/ci_smoke.sh` 把这条链路拆成 11 条断言，任何一条断了构建就红。

## 2. 接入准备

### 2.1 构建节点要求

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | ≥ 24，含 `docker compose` 插件 | 起 CI Broker、构建镜像、跑镜像冒烟 |
| git / curl | 任意新版 | — |
| python3 | ≥ 3.12 可选 | 有它就用它；没有时 uv 会自行取 3.12（`.python-version` 指定） |
| Node.js | `^20.19.0 \|\| >=22.13.0`（推荐 22，同 `web/.nvmrc`） | 版本过低流水线直接拒绝，避免产出不可复现的产物 |
| pnpm | ≥ 9 | 缺则 `corepack enable && corepack prepare pnpm@9 --activate` |
| uv | 不需要预装 | 缺失时流水线自动装进 `$WORKSPACE/.ci-tools`，不写节点全局环境 |
| 出网 | PyPI / npm / astral.sh / 容器仓库 | 首次构建需要；缓存（`.ci-cache`）在 workspace 内跨构建复用 |

节点标签由参数 `AGENT_LABEL` 指定（默认 `docker`）。

### 2.2 Jenkins 插件

只需常规几件：`workflow-aggregator`（Pipeline）、`git`、`credentials-binding`、
`timestamper`、`junit`。**不需要** SSH Agent 插件（流水线直接用凭据里的私钥文件）。

### 2.3 凭据（4 个，ID 必须一致）

| ID | 类型 | 内容 | 用途 |
|---|---|---|---|
| `kk-registry-creds` | Username with password | 镜像仓库账号 / 密码 | 推送镜像、目标机 `docker login` |
| `kk-deploy-ssh` | SSH Username with private key | 部署私钥（用户名由参数 `DEPLOY_USER` 给） | SSH 到目标机 |
| `kk-agent-ips` | Secret text | 部署目标机的 `KK_AGENT_IPS` 白名单（如 `10.0.0.0/24`） | 写入目标机 `.env` |
| `kk-admin-pass` | Secret text | 管理界面管理员强口令 | 写入目标机 `.env`；部署验证时登录 |

> 仓库是公开的，`.env` 严禁入库。流水线里凭据只经 **stdin** 落到目标机（`umask 077`），
> 不落 CI 工作区、不回显内容（注意：**不要**在 stage 里开 `set -x`，否则会把命令行打进日志）。

### 2.4 Job 配置

1. 新建 **Multibranch Pipeline**（或普通 Pipeline job + `Pipeline script from SCM`）；
2. SCM 指向本仓库，**Script Path = `Jenkinsfile`**（仓库根，默认即是）；
3. 触发方式二选一：仓库 Webhook（推荐）或 SCM 轮询；
4. 分支策略：`main` 走完整流程；其他分支建议只跑到 ⑧（把 `SKIP_DEPLOY` 默认勾上即可）。

### 2.5 部署目标机要求

| 项 | 要求 | 说明 |
|---|---|---|
| 仓库克隆 | `DEPLOY_DIR` 已是本仓库的克隆 | 流水线在其中 `git fetch` + `git reset --hard <SHA>`，保证 compose 文件与 `mosquitto.conf` 与本次提交一致 |
| Docker | ≥ 24 + compose 插件 | — |
| SSH | `DEPLOY_USER` 的公钥已加入目标机 `authorized_keys` | 支持首次连接 TOFU（`StrictHostKeyChecking=accept-new`） |
| 仓库登录 | 用镜像仓库时需能访问仓库 | 流水线会在目标机 `docker login` |
| 其他 | 见[部署指南](deployment.md) §1 与 §10 生产检查清单 | 8443 只对反代开放、1883 网络层受限等 |

> `git reset --hard` 会丢弃目标机仓库里的本地改动——**这是有意的**（部署目录不该手工改）。
> 未跟踪文件（含 `.env`）不受影响。

## 3. 为什么流水线要自己起一个 Broker

`server/tests/test_integration.py` 在没有 Broker 的环境会**整条 skip**（设计如此，保证本地无 Broker 时单测全绿）：

```
不可达时：198 passed + 4 skipped
可达时  ：202 passed
```

也就是说，一个「跑通了 CI」但没起 Broker 的流水线，是**用 4 条 skipped 换来的假绿**——
而跳过的恰好是「Agent 上线 → 指标可见 → 批量下发 → 结果回传」这几条最该被守护的用例。

所以流水线在 ③④⑧ 三个阶段各起一次 Mosquitto（端口 `CI_MQTT_PORT`，默认 **18830**，
只绑 `127.0.0.1`，避免与节点上可能存在的开发 Broker 抢 1883），并在这些阶段结束时收掉。
集成用例读的是 `KK_IT_MQTT_URL`，流水线已自动设为 `mqtt://127.0.0.1:18830`。

**验收口径：③ 阶段必须是 202 passed，出现任何 skipped 都应视为配置问题。**

## 4. 首次接入清单

1. 在节点上装齐 §2.1 的工具（`docker compose version`、`node -v`、`pnpm -v` 都能跑通）；
2. 建好 §2.3 的 4 个凭据；
3. 目标机 `git clone` 到 `DEPLOY_DIR`，并把 `kk-deploy-ssh` 对应公钥加入 `authorized_keys`；
4. **先只构建不部署**：跑一次，`SKIP_DEPLOY=true`。
   这一步会验证 ①~⑨ 全链路，并在 ⑧ 真起容器跑一遍；
5. 通过后再填 `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_DIR` 跑正式部署；
6. 生产环境建议保持 `AUTO_APPROVE=false`，部署前点一次确认。

**首次跑 ⑤ 阶段若报「前端产物与仓库不一致」**：这是真实存在的静默问题（仓库把构建产物也提交了，
而同步动作是手工的）。按提示修正后提交即可：

```bash
cd web && pnpm install && pnpm build
rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/
git add server/src/kk_server/web && git commit -m "chore(web): 同步前端构建产物"
```

确属工具链差异（同内容不同哈希）而非内容漂移时，把 `STRICT_WEB_SYNC` 置 `false` 重跑，
但仍建议顺手把产物刷新为 CI 节点产出的版本，保证「本地构建 / CI 构建 / 仓库产物」三者一致。

## 5. 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `AGENT_LABEL` | `docker` | 构建节点标签 |
| `ENVIRONMENT` | `staging` | `production` 时额外推 `latest`，并要求人工确认 |
| `IMAGE_REGISTRY` | 空 | 留空 = 只构建不推送（此时部署改为目标机本地 `--build`） |
| `IMAGE_TAG` | 空 | 留空 = `sha-<短SHA>`；**回滚时填上一个版本的标签** |
| `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_DIR` | 空 / `deploy` / `/opt/kontainkeeper` | 空 host = 不部署 |
| `DEPLOY_HEALTH_URL` | 空 | 留空 = `http://<DEPLOY_HOST>:8443`；走反代或 HTTPS 时填完整地址 |
| `ADMIN_USER` | `admin` | 部署验证登录用 |
| `TOPIC_PREFIX` | `kk/v1` | 写入目标机 `.env`，须与 Agent 一致 |
| `CI_MQTT_PORT` | `18830` | CI Broker 端口 |
| `SKIP_TESTS` | false | 跳过 ③④（仅应急；会跳过集成用例） |
| `SKIP_DEPLOY` | false | 只构建不部署（首次接入建议置 true） |
| `STRICT_WEB_SYNC` | true | 前端产物与仓库不一致时失败 |
| `AUTO_APPROVE` | false | 生产部署免人工确认 |
| `PACK_OFFLINE` | false | 额外打包离线镜像 tar（较慢，拉取全部基础镜像） |

## 6. 排障

失败时按这个顺序看（`post.failure` 里也印了同一份提示）：

| 现象 | 看哪里 | 常见根因 |
|---|---|---|
| ③ 出现 skipped | `reports/pytest.log` | CI Broker 没起来（端口被占 / 镜像拉不到）；确认 `KK_IT_MQTT_URL` 与 `CI_MQTT_PORT` 一致 |
| ④ 失败 | `reports/mqtt_e2e.log` | Broker 可达但语义不对：retain/LWT/离线队列。多为协议改动未同步 |
| ⑤ 失败 | 构建日志 + `reports/web-sync-drift.txt` | 前端类型错误、`web/mock/` 缺失、或产物漂移（见 §4） |
| ⑥ 失败 | 构建日志 | PyInstaller 装不上（网络/venv）、paho 子模块未被收集 |
| ⑧ 失败 | `reports/image-smoke.log`（含容器日志尾部） | 镜像起不来、生产自检不过（口令/白名单）、Broker 连不上、命令链路断 |
| ⑨ 失败 | 构建日志 | 仓库凭据错、仓库地址不可达、tag 无推权 |
| ⑩ 失败 | 构建日志（SSH 输出） | 私钥不对、`DEPLOY_DIR` 不是克隆、目标机连不上仓库、`.env` 缺 `KK_AGENT_IPS`（production 自检会拒绝启动） |
| ⑪ 失败 | 构建日志 | 服务端没起来、Broker 未连上、口令与目标机 `.env` 不一致 |

两个容易踩的坑：

- **登录锁定**：同一用户名连续失败 5 次会锁 300 秒。⑪ 报登录失败时先确认凭据，
  别连着重跑，否则会一直撞在锁定窗口上。
- **`KK_ENV=production` 自检**：目标机口令为默认 `admin123` 或 `KK_AGENT_IPS` 为空时，
  服务端会**直接拒绝启动**（fail-fast）。⑩ 之后容器反复重启、⑪ 探活失败，多半是这个。

## 7. 回滚

服务端无状态，回滚 = 重新部署上一个镜像标签：

1. 在 Jenkins 用参数 `IMAGE_TAG=<上一个标签>` 重跑（标签形如 `sha-1a2b3c4d5e6f`，
   取上一次成功构建的 ① 阶段输出，或 `docker images` / 仓库的 tag 列表）；
2. 勾上 `SKIP_TESTS` 可让回滚更快（镜像与产物都是既有的）；
3. ⑩ 会把目标机 `git reset --hard` 到**对应提交**——注意 `IMAGE_TAG` 与提交需匹配，
   否则会出现「新代码 + 旧镜像」的错配。**回滚时请用与目标标签对应的提交重跑**。

数据库侧无需动作：新增列由启动时 `_ensure_schema` 自动 ALTER，只增不减不删，
代码回滚后遗留的新列无害（有 `server_default`）。

> 更彻底的「让全网 Agent 退回旧版」不在这条流水线的能力内——Agent 只升不降，
> 需上传版本号更高的包，详见[优化方案](optimization-plan-2026-09-12.md) B6.6。

## 8. 本地等价命令（不依赖 Jenkins 也能跑同一套）

排障时用得上；也是验证流水线改动的最快方式：

```bash
# 0) 依赖
uv sync --all-packages

# 1) 后端测试（带真 Broker，期望 202 passed）
docker run -d --name kk-ci-broker -p 127.0.0.1:18830:1883 \
  -v "$PWD/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2
KK_IT_MQTT_URL=mqtt://127.0.0.1:18830 .venv/bin/python -m pytest agent/tests server/tests -q

# 2) Broker 语义冒烟
KK_MQTT_URL=mqtt://127.0.0.1:18830 .venv/bin/python scripts/mqtt_e2e.py

# 3) 前端
cd web && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build && cd ..
rm -rf server/src/kk_server/web/* && cp -r web/dist/* server/src/kk_server/web/

# 4) Agent 二进制 + 服务端镜像
agent/build/build_binary.sh
docker build -f server/Dockerfile -t kontainkeeper-server:local .

# 5) 镜像冒烟（会自己起 Broker 与服务端容器，跑完自动清理）
scripts/ci_smoke.sh kontainkeeper-server:local agent/dist/kk-agent
```

冒烟失败时脚本会打印三个容器的日志尾部；想保留现场供排查，加 `SMOKE_KEEP=1`。

## 9. 与优化方案的关系

[优化方案](optimization-plan-2026-09-12.md) 的 **B5「CI 落地」** 原本按 GitHub Actions 规划
（真库 job + nightly 压测）。按当前决定改为 Jenkins，落地情况：

| B5 子项 | 状态 |
|---|---|
| 单测 job（含真 Broker，202 passed） | ✅ 已落地（③④） |
| 服务端镜像构建与部署 | ✅ 已落地（⑦⑩⑪）——**超出 B5 原范围，属本次新增** |
| 镜像级部署冒烟 | ✅ 新增（⑧，`scripts/ci_smoke.sh`） |
| 前端产物漂移检查 | ✅ 新增（⑤） |
| **真库 job**（PG/MySQL 连真实库跑 `test_dialects.py`） | ⏳ 未做，仍按 B5 规划 |
| **nightly 压测**（`loadtest.py` / `bench_agent.py`） | ⏳ 未做，建议单独建一个定时 job 复用本流水线的 ①~⑥ |

真库与压测没有塞进这条流水线，是因为它们**耗时长且与部署无关**：每次 push 都跑会让反馈变慢。
建议后续单独建一个 daily job，`when` 里只跑这两段。

## 10. 本次配套改动的说明

| 文件 | 改动 | 为什么必须改 |
|---|---|---|
| `Jenkinsfile` | 新增 | 流水线定义 |
| `scripts/ci_smoke.sh` | 新增 | 镜像级部署冒烟；本地也能直接跑 |
| `docker-compose.prod.yml` | 给 `kk-server` 加 `image: ${KK_SERVER_IMAGE:-kk-server:latest}` | 原文件只有 `build:`，**无法部署已推送到仓库的镜像**（`pull` 没有可拉的对象）。默认值与 `pack.sh` / offline compose 一致，手工部署行为不变 |
| `.env.example` | 补 `KK_SERVER_IMAGE` 注释 | 让运维能发现这个开关 |
| `.gitignore` | 忽略 `reports/`、`.ci-cache/`、`.ci-tools/`、离线镜像 tar | 前者是 CI 产物；后者是 `pack.sh` 的 GB 级产物，此前**未被忽略**（`git status` 噪音 + 误提交风险） |
