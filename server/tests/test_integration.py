"""端到端集成测试：真实 uvicorn + 真实 MQTT Broker + 真实 Agent 主循环。

这条测试覆盖的是「方案能不能跑」这件事本身：Agent 上线 → 指标可见 →
批量下发 shell 与 collect 命令 → 结果回传可见 → 黑名单与审计。

需要本地有可达的 Mosquitto（开发环境见 scripts/mqtt_e2e.py 文档头的 WSL 说明）；
不可达时整条 skip，保证没有 Broker 的环境（含部分 CI）单测仍然全绿。
"""
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agent", "src"))

pytest.importorskip("uvicorn")

BROKER_URL = os.environ.get("KK_IT_MQTT_URL", "mqtt://127.0.0.1:1883")


def broker_reachable(timeout=2):
    try:
        rest = BROKER_URL.split("://", 1)[1]
        host, _, port = rest.rpartition(":")
        with socket.create_connection((host or "127.0.0.1", int(port or 1883)), timeout):
            return True
    except (OSError, ValueError):
        return False


def require_broker():
    """可达性必须在用例真正开跑时判：WSL 冷启动头几秒会瞬时拒绝连接，
    放在模块级 skipif 里会被误判成「没有 Broker」而整条静默跳过。"""
    for _ in range(10):
        if broker_reachable():
            return
        time.sleep(1)
    pytest.skip("需要本地 Mosquitto（%s 不可达）；见 scripts/mqtt_e2e.py 文档头" % BROKER_URL)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, method, path, body=None, token=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=8) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw}


def wait_until(fn, timeout=30, what="condition"):
    last = None
    for _ in range(int(timeout * 10)):
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError("timeout waiting for %s, last=%r" % (what, last))


