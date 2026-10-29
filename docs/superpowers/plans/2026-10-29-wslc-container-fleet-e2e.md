# wslc 容器机队 E2E 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把真实环境 E2E 的 Agent 机队从「一个 WSL 发行版里的 3 个进程」换成「4 个独立 wslc Linux 容器」，并让升级用例改用真 PyInstaller ELF 二进制。

**Architecture:** 新增一层 `Fleet` 抽象（`WslFleet` = 现有实现纯平移，`WslcFleet` = 4 个容器），`Stack` 保留原方法名委派，用例层不感知机队形态；Broker 仍在 Ubuntu-22.04、服务端仍在 Windows，容器只用 `host.wslc.internal` 打这两处。待替换的二进制放 guest 命名卷，使「主机离线时读磁盘真相」仍然成立。

**Tech Stack:** Python 3.12（标准库 + pytest）、wslc CLI 1.0.79 / WSL 3.0.3、Mosquitto 2、FastAPI 服务端、Vue3 前端、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-29-wslc-container-fleet-e2e-design.md`（本计划逐节实现它，实测证据与取舍都在 spec 的 §2/§4.2/§4.3/§8）

## Global Constraints

- **不新增 mock、不新增 skip**：E2E 的价值全在「真组件拼起来还能干活」；环境缺件就让 preflight 明确失败并写进报告。
- **不修改产品代码**（`agent/`、`server/`、`web/` 的运行时行为）。若容器机队逼出产品改动，停下重新评审 spec。
- **纯平移优先**：Task 1 搬 `WslFleet` 时行为一个字节不改，靠 `--fleet wsl` 全量回归当它的测试。
- `scripts/tests/` 里的单测**只测纯逻辑**（argv 构造、文本生成、JSON 解析、sha 映射），不得依赖 wslc / Broker / 网络 / 真实时钟；在任何机器上都不得 skip（CI 门禁里 `skipped != 0` 直接红）。
- 每个 Python 文件保持 ≤500 行（本仓库既有约定）。
- 所有配置继续走 `KK_*` 环境变量，不引入配置文件。
- 提交信息用中文，`feat/fix/docs/test/chore` 前缀，按模块分批提交。
- wslc 相关事实（实测得出，不许改）：Broker/服务端地址别名是 `host.wslc.internal`（**不是** `host.docker.internal`）；`wslc inspect -f` 只接受字面量 `json`；`State` 无 `Pid` 字段，只有 `ExitCode/FinishedAt/Health/Running/StartedAt/Status`；`wslc build --output` 本机不可用；容器内二进制路径 `/opt/kk/kk-agent`、镜像内初始二进制 `/opt/kk-image/kk-agent`。
- 每次提交前执行门禁：后端 `pytest agent/tests server/tests scripts/tests`（解析 junitxml，不许 `| tail`），前端 `pnpm typecheck && pnpm test`。

---

### Task 1: Fleet 抽象落地（WslFleet 纯平移 + Stack 委派）

**Files:**
- Create: `scripts/e2e/fleet.py`
- Create: `scripts/tests/__init__.py`、`scripts/tests/test_fleet.py`
- Create: `scripts/__init__.py`
- Modify: `scripts/e2e/setup.py`（把 `setup.py:409-545` 的机队方法改成委派）
- Modify: `scripts/e2e/resilience_cases.py:208`（裸 `wsl_stdin` → `stack.term_agent`）

**Interfaces:**
- Consumes: `Env`（`scripts/e2e/core.py:43`）、`core.wsl` / `core.wsl_stdin` / `core.wait_until` / `core.port_open`、`setup.launcher()`、`setup.OLD_VER` / `setup.NEW_VER`
- Produces（`scripts/e2e/fleet.py`，后续任务全部依赖这组签名）:
  - `Fleet.prepare() -> str`
  - `Fleet.start_all() -> str` / `Fleet.stop_all() -> str`
  - `Fleet.start(host: str) -> str` / `Fleet.stop_hard(host: str) -> str` / `Fleet.stop_graceful(host: str, wait: float = 20) -> str` / `Fleet.restart(host: str, wait: float = 30) -> str`
    （`wait` 必须透传：`Stack` 的委派签名带着它，吞掉就等于把用例的等待预算改写了）
  - `Fleet.snapshot() -> None`（把机队侧自证信息落进产物；基类 no-op，容器形态写 `wslc/inspect.json`）
  - `Fleet.instance(host: str) -> str`（空串 = 没在跑）
  - `Fleet.version(host: str) -> str`（运行中进程的 Agent 版本，不问服务端）
  - `Fleet.binary_sha(host: str) -> str`
  - `Fleet.set_env(host: str, key: str, value: str | None = None) -> None`
  - `Fleet.reset_binary(host: str, ver: str = OLD_VER) -> str`
  - `Fleet.tail_log(host: str, n: int = 40) -> str`
  - 模块级函数：`make_fleet(env: Env) -> Fleet`、`hosts_for(kind: str) -> tuple[str, ...]`
  - WSL 公共助手（从 `Stack` 提成模块函数，供 `Stack` 与 `WslFleet` 共用）：
    `resolve_root(env) -> str`、`write_wsl_file(env, path, content, mode=0o644) -> None`、
    `run_unit(env, unit, argv, wait=30) -> str`、`unit_pid(env, unit) -> str`

- [ ] **Step 1: 写失败测试（工厂选择 + 委派名保留）**

创建 `scripts/tests/test_fleet.py`：

```python
"""Fleet 工厂与委派名的单测（纯逻辑，不需要 WSL/wslc）。

锁两件事：一是 fleet 名写错时必须当场报错而不是静默走 WSL；二是 `Stack` 的委派
方法名一个都不能少——用例层是按这些名字调的，改名等于把 32 条用例打空。
"""
import pytest

from scripts.e2e.core import Env
from scripts.e2e.fleet import make_fleet
from scripts.e2e.setup import Stack

# 用例层依赖的机队方法名（改名即红灯，别在这里"顺手重构"）
FLEET_METHODS = ["deploy_agents", "start_agents", "stop_agents", "start_agent",
                 "stop_agent", "term_agent", "restart_agent", "agent_pid",
                 "agent_version", "agent_binary_sha", "set_agent_env",
                 "reset_agent_binary"]


def test_make_fleet_defaults_to_wsl():
    env = Env()
    assert env.fleet == "wsl"
    assert type(make_fleet(env)).__name__ == "WslFleet"


def test_make_fleet_rejects_unknown_kind():
    env = Env()
    env.fleet = "docker"
    with pytest.raises(ValueError, match="docker"):
        make_fleet(env)


@pytest.mark.parametrize("name", FLEET_METHODS)
def test_stack_exposes_fleet_method(name):
    assert callable(getattr(Stack, name)), "Stack 缺委派方法 %s" % name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_fleet.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.tests'` 或 `ImportError: cannot import name 'make_fleet'`；缺 `scripts/__init__.py` 时先补上再判（见 Step 3）。

- [ ] **Step 3: 建包基座，让 `scripts.e2e.*` 在 pytest 下可导入**

创建两个空文件（内容只有 docstring）：

```python
# scripts/__init__.py
"""scripts 作为常规包存在：pytest 靠「向上找到第一个没有 __init__.py 的目录」
把仓库根放进 sys.path，从而让 tests/ 里能 `from scripts.e2e.x import y`。"""
```

```python
# scripts/tests/__init__.py
"""E2E 工装（scripts/e2e）的单元测试；只测纯逻辑，任何机器上都不得 skip。"""
```

验证 `python -m scripts.e2e.run` 仍是唯一入口（`scripts/__init__.py` 不影响 `-m`，也不影响 `python scripts/mqtt_e2e.py` 直跑）：

Run: `.venv/Scripts/python.exe -c "import scripts.e2e.core as c; print(c.Env().fleet)"`
Expected: 输出 `wsl`（若 `Env` 还没有 fleet 字段则报 AttributeError —— 那正是 Step 4 要做的事）

- [ ] **Step 4: `Env` 加 fleet 字段**

修改 `scripts/e2e/core.py`，在 `Env` 的字段区 `skip_build: bool = False` 之后插入：

```python
    # 机队形态：wsl = 发行版里 3 个进程（默认，CI 与无 wslc 的机器可跑）；
    # wslc = 4 个独立 Linux 容器（每台真隔离的主机）。见 scripts/e2e/fleet.py。
    fleet: str = "wsl"
    rebuild_image: bool = False
```

- [ ] **Step 5: 写 `fleet.py`（基类 + WslFleet 纯平移）**

创建 `scripts/e2e/fleet.py`。把 `setup.py` 里 `resolve_root`（113-132）、`_write_wsl_file`（409-415）、
`_run_unit`（417-438）、`_unit_pid`（439-443）、`deploy_agents`（335-407）、`start_agents`（445-447）、
`agent_pid`（448-459）、`agent_version`（460-471）、`stop_agents`（472-490）、`stop_agent`（491-505）、
`start_agent`（506-510）、`restart_agent`（511-515）、`set_agent_env`（516-525）、`agent_binary_sha`（526-531）、
`reset_agent_binary`（532-545）的**函数体原样搬过来**，只把 `self.env` → `env`、`self.resolve_root()` →
`resolve_root(env)`、`self._write_wsl_file(...)` → `write_wsl_file(env, ...)` 这类自引用改掉；
`e.root`、`e.agent_dir(host)`、`self.agent_unit(host)` 的取值逻辑不变。文件头与基类：

