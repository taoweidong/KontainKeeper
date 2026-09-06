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
        assert os.path.exists(os.path.join(str(tmp_path / "bin"), "kk-agent.prev"))


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