@pytest.fixture
def stack(tmp_path):
    import uvicorn
    from kk_agent import config as kk_config
    from kk_agent import main as agent_main
    from kk_server.main import create_app

    require_broker()
    port = free_port()
    # 每次跑用独立主题前缀与主机名：共享 Broker 上不会有别的会话残留干扰断言
    prefix = "kk/it%d" % random.randint(1000, 9999)
    host = "it-host-1"
    env = {
        "KK_DB_PATH": str(tmp_path / "it.db"),
        "KK_AGENT_TOKENS": "it-token",
        "KK_ADMIN_USER": "admin",
        "KK_ADMIN_PASS": "it-pass-123",
        "KK_WEB_DIR": str(tmp_path / "no-web"),
        "KK_LOG_LEVEL": "warning",
        "KK_MQTT_URL": BROKER_URL,
        "KK_TOPIC_PREFIX": prefix,
        "KK_MQTT_CLIENT_ID": "kk-server-it-" + prefix,
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

    agent_env = {
        "KK_SERVER": BROKER_URL,
        "KK_TOKEN": "it-token",
        "KK_INTERVAL": "1",
        "KK_TOPIC_PREFIX": prefix,
        "KK_LOG": "-",
        "KK_LOG_LEVEL": "WARNING",
        "KK_HOST_NAME": host,
        "KK_UPDATE_DISABLED": "1",
    }
    stop = threading.Event()
    cfg = kk_config.load(env=agent_env)
    at = threading.Thread(target=agent_main.run, args=(stop,), kwargs={"cfg": cfg}, daemon=True)
    at.start()

    yield {"port": port, "server": server, "stop": stop, "agent_thread": at,
           "host": host, "prefix": prefix}

    stop.set()
    at.join(timeout=8)
    server.should_exit = True
    st.join(timeout=8)


def _login(port):
    status, body = http(port, "POST", "/api/login",
                        {"username": "admin", "password": "it-pass-123"})
    assert status == 200, body
    return body["token"]


def test_full_chain(stack):
    port, host = stack["port"], stack["host"]

    # 健康检查（含桥接状态）
    status, body = http(port, "GET", "/api/health")
    assert status == 200 and body["ok"] and body["proto_ver"] == 2

    status, _ = http(port, "POST", "/api/login", {"username": "admin", "password": "bad"})
    assert status == 401
    tok = _login(port)

    # Agent 上线：online 来自 retained status，指标来自首帧心跳
    def find_host():
        s, b = http(port, "GET", "/api/containers", token=tok)
        items = [i for i in b.get("items", []) if i["pod"] == host]
        it = items[0] if items else None
        return it if it and it["online"] and it["metrics"].get("mem_mb") is not None else None

    rec = wait_until(find_host, what="agent online with metrics")
    # psutil 真实采集：只验数量级与字段齐备，不锁死具体值
    assert rec["metrics"]["mem_mb"] > 0 and rec["metrics"]["cpu_cores"] >= 1
    assert "disks" in rec["metrics"] and "procs_top" in rec["metrics"]
    assert rec["agent_ver"]

    # 摘要视图：列表页轮询走这条，字段与在线/告警口径必须自洽
    status, summ = http(port, "GET", "/api/containers?view=summary", token=tok)
    assert status == 200, summ
    row = [i for i in summ["items"] if i["pod"] == host][0]
    assert row["online"] and row["cpu"] is not None and row["mem_mb"] > 0
    assert row["disk_alert"] == (row["disk_pct"] >= 85)
    assert "last_metrics" not in row and "metrics" not in row, "摘要不该带完整指标"

    # 可观测面板：链路是否活着要从这里一眼看出
    status, st = http(port, "GET", "/api/system/stats", token=tok)
    assert status == 200 and st["ok"], st
    assert st["hosts"]["online"] >= 1 and st["storage"]["heartbeats"] >= 1
    assert st["broker"]["connected"] is True
    assert st["broker"]["stats"]["hb"] >= 1, "桥接心跳计数应随 Agent 上报增长"
    assert st["broker"]["stats"]["cmd_published"] >= 0

    # 下发 shell 命令 → 执行 → 回传（argv 直传，避免含空格路径被 cmdline 拆坏）
    status, body = http(port, "POST", "/api/commands", token=tok, body={
        "pods": [host], "argv": [sys.executable, "-c", "print('kk-ok')"], "timeout": 30})
    assert status == 200, body
    cid = body["items"][0]["id"]
    assert body["items"][0]["status"] == "sent"

    def done():
        s, b = http(port, "GET", "/api/commands/" + cid, token=tok)
        return b if b.get("status") == "done" else None

    cmd = wait_until(done, what="command done")
    assert cmd["rc"] == 0 and cmd["out_chunks"] == 1
    # /out 返回纯文本，单独按原始字节读
    assert "kk-ok" in cmd2_out(port, cid, tok)

    # 列表里的 out_tail 让控制台不必再点详情就能看到回显（评审 P1-5）
    status, lst = http(port, "GET", "/api/commands?limit=20", token=tok)
    row = [c for c in lst["items"] if c["id"] == cid][0]
    assert "kk-ok" in row["out_tail"]

    # ---- 批量 collect：核心需求「批量下发采集数据的命令」首次端到端可用 ----
    status, items = http(port, "GET", "/api/collect/items", token=tok)
    assert status == 200 and "cpu" in items["items"] and "disk_io" in items["items"]

    status, body = http(port, "POST", "/api/commands", token=tok, body={
        "pods": [host], "kind": "collect", "items": ["cpu", "mem"]})
    assert status == 200, body
    cid2 = body["items"][0]["id"]

    def done2():
        s, b = http(port, "GET", "/api/commands/" + cid2, token=tok)
        return b if b.get("status") == "done" else None

    cmd2 = wait_until(done2, what="collect done")
    assert cmd2["rc"] == 0
    payload = json.loads(cmd2_out(port, cid2, tok))
    assert payload["items"] == ["cpu", "mem"]
    assert "mem_mb" in payload["data"] and "cpu_cores" in payload["data"]

    # collect 传未知采集项必须被服务端挡下（白名单在 API 边界，不放给 Agent 试错）
    status, body = http(port, "POST", "/api/commands", token=tok,
                        body={"pods": [host], "kind": "collect", "items": ["nope"]})
    assert status == 400 and "采集项不存在" in body["detail"]

    # 插件重载命令
    status, body = http(port, "POST", "/api/commands", token=tok,
                        body={"pods": [host], "kind": "plugin_reload"})
    cid3 = body["items"][0]["id"]

    def done3():
        s, b = http(port, "GET", "/api/commands/" + cid3, token=tok)
        return b if b.get("status") == "done" else None

    cmd3 = wait_until(done3, what="plugin_reload done")
    assert cmd3["rc"] == 0
    assert "plugins" in cmd2_out(port, cmd3["id"], tok)
    assert "out_b64" not in cmd3, "单条命令响应不该带全量 base64"

    # 黑名单拒绝 + 审计
    status, _ = http(port, "POST", "/api/commands", token=tok,
                     body={"pods": [host], "cmdline": "rm -rf /"})
    assert status == 400
    status, audit = http(port, "GET", "/api/audit?limit=50", token=tok)
    actions = [a["action"] for a in audit["items"]]
    assert {"login_ok", "command_create", "command_blocked"} <= set(actions)

    # 指标序列：按条件等，不用固定 sleep——本机一轮 psutil 采集要 2~4s，
    # 负载高时 sleep(3) 只等到一个点，断言就会假失败。
    def series_ok():
        st, b = http(port, "GET", "/api/containers/%s/metrics?hours=1" % host, token=tok)
        return b if st == 200 and len(b.get("series", [])) >= 2 else None

    series = wait_until(series_ok, timeout=45, what=">=2 metric points")
    assert series["source"] == "raw"

    # token 管理
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


def cmd2_out(port, cid, tok):
    """/api/commands/{cid}/out 返回纯文本，单独读原始字节。"""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/commands/%s/out" % (port, cid),
        headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode()


def test_agent_offline_marks_offline(stack):
    """停掉 Agent（发优雅 offline status）后，列表里的在线状态必须翻转。

    这条测的是「在线真相来自 Broker 的 retained status」而不是内存连接表。
    """
    port, host = stack["port"], stack["host"]
    tok = _login(port)

    def online():
        s, b = http(port, "GET", "/api/containers", token=tok)
        it = [i for i in b["items"] if i["pod"] == host]
        return it[0] if it and it[0]["online"] else None

    wait_until(online, what="agent online before stop")
    stack["stop"].set()
    stack["agent_thread"].join(timeout=10)

    def offline():
        s, b = http(port, "GET", "/api/containers", token=tok)
        it = [i for i in b["items"] if i["pod"] == host]
        return it[0] if it and not it[0]["online"] else None

    assert wait_until(offline, what="host marked offline after agent stop")
    # 停 Agent 会把本用例的 stop 事件用掉，teardown 再 set 一次无害
    assert online() is None


def test_blacklist_blocks_bypass_variants(stack):
    """黑名单必须拦住常见绕过写法（双空格、参数顺序、包装命令、高危程序）。"""
    port, host = stack["port"], stack["host"]
    tok = _login(port)
    for cmdline in [
        "rm  -rf /",
        "rm -fr /",
        "busybox rm -rf /",
        "chmod -R 777 /",
        "dd if=/dev/zero of=/x",
    ]:
        status, body = http(port, "POST", "/api/commands", token=tok,
                            body={"pods": [host], "cmdline": cmdline})
        assert status == 400, "expected block for %r but got %s %s" % (cmdline, status, body)
    status, cmds = http(port, "GET", "/api/commands?limit=100", token=tok)
    assert cmds["items"] == [], "被拦截的命令不应进入命令表"
