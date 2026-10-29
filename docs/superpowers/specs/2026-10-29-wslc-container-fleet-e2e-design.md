# wslc 容器机队 E2E 设计（4 台真 Linux 容器）

日期：2026-10-29 · 状态：已批准（待实现计划）
关联：`docs/e2e-real-env.md`（现有 3 台 WSL Agent 全流程 E2E）、`.qoder/skills/kk-e2e/SKILL.md`

## 1. 目标与非目标

**目标**：把真实环境 E2E 的 Agent 机队从「一个 WSL 发行版里的 3 个进程」换成「4 个独立
Linux 容器」，每台是真正隔离的一台主机（独立 PID/挂载/网络命名空间、独立主机名），在此
之上把四类能力端到端验一遍：命令下发与脚本执行、心跳指标上报与采集、二进制自更新升级、
真故障下的韧性收敛。

**非目标**（本轮明确不做，另开一轮）：

- 不引入「只有真容器才能测」的新用例（资源受限主机、tmpfs 写满触发磁盘告警、分组/labels
  过滤、逐主机采集项差异）。先把现有 32 条在容器机队上等权跑绿，一次只改一件事，
  否则红灯无法归因。
- 不改动 Broker 与服务端的位置，也不改动产品代码。若容器机队逼出产品改动，说明设计
  有问题，停下来重新评审。

## 2. 可行性实测（2026-10-29，本机 wslc 1.0.79 / WSL 3.0.3.0 / 内核 6.18.54.1）

| 探针 | 结果 | 对设计的影响 |
| --- | --- | --- |
| 容器 → `host.wslc.internal:18999`（Windows 侧监听） | **OK** | 容器里的 Agent 能直接打 Windows 上的服务端 |
| 容器 → `host.wslc.internal:18998`（WSL 发行版内监听） | **OK** | 容器里的 Agent 也能打发行版里的 mosquitto（mirrored 网络把两侧 loopback 并到同一平面） |
| 容器 → `172.17.0.1`（bridge 网关，即发行版） | Connection refused | 不能靠网关 IP 打发行版；必须用 `host.wslc.internal` |
| 容器 → 自身 `127.0.0.1` 上的 Windows 服务 | Connection refused | 命名空间隔离，符合预期 |
| `wslc run --network host` | `不支持主机模式网络` | 排除 host 网络方案 |
| `wslc build --output type=local,dest=…`（相对/绝对路径都试） | 报 usage error | **排除**构建期导出；改用 bind 卷 |
| `wslc run -v "E:/GitHub/…/artifacts-e2e:/out"` + 容器内写文件 | 文件确实落到 Windows FS | **ELF 取回方案**：跑一个一次性容器把 `/opt/kk/kk-agent` 拷到挂载点 |
| `wslc export -o <file> <container>` | 命令存在（未采用） | 仅作 fallback 记录 |
| 容器出网 `pip download paho-mqtt` | 成功 | 镜像内 pip 装依赖/PyInstaller 可行 |
| `wslc pull docker.m.daocloud.io/library/eclipse-mosquitto:2` | 成功 | Broker 容器化可行，但本轮不做（见 §3 决策） |
| 本机镜像缓存 | `python:3.12-slim-bookworm` 已存在 | 基础镜像不必每次拉 |

`wslc` 的 `hostLoopback` 设置（默认 `host.wslc.internal`，见
`%LOCALAPPDATA%\wslc\settings.yaml` 注释「DNS name that resolves to the host loopback
address」）是整套方案成立的前提。**注意**：`host.docker.internal` 在本机不存在
（`gaierror`），照 Docker 习惯写会白忙一场。

## 3. 拓扑决策

选定：**服务端与 Broker 位置不动，只有 Agent 机队进容器**。

```
Windows
 ├─ kk-server  :18443（python -m kk_server，独立 SQLite + web/dist）
 ├─ Playwright → Chromium
 └─ wslc session VM
     ├─ kk-ct-01 … kk-ct-04   4 个容器，PID1 = 真 ELF kk-agent（0.3.0）
     └─ (可选陪跑)
 Ubuntu-22.04（发行版，位置不变）
 └─ mosquitto :18832（systemd 瞬态单元，专用实例，可独立重启）
```

