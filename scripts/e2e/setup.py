"""E2E 环境编排：专用 broker、隔离服务端、WSL Agent 机队、自更新启动器。

设计要点（都是踩过的坑换来的）：

- **专用 broker（非 1883）**：开发机上的 1883 常被别的实例占用；专用实例还能被
  独立重启，用来验证「broker 重启后 Agent 自动重连 + 保留会话补投」。
- **独立 topic 前缀与 client_id**：`kk/e2e` + `kk-server-e2e`，不与开发中的服务端
  抢订阅、不把对方的在线状态判掉。
- **独立 SQLite + KK_WEB_DIR**：每次运行从空库开始，页面直接吃 `web/dist`，
  不往 `server/src/kk_server/web/` 里塞构建产物。
- **Agent 自更新**：源码形态不支持自替换（updater 只认 sys.frozen），这里用
  「启动器脚本」模拟打包后的单文件二进制——它走完全相同的 下载→校验→替换→execv
  路径，只是入口再 exec 到对应版本源码树（app-0.3.0 / app-0.4.0）。
"""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from .core import (Env, REPO, pnpm_exe, port_open, tail, wait_until, win_to_wsl,
                   wsl, wsl_stdin)

AGENT_SRC = REPO / "agent" / "src" / "kk_agent"
OLD_VER = "0.3.0"
NEW_VER = "0.4.0"


def pick_port(preferred: int, host: str = "127.0.0.1", tries: int = 30) -> int:
    """从 preferred 起找空闲端口：开发机上 8443/1883 常年被占，硬编码会互相打架。"""
    for p in range(preferred, preferred + tries):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port near %d" % preferred)


def launcher(env: Env, ver: str) -> str:
    """模拟打包产物：可执行、可被原子替换、execv 后仍是同一入口。

    与 PyInstaller 单文件二进制在 updater 眼里的区别只有「内部是不是 ELF」——
    形态校验只看文件名（不是 .py / 不含 python），所以这里能得到与真机一致的行为。
    路径写**绝对**的：这份启动器由 systemd 拉起，单元环境里没有 `$HOME`。
    """
    return """#!/bin/sh
# kk-e2e 自更新启动器（模拟打包后的单文件二进制；版本 %s）
APP_DIR="%s/app-%s"
PYTHONPATH="$APP_DIR"
export PYTHONPATH
exec python3 -m kk_agent "$@"
""" % (ver, env.root, ver)


