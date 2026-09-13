"""服务端 Agent 自更新接口 + 桥接推送 upgrade 帧的测试。

v3 起 Agent 侧接口（latest/download）按请求源 IP 校验 KK_AGENT_IPS 白名单，
替代原 Bearer token；上传仍需管理员会话。
"""
import hashlib
import json
import os
import time
import types

from fastapi.testclient import TestClient

from kk_server.main import create_app

ADMIN_USER = "admin"
ADMIN_PASS = "adm-pass"
GOOD_CLIENT = ("127.0.0.1", 50001)     # 白名单内的模拟 Agent 来源
BAD_CLIENT = ("192.0.2.9", 50002)     # 白名单外的模拟 Agent 来源


def _make_app(tmp_path):
    return create_app({
        "KK_AGENT_IPS": "127.0.0.1",
        "KK_ADMIN_USER": ADMIN_USER,
        "KK_ADMIN_PASS": ADMIN_PASS,
        "KK_DB_PATH": str(tmp_path / "au.db"),
        "KK_AGENT_BIN_DIR": str(tmp_path / "bin"),
    })


def _admin_token(client):
    r = client.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _upload(client, token, payload, version):
    return client.post("/api/system/agent",
                       headers={"Authorization": "Bearer %s" % token},
                       files={"file": ("kk-agent", payload)},
                       data={"version": version})


def test_upload_and_discovery(tmp_path):
    payload = b"\x7fELF-fake-kk-agent-binary"
    # TestClient 必须当上下文管理器用：建表与管理员初始化都在 lifespan 里做
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        r = _upload(client, token, payload, "0.3.0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"] == "0.3.0" and body["size"] == len(payload)
        assert body["sha256"] == hashlib.sha256(payload).hexdigest()

        # 落后版本查询应 available（白名单内来源直接放行，无需凭据）
        r = client.get("/api/system/agent/latest", params={"ver": "0.1.0"})
        assert r.status_code == 200 and r.json()["available"] is True
        assert r.json()["version"] == "0.3.0"

        # 已是最新则不 available
        r = client.get("/api/system/agent/latest", params={"ver": "0.3.0"})
        assert r.json()["available"] is False

        # 下载内容与上传一致
        r = client.get("/api/system/agent/download")
        assert r.status_code == 200 and r.content == payload

        # 再传一版：上一版要留成 .prev 便于回滚
        assert _upload(client, token, b"\x7fELF-second", "0.4.0").status_code == 200
        prev = os.path.join(str(tmp_path / "bin"), "kk-agent.prev")
        assert os.path.exists(prev)

        # 第三次上传时 .prev 已存在：shutil.move 在 Windows 上会因目标已存在而
        # 失败并被 except 静默吞掉，导致「上一版」从此不再更新（回滚失去意义）。
        # 故改用 os.replace（覆盖语义跨平台一致），这里守住该回归。
        assert _upload(client, token, b"\x7fELF-third", "0.5.0").status_code == 200
        with open(prev, "rb") as f:
            assert f.read() == b"\x7fELF-second"


def test_agent_rollback_restores_prev(tmp_path):
    """回滚要把**二进制**和**版本清单**一起换回上一版。

    只换其中一个都是坏的：只换文件 → Agent 下载到的字节与 latest 的 sha256 不符，
    校验失败；只换清单 → 下载的还是坏版本。
    """
    v1, v2 = b"\x7fELF-v1", b"\x7fELF-v2-longer"
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        assert _upload(client, token, v1, "0.3.0").status_code == 200
        assert _upload(client, token, v2, "0.4.0").status_code == 200
        assert client.get("/api/system/agent/download").content == v2

        r = client.post("/api/system/agent/rollback",
                        headers={"Authorization": "Bearer %s" % token})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == "0.3.0"

        # 下载回来的必须是 v1 本体，且 sha256 与 v1 相符（不是 v2 的）
        assert client.get("/api/system/agent/download").content == v1
        body = client.get("/api/system/agent/latest", params={"ver": "0.1.0"}).json()
        assert body["available"] is True and body["version"] == "0.3.0"
        assert body["sha256"] == hashlib.sha256(v1).hexdigest()
        assert body["size"] == len(v1)

        # 互换语义：再回滚一次回到 v2（prev 恒记「另一个版本」）
        r = client.post("/api/system/agent/rollback",
                        headers={"Authorization": "Bearer %s" % token})
        assert r.json()["version"] == "0.4.0"
        assert client.get("/api/system/agent/download").content == v2