容器侧只改两个地址，其余配置与现有 WSL 机队完全一致：

```sh
KK_SERVER=mqtt://host.wslc.internal:18832      # Broker 在发行版
KK_UPDATE_URL=http://host.wslc.internal:18443   # 服务端在 Windows（升级下载）
```

**为什么不动 Broker/服务端**：一是上面两条可达性实测已经成立，改动面最小；二是
`KK_AGENT_IPS` 白名单（`scripts/e2e/setup.py:257`）已含 `172.16.0.0/12`，容器 IP
（172.17.0.x）与 REST 侧源 IP 天然落在段内，不用为白名单再动产品配置；三是把服务端
也搬进容器会让 `web/dist`、SQLite、上传目录都要挂卷，排查日志变绕，收益换不回成本。
Broker 容器化（复用 `docker/mosquitto/mosquitto.conf`）留作后续轮次。

## 4. 分层与接口

机队形态只允许存在于 `setup.py` 之下。新增 `scripts/e2e/fleet.py`：

```python
class Fleet:                      # 抽象：一台主机能对被做的事，与它跑在哪无关
    def prepare(self) -> None: …            # 部署资产/镜像、写每台主机的启动配置
    def start(self, host) -> str: …         # 启动（返回实例标识）
    def stop_hard(self, host) -> str: …     # kill -9 等价：模拟断电，走 LWT
    def stop_graceful(self, host) -> str: … # SIGTERM 等价：Agent 自报 reason=stopping
    def restart(self, host) -> str: …
    def instance(self, host) -> str: …      # 空串 = 不在跑；变化 = 进程真的重启过
    def version(self, host) -> str: …       # 运行中进程的 Agent 版本（主机侧真相，不问服务端）
    def binary_sha(self, host) -> str: …    # 主机上待替换二进制的 sha256
    def set_env(self, host, key, value) -> None: …
    def reset_binary(self, host, ver) -> str: …  # 换回旧版 + 重启（模拟存量老主机重新上线）
    def tail_log(self, host, n) -> str: …   # 失败证据
```

两个实现：

- `WslFleet` —— 现有 `Stack` 里那批 `start_agent / stop_agent / agent_pid / agent_version /
  set_agent_env / agent_binary_sha / reset_agent_binary` 平移过来，行为一个字节不改。
- `WslcFleet` —— 4 个容器，用 `wslc run/stop/kill/start/remove/inspect/logs/exec` 实现同一组方法。

接口里两个方法的**消费方**要说清楚，免得长成死 API：`tail_log()` 给 `run.py` 的 ENV-01
环境级故障处理器用（今天它已经在贴 `journalctl -u` 的输出，容器侧对应 `wslc logs`）；
`ensure_session()` 是 `WslcFleet` 的内部方法（不在接口上），只服务于 §8 的会话被拆风险。

`Stack` **保留现有公开方法名并委派**给 `self.fleet`，所以 `api_cases.py`、`ui_cases.py`、
`upgrade_cases.py` 一行不改。用例层对机队的全部依赖本来就只有 7 个方法（已核对），唯一的
硬泄漏是 `resilience_cases.py:208` 直接 `wsl_stdin(env, 'kill -TERM …')` → 改成
`stack.term_agent(host)`（委派 `stop_graceful`）。

### 4.1 `WslcFleet.version()` 的取证口径（比现状更硬）

现在 WSL 机队读的是「进程 `PYTHONPATH` 指向的那棵源码树里的 `AGENT_VER`」。容器机队跑的是
真 ELF，没有源码树可 grep，改为：**取 `/proc/1/exe` 指向文件的 sha256，对照本轮构建出的
两块 ELF 的 sha 反查版本号**。

这不是退而求其次，是升级：它证明的是**正在运行的那个进程镜像**本身就是新二进制，
而不是「磁盘上某个文件叫这个名字」。`os.replace` + `os.execv` 之后 `/proc/1/exe` 指向替换后的
文件，旧 inode 随 execv 消失，所以「磁盘已换但进程还跑旧代码」这种半吊子状态会被直接抓到。

### 4.2 生命周期映射与一处语义取舍

