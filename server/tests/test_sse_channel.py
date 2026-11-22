"""QR1125 Phase 0 SSE 下行命令通道 + 下行路由回归锁（单元层，不连 Broker）。

SSE 端点测试用「直接调 endpoint 函数 + 显式 aclose() 触发 finally」的确定性手法：
ASGITransport 的流式断连传播不确定，且根 pyproject filterwarnings=error 下悬空
长连接任务会假红——直接驱动 body_iterator 并 aclose 干净收尾，真实 HTTP 流式 E2E
留验证批次（真 uvicorn + 真客户端断连）。
"""
import asyncio
import json
import time
import types

import httpx
import pytest
from starlette.requests import Request

from _kk_db import kk_db_env, pg_reset

from kk_server.config import parse_ip_whitelist
from kk_server.controllers.commands_stream import command_stream
from kk_server.main import create_app
from kk_server.services.downlink import RoutingDownlink, cmd_payload, get_downlink
from kk_server.services.sse_hub import SseHub

ADMIN = "admin"
PASS = "sse-pass"


class FakePublish:
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos})
        return types.SimpleNamespace(rc=self.rc)


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
        "KK_MQTT_URL": "mqtt://broker:1883", "KK_AGENT_IPS": "127.0.0.1",
        **kk_db_env(tmp_path), "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    await pg_reset(store)
    app.state.bridge.cli = FakePublish()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield types.SimpleNamespace(client=client, store=store, app=app,
                                    bridge=app.state.bridge, hub=app.state.sse_hub)
    await store.close()


# ---- SseHub 连接表 ----

async def test_hub_connect_push_disconnect():
    hub = SseHub()
    assert not hub.is_connected("h")
    q = hub.connect("h")
    assert hub.is_connected("h")
    assert hub.push_command("h", {"id": "c-1"}) is True
    assert q.get_nowait() == {"id": "c-1"}
    assert hub.push_command("absent", {"id": "c-2"}) is False
    hub.disconnect("h")
    assert not hub.is_connected("h")
    assert hub.push_command("h", {"id": "c-3"}) is False


async def test_hub_sweep_dead():
    hub = SseHub()
    hub.connect("alive")
    hub.connect("dead")
    hub.touch("alive")
    # 把 dead 的 ts 拨到超时外
    hub._conns["dead"]["ts"] = int(time.time()) - 200
    assert hub.sweep_dead(timeout=90) == 1
    assert hub.is_connected("alive") and not hub.is_connected("dead")


async def test_flush_queued_commands(api):
    await api.store.upsert_container("fl-1", "img", "1.0", 60)
    ids, _ = await api.store.create_commands_batch(
        ["fl-1", "fl-1"], "shell", ["echo", "hi"], 30, "admin", status="queued")
    q = api.hub.connect("fl-1")
    n = await api.hub.flush_queued_commands("fl-1", api.store)
    assert n == 2
    # 先转 sent 刷计时（防补投瞬间被短 timeout 误判）
    for cid in ids:
        row = await api.store.get_command(cid)
        assert row["status"] == "sent" and row["sent_at"] >= int(time.time()) - 5
    # 按 created_at 升序入队
    assert [q.get_nowait()["id"] for _ in range(2)] == ids
    # 再 flush 无 queued → 0
    assert await api.hub.flush_queued_commands("fl-1", api.store) == 0


# ---- cmd_payload 帧还原 ----

def test_cmd_payload_variants():
    shell = cmd_payload({"id": "c-1", "kind": "shell", "timeout": 30,
                         "argv": json.dumps(["echo", "hi"])})
    assert shell == {"id": "c-1", "kind": "shell", "timeout": 30, "argv": ["echo", "hi"]}
    collect = cmd_payload({"id": "c-2", "kind": "collect", "timeout": 30,
                           "argv": json.dumps({"items": ["cpu", "mem"]})})
    assert collect["items"] == ["cpu", "mem"] and "argv" not in collect
    use_shell = cmd_payload({"id": "c-3", "kind": "shell", "timeout": 30,
                             "argv": json.dumps({"argv": ["ls | wc"], "use_shell": True})})
    assert use_shell["use_shell"] is True and use_shell["argv"] == ["ls | wc"]
    bad = cmd_payload({"id": "c-4", "kind": "shell", "timeout": 30, "argv": "not-json"})
    assert bad["argv"] == []


