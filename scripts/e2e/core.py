"""KontainKeeper 真实环境 E2E —— 公共底座。

拓扑（全部真跑，不用 mock）：

  WSL(Ubuntu-22.04)
   ├─ mosquitto 18831（专用实例 + 持久化，可独立重启）
   └─ kk-agent x3（$HOME/kk-e2e/agents/<host>/kk-agent，真 MQTT 上行）
  Windows
   ├─ kk-server 18443（独立 SQLite + web/dist 静态托管）
   └─ Chromium(Playwright) 直连页面

本模块只放「与用例无关」的能力：HTTP/二进制下载、WSL 命令、用例登记、等待原语。
用例分文件：api_cases / ui_cases / upgrade_cases / resilience_cases。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DISTRO = "Ubuntu-22.04"


def win_to_wsl(path: str | Path) -> str:
    """Windows 绝对路径 → WSL 的 /mnt/<drive>/... 形式。

    比 `wsl wslpath` 稳：不必经过 cmd.exe 的引号层，也不会被 `\\` 吃掉。
    """
    p = str(Path(path).resolve())
    drive = p[0].lower()
    return "/mnt/%s/%s" % (drive, p[3:].replace("\\", "/"))


@dataclass
class Env:
    """一次运行的完整环境描述。所有用例只读它，不自己拼路径/端口。"""

    repo: Path = REPO
    distro: str = DEFAULT_DISTRO
    port: int = 18443
    broker_port: int = 18831
    prefix: str = "kk/e2e"
    admin_user: str = "admin"
    admin_pass: str = "admin123"
    hmac: str = "kk-e2e-hmac"
    headless: bool = True
    skip_build: bool = False
    agents: tuple[str, ...] = ("wsl-agent-01", "wsl-agent-02", "wsl-agent-03")
    token: str = ""
    base: str = ""
    # Agent 侧可达的服务端基址（WSL→Windows 的 curl 实测结果）。可能与 base 不同：
    # NAT 模式下 WSL 访问不到 Windows 的 127.0.0.1，必须改用默认网关地址。
    public_url: str = ""
    run_id: str = ""
    home_wsl: str = ""
    root_wsl: str = ""
    # 由 setup 填：运行产物目录（截图 / 下载 / 日志）
    artifacts: Path = field(default_factory=lambda: REPO / "artifacts-e2e")

    def __post_init__(self):
        if not self.base:
            self.base = "http://127.0.0.1:%d" % self.port
        if not self.run_id:
            self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.artifacts = Path(self.artifacts) / self.run_id

    # ---- 派生路径：Windows 侧 ----
    @property
    def db_path(self) -> Path:
        return self.artifacts / "kk-e2e.db"

    @property
    def agent_assets(self) -> Path:
        return self.artifacts / "agent_assets"

    @property
    def web_dist(self) -> Path:
        return self.repo / "web" / "dist"

    @property
    def new_agent_file(self) -> Path:
        """上传给服务端的「新版本二进制」（启动器脚本，见 upgrade_cases）。"""
        return self.artifacts / "kk-agent-0.4.0"

    # ---- 派生路径：WSL 侧 ----
    @property
    def root(self) -> str:
        return self.root_wsl or "$HOME/kk-e2e"

    def agent_dir(self, host: str) -> str:
        return "%s/agents/%s" % (self.root, host)

    def is_wsl_path(self) -> bool:
        return self.root_wsl.startswith("/")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http(env: Env, method: str, path: str, *, token: str | None = None,
         json_body: dict | None = None, params: dict | None = None,
         timeout: float = 20, data: bytes | None = None,
         headers: dict | None = None) -> tuple[int, str, dict]:
    """返回 (status, text, headers)。4xx/5xx 不抛异常，交给用例判。

    `data` / `headers` 是给 multipart 上传（自更新二进制）留的口子：那种请求体
    没法用 json_body 表达，而上传恰好是本套 E2E 的核心动作之一。
    """
    url = env.base + path
    if params:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if q:
            url += ("&" if "?" in url else "?") + q
    hdrs = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    if token if token is not None else env.token:
        hdrs["Authorization"] = "Bearer " + (token if token is not None else env.token)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, text, dict(r.headers)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        return e.code, text, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - 网络异常也要变成用例结果
        return -1, "ERR:%s" % e, {}


def http_bytes(env: Env, path: str, *, token: str | None = None,
               timeout: float = 60) -> tuple[int, bytes, dict]:
    url = env.base + path
    headers = {}
    tok = token if token is not None else env.token
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return -1, b"ERR:%s" % str(e).encode(), {}


def hdr(headers: dict, name: str, default: str = "") -> str:
    """大小写无关地取响应头。

    urllib 把响应头原样保存在 `email.message` 里，键名跟着**服务端**发的走；
    uvicorn 发的是小写（`content-disposition`），`dict(r.headers)` 因此拿不到
    首字母大写的写法。曾经 UPG-02 就因为 `headers.get("Content-Disposition")`
    恒为 None 而假红灯——actual 里其他字段全对，误导了很久。
    """
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return default


def jget(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_raw": text[:500]}


def api(env: Env, method: str, path: str, **kw) -> tuple[int, dict]:
    """http + jget 的组合：用例读 body 字段，不再各自解一遍 JSON。"""
    st, text, _ = http(env, method, path, **kw)
    return st, jget(text)


def login(env: Env, password: str | None = None) -> str:
    """登录并把 token 写回 env.token；失败时返回空串（不抛，交给用例判）。"""
    st, body = api(env, "POST", "/api/login", token="", json_body={
        "username": env.admin_user,
        "password": env.admin_pass if password is None else password,
    })
    tok = body.get("token", "") if st == 200 else ""
    if tok:
        # 覆盖而不是 `or`：服务端重启后旧 token 失效，重新登录必须能把新值写回去
        env.token = tok
    return tok


def wait_api(env: Env, path: str, pred, timeout: float = 30,
             interval: float = 1.0, **kw) -> tuple[dict | None, dict]:
    """轮询接口直到 pred(body) 为真；返回 (命中的 body | None, 最后一次 {status,body})。

    失败时把最后一次读数交给调用方拼进 actual —— 「等了 30 秒什么都没发生」
    与「接口返回了 500」必须能在报告里区分。
    """
    last: dict = {"status": 0, "body": {}}

    def _probe():
        st, body = api(env, "GET", path, **kw)
        last.update(status=st, body=body)
        return body if st == 200 and pred(body) else None

    got = wait_until(_probe, timeout, interval)
    return got, last


# ----------------------------------------------------------------------------
# WSL
# ----------------------------------------------------------------------------
def wsl(env: Env, cmd: str, timeout: float = 60) -> str:
    """shell=False + 列表参数：经 cmd.exe 会吃掉 $()、$!、& 等 shell 语法。"""
    try:
        # 必须钉 utf-8：Windows 上 text=True 默认走本地代码页（GBK），
        # 日志/工具输出里一有 UTF-8 字节就在读线程里 UnicodeDecodeError 炸掉。
        out = subprocess.run(["wsl", "-d", env.distro, "bash", "-lc", cmd],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as e:  # noqa: BLE001
        return "WSL_ERR:%s" % e


def wsl_stdin(env: Env, script: str, timeout: float = 120) -> str:
    """把脚本经 stdin 喂给 WSL 的 bash：彻底绕开 wsl.exe 的引号/分词破坏。"""
    try:
        p = subprocess.Popen(["wsl", "-d", env.distro, "bash"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out, err = p.communicate(input=script.encode("utf-8"), timeout=timeout)
        return (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return "WSL_ERR:%s" % e


def pnpm_exe() -> str:
    """可被 CreateProcess 直接执行的 pnpm 路径。

    Windows 上 `where pnpm` 的首行常是**无扩展名的 sh 脚本**
    （`D:\\nodejs\\node_global\\pnpm`），`subprocess.run(["pnpm", ...])` 会
    直接 WinError 2（找不到文件）——本仓库真实踩过一次，整轮 E2E 白跑。
    取 `.cmd` / `.exe` 那一行；都没有再退回 `pnpm`。
    """
    if os.name != "nt":
        return "pnpm"
    try:
        out = subprocess.run(["where", "pnpm"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "pnpm"
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.lower().endswith((".cmd", ".exe")):
            return line
    return "pnpm"


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Waiter:
    """轮询直到条件成立；返回是否成功，失败时把最后一次读数带出去。"""

    def __init__(self, timeout: float = 30, interval: float = 1.0):
        self.timeout = timeout
        self.interval = interval
        self.last = None

    def until(self, fn):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.last = fn()
                if self.last:
                    return self.last
            except Exception as e:  # noqa: BLE001
                self.last = "ERR:%s" % e
            if time.time() >= deadline:
                return None
            time.sleep(self.interval)


def wait_until(fn, timeout: float = 30, interval: float = 1.0):
    w = Waiter(timeout, interval)
    return w.until(fn)


# ----------------------------------------------------------------------------
# 用例登记
# ----------------------------------------------------------------------------
CASES: list[dict] = []


def record(cid: str, module: str, title: str, precond: str, steps: str,
           expect: str, actual: str, result: str, detail: str = "",
           screenshot: str = "") -> None:
    CASES.append({
        "id": cid, "module": module, "title": title, "precond": precond,
        "steps": steps, "expect": expect, "actual": actual, "result": result,
        "detail": detail, "screenshot": screenshot,
    })


def check(cid: str, module: str, title: str, ok: bool, actual,
          *, precond: str = "", steps: str = "", expect: str = "",
          detail: str = "", screenshot: str = "") -> bool:
    """`ok` 是唯一判据；actual/expect 只用于报告可读性。"""
    record(cid, module, title, precond, steps, expect,
           trunc(actual, 300), "PASS" if ok else "FAIL", detail, screenshot)
    print("[%s] %s %s — %s" % ("PASS" if ok else "FAIL", cid, title, trunc(actual, 140)))
    return bool(ok)


def trunc(s, n: int = 240) -> str:
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= n else s[:n] + "...(truncated)"


def tail(text: str, n: int = 600) -> str:
    text = text or ""
    return text[-n:]