| 用例动作 | WSL 机队 | wslc 机队 |
| --- | --- | --- |
| 断电（RES-01） | `systemctl kill -s KILL` | `wslc kill`（默认 SIGKILL；容器退出，Broker 发 LWT） |
| 优雅退出（RES-05） | `kill -TERM <pid>` | `wslc stop`（默认 SIGTERM 给 PID1） |
| 重新上线（RES-01/02/05） | `systemd-run` 拉起 | `wslc start`（同一容器） |
| 改环境变量（UPG-06） | 改 env 文件 + 重启 | **删除并重建容器**（env 在 create 时固定） |
| 版本取证 | grep 源码树 | 运行中：`wslc exec <c> sha256sum /proc/1/exe`；未运行：返回空串 |
| 磁盘二进制取证 | `sha256sum <宿主文件>` | 一次性容器读命名卷（见 §4.3） |
| 进程身份（RES-03） | MainPID + `/proc/environ` 双确认 | `State.Running` + `State.StartedAt`（**实测 `State` 里没有 `Pid` 字段**，只有 ExitCode/FinishedAt/Health/Running/StartedAt/Status） |

**取舍**：`set_env` 在容器侧只能重建，因为 env 在 `wslc create` 时就固定了。UPG-06 的语义恰好
是「把这台主机还原成可升级状态」（它随后就断言该主机版本仍是 0.3.0），对得上。二进制放在
命名卷里（§4.3），所以**重建不会丢掉已替换的二进制**——这点比「文件在容器层」的做法更贴近
真实主机（真实主机的 `/usr/local/bin/kk-agent` 也不会因为改环境变量重启服务而回滚）。
这条限制与结论会写进 `fleet.py` 的 docstring 与运行手册，而不是埋在代码里。

### 4.3 取证通路：待替换的二进制放命名卷，不放容器层

计划期发现的问题：`wslc exec` 只能对**运行中**的容器执行，而 RES-02 恰好要在主机离线时读
「磁盘上的二进制还没被换掉」这个证据（`disk_offline != sha`）。如果二进制在容器文件系统里，
容器一停就读不到，用例只能退化成「等它上线再读」——那就不再是离线断言了。

解法：每台主机一个 guest 命名卷挂到 `/opt/kk`，镜像把编译好的 ELF 放在 `/opt/kk-image/kk-agent`，
`prepare()` 用一次性容器把它拷进卷；Agent 容器的 `ENTRYPOINT` 直接是 `/opt/kk/kk-agent`。

于是三个取证点各自都成立且与容器状态无关：

| 需求 | 实现 | 容器停着时 |
| --- | --- | --- |
| `binary_sha(host)`（磁盘真相） | `wslc run --rm -v <vol>:/opt/kk <image> sha256sum /opt/kk/kk-agent` | **可读**（卷独立于容器） |
| `version(host)`（运行真相） | `wslc exec <c> sha256sum /proc/1/exe` → sha 对照表反查 | 返回空串（用例本就要求进程不存在） |
| `reset_binary(host, ver)` | 一次性容器 `cp /opt/kk-image/kk-agent <ver>` 进卷 + 重启 | 可用 |

额外收益：`os.replace` + `os.execv` 发生在 guest 文件系统（ext4）上，是**真原子替换**；若把
二进制放在 bind 挂载进来的 Windows 目录上，跨 9P/DrvFs 的原子改名是不可控的，测试会测到
「我们根本不支持的形态」。**自更新的目标路径绝不能放在 Windows bind 卷上。**
Windows 侧只用于**导出** ELF 给服务端上传（一次性 `wslc run -v <artifacts>:/out` 拷出，已实测可行）。

## 5. 镜像与升级保真度

Dockerfile 由脚本生成到 `artifacts-e2e/<run>/wslc/Dockerfile`，构建上下文是同目录（已实测
`wslc build` 可用相对路径上下文）。`prepare()` 先把仓库的 `agent/` 拷进上下文
（排除 `dist/`、`build/`、`__pycache__`），编译直接复用仓库现成的 `agent/build/build_binary.sh`
——它自己 `cd` 到 agent 根、按 `PYINSTALLER_PYTHON` 取解释器、产出 `agent/dist/kk-agent`，
不在 Dockerfile 里重抄一遍 PyInstaller 参数（重抄就会漂移）：