class Stack:
    # 长驻进程一律交给 systemd 瞬态单元托管：`wsl.exe` 客户端一退出，WSL 会把
    # 该会话里的后台进程连锅端（setsid/nohup 都挡不住，实测 `&` 的作业连第一行
    # 都没执行就被杀）。表现极具误导性：`STARTED pid=…` 返回成功、agent 日志为空、
    # 端口不监听。`systemd-run --unit=… --collect` 让 systemd 当父进程，客户端退出
    # 与它无关，还能用 `systemctl stop/kill` 精确控制「优雅停机」和「kill -9」。
    BROKER_UNIT = "kk-e2e-broker"

    def __init__(self, env: Env):
        self.env = env
        self.server: subprocess.Popen | None = None
        self.server_log = None
        self.keeper: subprocess.Popen | None = None

    # ---------------------------------------------------------------- WSL 保活
    def start_keeper(self, hours: float = 6) -> None:
        """挂一个长睡会话，把 WSL 实例钉住（**先于 Broker 启动**）。

        这是整轮 E2E 的生死线：WSL 的实例生命周期跟「还有没有 wsl.exe 客户端」
        绑定——最后一个客户端退出后 8~16 秒，整个发行版被终止，systemd 里的
        瞬态单元（Broker、Agent）跟着一起死。实测证据（20261029-190231 那轮）：
        19:02:57 Broker 掉线、19:03:07 `logind: System is powering down`，
        随后 UI-04/05/07 连锁超时，报告看起来像「命令通道不通」，其实栈已经没了。

        systemd-run 只能让进程「脱离 wsl.exe 的会话组」，管不了实例本身：
        实例没了，systemd 也没了。保活会话是最小代价的解法（不碰用户的
        .wslconfig，不改发行版配置），收尾随 stop_all 一起放掉。
        """
        if self.keeper and self.keeper.poll() is None:
            return
        self.keeper = subprocess.Popen(
            ["wsl", "-d", self.env.distro, "--exec", "sleep", str(int(hours * 3600))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def stop_keeper(self) -> None:
        if self.keeper and self.keeper.poll() is None:
            self.keeper.terminate()
            try:
                self.keeper.wait(10)
            except subprocess.TimeoutExpired:
                self.keeper.kill()
                self.keeper.wait(5)
        self.keeper = None

    @property
    def broker_unit(self) -> str:
        return self.BROKER_UNIT

    def agent_unit(self, host: str) -> str:
        return "kk-e2e-%s" % host

    def resolve_root(self) -> str:
        """把 `$HOME/kk-e2e` 落成绝对路径（幂等）。

        不能把 `$HOME` 留在脚本里：Agent 由 systemd 瞬态单元拉起，单元环境没有
        HOME，`cd "$HOME/kk-e2e"` 会变成 `cd /kk-e2e` 直接失败（实测报
        "can't cd to /kk-e2e"）。broker 那边是 shell 展开所以没暴露这个问题。
        """
        e = self.env
        if e.root_wsl.startswith("/"):
            return e.root_wsl
        out = wsl_stdin(e, 'printf "KKHOME=%s\\n" "$HOME"', timeout=30)
        home = ""
        for line in out.splitlines():
            if line.startswith("KKHOME="):
                home = line.split("=", 1)[1].strip()
        if not home.startswith("/"):
            raise RuntimeError("解析 WSL $HOME 失败：%r" % out)
        e.home_wsl = home
        e.root_wsl = "%s/kk-e2e" % home
        return e.root_wsl

    # ------------------------------------------------------------------ 探测
    def preflight(self) -> list[tuple[str, bool, str]]:
        """返回 [(检查项, 是否通过, 细节)]；不抛异常，由调用方决定是否中止。"""
        e = self.env
        checks: list[tuple[str, bool, str]] = []

        distros = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True)
        names = distros.stdout.decode("utf-16-le", "replace").replace("\x00", "").split()
        checks.append(("WSL 发行版 %s" % e.distro, e.distro in names, str(names)))

        py = e.repo / ".venv" / "Scripts" / "python.exe"
        checks.append((".venv Python", py.is_file(), str(py)))

        mosq = wsl(e, "command -v mosquitto || true").strip()
        checks.append(("WSL mosquitto", bool(mosq), mosq or "未安装"))

        deps = wsl(e, "python3 -c 'import paho.mqtt, psutil, loguru; print(\"ok\")' "
                      "2>&1 | tail -1").strip()
        checks.append(("WSL Agent 依赖", deps == "ok", deps))

        # 探到的是 .cmd 而不是无扩展名的 sh 脚本：后者 CreateProcess 起不来
        pnpm = pnpm_exe()
        checks.append(("pnpm", pnpm.lower().endswith((".cmd", ".exe"))
                       or pnpm == "pnpm", pnpm))

        browsers = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        checks.append(("Playwright Chromium", browsers.is_dir(), str(browsers)))

        mock_dir = e.repo / "web" / "mock"
        checks.append(("web/mock 目录", mock_dir.is_dir(), str(mock_dir)))
        return checks

    # ------------------------------------------------------------------ 前端
    def prepare_web(self) -> str:
        """构建 web/dist（可用 --skip-build 复用上次产物）。返回摘要文本。"""
        e = self.env
        if e.skip_build:
            idx = e.web_dist / "index.html"
            return "skip-build: index.html=%s" % idx.is_file()
        t0 = time.time()
        # 钉 utf-8：pnpm/vite 的输出含中文与 UTF-8 符号，按 GBK 解码会在读线程
        # 里 UnicodeDecodeError（见过一次：整轮在「构建前端」阶段白跑）
        p = subprocess.run([pnpm_exe(), "build"], cwd=str(e.repo / "web"),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
        (e.artifacts / "web-build.log").write_text(out, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError("pnpm build 失败：%s" % tail(out, 800))
        return "pnpm build %.1fs" % (time.time() - t0)

    # ------------------------------------------------------------------ Broker
    def start_broker(self) -> None:
        e = self.env
        self.resolve_root()
        # 配置在 Windows 侧写好、经 /mnt 挂载点拷进 WSL。**不要**用 heredoc 从
        # `wsl.exe` 的 stdin 写它：WSL 的 DLT 日志会往同一个流里插内核消息，conf
        # 第 4 行可能变成 `[ 141.309931]~DLT~ ...`，mosquitto 报
        # `Unknown configuration variable "["` 退出，随后以默认配置去抢 1883 ——
        # 症状是「Using default config + Address already in use」，极难联想。
        # 同理 `@ROOT@` 占位由 sed 展开：`root` 常是 `$HOME/kk-e2e` 字面量，
        # 而 mosquitto 不做变量展开，直接写进去会让持久化目录不存在。
        conf = e.artifacts / "mosquitto.conf"
        conf.write_text(
            "listener %d 0.0.0.0\n"
            "allow_anonymous true\n"
            # mosquitto 以 root 启动会自动降权到 `mosquitto` 用户，而持久化/日志
            # 目录在 /root（0700）下 → "Unable to open log file" 直接退出。
            # 一次性 e2e 实例显式保持 root 最简单也最可控。
            "user root\n"
            "persistence true\n"
            "persistence_location @ROOT@/broker/data/\n"
            "log_dest file @ROOT@/broker/mosquitto.log\n"
            "log_type all\n" % e.broker_port,
            encoding="utf-8")
        script = """set -e
ROOT="%s"
mkdir -p "$ROOT/broker/data" "$ROOT/logs"
sed "s|@ROOT@|$ROOT|g" "%s" > "$ROOT/broker/mosquitto.conf"
systemctl stop %s >/dev/null 2>&1 || true   # 残留的 --keep 栈占着同名单元
systemctl reset-failed %s >/dev/null 2>&1 || true
systemd-run --unit=%s --collect --quiet \\
  /usr/sbin/mosquitto -c "$ROOT/broker/mosquitto.conf"
echo BROKER_UNIT=%s
""" % (e.root, win_to_wsl(conf), self.broker_unit, self.broker_unit,
       self.broker_unit, self.broker_unit)
        out = wsl_stdin(e, script, timeout=60)
        if not wait_until(lambda: port_open("127.0.0.1", e.broker_port), 15):
            # 起不来时把单元状态 + 进程日志一起带进异常：只报「没监听」没法排查
            log = wsl_stdin(e, 'systemctl status %s --no-pager -l | head -20; '
                               'journalctl -u %s -n 20 --no-pager 2>&1; '
                               'tail -20 "%s/broker/mosquitto.log"'
                            % (self.broker_unit, self.broker_unit, e.root), timeout=30)
            raise RuntimeError("broker 未在 %d 起监听：%s\n%s"
                               % (e.broker_port, out, log))

    def stop_broker(self) -> None:
        wsl_stdin(self.env,
                  'systemctl stop %s 2>/dev/null; systemctl reset-failed %s '
                  '2>/dev/null; rm -f "%s/broker/pid"; echo stopped'
                  % (self.broker_unit, self.broker_unit, self.env.root), timeout=30)
        wait_until(lambda: not port_open("127.0.0.1", self.env.broker_port), 10)

    def restart_broker(self) -> None:
        self.stop_broker()
        self.start_broker()

    # ------------------------------------------------------------------ Server
    def server_env(self) -> dict:
        e = self.env
        env = os.environ.copy()
        env.update({
            "KK_HOST": "0.0.0.0",
            "KK_PORT": str(e.port),
            "KK_DB_PATH": str(e.db_path),
            "KK_DB_URL": "",
            "KK_WEB_DIR": str(e.web_dist),
            "KK_AGENT_BIN_DIR": str(e.agent_assets),
            "KK_MQTT_URL": "mqtt://127.0.0.1:%d" % e.broker_port,
            "KK_TOPIC_PREFIX": e.prefix,
            "KK_MQTT_CLIENT_ID": "kk-server-e2e",
            # Agent 自报 IP 会命中：镜像网络下 WSL 报 192.168.0.107，docker 网桥报 172.x。
            # 负例由「203.0.113.9 的伪 Agent」承担，不用真机自断。
            "KK_AGENT_IPS": "127.0.0.1/32,192.168.0.0/24,172.16.0.0/12,10.0.0.0/8",
            "KK_PUBLIC_URL": e.public_url,
            "KK_UPDATE_MODE": "manual",
            "KK_UPDATE_HMAC_KEY": e.hmac,
            "KK_ADMIN_USER": e.admin_user,
            "KK_ADMIN_PASS": e.admin_pass,
            "KK_ENV": "dev",
            "KK_LOG": "",
            "KK_LOG_LEVEL": "INFO",
        })
        return env

    def start_server(self, timeout: float = 90) -> None:
        e = self.env
        py = e.repo / ".venv" / "Scripts" / "python.exe"
        log_path = e.artifacts / "server.log"
        # 端口已被占（多半是上一次 --keep 留下的服务端）时不能硬起：新进程绑不上，
        # 但健康检查会被**旧栈**应答，整轮用例就悄悄跑在了别人的数据上。
        # 自己刚停掉的进程释放监听有几秒延迟（restart_server 会走到这里），先等它退干净。
        if port_open("127.0.0.1", e.port) and not wait_until(
                lambda: not port_open("127.0.0.1", e.port), 10):
            raise RuntimeError(
                "端口 %d 已被占用（上次 --keep 的服务端还在？）。先结束占用进程："
                "netstat -ano | findstr :%d → taskkill /PID <pid> /F" % (e.port, e.port))
        # 追加而非覆盖：一次运行里服务端会被重启一次（注入 KK_PUBLIC_URL），
        # 用 "wb" 会把首次启动的日志抹掉——恰好是排障最需要的那段
        self.server_log = open(log_path, "ab")
        self.server = subprocess.Popen(
            [str(py), "-m", "kk_server"], cwd=str(e.repo), env=self.server_env(),
            stdout=self.server_log, stderr=subprocess.STDOUT)

        from .core import http
        ok = wait_until(lambda: http(e, "GET", "/api/health", token="", timeout=3)[0] == 200,
                        timeout, 1.5)
        if not ok:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError("服务端未就绪（%s）：\n%s" % (e.base, tail(text, 1200)))

    def stop_server(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(15)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(5)
        if self.server_log:
            self.server_log.close()
            self.server_log = None

    def restart_server(self, timeout: float = 90) -> None:
        self.stop_server()
        self.start_server(timeout)

    def detect_public_url(self) -> str:
        """找出 **WSL 能访问到** 的服务端基址（自更新下载必须走这条）。

        WSL 有两种网络形态：mirrored（127.0.0.1 直达 Windows）与 NAT（要打默认
        网关）。两种都试一遍再写死，比在文档里猜更可靠；都失败就返回空串，
        由调用方把「自更新用例无法执行」如实写进报告，而不是假装通过。
        """
        e = self.env
        gw = wsl(e, "ip route show default 2>/dev/null | awk '{print $3}' "
                    "| head -1").strip().splitlines()
        gw = gw[-1].strip() if gw else ""
        cands = ["http://127.0.0.1:%d" % e.port]
        if gw:
            cands.append("http://%s:%d" % (gw, e.port))
        for c in cands:
            out = wsl_stdin(
                e,
                "curl -s -m 3 -o /dev/null -w '%%{http_code}' %s/api/health || true\n" % c,
                timeout=30).strip()
            if out.endswith("200"):
                return c
        return ""

    # ------------------------------------------------------------------ Agents
    def deploy_agents(self) -> str:
        """把当前 agent 源码复制成两份版本树，并生成每台主机的启动器与 env。"""
        e = self.env
        src = win_to_wsl(AGENT_SRC)
        root = self.resolve_root()
        lines = [
            "set -e",
            'ROOT="%s"' % root,
            'mkdir -p "$ROOT/app-%s" "$ROOT/app-%s" "$ROOT/bin" "$ROOT/logs" "$ROOT/agents"' % (OLD_VER, NEW_VER),
            'rm -rf "$ROOT/app-%s/kk_agent" "$ROOT/app-%s/kk_agent"' % (OLD_VER, NEW_VER),
            'cp -a "%s" "$ROOT/app-%s/kk_agent"' % (src, OLD_VER),
            'cp -a "$ROOT/app-%s/kk_agent" "$ROOT/app-%s/kk_agent"' % (OLD_VER, NEW_VER),
            'find "$ROOT/app-%s/kk_agent" "$ROOT/app-%s/kk_agent" -name __pycache__ '
            '-type d -prune -exec rm -rf {} +' % (OLD_VER, NEW_VER),
            "grep -n '^AGENT_VER' \"$ROOT/app-%s/kk_agent/config.py\" | head -1" % OLD_VER,
        ]
        out = wsl_stdin(e, "\n".join(lines), timeout=180)

        # 0.4.0：只改版本常量（模拟一次真实的发版）
        patch = """set -e
ROOT="%s"
sed -i 's/^AGENT_VER = "%s"/AGENT_VER = "%s"/' "$ROOT/app-%s/kk_agent/config.py"
grep -n '^AGENT_VER' "$ROOT/app-%s/kk_agent/config.py" | head -1
""" % (root, OLD_VER, NEW_VER, NEW_VER, NEW_VER)
        out += wsl_stdin(e, patch, timeout=60)

        # 启动器（两个版本 + 每台主机一份 0.3.0 副本：自更新只该换掉被升级那台）
        for ver in (OLD_VER, NEW_VER):
            self._write_wsl_file("%s/bin/kk-agent-%s" % (root, ver), launcher(e, ver), 0o755)
        # newline="\n" 不是可选项：`write_text` 默认 newline=None 会把 `\n` 翻成
        # `os.linesep`，在 Windows 上就是 CRLF。这份文件随后被上传进服务端、再下载
        # 到 WSL 覆盖 kk-agent，第一行变成 `#!/bin/sh\r` —— 内核找不到解释器，
        # execv 报 ENOENT，Agent 回执 exec_error 并回滚。实测踩过一轮：UPG-03 假失败、
        # 之后 RES-01/02/03 全部连锁红灯，而 sha256/size 校验全都通过，极难定位。
        self.env.new_agent_file.write_text(launcher(e, NEW_VER), encoding="utf-8",
                                           newline="\n")
        # 写完立刻自检：CRLF 的失败现场（execv ENOENT → 回滚 → 退避）离病因太远，
        # 宁可在这里以环境级故障中止，也不让整轮用例去替一个坏 fixture 陪跑。
        blob = self.env.new_agent_file.read_bytes()
        if b"\r" in blob or not blob.startswith(b"#!/bin/sh\n"):
            raise RuntimeError("自更新 fixture 被换行符污染（%s）：首行必须是 "
                               "LF 结尾的 #!/bin/sh" % self.env.new_agent_file)

        for host in e.agents:
            d = e.agent_dir(host)
            env_file = "\n".join([
                "export KK_SERVER=mqtt://127.0.0.1:%d" % e.broker_port,
                "export KK_TOPIC_PREFIX=%s" % e.prefix,
                "export KK_HOST_NAME=%s" % host,
                "export KK_GROUP=e2e-group",
                "export KK_LABELS=role=e2e,zone=wsl",
                "export KK_INTERVAL=3",
                "export KK_UPDATE_URL=%s" % (e.public_url or e.base),
                "export KK_UPDATE_INTERVAL=30",
                "export KK_UPDATE_HMAC_KEY=%s" % e.hmac,
                "export KK_AGENT_BIN=%s/kk-agent" % d,
                "export KK_LOG=%s/logs/%s.log" % (root, host),
                "export KK_LOG_LEVEL=INFO",
                "export KK_E2E=1",
                "",
            ])
            start = """#!/bin/sh
set -e
cd "%s"
set -a
. "./agents/%s/env"
set +a
exec "./agents/%s/kk-agent" "$@"
""" % (root, host, host)
            self._write_wsl_file("%s/env" % d, env_file, 0o644)
            self._write_wsl_file("%s/kk-agent" % d, launcher(e, OLD_VER), 0o755)
            self._write_wsl_file("%s/start.sh" % d, start, 0o755)
        return out

    def _write_wsl_file(self, path: str, content: str, mode: int = 0o644) -> None:
        marker = "KK_EOF_%d" % int(time.time() * 1000)
        script = 'mkdir -p "$(dirname "%s")"\ncat > "%s" <<\'%s\'\n%s%s\necho wrote\nchmod %o "%s"\n' % (
            path, path, marker, content, marker, mode, path)
        out = wsl_stdin(self.env, script, timeout=60)
        if "wrote" not in out:
            raise RuntimeError("写 %s 失败：%s" % (path, out))

    def _run_unit(self, unit: str, argv: str, wait: float = 30) -> str:
        """把一条命令挂成 systemd 瞬态单元（前台 exec，不自己 daemon 化）。

        单元 stdout/stderr 进 journal，起不来时用 `journalctl -u` 取，避免又回到
        「重定向到一个永远空着的 .out 文件」那种排障死胡同。
        """
        e = self.env
        # 先 stop 再 reset-failed：`--keep` 留下的栈会让同名单元**仍处于运行态**，
        # 光 reset-failed 清不掉它，systemd-run 直接报「Unit … already exists」。
        script = """set -e
systemctl stop %s >/dev/null 2>&1 || true
systemctl reset-failed %s >/dev/null 2>&1 || true
systemd-run --unit=%s --collect --quiet --working-directory="%s" %s
echo UNIT=%s
""" % (unit, unit, unit, e.root, argv, unit)
        out = wsl_stdin(e, script, timeout=60)
        if not wait_until(lambda: bool(self._unit_pid(unit)), wait):
            raise RuntimeError("单元 %s 未起来：%s\n%s" % (
                unit, out, wsl_stdin(e, "journalctl -u %s -n 30 --no-pager 2>&1" % unit,
                                     timeout=30)))
        return out.strip()

    def _unit_pid(self, unit: str) -> str:
        return wsl_stdin(
            self.env, "systemctl show -p MainPID --value %s 2>/dev/null | "
                      "awk '$1!=\"\" && $1!=0 {print; exit}'" % unit,
            timeout=30).strip()

    def start_agents(self) -> str:
        return "\n".join(self.start_agent(host) for host in self.env.agents)

    def agent_pid(self, host: str) -> str:
        """MainPID + /proc/environ 双确认：只认「确实是这台主机」的那个进程。"""
        pid = self._unit_pid(self.agent_unit(host))
        if not pid:
            return ""
        script = """if [ -r "/proc/%s/environ" ] && \\
   tr '\\0' '\\n' < "/proc/%s/environ" | grep -qx "KK_HOST_NAME=%s"; then
  echo "%s"
fi
""" % (pid, pid, host, pid)
        return wsl_stdin(self.env, script, timeout=30).strip()

    def agent_version(self, host: str) -> str:
        """从 WSL 侧问 Agent 进程自己（读的是进程加载的 config.py）。"""
        pid = self.agent_pid(host)
        if not pid:
            return ""
        app = wsl_stdin(self.env, 'tr "\\0" "\\n" < /proc/%s/environ | grep "^PYTHONPATH=" '
                                   '| cut -d= -f2-' % pid, timeout=20).strip()
        if not app:
            return ""
        return wsl_stdin(self.env, "grep -m1 '^AGENT_VER' \"%s/kk_agent/config.py\" "
                                   "| cut -d'\"' -f2" % app, timeout=20).strip()

    def stop_agents(self) -> str:
        """停掉全部 Agent：先按单元停（本栈用的），再按 KK_E2E=1 标记兜底清残留。

        兜底那半截是给「上一次运行没走完 finally」留的：同一个 WSL 里可能挂着
        上次的孤儿进程，只按单元停会漏。
        """
        units = " ".join(self.agent_unit(h) for h in self.env.agents)
        script = """for u in %s; do systemctl stop "$u" 2>/dev/null && echo "stopped $u"
  systemctl reset-failed "$u" 2>/dev/null || true
done
for pid in $(pgrep -f 'python3 -m kk_agent'); do
  if tr '\\0' '\\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx 'KK_E2E=1'; then
    kill -9 "$pid" 2>/dev/null && echo "killed $pid"
  fi
done
echo SCAN_DONE
""" % units
        return wsl_stdin(self.env, script, timeout=60)

    def stop_agent(self, host: str, wait: float = 20) -> str:
        """单台停掉（SIGKILL 模拟宕机：TCP 直接断，Broker 立刻发 LWT）。

        用 SIGKILL 而不是 SIGTERM：SIGTERM 会走 Agent 的正常退出路径（发 offline
        帧再 disconnect），那样验证的是「优雅退出」，测不到 LWT 这条真实掉线路径。
        `systemctl kill -s KILL` 直接砸主进程，systemd 不会先代发 TERM。
        """
        pid = self.agent_pid(host)
        if not pid:
            return "not-running"
        out = wsl_stdin(self.env, 'systemctl kill -s KILL --kill-who=main %s; echo killed %s'
                        % (self.agent_unit(host), pid), timeout=30)
        wait_until(lambda: not self.agent_pid(host), wait)
        return out.strip()

    def start_agent(self, host: str, wait: float = 30) -> str:
        return self._run_unit(self.agent_unit(host),
                              '"/bin/sh" "%s/start.sh"' % self.env.agent_dir(host),
                              wait=wait)

    def restart_agent(self, host: str, wait: float = 30) -> str:
        self.stop_agent(host)
        time.sleep(1)
        return self.start_agent(host, wait)

    def set_agent_env(self, host: str, key: str, value: str | None = None) -> None:
        """增删一条 `export KEY=...`。Agent 只读**进程环境**，改完必须 restart 才生效。"""
        path = "%s/env" % self.env.agent_dir(host)
        cur = wsl_stdin(self.env, 'cat "%s"' % path, timeout=30)
        lines = [ln for ln in cur.splitlines()
                 if not ln.startswith("export %s=" % key) and "WSL_ERR" not in ln]
        if value is not None:
            lines.append("export %s=%s" % (key, value))
        self._write_wsl_file(path, "\n".join(lines) + "\n", 0o644)

    def agent_binary_sha(self, host: str) -> str:
        """主机上那支启动器/二进制的 sha256：与清单里的 sha256 对齐才算真替换。"""
        return wsl_stdin(
            self.env, 'sha256sum "%s/kk-agent" 2>/dev/null | cut -d" " -f1'
            % self.env.agent_dir(host), timeout=30).strip()

    def reset_agent_binary(self, host: str, ver: str = OLD_VER) -> str:
        """把该主机的启动器换回旧版并重启：模拟「一台从未升级的老主机重新上线」。

        自更新只升不降，所以「降级」只能靠换文件 + 重启；这也是真实世界里
        「存量 0.3.0 主机」被发现、被补升的同一条路径。
        """
        d = self.env.agent_dir(host)
        out = wsl_stdin(self.env,
                        'cp "%s/bin/kk-agent-%s" "%s/kk-agent" && chmod 755 "%s/kk-agent" '
                        '&& echo reset' % (self.env.root, ver, d, d), timeout=30)
        if "reset" not in out:
            raise RuntimeError("重置 %s 启动器失败：%s" % (host, out))
        return self.restart_agent(host)

    # ------------------------------------------------------------------ 收尾
    def stop_all(self) -> None:
        try:
            self.stop_agents()
        finally:
            try:
                self.stop_server()
            finally:
                try:
                    self.stop_broker()
                finally:
                    # 保活会话放最后：前面每一步都要靠它让实例活着
                    self.stop_keeper()


def venv_python(env: Env) -> Path:
    return env.repo / ".venv" / "Scripts" / "python.exe"


def ensure_artifacts(env: Env) -> None:
    for sub in ("screenshots", "downloads"):
        (env.artifacts / sub).mkdir(parents=True, exist_ok=True)
    env.agent_assets.mkdir(parents=True, exist_ok=True)


def wsl_home(env: Env) -> str:
    home = wsl(env, 'echo "$HOME"').strip().splitlines()[-1] if wsl(env, 'echo "$HOME"') else ""
    return home or "/root"


def shell_quote(s: str) -> str:
    return shlex.quote(s)


def python_exe() -> str:
    return sys.executable