# ---- RoutingDownlink 路由 ----

async def test_routing_sse_first(api):
    api.hub.connect("rt-1")
    dl = RoutingDownlink(api.hub, api.bridge, api.store)
    ok = dl.dispatch_command({"id": "c-1", "pod": "rt-1", "kind": "shell",
                              "argv": json.dumps(["echo"]), "timeout": 30})
    assert ok is True
    # SSE 直推，不经 paho
    assert api.bridge.cli.msgs == []
    assert api.hub._conns["rt-1"]["queue"].get_nowait()["id"] == "c-1"


async def test_routing_mqtt_fallback(api):
    dl = RoutingDownlink(api.hub, api.bridge, api.store)
    ok = dl.dispatch_command({"id": "c-2", "pod": "rt-off", "kind": "shell",
                              "argv": json.dumps(["echo"]), "timeout": 30})
    assert ok is True
    assert len(api.bridge.cli.msgs) == 1       # 回落 paho
    assert api.bridge.cli.msgs[0]["topic"] == "kk/v1/rt-off/cmd"


async def test_routing_no_channel(api):
    dl = RoutingDownlink(api.hub, None, api.store)
    assert dl.dispatch_command({"id": "c-3", "pod": "x", "kind": "shell",
                                "argv": "[]", "timeout": 30}) is False


async def test_routing_publish_update_delegates_bridge(api):
    # Phase 0 升级帧仍走 MQTT
    dl = RoutingDownlink(api.hub, api.bridge, api.store)
    latest = {"version": "9.9.9", "sha256": "ab", "size": 10}
    ok = await dl.publish_update("up-host", "up-1", latest)
    assert ok is True
    frame = json.loads(api.bridge.cli.msgs[0]["payload"])
    assert frame["kind"] == "update" and frame["version"] == "9.9.9"


def test_get_downlink_fallback_duck_type():
    bridge = types.SimpleNamespace(dispatch_command=lambda row: True)
    store = object()
    # 无 sse_hub → 直接 bridge（保 _FakeBridge 鸭子类型）
    req = types.SimpleNamespace(app=types.SimpleNamespace(
        state=types.SimpleNamespace(bridge=bridge, store=store)))
    assert get_downlink(req) is bridge
    # 有 sse_hub → RoutingDownlink
    req.app.state.sse_hub = SseHub()
    dl = get_downlink(req)
    assert isinstance(dl, RoutingDownlink) and dl.bridge is bridge


# ---- SSE 端点：flush + 断连置 offline（直接驱动生成器，确定性收尾）----

async def test_sse_endpoint_flush_and_disconnect_offline(api):
    await api.store.upsert_container("sse-1", "img", "1.0", 60)
    await api.store.set_online("sse-1", True)
    ids, _ = await api.store.create_commands_batch(
        ["sse-1"], "shell", ["echo", "hi"], 30, "admin", status="queued")

    scope = {"type": "http", "app": api.app, "method": "GET", "path": "/",
             "headers": [], "query_string": b"", "client": ("127.0.0.1", 1)}

    async def receive():
        return {"type": "http.request"}

    resp = await command_stream(Request(scope, receive), "sse-1")
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    it = resp.body_iterator
    assert (await it.__anext__()).startswith("retry:")     # 重连延迟告知
    event = await it.__anext__()                            # flush 的 queued 命令
    assert event.startswith("id: %s" % ids[0])
    data = json.loads(event.split("data: ", 1)[1].strip())
    assert data["kind"] == "shell" and data["argv"] == ["echo", "hi"]
    assert (await api.store.get_command(ids[0]))["status"] == "sent"
    # 显式关闭生成器 → finally disconnect + set_online(False)
    await it.aclose()
    assert not api.hub.is_connected("sse-1")
    assert (await api.store.get_container("sse-1"))["online"] == 0


async def test_sse_requires_whitelist(api):
    api.app.state.agent_ips = parse_ip_whitelist("10.0.0.0/24")
    async with api.client.stream(
        "GET", "/api/v1/commands/stream?host=x"
    ) as resp:
        assert resp.status_code == 403       # 依赖先拒，不进流式


async def test_sse_bad_host_422(api):
    r = await api.client.get("/api/v1/commands/stream?host=" + "a" * 121)
    assert r.status_code == 422