```
FROM python:3.12-slim-bookworm
COPY agent/ /build/agent/
ARG AGENT_VER=0.3.0
# [build] extra 一次装齐 psutil/paho-mqtt/loguru + pyinstaller
RUN python3 -m pip install --no-cache-dir "/build/agent[build]" \
 && sed -i "s/^AGENT_VER = .*/AGENT_VER = \"${AGENT_VER}\"/" /build/agent/src/kk_agent/config.py \
 && bash /build/agent/build/build_binary.sh \
 && mkdir -p /opt/kk-image /opt/kk \
 && cp /build/agent/dist/kk-agent /opt/kk-image/kk-agent && chmod 755 /opt/kk-image/kk-agent
# 入口是**卷里的**路径，不是镜像里的：镜像只提供初始二进制（/opt/kk-image），
# prepare() 用一次性容器把它拷进命名卷；单独跑这个镜像会失败，这是有意的。
ENTRYPOINT ["/opt/kk/kk-agent"]
```

产两个 tag：`kk-agent-e2e:0.3.0`（4 台容器跑它）与 `kk-agent-e2e:0.4.0`。**两个 tag 的 ELF
都导出到 Windows**（`0.3.0` 那份不参与上传，只喂给 §4.1 的 sha→版本对照表；`0.4.0` 那份是
上传给服务端的「新版本二进制」）。导出用已实测可行的 bind 卷（`E:/…` 正斜杠形式）：

```sh
wslc run --rm -v "E:/…/artifacts-e2e/<run>/wslc:/out" kk-agent-e2e:0.4.0 \
  sh -c "cp /opt/kk-image/kk-agent /out/kk-agent-0.4.0"
```

**保真度提升是本轮的核心收益**：上一轮升级测的是「`execv` 一个 sh 脚本」，改完是
`下载 → size → sha256 → HMAC → os.replace → os.execv(真 ELF)`，与生产发布物完全同一条路径；
上一类 CRLF 冒充二进制的 fixture 陷阱从根上消失（不再有文本文件参与）。

镜像默认复用，`--rebuild-image` 才重建（PyInstaller 一轮 1–2 分钟）。

## 6. 机队规模与用例适配

- `Env.fleet: str = "wsl"`（默认，保持 CI 与无 wslc 机器可跑）；`Env.agents` 由 fleet 决定：
  `wsl` → `wsl-agent-01..03`（不变），`wslc` → `kk-ct-01..04`。
- **断言逻辑一处不改**：现有断言已全部按 `len(env.agents)` 数据驱动（已核对 API/RES）。
- 要改的只有用例**标题与前置条件里写死的「3 台」**（API-01、API-03、API-07、RES-03 等约 8 处
  字符串）→ 一律用 `%d 台` 从 `len(env.agents)` 取，报告里不再出现与机队规模矛盾的句子。
- `--only` / `--keep` / `--skip-build` / `--headed` 语义不变；新增 `--fleet {wsl,wslc}`、
  `--rebuild-image`。

## 7. preflight 与产物

`--fleet wslc` 时新增**硬项**（不过就环境级中止，不留到用例里炸）：

1. `wslc` 可执行 + `wslc system info` 有会话；
2. 基础镜像在本地或可拉（`python:3.12-slim-bookworm`）；
3. `host.wslc.internal` 实测可达：Windows 侧起一个临时监听口，容器内 socket 连它
   ——这条是把 §2 的探针固化成门禁，网络形态一变（关 mirrored、换 wslc 版本）立刻失败。

产物新增 `artifacts-e2e/<run>/wslc/`：`image-build.log`、`inspect.json`、
`logs/<host>.log`（`wslc logs` 落盘）、`kk-agent-0.4.0`（导出的真 ELF）。报告 `env` 表加
`fleet` 字段与容器 subnet/白名单实测值。

## 8. 风险

