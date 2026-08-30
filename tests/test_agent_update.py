"""服务端 Agent 自更新接口 + hello 推送 upgrade 帧的集成测试。"""
import os

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


def test_hello_pushes_upgrade(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    token = _admin_token(client)

    # 先发布一个比当前 AGENT_VER 更新的版本
    payload = b"\x7fELF-newer"
    r = client.post("/api/system/agent",
                    headers={"Authorization": "Bearer %s" % token},
                    files={"file": ("kk-agent", payload)},
                    data={"version": "99.0.0"})
    assert r.status_code == 200

    # 用一个落后版本的 Agent 连上来，应立刻收到 upgrade 帧
    from kk_agent import config as kk_config
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({
            "t": "hello", "id": "x1", "proto_ver": 1,
            "pod": "pod-upgrade", "image": "img", "agent_ver": "0.0.1",
            "token": AGENT_TOKEN, "interval": 60,
        })
        msg = ws.receive_json()
        assert msg["t"] == "upgrade", msg
        assert msg["version"] == "99.0.0"
        assert msg["url"].endswith("/api/system/agent/download")