```python
"""机队抽象：一台主机能对被做的事，与它跑在哪无关。

存在这一层的原因：E2E 要在「4 个独立 Linux 容器」上验证命令下发、指标上报、
自更新与韧性，而现有工装把 Agent 生命周期钉死在「Ubuntu-22.04 里的 systemd 瞬态
单元」。用例层只关心 8 件事——起、停（断电/优雅）、重启、进程身份、运行版本、
磁盘二进制 sha、改环境、换回旧版——所以按这 8 件事定义接口，两种形态各自实现。

语义钉住（改实现不许改语义，否则 32 条用例会集体说谎）：
- `instance()` 空串 = 该主机没在跑；值变化 = 进程真的重启过（RES-03 靠它判「靠重连
  而非重启」）。
- `version()` 是**主机侧真相**，不问服务端。容器形态取 `/proc/1/exe` 的 sha 反查，
  证明的是「正在运行的进程镜像本身就是新二进制」。
- `binary_sha()` 在主机**离线时也必须可读**（RES-02 要断言离线期磁盘没被动过），
  所以容器形态把二进制放命名卷，而不是容器文件系统。
- `set_env()` 在容器形态只能删容器重建（env 在 create 时固定）；因为二进制在卷里，
  重建不会回滚已替换的版本。
"""
from __future__ import annotations

import time

from .core import Env, port_open, tail, wait_until, win_to_wsl, wsl, wsl_stdin
from .setup import OLD_VER, launcher

WSL_HOSTS = ("wsl-agent-01", "wsl-agent-02", "wsl-agent-03")
WSLC_HOSTS = ("kk-ct-01", "kk-ct-02", "kk-ct-03", "kk-ct-04")


def hosts_for(kind: str) -> tuple[str, ...]:
    if kind == "wsl":
        return WSL_HOSTS
    if kind == "wslc":
        return WSLC_HOSTS
    raise ValueError("未知机队形态：%r（可选 wsl / wslc）" % kind)


def make_fleet(env: Env) -> Fleet:
    """按 env.fleet 装配实现。wslc 分支延迟 import：`wslc_fleet.py` 在 Task 4 才存在，
    延迟 import 让本任务（纯平移）先立起来，不背后续任务的依赖。"""
    if env.fleet == "wsl":
        return WslFleet(env)
    if env.fleet == "wslc":
        from .wslc_fleet import WslcFleet
        return WslcFleet(env)
    raise ValueError("未知机队形态：%r（可选 wsl / wslc）" % env.fleet)


class Fleet:
    """8 个原语 + prepare/teardown。子类只碰进程与文件，不碰服务端与 Broker。"""

    kind = "wsl"

    def __init__(self, env: Env):
        self.env = env

    def prepare(self) -> str:
        raise NotImplementedError

    def start_all(self) -> str:
        return "\n".join(self.start(h) for h in self.env.agents)

    def stop_all(self) -> str:
        raise NotImplementedError

    def start(self, host: str, wait: float = 30) -> str:
        raise NotImplementedError

    def stop_hard(self, host: str, wait: float = 20) -> str:
        raise NotImplementedError

    def stop_graceful(self, host: str, wait: float = 20) -> str:
        raise NotImplementedError

    def restart(self, host: str, wait: float = 30) -> str:
        self.stop_hard(host)
        return self.start(host, wait)

    def instance(self, host: str) -> str:
        raise NotImplementedError

    def version(self, host: str) -> str:
        raise NotImplementedError

    def binary_sha(self, host: str) -> str:
        raise NotImplementedError

    def set_env(self, host: str, key: str, value: str | None = None) -> None:
        raise NotImplementedError

    def reset_binary(self, host: str, ver: str = OLD_VER) -> str:
        raise NotImplementedError

    def tail_log(self, host: str, n: int = 40) -> str:
        raise NotImplementedError

    def snapshot(self) -> None:
        """机队侧自证信息落产物。基类不做事：WSL 形态的证据都在服务端日志里。"""


class WslFleet(Fleet):
    """现有形态：Ubuntu-22.04 里 `systemd-run` 拉起的 N 个 Agent 进程。"""

    kind = "wsl"

    def agent_unit(self, host: str) -> str:
        return "kk-e2e-%s" % host
```

`WslFleet` 的方法名映射（旧 → 新，用例层通过 `Stack` 委派，外部名字不变）：
`deploy_agents→prepare`、`start_agent→start`、`stop_agent→stop_hard`、
`agent_pid→instance`、`agent_version→version`、`agent_binary_sha→binary_sha`、
`set_agent_env→set_env`、`reset_agent_binary→reset_binary`、`stop_agents→stop_all`。
新增 `stop_graceful(host)`：把 `resilience_cases.py:208` 那三行搬进来——

```python
    def stop_graceful(self, host: str, wait: float = 20) -> str:
        """SIGTERM 走 Agent 自己的收尾（先发 offline 再断），与 stop_hard 的断电区分开。"""
        pid = self.instance(host)
        if not pid:
            return "not-running"
        out = wsl_stdin(self.env, 'kill -TERM "%s" 2>/dev/null; echo sent' % pid, timeout=30)
        wait_until(lambda: not self.instance(host), wait)
        return out.strip()

    def tail_log(self, host: str, n: int = 40) -> str:
        return wsl_stdin(self.env, 'tail -%d "%s/logs/%s.log" 2>&1'
                         % (n, self.env.root, host), timeout=30)
```

- [ ] **Step 6: `Stack` 改成委派**

修改 `scripts/e2e/setup.py`：删掉 Step 5 搬走的 15 个方法体，改成——

```python
    # ------------------------------------------------------------------ 机队
    # 机队形态（WSL 进程 / wslc 容器）收在 fleet.py；Stack 保留用例在用的方法名，
    # 只做委派。新增机队形态不许动用例（见 fleet.py 模块 docstring 的语义钉住）。
    @property
    def fleet(self):
        if self._fleet is None:
            from .fleet import make_fleet
            self._fleet = make_fleet(self.env)
        return self._fleet

    def deploy_agents(self) -> str:
        return self.fleet.prepare()

    def start_agents(self) -> str:
        return self.fleet.start_all()

    def stop_agents(self) -> str:
        return self.fleet.stop_all()

    def start_agent(self, host: str, wait: float = 30) -> str:
        return self.fleet.start(host, wait)

    def stop_agent(self, host: str, wait: float = 20) -> str:
        return self.fleet.stop_hard(host, wait)

    def term_agent(self, host: str, wait: float = 20) -> str:
        return self.fleet.stop_graceful(host, wait)

    def restart_agent(self, host: str, wait: float = 30) -> str:
        return self.fleet.restart(host, wait)

    def agent_pid(self, host: str) -> str:
        return self.fleet.instance(host)

    def agent_version(self, host: str) -> str:
        return self.fleet.version(host)

    def agent_binary_sha(self, host: str) -> str:
        return self.fleet.binary_sha(host)

    def set_agent_env(self, host: str, key: str, value: str | None = None) -> None:
        self.fleet.set_env(host, key, value)

    def reset_agent_binary(self, host: str, ver: str = OLD_VER) -> str:
        return self.fleet.reset_binary(host, ver)
```

`Stack.__init__` 加一行 `self._fleet = None`（懒装配：`make_fleet` 会 import `setup`，构造期拿不到）。
`Stack.resolve_root` / `_write_wsl_file` / `_run_unit` / `_unit_pid` 保留为薄壳，转调 `fleet.py` 的模块函数
（`start_broker` 仍在用它们）：

```python
    def resolve_root(self) -> str:
        from .fleet import resolve_root          # 避环：fleet.py 会 import setup
        return resolve_root(self.env)
```

- [ ] **Step 7: 消除用例层的 WSL 泄漏**

修改 `scripts/e2e/resilience_cases.py` 的 `case_graceful_stop`，把两行 WSL 直调换成一次委派，
并删掉该文件顶部不再使用的 `wsl_stdin` 导入：

```python
    pid = stack.agent_pid(host)
    stack.term_agent(host)
    gone = bool(wait_until(lambda: not stack.agent_pid(host), 60, 1.0))
```

- [ ] **Step 8: 跑单测确认通过**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_fleet.py -q`
Expected: `PASS`，`14 passed`（1 个默认 + 1 个未知形态 + 12 个方法名参数化）

- [ ] **Step 9: WSL 机队全量回归——这就是纯平移的测试**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --skip-build`
Expected: 末行 `用例 32 条：通过 32，失败 0`，退出码 0。任一红即说明平移带了行为差异，
按 `artifacts-e2e/<run>/report.md` 的 actual 回查 Step 5 的搬运，不许跳过。

- [ ] **Step 10: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml
git add scripts/__init__.py scripts/tests scripts/e2e/fleet.py scripts/e2e/setup.py scripts/e2e/core.py scripts/e2e/resilience_cases.py
git commit -m "test(e2e): 机队抽象 Fleet（WslFleet 纯平移 + Stack 委派 + term_agent 收敛）"
```

---

### Task 2: 机队规模与用例自述一致（4 台不撒谎）

**Files:**
- Modify: `scripts/e2e/core.py:56`（`agents` 默认值改由 fleet 决定）
- Modify: `scripts/e2e/fleet.py`（`make_fleet` 里填 `env.agents`）
- Modify: `scripts/e2e/ui_cases.py:22,205,207,215,279,292,293,294`（`PODS` 与台数文案）
- Modify: `scripts/e2e/api_cases.py:81,83,124,187,226,230`、`scripts/e2e/resilience_cases.py:155,158,160,161,198`、`scripts/e2e/upgrade_cases.py:147`、`scripts/e2e/run.py:143`（标题/前置里写死的「3 台」）
- Modify: `scripts/tests/test_fleet.py`（新增规模与文案格式化用例）

**Interfaces:**
- Consumes: Task 1 的 `hosts_for(kind)`、`Env.fleet`
- Produces: `Env.agents` 在 `make_fleet(env)` 之后等于 `hosts_for(env.fleet)`；`ui_cases.set_pods(env)`

- [ ] **Step 1: 写失败测试**

在 `scripts/tests/test_fleet.py` 追加：

```python
def test_hosts_size_per_fleet():
    from scripts.e2e.fleet import hosts_for
    assert len(hosts_for("wsl")) == 3
    assert len(hosts_for("wslc")) == 4          # 用户要的「4 个 linux 容器」
    assert hosts_for("wslc") == ("kk-ct-01", "kk-ct-02", "kk-ct-03", "kk-ct-04")


def test_agents_default_to_fleet_size_without_build():
    """Env 构造期就该按 fleet 填好机队：`make_fleet` 要 import wslc_fleet（Task 4 才有），
    把填充压在它里面会让本任务无法独立成立。"""
    assert Env(fleet="wsl").agents == ("wsl-agent-01", "wsl-agent-02", "wsl-agent-03")
    assert len(Env(fleet="wslc").agents) == 4


