"""端到端集成测试：真实 uvicorn 服务 + 真实 Agent 主循环（伪造 /proc）。

验证链路：Agent 出站连接 → hello → 心跳入库 → REST 登录 → 下发 shell 命令
→ Agent 执行 → 分块回传 → 状态 done → 插件重载 → 黑名单拒绝。
"""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from conftest import make_fake_fs  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(port, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), method=method,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait_until(fn, timeout=25, what="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.3)
    raise AssertionError("timeout waiting for %s, last=%r" % (what, last))


@pytest.fixture
def stack(tmp_path):
    import uvicorn
    from kk_server.main import create_app
    from kk_agent import config as kk_config, main as agent_main

    port = free_port()
    env = {
        "KK_DB_PATH": str(tmp_path / "it.db"),
        "KK_AGENT_TOKENS": "it-token",
        "KK_ADMIN_USER": "admin",
        "KK_ADMIN_PASS": "it-pass-123",
        "KK_WEB_DIR": str(tmp_path / "no-web"),
        "KK_LOG_LEVEL": "warning",
    }
    server = uvicorn.Server(uvicorn.Config(create_app(env), host="127.0.0.1",
                                           port=port, log_level="error"))
    st = threading.Thread(target=server.run, daemon=True)
    st.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started

    fs = make_fake_fs(tmp_path / "agentfs")
    agent_env = {
        "KK_SERVER": "ws://127.0.0.1:%d/ws/agent" % port,
        "KK_TOKEN": "it-token",
        "KK_INTERVAL": "1",
        "KK_FS_ROOT": str(fs),
        "KK_LOG": "-",
        "KK_LOG_LEVEL": "WARNING",
        "KK_POD_NAME": "it-pod-1",
    }
    stop = threading.Event()
    cfg = kk_config.load(env=agent_env)
    at = threading.Thread(target=agent_main.run, args=(stop,), kwargs={"cfg": cfg}, daemon=True)
    at.start()

    yield {"port": port, "server": server, "stop": stop, "agent_thread": at}

    stop.set()
    at.join(timeout=5)
    server.should_exit = True
    st.join(timeout=5)


def test_full_chain(stack):
    port = stack["port"]

    # 健康检查
    status, body = http(port, "GET", "/api/health")
    assert status == 200 and body["ok"]

    # 登录（坏密码 401）
    status, _ = http(port, "POST", "/api/login", {"username": "admin", "password": "bad"})
    assert status == 401
    status, body = http(port, "POST", "/api/login", {"username": "admin", "password": "it-pass-123"})
    assert status == 200
    tok = body["token"]

    # Agent 上线并开始心跳（在线即可见，需等首帧心跳把指标带上来）
    def find_pod():
        s, b = http(port, "GET", "/api/containers", token=tok)
        items = [i for i in b.get("items", []) if i["pod"] == "it-pod-1"]
        it = items[0] if items else None
        return it if it and it["online"] and it["metrics"].get("mem_mb") is not None else None

    pod = wait_until(find_pod, what="agent online")
    assert pod["metrics"]["mem_mb"] == 500.0  # 伪造 /proc 的数据
    assert any(u["vscode"] for u in pod["metrics"]["users"])

    # 下发 shell 命令 → 执行 → 回传（argv 直传，避免路径含空格被 cmdline 拆分）
    status, body = http(port, "POST", "/api/commands", token=tok, body={
        "pods": ["it-pod-1"], "argv": [sys.executable, "-c", "print('kk-ok')"],
        "timeout": 30})
    assert status == 200
    cid = body["items"][0]["id"]
    assert body["items"][0]["status"] == "sent"

    def done():
        s, b = http(port, "GET", "/api/commands/" + cid, token=tok)
        return b if b.get("status") == "done" else None

    cmd = wait_until(done, what="command done")
    assert cmd["rc"] == 0 and "kk-ok" in cmd["out"]

    # 插件重载命令
    status, body = http(port, "POST", "/api/commands", token=tok,
                        body={"pods": ["it-pod-1"], "kind": "plugin_reload"})
    cid2 = body["items"][0]["id"]

    def done2():
        s, b = http(port, "GET", "/api/commands/" + cid2, token=tok)
        return b if b.get("status") == "done" else None

    cmd2 = wait_until(done2, what="plugin_reload done")
    assert cmd2["rc"] == 0 and "plugins" in cmd2["out"]

    # 黑名单拒绝 + 审计
    status, body = http(port, "POST", "/api/commands", token=tok,
                        body={"pods": ["it-pod-1"], "cmdline": "rm -rf /"})
    assert status == 400

    status, audit = http(port, "GET", "/api/audit?limit=50", token=tok)
    actions = [a["action"] for a in audit["items"]]
    assert "login_ok" in actions and "command_create" in actions and "command_blocked" in actions

    # 指标序列（interval=1s，几秒内应有多点）
    time.sleep(3)
    status, series = http(port, "GET", "/api/containers/it-pod-1/metrics?hours=1", token=tok)
    assert status == 200 and len(series["series"]) >= 2

    # token 管理接口：列表（脱敏）、吊销、恢复
    status, toks = http(port, "GET", "/api/tokens", token=tok)
    assert status == 200 and toks["items"] == [{"token": "***", "revoked": False}]
    status, _ = http(port, "POST", "/api/tokens/revoke", token=tok, body={"token": "it-token"})
    assert status == 200
    status, toks = http(port, "GET", "/api/tokens", token=tok)
    assert toks["items"][0]["revoked"] is True
    status, _ = http(port, "POST", "/api/tokens/restore", token=tok, body={"token": "it-token"})
    assert status == 200
    status, _ = http(port, "POST", "/api/tokens/revoke", token=tok, body={"token": "no-such"})
    assert status == 404

    # 未鉴权访问被拒
    status, _ = http(port, "GET", "/api/containers")
    assert status == 401