def test_agent_rollback_without_prev_404(tmp_path):
    """只上传过一次就没有「上一版」，回滚必须 404 而不是把现有二进制弄丢。"""
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        payload = b"\x7fELF-only-one"
        assert _upload(client, token, payload, "0.3.0").status_code == 200

        r = client.post("/api/system/agent/rollback",
                        headers={"Authorization": "Bearer %s" % token})
        assert r.status_code == 404, r.text
        # 现有二进制完好无损
        assert client.get("/api/system/agent/download").content == payload

        # 回滚是管理动作：无会话不得触发
        assert client.post("/api/system/agent/rollback").status_code == 401


def test_ip_whitelist_and_admin_auth(tmp_path):
    """Agent 接口按源 IP 把关（403）；上传接口按管理员会把关（401）。"""
    with TestClient(_make_app(tmp_path), client=BAD_CLIENT) as bad:
        assert bad.get("/api/system/agent/latest", params={"ver": "0.1.0"}).status_code == 403
        assert bad.get("/api/system/agent/download").status_code == 403

    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        # 上传仍需管理员会话：白名单放行不了管理动作
        assert client.post("/api/system/agent",
                           files={"file": ("kk-agent", b"x")},
                           data={"version": "0.3.0"}).status_code == 401


async def test_status_pushes_upgrade(tmp_path):
    """上传新版本 → 落后的 Agent 一上线（status 帧）就该收到 update 命令。

    原实现走 WS hello；改用 MQTT 后这条链路两端分别是 HTTP 上传与桥接的
    retained status 处理，这里把它们串起来测。
    """
    from kk_server.services.mqtt_bridge import MqttBridge

    app = _make_app(tmp_path)
    published = []
    with TestClient(app, client=GOOD_CLIENT) as client:
        assert _upload(client, _admin_token(client), b"\x7fELF-newer",
                       "99.0.0").status_code == 200
        store = app.state.store

    bridge = MqttBridge(store, app.state.settings, app.state.agent_ips, proto_ver=3)
    bridge.cli = types.SimpleNamespace(
        publish=lambda topic, body, qos=0, retain=False: (
            published.append((topic, json.loads(body))),
            types.SimpleNamespace(rc=0))[1])
    await bridge._on_status("pod-upgrade", {
        "online": True, "host": "pod-upgrade", "ip": "127.0.0.1", "proto_ver": 3,
        "agent_ver": "0.0.1", "image": "img", "interval": 60, "ts": int(time.time())})

    assert published, "落后的 Agent 上线未触发升级推送"
    topic, body = published[-1]
    assert topic.endswith("/pod-upgrade/cmd")
    assert body["kind"] == "update" and body["version"] == "99.0.0"
    assert body["url"].endswith("/api/system/agent/download")


# ---- A6.1：服务端下发绝对下载地址 ----

