"""QR1125 Phase 0 HTTP 上行 ingest 回归锁。

组合复用 bridge 处理器：ingest 落库与 MQTT 路径写同一 store/stats 一致。鉴权挂
agent_ip_auth 真实 TCP 源 IP（ASGITransport 默认源 127.0.0.1）。不连真实 Broker
（FakePublish + 直接 await 处理器），落单元层不顶 skipped 门禁。
"""
import json
import time
import types

import httpx
import pytest

from _kk_db import kk_db_env, kk_db_url, pg_reset

from kk_server.config import parse_ip_whitelist
from kk_server.main import create_app
from kk_server.services.mqtt_bridge import MAX_FRAME_BYTES

ADMIN = "admin"
PASS = "ing-pass"


class FakePublish:
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos})
        return types.SimpleNamespace(rc=self.rc)


def _mk_app(tmp_path, **extra):
    return create_app({
        "KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
        "KK_MQTT_URL": "mqtt://broker:1883",   # bridge 存在（Phase 0 并存）
        "KK_AGENT_IPS": "127.0.0.1",           # 放行 ASGITransport 源 IP
        **kk_db_env(tmp_path), "KK_WEB_DIR": str(tmp_path / "noweb"),
        **extra,
    })


@pytest.fixture
async def api(tmp_path):
    app = _mk_app(tmp_path)
    store = app.state.store
    await store.setup()
    await pg_reset(store)
    bridge = app.state.bridge
    bridge.cli = FakePublish()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield types.SimpleNamespace(client=client, store=store, app=app, bridge=bridge)
    await store.close()


def _status(host, **over):
    return {"online": True, "host": host, "ip": "127.0.0.1", "proto_ver": 4,
            "agent_ver": "1.0.0", "image": "img:1", "interval": 60,
            "reason": "online", "ts": int(time.time()), **over}


def _hb(host):
    return {"host": host, "ip": "127.0.0.1", "interval": 60,
            "metrics": {"cpu": 5.0, "mem_mb": 256.0}}


async def _post(api, suffix, frame):
    return await api.client.post("/api/v1/ingest/" + suffix, json=frame)


async def test_ingest_status_registers(api):
    r = await _post(api, "status", _status("ing-1"))
    assert r.status_code == 200 and r.json()["ok"] is True
    row = await api.store.get_container("ing-1")
    assert row and row["online"] == 1 and row["agent_ver"] == "1.0.0"
    # 与 MQTT 共享 stats：status 与 ingest_status 都累加
    assert api.bridge.stats["status"] == 1
    assert api.bridge.stats["ingest_status"] == 1


async def test_ingest_hb_recorded(api):
    await _post(api, "status", _status("ing-2"))
    r = await _post(api, "hb", _hb("ing-2"))
    assert r.status_code == 200
    series, source = await api.store.metrics_series("ing-2", 1)
    assert source == "raw" and len(series) == 1 and series[0]["cpu"] == 5.0
    assert api.bridge.stats["hb"] == 1 and api.bridge.stats["ingest_hb"] == 1


async def test_ingest_result_full_chain(api):
    await _post(api, "status", _status("ing-3"))
    cid = await api.store.create_command("ing-3", "shell", ["echo", "hi"], 30, "admin")
    await api.store.mark_sent(cid)
    r0 = await _post(api, "result", {"host": "ing-3", "id": cid, "seq": 0,
                                     "total": 2, "out_b64": "aGVsbG8g", "done": False})
    r1 = await _post(api, "result", {"host": "ing-3", "id": cid, "seq": 1,
                                     "total": 2, "out_b64": "d29ybGQ=", "done": True, "rc": 0})
    assert r0.status_code == 200 and r1.status_code == 200
    row = await api.store.get_command(cid)
    assert row["status"] == "done" and row["out_chunks"] == 2
    assert await api.store.command_output(cid) == "hello world"
    assert api.bridge.stats["result"] == 2 and api.bridge.stats["ingest_result"] == 2


async def test_ingest_whitelist_denied(api):
    # 把白名单翻到不含源 IP 127.0.0.1 → agent_ip_auth 真实源 IP 拒收
    api.app.state.agent_ips = parse_ip_whitelist("10.0.0.0/24")
    r = await _post(api, "status", _status("ing-evil"))
    assert r.status_code == 403
    assert await api.store.get_container("ing-evil") is None


async def test_ingest_unknown_host_hb_rejected(api):
    # 没上线过的主机直接发 hb：bridge._on_hb 拒收（与 MQTT 同语义）
    r = await _post(api, "hb", _hb("ghost"))
    assert r.status_code == 200            # HTTP 收到，内部拒收
    assert api.bridge.stats["rejected"] >= 1
    series, _ = await api.store.metrics_series("ghost", 1)
    assert series == []


async def test_ingest_proto_mismatch_ignored(api):
    r = await _post(api, "status", _status("ing-old", proto_ver=1))
    assert r.status_code == 200
    assert await api.store.get_container("ing-old") is None   # 窗外拒收
    assert api.bridge.stats["rejected"] >= 1


async def test_ingest_cross_host_result_rejected(api):
    await _post(api, "status", _status("pod-a"))
    await _post(api, "status", _status("pod-b"))
    cid = await api.store.create_command("pod-b", "shell", ["echo"], 30, "admin")
    # pod-a 替 pod-b 回传 → 归属拒收
    r = await _post(api, "result", {"host": "pod-a", "id": cid, "seq": 0,
                                    "total": 1, "out_b64": "eA==", "done": True, "rc": 0})
    assert r.status_code == 200
    assert (await api.store.get_command(cid))["status"] == "pending"


async def test_ingest_oversized_413(api):
    raw = b'{"host":"x","pad":"' + b"a" * (MAX_FRAME_BYTES + 10) + b'"}'
    r = await api.client.post("/api/v1/ingest/status", content=raw,
                              headers={"Content-Type": "application/json"})
    assert r.status_code == 413


async def test_ingest_oversized_chunked_413(api):
    """QR1128-S6：无 Content-Length（chunked 传输）时流式累计超限即拒。

    旧实现先 await request.body() 整包缓冲再查尺寸——1GB body 会先被完整
    读进内存才 413，预检形同虚设。异步生成器 content 让 httpx 走 chunked、
    不带 Content-Length，精确覆盖流式累计分路。
    """
    async def body():
        for _ in range(64):
            yield b"x" * 65536      # 共 4MB，远超 1MB 上限
    r = await api.client.post("/api/v1/ingest/status", content=body(),
                              headers={"Content-Type": "application/json"})
    assert r.status_code == 413


async def test_ingest_bad_json_422(api):
    r = await api.client.post("/api/v1/ingest/status", content=b"not json",
                              headers={"Content-Type": "application/json"})
    assert r.status_code == 422


async def test_ingest_missing_host_422(api):
    r = await _post(api, "status", {"online": True, "proto_ver": 4})
    assert r.status_code == 422


async def test_ingest_no_bridge_503(tmp_path):
    # 未配 KK_MQTT_URL → bridge None → 503（Phase 0 并存模式）
    app = create_app({
        "KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
        "KK_AGENT_IPS": "127.0.0.1",
        **kk_db_env(tmp_path), "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    await pg_reset(store)
    assert app.state.bridge is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/v1/ingest/status", json=_status("x"))
        assert r.status_code == 503 and "KK_MQTT_URL" in r.json()["detail"]
    await store.close()