def test_ui_title_text_tracks_fleet_size():
    """用例自述里的台数必须跟着机队规模走，否则 4 台时报告在说谎。"""
    from scripts.e2e.ui_cases import fleet_text
    assert "4 台" in fleet_text("已选 %d 台", 4)
    assert "3 台" in fleet_text("已选 %d 台", 3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_fleet.py -q`
Expected: FAIL —— `ImportError: cannot import name 'fleet_text'`；`env.agents` 仍是 3 元组。

- [ ] **Step 3: `Env.agents` 交给 fleet 决定**

修改 `scripts/e2e/core.py`：把字段默认值改成空元组，并在 `__post_init__` 里按 fleet 填——

```python
    # 默认空：由 fleet.make_fleet() 按 env.fleet 填（wsl=3 台 / wslc=4 台）。
    # 留死默认值会让 wslc 模式只有 3 台，而用例断言全是 len(env.agents) 驱动的，
    # 结果是一轮「4 台容器」的验证实际只跑了 3 台。
    agents: tuple[str, ...] = ()
```

```python
    def __post_init__(self):
        if not self.agents:
            from .fleet import hosts_for          # 循环导入靠延迟 import 打破
            self.agents = hosts_for(self.fleet)
        if not self.base:
            ...
```

（`make_fleet` **不**再重复填 `agents`：填充只发生在 `Env.__post_init__` 一处，两个真相迟早会漂移。）

`run.py` 侧不用改——`Env(...)` 构造完 `agents` 已按 `--fleet` 就位。检查一遍 `run.py:143` 的
`len(env.agents)` 打印与 `_ready()` 的就绪条件仍然成立（它们本来就是数据驱动的）。

- [ ] **Step 4: UI 层的台数与主机名动态化**

修改 `scripts/e2e/ui_cases.py`。删掉模块级硬编码 `PODS = ("wsl-agent-01", ...)`，改为：

```python
PODS: tuple[str, ...] = ()          # 由 set_pods(env) 在 run() 入口填


def set_pods(env) -> None:
    """把机队名单交给 UI 用例：页面文案与筛选目标都跟着它走。"""
    global PODS
    PODS = tuple(env.agents)


def fleet_text(template: str, n: int | None = None) -> str:
    """用例自述的统一出口：台数一律从机队取，别在字符串里写死 3。"""
    return template % (len(PODS) if n is None else n)
```

`run()` 第一行加 `set_pods(env)`。逐处替换台数/主机名断言（**这些是真断言，不是注释**）：

```python
    # _monitor(): 筛选目标用机队第二台，不再写死 wsl-agent-02
    probe_host = PODS[1] if len(PODS) > 1 else PODS[0]
    page.get_by_placeholder("主机名 / 镜像 / 系统 / 分组").fill(probe_host)
    _wait_rows(page, 1, timeout=15000)
    _need(probe_host in page.locator(".el-table__row").first.inner_text(),
          "筛选后首行不是 %s" % probe_host)
    ...
    page.wait_for_function(
        "() => document.body.innerText.includes('%s')" % fleet_text("已选 %d 台"),
        timeout=8000)
```

```python
    # _collect(): 按钮名、toast 文案、完成条数三处都跟机队规模走
    btn = page.get_by_role("button", name=fleet_text("下发采集（%d 台）"))
    ...
    _toast(page, fleet_text("已下发 %d 条采集命令"))
    _wait_done(page, len(PODS))
    return fleet_text("默认预勾 %d 项；%%d 台采集全部完成" % checked).replace(
        "默认预勾 %d 项；", "默认预勾 %d 项；" % checked, 1)
```

上式里 `return` 一行改直白写，别留嵌套格式化：

```python
    return "%d 项默认预勾；%s 台采集全部完成" % (checked, len(PODS))
```

- [ ] **Step 5: 其余用例的标题/前置动态化**

按同一口径改字符串（只改文案，**断言逻辑一处不改**）。示例（`api_cases.py:81` 与 `resilience_cases.py:155`）：

```python
    n = len(env.agents)
    check("API-01", MOD, "健康检查：服务端就绪 + Broker 已连接 + %d 台 Agent 在线" % n,
```

```python
    check("RES-03", MOD, "Broker 重启：REST 不受影响，%d 台 Agent 自动重连（进程未重启）"
          % len(env.agents),
```

其余 12 处把 `3 台` → `%d 台` 并在实参位补 `len(env.agents)`；`run.py:143` 的打印同步。

- [ ] **Step 6: 跑单测确认通过**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests -q`
Expected: `PASS`（Task 1 的 14 条 + 本任务 3 条）

- [ ] **Step 7: WSL 模式回归（3 台文案仍须对）**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --skip-build`
Expected: `用例 32 条：通过 32，失败 0`；`report.md` 里 API-01/UI-02/UI-05 的标题显示「3 台」。

- [ ] **Step 8: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml
git add scripts/e2e scripts/tests
git commit -m "test(e2e): 机队规模驱动用例自述（wslc=4 台），UI 台数与主机名断言去硬编码"
```

---

### Task 3: wslc 纯函数层（命令构造 / 解析 / 镜像文本）

**Files:**
- Create: `scripts/e2e/wslc.py`
- Create: `scripts/tests/test_wslc.py`
- Modify: `scripts/e2e/core.py`（新增 `wslc()` 调用器）
- Modify: `AGENTS.md`、`Jenkinsfile`（测试命令加 `scripts/tests`）

**Interfaces:**
- Consumes: `Env`
- Produces（Task 4/5 依赖，签名钉死）:
  - 常量：`wslc.HOST_ALIAS` / `BIN_PATH` / `PRISTINE` / `BIN_DIR` / `VOL_MOUNT` / `IMAGE_REPO` /
    `CT_PREFIX` / `VOL_PREFIX` / `BASE_IMAGE` / `CONTAINER_LOG` / `VER_TABLE: dict[str, str]`
  - `ct_name(env, host) -> str`、`vol_name(env, host) -> str`、`image_tag(ver) -> str`
  - `mount_path(p: Path | str) -> str`
  - `agent_env(env, host) -> dict[str, str]`
  - `run_args(env, host, extra_env: dict | None = None, ver: str = "0.3.0") -> list[str]`
  - `seed_args(env, host, ver) -> list[str]`、`export_args(env, ver, win_dir) -> list[str]`
  - `exec_args(env, host, inner: list[str]) -> list[str]`、`sha_args(env, host) -> list[str]`
  - `exe_sha_args(env, host) -> list[str]`、`logs_args(env, host, n=40) -> list[str]`、
    `inspect_args(name) -> list[str]`
  - `dockerfile_text() -> str`、`parse_inspect(text) -> dict`（→ `{running, instance, ip}`）、
    `version_from_sha(sha: str) -> str`
  - `core.wslc(env, *args, timeout=120) -> tuple[int, str]`

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/test_wslc.py`：

```python
"""wslc 命令层单测：全部是纯函数，不碰 wslc、不碰网络。

这里锁的是「容器能不能连回 Windows 服务端 / 卷挂在哪 / 入口是哪支文件」这些
一旦写错就整轮白跑的细节。真实可达性与行为由 E2E 的 --fleet wslc 全量跑裁决。
"""
import json

from scripts.e2e.core import Env
from scripts.e2e import wslc


def _env():
    e = Env()
    e.fleet = "wslc"
    e.port = 18443
    e.broker_port = 18832
    return e


def test_agent_env_reaches_host_via_alias():
    """Broker 在发行版、服务端在 Windows，两者都只能靠 host.wslc.internal 够着。
    host.docker.internal 在本机不存在（实测 gaierror），写成它等于静默失联。"""
    e = _env()
    env = wslc.agent_env(e, "kk-ct-01")
    assert env["KK_SERVER"] == "mqtt://host.wslc.internal:18832"
    assert env["KK_UPDATE_URL"] == "http://host.wslc.internal:18443"
    assert env["KK_HOST_NAME"] == "kk-ct-01"
    # 自更新目标必须是卷里的真 ELF；镜像路径只读，Windows bind 卷上 os.replace 不可控
    assert env["KK_AGENT_BIN"] == "/opt/kk/kk-agent"


def test_mount_path_uses_forward_slash_form():
    assert wslc.mount_path("E:\\GitHub\\KontainKeeper\\artifacts-e2e\\x") \
        == "E:/GitHub/KontainKeeper/artifacts-e2e/x"


def test_run_args_shape():
    e = _env()
    args = wslc.run_args(e, "kk-ct-02")
    assert args[:3] == ["run", "-d", "--name"]
    assert "kk-e2e-kk-ct-02" in args
    assert "--hostname" in args and "kk-ct-02" in args
    # 每台一个命名卷挂到 BIN_PATH 的父目录
    vol = "kk-e2e-kk-ct-02"
    assert "%s:/opt/kk" % vol in args
    assert args[-1] == "kk-agent-e2e:0.3.0"        # 容器永远从旧版起
    assert sum(1 for a in args if a.startswith("KK_SERVER=")) == 1


def test_set_env_removal_drops_the_default_not_overrides_it():
    """value=None 是「删除」：UPG-06 还原坏目标后必须回到默认 KK_AGENT_BIN 之外，
    若把 None 当「不覆盖」，还原后仍指向 kk-broken.py，用例会以错误的方式变绿。"""
    e = _env()
    args = wslc.run_args(e, "kk-ct-01", extra_env={"KK_AGENT_BIN": None})
    assert not any(a.startswith("KK_AGENT_BIN=") for a in args)


def test_dockerfile_entrypoints_volume_not_pristine():
    text = wslc.dockerfile_text()
    assert 'ENTRYPOINT ["/opt/kk/kk-agent"]' in text
    assert "/opt/kk-image/kk-agent" in text          # 初始二进制在镜像里，供种子/对照
    assert "build_binary.sh" in text                 # 复用仓库脚本，不重抄 PyInstaller 参数
    assert "sed -i" in text and "AGENT_VER" in text  # 版本靠构建参数注入


def test_parse_inspect_reads_running_started_at_and_ip():
    # 实测样本：State 里没有 Pid 字段（wslc 1.0.79）
    text = json.dumps([{
        "Name": "kk-e2e-kk-ct-01",
        "State": {"ExitCode": 0, "FinishedAt": "0001-01-01T00:00:00Z", "Health": None,
                  "Running": True, "StartedAt": "2026-10-29T14:28:54.650314843Z",
                  "Status": "running"},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.3"}}},
    }])
    st = wslc.parse_inspect(text)
    assert st["running"] is True
    assert st["instance"] == "2026-10-29T14:28:54.650314843Z"
    assert st["ip"] == "172.17.0.3"
    assert st["exists"] is True


def test_parse_inspect_stopped_container_has_no_instance():
    st = wslc.parse_inspect(json.dumps([{"State": {"Running": False, "StartedAt": "x"},
                                         "NetworkSettings": {}}]))
    assert st["running"] is False and st["instance"] == ""
    assert st["exists"] is True          # 停着但还在：start() 该走 `wslc start`


def test_parse_inspect_missing_container_is_not_exists():
    """`[]` 与坏输出都必须判成「容器不存在」→ start() 走 run 重建。"""
    assert wslc.parse_inspect("[]")["exists"] is False
    assert wslc.parse_inspect("no such object")["exists"] is False


def test_version_from_sha_uses_build_time_table():
    wslc.VER_TABLE.clear()
    wslc.VER_TABLE.update({"a" * 64: "0.3.0", "b" * 64: "0.4.0"})
    assert wslc.version_from_sha("b" * 64) == "0.4.0"
    assert wslc.version_from_sha("") == ""
    assert wslc.version_from_sha("f" * 64) == ""      # 对不上就当未知，不猜版本
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_wslc.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.e2e.wslc'`

- [ ] **Step 3: 写 `scripts/e2e/wslc.py`**

```python
"""wslc 命令与镜像文本的纯函数层。

只放「不需要真的调用 wslc 就能算出来」的东西：argv 构造、inspect 解析、
sha→版本反查、Dockerfile 文本。实际的起停与取证在 wslc_fleet.py。

本机 wslc 1.0.79 的硬事实（实测，别按 Docker 的习惯改）：
- `host.docker.internal` 不存在，能打通 Windows 与 WSL 发行版的是 `host.wslc.internal`；
- `inspect -f` 只接受字面量 `json`，不支持 Go 模板；`State` 无 `Pid`；
- `build --output` 报 usage error，ELF 导出走 bind 卷（`mount_path()` 的形态）；
- Git Bash 会把 `/v/marker` 转成 `V:/marker`，所以这里一律产出 argv 列表、
  由 `core.wslc()` 不经 shell 执行。
"""
from __future__ import annotations

import json
from pathlib import Path

HOST_ALIAS = "host.wslc.internal"      # 容器 → Windows 宿主 loopback（实测可达）
BIN_PATH = "/opt/kk/kk-agent"          # 命名卷内：自更新的替换目标
PRISTINE = "/opt/kk-image/kk-agent"    # 镜像内：初始二进制（只读参照 + 种子源）
BIN_DIR = "/opt/kk"
VOL_MOUNT = "/opt/kk"
IMAGE_REPO = "kk-agent-e2e"
CT_PREFIX = "kk-e2e-"
VOL_PREFIX = "kk-e2e-"
BASE_IMAGE = "python:3.12-slim-bookworm"
CONTAINER_LOG = "/tmp/kk-agent.log"    # 容器里不落 KK_LOG 文件，日志走 stdout

# 构建期由 WslcFleet.prepare() 填：{sha256(ELF): 版本号}
VER_TABLE: dict[str, str] = {}


def ct_name(env, host: str) -> str:
    return "%s%s" % (CT_PREFIX, host)


def vol_name(env, host: str) -> str:
    return "%s%s" % (VOL_PREFIX, host)


def image_tag(ver: str) -> str:
    return "%s:%s" % (IMAGE_REPO, ver)


def mount_path(p) -> str:
    """Windows 路径 → wslc bind 源（实测 `E:/dir/sub` 正斜杠形式可用）。"""
    return str(Path(p)).replace("\\", "/")


def agent_env(env, host: str) -> dict[str, str]:
    """与 WSL 机队同一套 KK_*，只改两个地址与二进制路径。

    KK_LOG 故意不设：loguru 的文件 sink 会落在容器文件系统里，容器一停就取不到；
    让日志走 stdout 由 `wslc logs` 收，反而与「主机断电」的取证需求一致。
    """
    return {
        "KK_SERVER": "mqtt://%s:%d" % (HOST_ALIAS, env.broker_port),
        "KK_TOPIC_PREFIX": env.prefix,
        "KK_HOST_NAME": host,
        "KK_GROUP": "e2e-group",
        "KK_LABELS": "role=e2e,zone=wslc",
        "KK_INTERVAL": "3",
        "KK_UPDATE_URL": "http://%s:%d" % (HOST_ALIAS, env.port),
        "KK_UPDATE_INTERVAL": "30",
        "KK_UPDATE_HMAC_KEY": env.hmac,
        "KK_AGENT_BIN": BIN_PATH,
        "KK_LOG_LEVEL": "INFO",
        "KK_E2E": "1",
    }


def _env_args(env, host: str, extra: dict | None) -> list[str]:
    """`extra` 里 value=None 表示**删除**该项（`Fleet.set_env(key, None)` 的语义），
    不是「不覆盖」——留着默认值会让 UPG-06 还原后仍然指向坏目标。"""
    merged = dict(agent_env(env, host))
    for k, v in (extra or {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    out: list[str] = []
    for k, v in merged.items():
        out += ["--env", "%s=%s" % (k, v)]
    return out


def run_args(env, host: str, extra_env: dict | None = None, ver: str = "0.3.0") -> list[str]:
    """一次性把容器起好：命名卷挂到 BIN_DIR，入口就是卷里那支真 ELF。"""
    return (["run", "-d", "--name", ct_name(env, host),
             "--hostname", host,
             "--volume", "%s:%s" % (vol_name(env, host), VOL_MOUNT)]
            + _env_args(env, host, extra_env)
            + [image_tag(ver)])


def seed_args(env, host: str, ver: str) -> list[str]:
    """无条件用镜像内的二进制覆盖卷里的二进制。

    残留即假绿灯：上轮 `--keep` 或中断会让同名卷里躺着升完的 0.4.0，
    那时「开机就是最新版」，全部升级断言都失去意义。
    """
    return (["run", "--rm", "--name", "kk-e2e-seed-%s" % host,
             "--volume", "%s:%s" % (vol_name(env, host), VOL_MOUNT),
             image_tag(ver), "sh", "-c",
             "cp %s %s && chmod 755 %s && sha256sum %s"
             % (PRISTINE, BIN_PATH, BIN_PATH, BIN_PATH)])


def export_args(env, ver: str, win_dir) -> list[str]:
    """把镜像里的 ELF 拷到 Windows 目录（服务端上传载荷 + sha 对照表）。"""
    return (["run", "--rm", "--name", "kk-e2e-export-%s" % ver,
             "--volume", "%s:/out" % mount_path(win_dir),
             image_tag(ver), "sh", "-c",
             "cp %s /out/kk-agent-%s && sha256sum /out/kk-agent-%s" % (PRISTINE, ver, ver)])


def exec_args(env, host: str, inner: list[str]) -> list[str]:
    return ["exec", ct_name(env, host)] + inner


def sha_args(env, host: str) -> list[str]:
    """磁盘真相取证：跑一次性容器读卷，**目标容器停着也能读**。"""
    return (["run", "--rm", "--name", "kk-e2e-sha-%s" % host,
             "--volume", "%s:%s" % (vol_name(env, host), VOL_MOUNT),
             image_tag("0.3.0"), "sha256sum", BIN_PATH])


def exe_sha_args(env, host: str) -> list[str]:
    """运行真相取证：/proc/1/exe 的 sha 就是正在跑的那个进程镜像。"""
    return exec_args(env, host, ["sha256sum", "/proc/1/exe"])


def logs_args(env, host: str, n: int = 40) -> list[str]:
    return ["logs", "--tail", str(n), ct_name(env, host)]


def inspect_args(name: str) -> list[str]:
    return ["inspect", "-f", "json", name]


def parse_inspect(text: str) -> dict:
    """`wslc inspect -f json` → {running, instance, ip, exists}。

    instance 取 StartedAt：实测 State 里没有 Pid 字段。容器停着时 instance 给空串，
    与 `Fleet.instance()` 的语义（空串 = 没在跑）对齐。

    exists 单独区分出来是给 `WslcFleet.start()` 用的：「容器存在但停着」必须走
    `wslc start`（容器与卷都保留 = 主机重新上线），「容器不存在」才走 `wslc run`
    重建（新 env 生效）。两者混成一条路径会让 RES-02 丢掉补投所需的现场。
    """
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001 - 解析失败一律视为「没有这个容器」，由用例红灯暴露
        return {"running": False, "instance": "", "ip": "", "exists": False}
    item = obj[0] if isinstance(obj, list) and obj else (obj if isinstance(obj, dict) else {})
    if not item:
        return {"running": False, "instance": "", "ip": "", "exists": False}
    st = item.get("State") or {}
    nets = ((item.get("NetworkSettings") or {}).get("Networks") or {})
    ip = ""
    for v in nets.values():
        ip = (v or {}).get("IPAddress") or ""
        if ip:
            break
    return {"running": bool(st.get("Running")),
            "instance": (st.get("StartedAt") or "") if st.get("Running") else "",
            "ip": ip, "exists": True}


def version_from_sha(sha: str) -> str:
    return VER_TABLE.get((sha or "").strip().lower(), "")


DOCKERFILE = '''# 由 scripts/e2e/wslc.py 生成，别手改。
# Agent 编成真 ELF：容器 PID1 就是它，自更新走 下载→sha256→os.replace→execv 全真路径。
FROM python:3.12-slim-bookworm
COPY agent/ /build/agent/
ARG AGENT_VER=0.3.0
# [build] extra 一次装齐 psutil/paho-mqtt/loguru + pyinstaller
RUN python3 -m pip install --no-cache-dir "/build/agent[build]" \\
 && sed -i "s/^AGENT_VER = .*/AGENT_VER = \\"${AGENT_VER}\\"/" /build/agent/src/kk_agent/config.py \\
 && bash /build/agent/build/build_binary.sh \\
 && mkdir -p /opt/kk-image /opt/kk \\
 && cp /build/agent/dist/kk-agent %(pristine)s && chmod 755 %(pristine)s
# 入口是**卷里的**路径：镜像只提供初始二进制，prepare() 用一次性容器拷进卷。
# 单独 `wslc run` 这个镜像会失败——这是有意的，见 spec §4.3。
ENTRYPOINT ["%(bin)s"]
'''


def dockerfile_text() -> str:
    return DOCKERFILE % {"pristine": PRISTINE, "bin": BIN_PATH}
```

- [ ] **Step 4: 加 `core.wslc()` 调用器**

在 `scripts/e2e/core.py` 的 `wsl_stdin()` 之后加：

```python
def wslc(env: Env, *args, timeout: float = 120) -> tuple[int, str]:
    """wslc 调用统一入口：argv 列表 + 钉 utf-8，返回 (rc, stdout+stderr)。

    必须走 argv 列表：一是 Windows 上没有 shell 可拼；二是 Git Bash 会把
    `/v/marker` 这类参数转换成 `V:/marker`（实测踩过），列表形式不经那一层。
    """
    try:
        p = subprocess.run(["wslc", *[str(a) for a in args]],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001 - 环境缺件也要变成可判定的返回值
        return -1, "WSLC_ERR:%s" % e
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests -q`
Expected: `PASS`，0 failed / 0 skipped（Task 1+2 的 17 条 + 本任务 9 条 = 26 条，以 pytest 汇总为准）

- [ ] **Step 6: 把 `scripts/tests` 接进门禁与 CI**

修改 `AGENTS.md` 的「协作规范」代码块与「常用命令」，把三处
`pytest agent/tests server/tests` 改为 `pytest agent/tests server/tests scripts/tests`，
并在同一段落补一句口径：

```markdown
> `scripts/tests` 是 E2E 工装自身的纯逻辑单测（wslc argv 构造、inspect 解析、版本对照表），
> 不需要 WSL/wslc 也不允许 skip；CI 里 `skipped != 0` 仍然直接红。
```

修改 `Jenkinsfile:245` 同一行命令加 `scripts/tests`（`tests >= 340` 下限不动，只会多不会少）。

- [ ] **Step 7: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml
git add scripts/e2e/wslc.py scripts/e2e/core.py scripts/tests/test_wslc.py AGENTS.md Jenkinsfile
git commit -m "test(e2e): wslc 纯函数层（argv 构造/inspect 解析/Dockerfile）+ scripts/tests 入门禁"
```

---

### Task 4: 镜像构建、ELF 导出与命名卷种子

**Files:**
- Create: `scripts/e2e/wslc_fleet.py`（本任务只写 `prepare()` 与镜像/卷部分）
- Modify: `scripts/e2e/core.py`（`Env.ct_dir` / `Env.wslc_dir` 派生产物路径）
- Modify: `scripts/e2e/setup.py`（`Stack.preflight()` 加 wslc 硬项；`agent_assets` 在 wslc 模式下仍用）
- Modify: `scripts/e2e/run.py`（`--fleet` / `--rebuild-image` 参数与装配）
- Create: `scripts/tests/test_wslc_prepare.py`

**Interfaces:**
- Consumes: Task 3 全部纯函数、`core.wslc()`、Task 1 `Fleet`
- Produces:
  - `WslcFleet.prepare() -> str`（构建两镜像 → 导出两块 ELF → 建/种 4 卷 → 填 `wslc.VER_TABLE` → 把 0.4.0 写到 `env.new_agent_file`）
  - `WslcFleet.ensure_image(ver) -> str`（返回 tag；已存在且未 `--rebuild-image` 则复用）
  - `WslcFleet.build_context() -> Path`（把 `agent/` 拷进 `env.wslc_dir/ctx`，排除 `dist`/`build`/`__pycache__`）
  - `Env.wslc_dir: Path` = `artifacts/<run>/wslc`（Dockerfile 上下文、构建日志、导出的 ELF、inspect 快照都落这里）

- [ ] **Step 1: 写失败测试（prepare 的可测部分抽成纯逻辑）**

创建 `scripts/tests/test_wslc_prepare.py`：

```python
"""prepare 里能纯算的部分：上下文拷贝清单与版本对照表。

真构建（PyInstaller）与真卷种子不在此测——那是 E2E 的活，用 mock 测等于没测。
"""
from scripts.e2e import wslc
from scripts.e2e.core import Env


def test_version_table_maps_both_elfs():
    wslc.VER_TABLE.clear()
    wslc.VER_TABLE.update({"1" * 64: "0.3.0", "2" * 64: "0.4.0"})
    assert wslc.version_from_sha("1" * 64) == "0.3.0"
    assert wslc.version_from_sha("2" * 64) == "0.4.0"


def test_upload_payload_is_new_elf(tmp_path):
    """服务端拿到的「新版本二进制」必须是本轮真构建的 ELF，不是启动器脚本。"""
    e = Env()
    e.fleet = "wslc"
    e.artifacts = tmp_path
    blob = b"\x7fELF" + b"\x00" * 32
    e.new_agent_file.write_bytes(blob)
    assert e.new_agent_file.read_bytes().startswith(b"\x7fELF")
    assert b"\r" not in e.new_agent_file.read_bytes()


def test_wslc_dir_is_under_artifacts():
    e = Env()
    assert str(e.wslc_dir).endswith("wslc")
    assert e.wslc_dir == e.artifacts / "wslc"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_wslc_prepare.py -q`
Expected: FAIL —— `AttributeError: 'Env' object has no attribute 'wslc_dir'`

- [ ] **Step 3: `Env` 加 wslc 派生路径**

在 `scripts/e2e/core.py` 的 `agent_assets` 属性旁加：

```python
    @property
    def wslc_dir(self) -> Path:
        """wslc 机队的产物目录：Dockerfile 上下文、构建日志、导出的 ELF、inspect 快照。"""
        return self.artifacts / "wslc"

    @property
    def container_ip(self) -> dict:
        """每台主机这轮实际拿到的容器 IP（写进报告，供 KK_AGENT_IPS 白名单复核）。"""
        return self._ips
```

容器内路径不属于 `Env`——它是固定的（`/opt/kk`），已经是 `wslc.BIN_DIR` 常量；再在 `Env` 上
开一个 `ct_dir()` 只会多一处需要同步的真相。`_ips: dict = field(default_factory=dict)` 与
`rebuild_image` 一起加进 `Env` 的字段区。

- [ ] **Step 4: 写 `wslc_fleet.py` 的构建与种子部分**

创建 `scripts/e2e/wslc_fleet.py`：

```python
"""wslc 容器机队：4 台独立 Linux 容器，PID1 是真 ELF kk-agent。

设计与取舍在 docs/superpowers/specs/2026-10-29-wslc-container-fleet-e2e-design.md，
尤其 §4.3（二进制放命名卷，为的是主机离线时还能读磁盘真相）。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import wslc
from .core import Env, tail, wait_until, wslc as wslc_run
from .fleet import Fleet
from .setup import NEW_VER, OLD_VER

SKIP = ("__pycache__", "dist", "build", ".pytest_cache")


class WslcFleet(Fleet):
    kind = "wslc"

    # ---------------------------------------------------------------- 准备
    def build_context(self) -> Path:
        """把 agent/ 拷成构建上下文（排除编译中间物，别把上轮 dist 拖进镜像）。"""
        ctx = self.env.wslc_dir / "ctx"
        src = self.env.repo / "agent"
        if ctx.exists():
            shutil.rmtree(ctx)
        ctx.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, ctx / "agent", ignore=shutil.ignore_patterns(*SKIP))
        (ctx / "Dockerfile").write_text(wslc.dockerfile_text(), encoding="utf-8",
                                        newline="\n")
        return ctx

    def ensure_image(self, ver: str) -> str:
        tag = wslc.image_tag(ver)
        rc, out = wslc_run(self.env, "images")
        if not self.env.rebuild_image and tag in out:
            return tag                                  # 复用：PyInstaller 一轮 1–2 分钟
        ctx = self.build_context()
        rc, out = wslc_run(self.env, "build", "-t", tag, "-f", "Dockerfile",
                           "--build-arg", "AGENT_VER=%s" % ver, str(ctx), timeout=900)
        (self.env.wslc_dir / ("image-build-%s.log" % ver)).write_text(
            out, encoding="utf-8", errors="replace")
        if rc != 0:
            raise RuntimeError("镜像 %s 构建失败（rc=%s）：\n%s" % (tag, rc, tail(out, 1200)))
        return tag

    def export_elf(self, ver: str) -> tuple[str, Path]:
        """镜像里的 ELF → Windows 目录，并返回它的 sha256（版本对照表 + 上传载荷）。"""
        rc, out = wslc_run(self.env, *wslc.export_args(self.env, ver, self.env.wslc_dir),
                           timeout=300)
        path = self.env.wslc_dir / ("kk-agent-%s" % ver)
        if rc != 0 or not path.is_file():
            raise RuntimeError("导出 %s 失败：\n%s" % (ver, tail(out, 800)))
        return hashlib.sha256(path.read_bytes()).hexdigest(), path

    def prepare(self) -> str:
        out: list[str] = []
        for ver in (OLD_VER, NEW_VER):
            out.append("image %s" % self.ensure_image(ver))
            sha, path = self.export_elf(ver)
            wslc.VER_TABLE[sha] = ver
            out.append("elf %s sha=%s %d bytes" % (ver, sha[:12], path.stat().st_size))
        self.check_table()
        # 上传载荷：服务端按 sha256/size 建清单，直接吃本轮真构建的 ELF 字节
        self.env.new_agent_file.write_bytes(
            (self.env.wslc_dir / ("kk-agent-%s" % NEW_VER)).read_bytes())
        for host in self.env.agents:
            out.append(self.seed_volume(host))
        return "\n".join(out)
```

- [ ] **Step 5: 卷种子 + 版本对照表的完整性闸**

`prepare()` 里调用的 `seed_volume()` 与 `check_table()` 在这一步定义（顺序要求：表先齐、
再种卷，否则 `seed_volume` 的 sha 判据恒不成立）——

```python
    def seed_volume(self, host: str) -> str:
        """建卷 + **无条件**覆盖成 0.3.0，然后核对 sha 真的等于本轮 0.3.0。

        不核对就是假绿灯：同名卷若躺着上轮的 0.4.0（`--keep` 或中断残留），
        「开机就是最新版」会让全部升级断言失去意义。sha 对不上就环境级中止。
        """
        wslc_run(self.env, "volume", "create", wslc.vol_name(self.env, host))
        rc, out = wslc_run(self.env, *wslc.seed_args(self.env, host, OLD_VER), timeout=300)
        old = [k for k, v in wslc.VER_TABLE.items() if v == OLD_VER]
        if rc != 0 or not old or old[0] not in (out or ""):
            raise RuntimeError("卷 %s 种子失败或 sha 不等于本轮 %s：%s\n%s"
                               % (wslc.vol_name(self.env, host), OLD_VER, rc,
                                  tail(out, 400)))
        return "seeded %s" % host

    def check_table(self) -> None:
        got = sorted(wslc.VER_TABLE.values())
        if got != sorted([OLD_VER, NEW_VER]):
            raise RuntimeError("版本对照表不完整（%s），主机侧运行版本无法判定" % got)
```

`prepare()` 末尾按这个顺序收尾：两块 ELF 导出后 `check_table()` → 写 `env.new_agent_file` →
逐台 `seed_volume(host)`。卷必须**在表齐之后**再种，否则 `seed_volume` 的 sha 判据恒不成立。

- [ ] **Step 6: CLI 与 preflight 接线**

修改 `scripts/e2e/run.py`：`_parse` 加两个参数，`Env` 构造处带上——

```python
    p.add_argument("--fleet", default="wsl", choices=["wsl", "wslc"],
                   help="机队形态：wsl=发行版内 3 进程（默认），wslc=4 个独立容器")
    p.add_argument("--rebuild-image", action="store_true",
                   help="强制重建 kk-agent-e2e 镜像（改了 agent 源码时用）")
```

```python
    env = Env(distro=args.distro, skip_build=args.skip_build,
              headless=not args.headed, root_wsl=args.root_wsl,
              fleet=args.fleet, rebuild_image=args.rebuild_image)
```

并把 `== 部署并启动 3 台 Agent ==` 的打印改成 `"== 部署并启动 %d 台 Agent（%s 机队）== "
% (len(env.agents), env.fleet)`。

修改 `scripts/e2e/setup.py` 的 `preflight()`，在返回前按形态追加硬项：

```python
        if e.fleet == "wslc":
            from . import wslc
            from .core import wslc as run_wslc
            rc, out = run_wslc(e, "--version")
            checks.append(("wslc CLI", rc == 0, (out or "").strip().splitlines()[:1] or "无"))
            rc, out = run_wslc(e, "system", "info")
            checks.append(("wslc 会话", rc == 0 and "会话:" in out, tail(out, 120)))
            rc, out = run_wslc(e, "images")
            checks.append(("wslc 基础镜像 %s" % wslc.BASE_IMAGE,
                           wslc.BASE_IMAGE in out or e.rebuild_image, tail(out, 120)))
            checks.append(("容器→宿主 loopback", self._probe_host_alias(),
                          "host.wslc.internal 实测可达（不可达则升级下载必失败）"))
```

`_probe_host_alias()` 用一次性 python 监听 + `wslc run --rm` 连它，复用 spec §2 那条探针，
返回值只取 True/False：

```python
    def _probe_host_alias(self) -> bool:
        """把「容器能不能打回宿主 loopback」固化成门禁：网络形态一变（关 mirrored、
        换 wslc 版本）就在这里当场失败，而不是等 UPG-03 报 exec_error。"""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"kk")

            def log_message(self, *a):  # 静默：探针不该污染日志
                pass

        srv = HTTPServer(("0.0.0.0", 0), H)
        port = srv.server_port
        threading.Thread(target=srv.handle_request, daemon=True).start()
        from . import wslc
        rc, out = wslc_run(self.env, "run", "--rm", "--name", "kk-e2e-netprobe",
                           wslc.BASE_IMAGE, "python3", "-c",
                           "import urllib.request;print(urllib.request.urlopen("
                           "'http://%s:%d/',timeout=5).read())" % (wslc.HOST_ALIAS, port),
                           timeout=90)
        srv.server_close()
        return rc == 0 and b"kk" in out.encode() or "kk" in out
```

- [ ] **Step 7: 真跑一次准备（这就是它的验证）**

Run: `.venv/Scripts/python.exe -u -c "
from scripts.e2e.core import Env
from scripts.e2e.setup import Stack
e = Env(fleet='wslc', skip_build=True); e.base='http://127.0.0.1:%d'%e.port
s = Stack(e); print(s.preflight())
print(s.fleet.prepare())
"`
Expected: preflight 的 wslc 四项全 True；打印两行 `image kk-agent-e2e:0.3.0` /
`image kk-agent-e2e:0.4.0`、两行 `elf 0.x.0 sha=<12位> <N> bytes`（N 应为十几 MB 量级，
**不是** 上一轮的 198 字节——那就是「还在用启动器脚本」的信号），最后一行
`seeded` ×4 无异常。`artifacts-e2e/<run>/wslc/` 下应有 `kk-agent-0.3.0`、`kk-agent-0.4.0`、
`image-build-0.3.0.log`、`image-build-0.4.0.log`。

- [ ] **Step 8: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml
git add scripts/e2e/wslc_fleet.py scripts/e2e/core.py scripts/e2e/run.py scripts/e2e/setup.py scripts/tests/test_wslc_prepare.py
git commit -m "test(e2e): wslc 镜像构建与命名卷种子（真 ELF 载荷 + 版本对照表 + 假绿灯闸）"
```

---

### Task 5: WslcFleet 生命周期方法 + 4 台容器上线（API 13 条）

**Files:**
- Modify: `scripts/e2e/wslc_fleet.py`（补齐 `Fleet` 剩余方法 + `snapshot()`）
- Modify: `scripts/e2e/fleet.py`（基类 `snapshot()` 默认 no-op，见 Step 5）
- Modify: `scripts/e2e/setup.py`（`start_keeper` 在 wslc 模式下仍要跑——Broker 在发行版；`server_env()` 的 `KK_PUBLIC_URL` 在 wslc 模式取 `http://host.wslc.internal:<port>`）
- Modify: `scripts/e2e/run.py`（wslc 模式跳过 `detect_public_url`，改用固定别名；机队就绪后调 `snapshot()`）
- Modify: `scripts/e2e/report.py`（环境表加「机队形态 / 容器 IP」两行）
- Modify: `scripts/e2e/core.py`（`Env.container_ip` 属性 + `_ips` 字段，Task 4 Step 3 已声明）
- Create: `scripts/tests/test_wslc_lifecycle.py`

**Interfaces:**
- Consumes: Task 3 全部、Task 4 的 `VER_TABLE` / `prepare`
- Produces: `WslcFleet.start/stop_hard/stop_graceful/restart/instance/version/binary_sha/set_env/reset_binary/tail_log/stop_all`；`Env.container_ip: dict[str, str]`；`report.py` 的 `fleet` / 容器 IP 行

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/test_wslc_lifecycle.py`：

```python
"""WslcFleet 的方法级单测：用「假 wslc」锁我们的 argv 决策，不锁产品行为。

这里的 fake 只替换 `core.wslc`（工装自己的 subprocess 出口），断言的是
「对容器该发哪条命令、按什么顺序发」。真实的起停、连接、升级由
`--fleet wslc` 的全量 E2E 裁决——那一层绝不允许有 fake。
"""
import json

from scripts.e2e import wslc
from scripts.e2e.core import Env


class FakeWslc:
    """记录每次 argv；inspect 按预设状态回答。

    running=False, exists=True  → 容器停着（`wslc start` 那条路）
    exists=False                → 容器不存在（`wslc run` 重建那条路）
    """

    def __init__(self, running=True, exists=True):
        self.calls: list[list[str]] = []
        self.running = running
        self.exists = exists

    def __call__(self, env, *args, timeout=120):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if argv[:2] == ["inspect", "-f"]:
            if not self.exists:
                return 1, "no such object: %s" % argv[-1]
            return 0, json.dumps([{
                "State": {"Running": self.running,
                          "StartedAt": "2026-10-29T14:28:54Z" if self.running else ""},
                "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.9"}}}}])
        if argv[:3] == ["run", "-d", "--name"] or argv[0] == "start":
            self.running = True          # 创建/启动之后就该「在跑」，否则 start() 会等满 30s
        if argv[0] == "kill" or argv[0] == "stop":
            self.running = False
        if argv[0] == "logs":
            return 0, "agent log line\n"
        if argv[0] == "exec":
            return 0, "%s  /proc/1/exe\n" % ("b" * 64)
        if argv[0] == "run":                     # 一次性取证/种子容器（--rm）
            return 0, "%s  /opt/kk/kk-agent\n" % ("a" * 64)
        return 0, ""


def _create_call(fake, host):
    """找出「创建该主机容器」的那条 argv（一次性容器同前缀，靠 -d 区分）。"""
    name = "kk-e2e-%s" % host
    for c in fake.calls:
        if c[:3] == ["run", "-d", "--name"] and c[3] == name:
            return c
    return []


def _fleet(monkeypatch, running=True, exists=True):
    import scripts.e2e.wslc_fleet as wf
    fake = FakeWslc(running=running, exists=exists)
    monkeypatch.setattr(wf, "wslc_run", fake)
    e = Env()
    e.fleet = "wslc"
    return wf.WslcFleet(e), fake


def test_stop_hard_kills_and_stop_graceful_uses_sigterm(monkeypatch):
    f, fake = _fleet(monkeypatch)
    f.stop_hard("kk-ct-01")
    assert ["kill", "kk-e2e-kk-ct-01"] in fake.calls          # SIGKILL = 断电
    f2, fake2 = _fleet(monkeypatch)
    f2.stop_graceful("kk-ct-01")
    assert ["stop", "-t", "20", "kk-e2e-kk-ct-01"] in fake2.calls  # SIGTERM = 优雅


def test_instance_empty_when_not_running(monkeypatch):
    f, _ = _fleet(monkeypatch, running=False)
    assert f.instance("kk-ct-01") == ""


def test_version_reads_running_exe_sha_and_maps(monkeypatch):
    f, _ = _fleet(monkeypatch)
    wslc.VER_TABLE.clear()
    wslc.VER_TABLE["b" * 64] = "0.4.0"
    assert f.version("kk-ct-01") == "0.4.0"                   # /proc/1/exe 真相
    wslc.VER_TABLE.clear()
    assert f.version("kk-ct-01") == ""                        # 对不上不猜版本


def test_binary_sha_reads_volume_via_throwaway_container(monkeypatch):
    """目标容器停着也要能读磁盘真相——RES-02 的离线断言靠它。"""
    f, fake = _fleet(monkeypatch, running=False)
    assert f.binary_sha("kk-ct-01") == "a" * 64
    run = [c for c in fake.calls if c and c[0] == "run"]
    assert run and "kk-agent-e2e:0.3.0" in run[0]


def test_start_records_container_ip(monkeypatch):
    f, _ = _fleet(monkeypatch)
    f.start("kk-ct-02")
    assert f.env.container_ip["kk-ct-02"] == "172.17.0.9"


def test_start_reuses_stopped_container_with_its_volume(monkeypatch):
    """停着的容器必须走 `wslc start`：RES-02 靠「同一容器 + 同一个卷」演离线补投；
    若误走 run 重建，容器名冲突直接报错，或现场被换掉。"""
    f, fake = _fleet(monkeypatch, running=False, exists=True)
    f.start("kk-ct-01")
    assert ["start", "kk-e2e-kk-ct-01"] in fake.calls
    assert _create_call(fake, "kk-ct-01") == []


def test_start_recreates_when_container_absent(monkeypatch):
    f, fake = _fleet(monkeypatch, exists=False)
    f.start("kk-ct-01")
    assert _create_call(fake, "kk-ct-01"), "容器不存在时必须走 run 重建"


def test_set_env_records_override_and_removes_container(monkeypatch):
    """env 在 create 时固定 → 改 env 只能删容器重建（卷保留，二进制不回滚）。"""
    f, fake = _fleet(monkeypatch)
    f.set_env("kk-ct-01", "KK_AGENT_BIN", "/opt/kk/kk-broken.py")
    assert ["remove", "--force", "kk-e2e-kk-ct-01"] in fake.calls
    f2, fake2 = _fleet(monkeypatch, exists=False)
    f2._overrides = {"kk-ct-01": {"KK_AGENT_BIN": "/opt/kk/kk-broken.py"}}
    f2.start("kk-ct-01")
    assert "KK_AGENT_BIN=/opt/kk/kk-broken.py" in _create_call(fake2, "kk-ct-01")


def test_set_env_none_removes_the_item_not_keeps_default(monkeypatch):
    """value=None 是「删除该项」：UPG-06 还原后若仍带着坏 KK_AGENT_BIN，
    它会继续以错误理由失败——那是「以错误的方式变绿」。"""
    f2, fake2 = _fleet(monkeypatch, exists=False)
    f2._overrides = {"kk-ct-01": {"KK_AGENT_BIN": None}}
    f2.start("kk-ct-01")
    assert not any(a.startswith("KK_AGENT_BIN=") for a in _create_call(fake2, "kk-ct-01"))


def test_reset_binary_copies_pristine_of_requested_version(monkeypatch):
    f, fake = _fleet(monkeypatch)
    f.reset_binary("kk-ct-03", "0.3.0")
    run = [c for c in fake.calls if c and c[0] == "run"]
    assert run and "kk-agent-e2e:0.3.0" in run[0]
    assert "/opt/kk-image/kk-agent" in run[0][-1]


def test_stop_all_removes_container_before_volume(monkeypatch):
    """顺序不能反：卷被容器占着时 `volume remove` 会失败并留下残留。"""
    f, fake = _fleet(monkeypatch)
    f.env.agents = ("kk-ct-01",)
    f.stop_all()
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("remove") < kinds.index("volume")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests/test_wslc_lifecycle.py -q`
Expected: FAIL —— 除 `instance`/`version` 之外多数报 `AttributeError: 'WslcFleet' object has no attribute 'stop_hard'`（Task 4 只落了 prepare 相关方法）。必须看到红灯再进 Step 3，别跳过。

- [ ] **Step 3: `server_env()` 与 `run.py` 的公开地址**

`scripts/e2e/run.py`：wslc 模式不需要探测，直接钉别名——

```python
        if env.fleet == "wslc":
            from . import wslc
            env.public_url = "http://%s:%d" % (wslc.HOST_ALIAS, env.port)
            notes.append("容器机队：Agent 可达基址=%s（实测可达）" % env.public_url)
        else:
            env.public_url = stack.detect_public_url()
```

（`stack.start_server()` 第一次仍用空 `KK_PUBLIC_URL` 起，探测/赋值后统一 `restart_server`，
两种形态走同一条注入路径，别分叉出第二套重启逻辑。）

- [ ] **Step 4: 补齐 `WslcFleet` 的方法**

```python
    # ---------------------------------------------------------------- 生命周期
    DEAD = {"running": False, "instance": "", "ip": "", "exists": False}

    def _state(self, host: str) -> dict:
        rc, out = wslc_run(self.env, *wslc.inspect_args(wslc.ct_name(self.env, host)))
        return wslc.parse_inspect(out) if rc == 0 else dict(self.DEAD)

    def start(self, host: str, wait: float = 30) -> str:
        st = self._state(host)
        if st["exists"]:
            # 容器还在（停着）→ `wslc start`：同一容器 + 同一个卷，
            # RES-02 的离线补投需要这个现场；误走 run 会撞容器名或换掉现场。
            rc, out = wslc_run(self.env, "start", wslc.ct_name(self.env, host))
        else:
            # 不在 → 创建。env 只能在创建时给，所以 set_env 之后的第一次 start 走这里
            rc, out = wslc_run(self.env,
                               *wslc.run_args(self.env, host,
                                              extra_env=dict(self._overrides.get(host, {}))))
        if rc != 0:
            raise RuntimeError("启动 %s 失败：%s" % (host, tail(out, 400)))
        if not wait_until(lambda: self._state(host)["running"], wait, 1.0):
            raise RuntimeError("%s 起不来：\n%s" % (host, self.tail_log(host)))
        self.env.container_ip[host] = self._state(host)["ip"]
        return "started %s ip=%s" % (host, self.env.container_ip[host])

    def stop_hard(self, host: str, wait: float = 20) -> str:
        """kill = SIGKILL：容器立刻退出，TCP 断，Broker 发 LWT。"""
        rc, out = wslc_run(self.env, "kill", wslc.ct_name(self.env, host))
        wait_until(lambda: not self._state(host)["running"], wait)
        return (out or "").strip() or ("killed rc=%s" % rc)

    def stop_graceful(self, host: str, wait: float = 20) -> str:
        """stop = SIGTERM 给 PID1（Agent 自己收尾，先发 offline 再断）。"""
        rc, out = wslc_run(self.env, "stop", "-t", "20", wslc.ct_name(self.env, host))
        wait_until(lambda: not self._state(host)["running"], wait)
        return (out or "").strip() or ("stopped rc=%s" % rc)

    def restart(self, host: str, wait: float = 30) -> str:
        self.stop_hard(host)
        return self.start(host, wait)

    def instance(self, host: str) -> str:
        return self._state(host)["instance"]

    def version(self, host: str) -> str:
        """运行真相：/proc/1/exe 的 sha 反查。拿不到就空串，绝不猜。"""
        if not self._state(host)["running"]:
            return ""
        rc, out = wslc_run(self.env, *wslc.exe_sha_args(self.env, host))
        if rc != 0:
            return ""
        fields = (out or "").split()
        return wslc.version_from_sha(fields[0]) if fields else ""

    def binary_sha(self, host: str) -> str:
        """磁盘真相，容器停着也能读（一次性容器挂同一个卷）。"""
        rc, out = wslc_run(self.env, *wslc.sha_args(self.env, host))
        if rc != 0:
            return ""
        for line in (out or "").splitlines():
            if wslc.BIN_PATH in line:
                return line.split()[0]
        return ""

    def set_env(self, host: str, key: str, value: str | None = None) -> None:
        """记一条覆盖项并删掉容器：env 只能在创建时给，下一次 start 才生效。

        删容器不删卷 → 已替换的二进制保留（与真实主机同一形态）。
        """
        self._overrides.setdefault(host, {})[key] = value
        wslc_run(self.env, "remove", "--force", wslc.ct_name(self.env, host))

    def reset_binary(self, host: str, ver: str = OLD_VER) -> str:
        """换回指定版本的二进制并重启：模拟「一台从未升级的老主机重新上线」。

        自更新只升不降，降级只能靠换文件 + 重启（与 WSL 形态同一语义）。
        """
        rc, out = wslc_run(self.env, *wslc.seed_args(self.env, host, ver), timeout=300)
        if rc != 0:
            raise RuntimeError("重置 %s 二进制失败：%s" % (host, tail(out, 400)))
        return self.restart(host)

    def tail_log(self, host: str, n: int = 40) -> str:
        rc, out = wslc_run(self.env, *wslc.logs_args(self.env, host, n))
        return out or "no-logs rc=%s" % rc

    def stop_all(self) -> str:
        out = []
        for host in self.env.agents:
            # 顺序不能反：卷被容器占着时 volume remove 会失败并留下残留
            rc, o = wslc_run(self.env, "remove", "--force", wslc.ct_name(self.env, host))
            out.append("removed %s rc=%s" % (host, rc))
            wslc_run(self.env, "volume", "remove", wslc.vol_name(self.env, host))
        return "\n".join(out)
```

`WslcFleet.__init__` 里加 `self._overrides: dict[str, dict] = {}`（父类 `__init__` 之后）。
`reset_binary` 复用 `seed_args`：那条命令本来就是「把镜像里的初始二进制拷进卷并打 sha」，
两个动作的差别只在版本参数。

- [ ] **Step 5: 报告与产物接线（spec §7 要求的 fleet 字段与 inspect 快照）**

`Fleet` 基类加一个默认不做事的钩子（Task 1 的 Produces 同步补上这一条）：

```python
    def snapshot(self) -> None:
        """把机队侧的自证信息落进产物。基类 no-op：WSL 形态的证据都在服务端日志里。"""
```

`WslcFleet` 实现它——每台主机的 inspect 原文落 `wslc/inspect.json`，容器 IP 顺手记进
`env.container_ip`（报告的 `env` 表要显示，白名单复核靠它）：

```python
    def snapshot(self) -> None:
        data = {}
        for host in self.env.agents:
            rc, out = wslc_run(self.env, *wslc.inspect_args(wslc.ct_name(self.env, host)))
            data[host] = {"rc": rc, "state": self._state(host), "raw": out[:4000]}
            self.env.container_ip[host] = data[host]["state"]["ip"]
        self.env.wslc_dir.mkdir(parents=True, exist_ok=True)
        (self.env.wslc_dir / "inspect.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
```

（`wslc_fleet.py` 顶部需要 `import json`。）

`run.py` 在机队就绪的 `wait_until(_ready, …)` 之后调用一次：

```python
        stack.fleet.snapshot()
        notes.append("容器 IP=%s" % (env.container_ip or "-"))
```

`scripts/e2e/report.py` 的 `_render_md` 环境表补两行（放在 `WSL 发行版` 之后）：

```python
        "| 机队形态 | `%s` |" % env.fleet,
        "| 容器 IP | `%s` |" % (", ".join("%s=%s" % kv for kv in sorted(
            getattr(env, "container_ip", {}).items())) or "-"),
```

- [ ] **Step 6: 单测 + 4 台容器上线**

Run: `.venv/Scripts/python.exe -m pytest scripts/tests -q`
Expected: `PASS`，0 skipped

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only API --skip-build`
Expected: `模块 API` 段 `13` 条全 PASS；报告 `env` 里 fleet=wslc、四台容器 IP 均为 172.17.0.x。
若 `API-01` 红（在线数不足 4），先查 `wslc logs` 里 Agent 是否报 MQTT 连接失败——那基本是
`host.wslc.internal` 或 Broker 监听口的问题，属环境级，按 spec §2 复核，不许改用例门槛。

- [ ] **Step 7: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k: s.get(k) for k in ('tests','failures','errors','skipped')})"
rm -f test-results.xml
git add scripts/e2e/wslc_fleet.py scripts/e2e/fleet.py scripts/e2e/core.py scripts/e2e/run.py scripts/e2e/setup.py scripts/e2e/report.py scripts/tests/test_wslc_lifecycle.py
git commit -m "test(e2e): WslcFleet 生命周期（kill/stop/start/取证）+ 4 台容器 API 全链路跑通"
```

---

### Task 6: UI 模块在 4 台容器机队上跑通（8 条）

**Files:**
- Modify: `scripts/e2e/ui_cases.py`（台数/主机名相关断言的实测校正）
- Modify: `scripts/e2e/core.py`（`Env.container_ip` 字段，Task 5 已用）

**Interfaces:**
- Consumes: Task 5 的 `WslcFleet`
- Produces: `--fleet wslc` 下 UI-01..08 全 PASS；`PODS` 驱动的所有页面断言在 3 台/4 台两种规模下都成立

- [ ] **Step 1: 跑一次，把失败现场拿到**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only UI --skip-build`
Expected: 至少 UI-02（总览筛选/全选读数）与 UI-05（采集面板）可能红——文案台数或
`已选 N 台` 的等待条件与实际机队不符。这是**预期中的探索步**，不要跳过。

- [ ] **Step 2: 按现场校正**

对每条红灯，先看 `report.md` 的 actual 与 `screenshots/UI-0x.png`，再判断是断言写得
太窄还是功能真有问题：

```python
    # 例：全选读数要跟着实际机队规模，而不是假设页面显示 3
    page.wait_for_function(
        "() => document.body.innerText.includes('%s')" % fleet_text("已选 %d 台"),
        timeout=8000)
```

若某处 `已选 N 台` 在页面上确实不出现（比如前端把批量上限写死、或 4 台时文案不同），
那是**产品行为**，按 spec「不改产品代码」的约束停下来回一句说明，等重新评审——
不许把断言放宽到能过为止。

- [ ] **Step 3: 全 UI 段跑通**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only API,UI --skip-build`
Expected: `21` 条 PASS（API 13 + UI 8）。截图落 `artifacts-e2e/<run>/screenshots/`，
`UI-05` 的采集 toast 显示「已下发 4 条采集命令」。

- [ ] **Step 4: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
rm -f test-results.xml
cd web && pnpm typecheck && pnpm test    # 前端未改，也必须绿（门禁不因"没改前端"豁免）
cd ..
git add scripts/e2e/ui_cases.py scripts/e2e/core.py
git commit -m "test(e2e): UI 用例适配 4 台容器机队（台数与主机名断言随规模走）"
```

---

### Task 7: UPG 真 ELF 升级（6 条）

**Files:**
- Modify: `scripts/e2e/upgrade_cases.py`（UPG-05 的 0.4.1 变体载荷；前置文案）
- Modify: `scripts/tests/test_wslc_prepare.py`（加一条「载荷是 ELF」的锁）

**Interfaces:**
- Consumes: `env.new_agent_file`（Task 4 写成真 ELF）、`WslcFleet.version()`、`binary_sha()`
- Produces: UPG-01..06 在 wslc 机队上全 PASS，其中 UPG-03 证明「运行进程镜像 = 新 ELF」

- [ ] **Step 1: 写失败测试（锁住「上传载荷必须是真二进制」）**

在 `scripts/tests/test_wslc_prepare.py` 追加：

```python
def test_upload_payload_must_be_elf_in_wslc_mode(tmp_path):
    """wslc 机队的升级载荷是 PyInstaller ELF；若又变回 198 字节的 sh 脚本，
    说明 prepare() 没跑或导出失败——这种「测了个假升级」必须当场锁住。"""
    from scripts.e2e import wslc
    e = Env(); e.fleet = "wslc"; e.artifacts = tmp_path
    (e.wslc_dir).mkdir(parents=True, exist_ok=True)
    (e.wslc_dir / "kk-agent-0.4.0").write_bytes(b"\x7fELF" + b"z" * 100)
    e.new_agent_file.write_bytes((e.wslc_dir / "kk-agent-0.4.0").read_bytes())
    assert e.new_agent_file.read_bytes().startswith(b"\x7fELF")
    assert len(e.new_agent_file.read_bytes()) > 96
    assert wslc.BIN_PATH == "/opt/kk/kk-agent"
```

Run: `.venv/Scripts/python.exe -m pytest scripts/tests -q` → Expected: PASS（本条是语义锁，
实现已在 Task 4；写在这里是为了让「载荷退化」在单测层就红，而不是等 E2E 才发现）。

- [ ] **Step 2: UPG-05 的「另一版」改用真 ELF**

`upgrade_cases.py:217` 现在是 `var_b = launcher(env, "0.4.1").encode("utf-8")`——文本脚本冒充
二进制。改成从本轮 0.4.0 载荷派生一份可辨识的变体（只改尾部注释字节，保持 ELF 头），
WSL 模式下仍走启动器脚本：

```python
def _variant_payload(env: Env) -> bytes:
    """wslc 模式：真 ELF + 追加标记字节（sha 变、内容仍可执行）；wsl 模式：旧文本启动器。"""
    if env.fleet == "wslc":
        base = env.new_agent_file.read_bytes()
        return base + b"\n# kk-e2e variant 0.4.1\n"
    return launcher(env, "0.4.1").encode("utf-8")
```

`case_rollback` 里 `var_b = _variant_payload(env)`。（追加字节不影响 `_sha(var_b)` 自洽，
回滚断言比的是「下载字节 == 上传字节」，仍然成立。）

- [ ] **Step 3: 跑 UPG 段**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only UPG --skip-build`
Expected: `6` 条 PASS。关键读数是 UPG-03 的 actual：`台账=done(...)`、`磁盘sha==清单:True`、
`容器版本=0.4.0`；`stack.agent_version(host)` 走的是 `/proc/1/exe` 的 sha 反查，
它等于 0.4.0 就是「正在跑的进程镜像确实是新 ELF」的证据。

若 UPG-03 红在 `exec_error`：先 `wslc logs` 看 Agent 侧的替换日志，再确认卷内文件权限
`755`（`seed_args` 里有 `chmod`）与 `KK_AGENT_BIN=/opt/kk/kk-agent` 一致。若红在
`sha256_mismatch`：说明 `VER_TABLE` 与实际上传字节不同源（Task 4 的导出与 Step 2 的变体
各自算了一次 sha），统一到 `env.new_agent_file` 的字节为准。

- [ ] **Step 4: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
rm -f test-results.xml
git add scripts/e2e/upgrade_cases.py scripts/tests/test_wslc_prepare.py
git commit -m "test(e2e): UPG 升级用例改用真 ELF 载荷（wslc 机队 os.replace/execv 全真）"
```

---

### Task 8: RES 韧性 + 双模式全量回归

**Files:**
- Modify: `scripts/e2e/resilience_cases.py`（前置文案与 `instance` 语义复核）
- Modify: `scripts/e2e/run.py`（收尾顺序：wslc 模式下先停容器再放 Broker）

**Interfaces:**
- Consumes: 前 7 个任务的全部产出
- Produces: `--fleet wslc` 全量 32 条 PASS；`--fleet wsl` 全量 32 条 PASS

- [ ] **Step 1: 跑 RES 段**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only API,UI,UPG,RES --skip-build`
Expected: `32` 条 PASS，退出码 0。

RES 各条在容器语义下的判读口径（红灯时按此定位，别改用例门槛）：

| 用例 | 容器侧含义 | 常见假红根因 |
| --- | --- | --- |
| RES-01 | `wslc kill` 后 `instance()` 必须变空串 | `_state()` 解析不到 `Running` → `parse_inspect` 与真实 `-f json` 输出对不上 |
| RES-02 | 离线期 `binary_sha()` 读卷必须成功且 ≠ 清单 sha；`wslc start` 后补投升完 | 卷被上轮残留污染（Task 4 的种子闸没生效）；或 `start()` 走了 `run` 而非 `start`，容器重名报错 |
| RES-03 | Broker 重启期间 `instance()` 不变 = 容器没重启过 | `StartedAt` 在 stop/start 之间会变，若 `restart` 路径被误调，pid 断言即红 |
| RES-04 | 服务端重启与容器无关 | — |
| RES-05 | `wslc stop` = SIGTERM → `status_reason=stopping` | 容器 StopSignal 被镜像改成别的；`python:3.12-slim` 默认 SIGTERM，无 Dockerfile STOPSIGNAL 即为对 |

- [ ] **Step 2: 双模式全量回归**

```bash
.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc            # 含 pnpm build，全量
.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wsl             # 回归现有 3 台进程机队
```
Expected: 两次末行都是 `用例 32 条：通过 32，失败 0`，退出码 0。这是 spec §9 的第 1、2 条验收。

- [ ] **Step 3: `--keep` 残留必须可见（否则下一轮「种子闸」报错没人看得懂）**

收尾顺序本身不用改：`Stack.stop_all()` 已是「机队 → 服务端 → Broker → 保活会话」，
Task 1 的委派让 wslc 形态自动继承这个顺序。要补的是 `--keep` 分支的自证信息——
`scripts/e2e/run.py` 里那段 `print("== --keep：栈保留在 … ==")` 之后追加：

```python
            if env.fleet == "wslc":
                from .core import wslc as run_wslc
                rc, listing = run_wslc(env, "list", "-a")
                _, vols = run_wslc(env, "volume", "ls")
                notes.append("残留容器=%s 残留卷=%s"
                             % (len([l for l in listing.splitlines() if "kk-e2e-" in l]),
                                len([l for l in vols.splitlines() if "kk-e2e-" in l])))
                print("   清理：wslc remove --force <name>；wslc volume remove <name>")
```

验证：

```bash
.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only API --skip-build --keep
wslc list -a | grep -c kk-e2e-
wslc volume ls | grep -c kk-e2e-
.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only UPG --skip-build   # 下一轮：种子闸必须把残留卷覆盖回 0.3.0
```
Expected: 第一次运行报告的 `notes` 里带残留容器数与卷数；第二次运行**不因残留报错**，
且 UPG-03 的起点确实是 0.3.0（这一步同时验证了 spec §8「假绿灯」那道闸真的有效）。

- [ ] **Step 4: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
rm -f test-results.xml
git add scripts/e2e
git commit -m "test(e2e): 韧性用例在容器机队跑通（kill/stop/卷取证），双模式 32 条全绿"
```

---

### Task 9: 文档、skill 与运行手册同步

**Files:**
- Modify: `docs/e2e-real-env.md`（新增 §10 wslc 容器机队；§2 前置、§3 参数、§7 已知坑各补条目）
- Modify: `.qoder/skills/kk-e2e/SKILL.md`（何时用容器机队、取证口径、命令样例）
- Modify: `docs/development.md`（wslc 环境搭建一节，指向 spec 的实测表）
- Modify: `AGENTS.md`（目录与架构边界里补 `scripts/e2e/fleet.py` / `wslc.py` / `wslc_fleet.py`）

**Interfaces:**
- Consumes: 全部已完成实现
- Produces: 下一个人照着文档能在有 wslc 的机器上跑起容器机队；没有 wslc 的机器默认走 WSL 形态

- [ ] **Step 1: 写 wslc 章节**

`docs/e2e-real-env.md` 新增一节，必须包含：spec §2 的实测表原样引用（哪条路可达、哪条不可达）、
`--fleet wslc` 的前置（wslc 版本、`hostLoopback` 设置、基础镜像、容器出网）、镜像构建耗时与
`--rebuild-image` 的适用场景、卷与容器的残留清理顺序（`wslc remove --force` 先于
`wslc volume remove`）、取证口径表（`instance`/`version`/`binary_sha` 各自证明什么）。

已知坑表补两条，措辞照下面写实：

```markdown
| 容器里 Agent 连不上 Broker/服务端 | wslc 容器打不到 `172.17.0.1`，也没有 `host.docker.internal`；只有 `host.wslc.internal` 同时够着 Windows 与发行版 loopback | preflight 的「容器→宿主 loopback」硬项实测把关；地址一律取 `wslc.HOST_ALIAS` |
| 手工敲 `wslc exec/run ... cat /v/marker` 报 `V:/marker 不存在` | Git Bash 把 POSIX 绝对路径参数转换成 Windows 路径 | harness 用 argv 列表调用不受影响；手工验证加 `MSYS_NO_PATHCONV=1` |
```

- [ ] **Step 2: 更新 skill**

`.qoder/skills/kk-e2e/SKILL.md`：description 补「4 台 wslc 容器机队」；「怎么跑」加
`--fleet wslc` / `--rebuild-image` 两条样例；「改用例时必守的规矩」新增两条——
「取证必须走主机侧真相：`instance/version/binary_sha` 由 Fleet 提供，容器形态的 `version()` 读
`/proc/1/exe` 的 sha，**不要**退回服务端字段」（并把原第 2 条「只断言 API 真值」改成
「API 真值 + 主机侧真相双向对齐，两者互为独立证据」）；以及「升级载荷必须是真 ELF，
若 `env.new_agent_file` 小于 1MB 就说明 prepare 没跑或导出失败，属环境级故障」。

- [ ] **Step 3: 交叉核对文档与实际输出**

Run: `.venv/Scripts/python.exe -u -m scripts.e2e.run --fleet wslc --only API --skip-build`
Expected: 文档里写的每条命令、每个 preflight 项名称、每个产物文件名都能在实跑输出里
逐字对上。对不上就改文档（不是改用例）。

- [ ] **Step 4: 门禁 + 提交**

```bash
.venv/Scripts/python.exe -m pytest agent/tests server/tests scripts/tests -q --junitxml=test-results.xml
rm -f test-results.xml
git add docs .qoder/skills/kk-e2e/SKILL.md AGENTS.md
git commit -m "docs(e2e): wslc 容器机队运行手册与 skill 同步（实测可达性、取证口径、两个新坑）"
```

---

## 完成判定

1. `--fleet wslc`（4 台容器）全量 32 条 PASS；`--fleet wsl`（3 台）全量 32 条 PASS。
2. `pytest agent/tests server/tests scripts/tests` failures/errors = 0 且 skipped = 0（新目录）；
   `pnpm typecheck && pnpm test` 全绿。
3. 升级证据链完整：服务端台账 `done` + 容器 `/proc/1/exe` sha 反查 = 0.4.0 + 卷内文件 sha == 清单 sha，
   三者互为独立取证。
4. 文档/skill/AGENTS.md 与实跑输出逐字一致，没有「文档里能跑、实际跑不出来」的命令。