| 风险 | 处置 |
| --- | --- |
| wslc `session.idleTimeout` 默认 30s，空闲会话 VM 被拆 | 4 台常驻容器使会话不空闲；每次机队操作前用 `wslc inspect` 校验状态，异常即报环境级故障并附 `wslc system info` |
| wslc 仍在快速演进，已声明的选项实际不可用：`build --output` 报 usage error、`inspect -f` 只接受字面量 `json`（不支持 Go 模板）、`State` 里**没有 `Pid` 字段**（只有 ExitCode/FinishedAt/Health/Running/StartedAt/Status） | 只依赖实测过的命令与字段：`build/run/kill/stop/start/remove/exec/logs/inspect -f json/volume create`；`instance()` 用 `Running`+`StartedAt`；证据留在 §2 |
| `/proc/1/exe` 需要容器内 root | 已实测 `python:3.12-slim-bookworm` 默认 uid=0 且 `sha256sum /proc/1/exe` 可用。将来换非 root 镜像时 `version()` 拿不到 sha 只能返回空串——用例随即红灯，不会假通过 |
| **命名卷残留造成假绿灯**：`--keep` 或上轮中断后，同名卷里可能还躺着上一轮升完的 0.4.0 | `prepare()` **无条件**用镜像内 `/opt/kk-image/kk-agent` 覆盖卷内 `/opt/kk/kk-agent`，并断言之后的 sha 等于本轮 0.3.0；不满足即环境级中止。「开机就是 0.4.0」的机队会让所有升级断言失去意义 |
| 自更新目标路径落在 Windows bind 卷上 → `os.replace` 跨 9P/DrvFs 的原子性不可控 | 二进制只放 guest 命名卷（ext4）；Windows bind 卷仅用于**导出** ELF 给服务端上传（§4.3） |
| Git Bash 会把 `/v/marker` 这类 POSIX 绝对路径参数转换成 `V:/marker`（本轮实测踩过） | harness 全部用 `subprocess` 的 argv 列表调用，不经 bash，不受影响；但**手工验证命令一律加 `MSYS_NO_PATHCONV=1`**，这条要写进运行手册的已知坑表 |
| PyInstaller 镜像内构建失败（pip 源慢/断） | 构建失败在 `prepare()` 阶段即环境级中止，并附 `wslc/image-build.log` 尾部 |
| 容器 IP 变化导致白名单失效 | 白名单已含 `172.16.0.0/12`；报告记录 `inspect` 里 `NetworkSettings.Networks.bridge.IPAddress` 的实测值，便于复核 |

## 9. 验收

1. `--fleet wslc`（4 台容器）全量 32 条 PASS，退出码 0；
2. `--fleet wsl`（3 台）回归 32 条仍 PASS —— 证明抽象没有把行为带偏；
3. AGENTS.md 门禁照旧且**新增 `scripts/tests`**：后端
   `pytest agent/tests server/tests scripts/tests` failures/errors=0（`scripts/tests` 只测纯
   逻辑，任何机器上都不得 skip——CI 门禁里 `skipped != 0` 直接红），前端
   `pnpm typecheck && pnpm test` 全绿；AGENTS.md 与 `Jenkinsfile` 的测试命令同步更新；
4. `docs/e2e-real-env.md` 增 wslc 章节（含 §2 实测表、§4.2 映射表、§4.3 取证通路），
   `.qoder/skills/kk-e2e/SKILL.md` 增「何时用容器机队」与新取证口径。

## 10. 实现顺序（供 writing-plans 展开）

1. `fleet.py` 抽象 + `WslFleet` 纯平移（不改行为）+ `Stack` 委派 + `term_agent` 收敛
   → 立即用 `--fleet wsl` 全量回归 32 条；
2. 机队规模与用例自述一致（`hosts_for(fleet)`：wsl→3、wslc→4；标题/前置里的「3 台」动态化）；
3. `wslc.py` 纯函数层（argv 构造、`inspect` 解析、sha→版本对照、Dockerfile 生成、容器 env）
   + `scripts/tests` 单测；
4. 镜像构建与 ELF 导出 + 命名卷种子（含「残留即假绿灯」的断言）；
5. `WslcFleet` 生命周期方法打通 1 台容器：上线 → `version()`/`binary_sha()` 对得上；
6. 扩到 4 台，按 API → UI → UPG → RES 分段跑通；
7. preflight/产物/`--fleet`/`--rebuild-image` 收口；
8. 文档、skill、AGENTS.md 与 Jenkinsfile 门禁同步。