async def test_agent_latest_returns_absolute_url_when_public_url_set(tmp_path):
    """配了 KK_PUBLIC_URL → /agent/latest 给绝对地址，镜像侧零配置即可升级。"""
    from kk_server.main import create_app

    app = create_app({
        "KK_DB_PATH": str(tmp_path / "pub.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
        "KK_PUBLIC_URL": "http://10.0.0.1:8443",
    })
    store = app.state.store
    await store.setup()
    await store.set_agent_latest({"version": "9.9.9", "sha256": "s" * 64, "size": 10})
    import httpx
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            body = (await c.get("/api/system/agent/latest", params={"ver": "0.3.0"})).json()
        assert body["available"] is True
        assert body["url"] == "http://10.0.0.1:8443/api/system/agent/download"
    finally:
        await store.close()


async def test_concurrent_uploads_stay_consistent(tmp_path):
    """并发上传必须串行化（B6.5）。

    无锁时两路「换 .prev / 写文件 / 更新 KV」会交错，可能落到「KV 清单是 A 的版本号、
    磁盘却是 B 的字节」这种错配 —— Agent 下载后 sha256 校验必失败。持锁后从清单 sha
    到磁盘字节恒一致。
    """
    import asyncio

    import httpx

    app = _make_app(tmp_path)
    store = app.state.store
    await store.setup()
    await store.ensure_admin(ADMIN_USER, ADMIN_PASS)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            tok = (await c.post("/api/login",
                                json={"username": ADMIN_USER, "password": ADMIN_PASS})
                   ).json()["token"]
            h = {"Authorization": "Bearer %s" % tok}
            a, b = b"A" * 4096, b"B" * 8192
            await asyncio.gather(
                c.post("/api/system/agent", headers=h,
                       files={"file": ("kk-agent", a)}, data={"version": "1.0.0"}),
                c.post("/api/system/agent", headers=h,
                       files={"file": ("kk-agent", b)}, data={"version": "2.0.0"}),
            )
        latest = await store.get_agent_latest()
        on_disk = open(os.path.join(str(tmp_path / "bin"), "kk-agent"), "rb").read()
        assert hashlib.sha256(on_disk).hexdigest() == latest["sha256"], \
            "清单与磁盘字节必须一致（并发交错会破坏它）"
        assert latest["version"] in ("1.0.0", "2.0.0")
        assert latest["size"] == len(on_disk)
    finally:
        await store.close()


async def test_status_reason_recorded_via_bridge(tmp_path):
    """B6.1：status 帧的 reason 经桥接落库（离线视图据此分辨 updating / 容器停了）。"""
    from kk_server.models.store import Store
    from kk_server.services.mqtt_bridge import MqttBridge

    app = _make_app(tmp_path)
    store = Store(str(tmp_path / "reason.db"))
    await store.setup()
    try:
        bridge = MqttBridge(store, app.state.settings, app.state.agent_ips, proto_ver=3)
        bridge.cli = types.SimpleNamespace(
            publish=lambda *a, **k: types.SimpleNamespace(rc=0))
        base = {"host": "pod-r", "ip": "127.0.0.1", "proto_ver": 3,
                "agent_ver": "0.3.0", "image": "img", "interval": 60,
                "ts": int(time.time())}
        await bridge._on_status("pod-r", dict(base, online=True, reason="online"))
        assert (await store.get_container("pod-r"))["status_reason"] == "online"

        # 自更新宣告
        await bridge._on_status("pod-r", dict(base, online=False, reason="updating"))
        assert (await store.get_container("pod-r"))["status_reason"] == "updating"

        # 紧随其后的 LWT（reason 为空）不得抹掉 updating
        await bridge._on_status("pod-r", dict(base, online=False, reason=""))
        assert (await store.get_container("pod-r"))["status_reason"] == "updating"
    finally:
        await store.close()


async def test_agent_latest_relative_url_without_public_url(tmp_path):
    """未配 KK_PUBLIC_URL 时行为不变（相对路径），不破坏既有部署。"""
    from kk_server.main import create_app

    app = create_app({"KK_DB_PATH": str(tmp_path / "nopub.db"),
                      "KK_WEB_DIR": str(tmp_path / "noweb")})
    store = app.state.store
    await store.setup()
    await store.set_agent_latest({"version": "9.9.9", "sha256": "s" * 64, "size": 10})
    import httpx
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            body = (await c.get("/api/system/agent/latest", params={"ver": "0.3.0"})).json()
        assert body["url"] == "/api/system/agent/download"
    finally:
        await store.close()
