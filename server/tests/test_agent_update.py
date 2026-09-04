"""服务端 Agent 自更新接口 + 桥接推送 upgrade 帧的测试。"""
import json
import os
import time
import types

from fastapi.testclient import TestClient

from kk_server.main import create_app

AGENT_TOKEN = "test-agent-token"
ADMIN_USER = "admin"
ADMIN_PASS = "adm-pass"


def _make_app(tmp_path):
    env = {
        "KK_AGENT_TOKENS": AGENT_TOKEN,
        "KK_ADMIN_USER": ADMIN_USER,
        "KK_ADMIN_PASS": ADMIN_PASS,
        "KK_DB_PATH": ":memory:",
        "KK_AGENT_BIN_DIR": str(tmp_path / "bin"),
    }
    return create_app(env)


def _admin_token(client):
    r = client.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_upload_and_discovery(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    token = _admin_token(client)

    payload = b"\x7fELF-fake-kk-agent-binary"
    r = client.post("/api/system/agent",
                    headers={"Authorization": "Bearer %s" % token},
                    files={"file": ("kk-agent", payload)},
                    data={"version": "0.2.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "0.2.0"
    assert body["size"] == len(payload)
    import hashlib
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()

    # 落后版本查询：应 available
    r = client.get("/api/system/agent/latest?ver=0.1.0",
                   headers={"Authorization": "Bearer %s" % AGENT_TOKEN})
    assert r.status_code == 200
    assert r.json()["available"] is True
    assert r.json()["version"] == "0.2.0"

    # 已是最新：不 available
    r = client.get("/api/system/agent/latest?ver=0.2.0",
                   headers={"Authorization": "Bearer %s" % AGENT_TOKEN})
    assert r.json()["available"] is False

    # 下载：内容与上传一致
    r = client.get("/api/system/agent/download",
                   headers={"Authorization": "Bearer %s" % AGENT_TOKEN})
    assert r.status_code == 200
    assert r.content == payload


def test_auth_required(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    _admin_token(client)

    # 下载无 token → 401
    assert client.get("/api/system/agent/download").status_code == 401
    # 最新清单无 token → 401
    assert client.get("/api/system/agent/latest?ver=0.1.0").status_code == 401
    # 上传无 admin token → 401
    assert client.post("/api/system/agent",
                       files={"file": ("kk-agent", b"x")},
                       data={"version": "0.2.0"}).status_code == 401


def test_status_pushes_upgrade(tmp_path):
    """上传新版本 → 落后的 Agent 一上线（status 帧）就该收到 update 命令。

    原实现走 WS hello，改用 MQTT 后这条链路的两端分别是 HTTP 上传与桥接的
    retained status 处理，这里把它们串起来测。
    """
    app = _make_app(tmp_path)
    client = TestClient(app)
    token = _admin_token(client)

    payload = b"ELF-newer"
    r = client.post("/api/system/agent",
                    headers={"Authorization": "Bearer %s" % token},
                    files={"file": ("kk-agent", payload)},
                    data={"version": "99.0.0"})
    assert r.status_code == 200

    from kk_server.services.mqtt_bridge import MqttBridge
    published = []
    bridge = MqttBridge(app.state.store, app.state.settings,
                        app.state.agent_tokens, proto_ver=2)
    bridge.cli = types.SimpleNamespace(
        publish=lambda topic, body, qos=0, retain=False: (
            published.append((topic, json.loads(body))), types.SimpleNamespace(rc=0))[1])
    bridge._on_status("pod-upgrade", {
        "online": True, "host": "pod-upgrade", "token": AGENT_TOKEN, "proto_ver": 2,
        "agent_ver": "0.0.1", "image": "img", "interval": 60, "ts": int(time.time())})

    assert published, "落后的 Agent 上线未触发升级推送"
    topic, body = published[-1]
    assert topic.endswith("/pod-upgrade/cmd")
    assert body["kind"] == "update" and body["version"] == "99.0.0"
    assert body["url"].endswith("/api/system/agent/download")
