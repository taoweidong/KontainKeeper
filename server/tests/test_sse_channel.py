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


# ---- QR1128-S11：queued 写侧接线（下发失败 → queued → status online 补投）----

def _status_frame(host):
    return {"online": True, "host": host, "ip": "127.0.0.1", "proto_ver": 4,
            "agent_ver": "1.0.0", "image": "img:1", "interval": 60,
            "reason": "online", "ts": int(time.time())}


async def _login_admin(api):
    await api.store.ensure_admin(ADMIN, PASS)
    r = await api.client.post("/api/login", json={"username": ADMIN, "password": PASS})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


async def test_dispatch_failure_queues_then_status_online_flushes(api, monkeypatch):
    """QR1128-S11：下发失败 → DB queued（不再留 pending 被 timeout+slack 误判）；
    status online 触发点经 MQTT 兜底补投、转 sent 刷计时。此前写侧从未接线，
    store/sweep/flush/前端词表整套 queued 机制在生产是死功能（design-transport §5.2）。
    """
    await api.store.upsert_container("q-1", "img", "1.0", 60)
    h = await _login_admin(api)
    orig = api.bridge.dispatch_command
    monkeypatch.setattr(api.bridge, "dispatch_command", lambda row: False)
    r = await api.client.post("/api/commands", headers=h,
                              json={"pods": ["q-1"], "kind": "shell", "argv": ["echo"]})
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["status"] == "queued"
    assert (await api.store.get_command(item["id"]))["status"] == "queued"

    # 重连（status online）→ 补投：恢复 dispatch，帧经 MQTT 出去、行转 sent
    monkeypatch.setattr(api.bridge, "dispatch_command", orig)
    await api.bridge._on_status("q-1", _status_frame("q-1"))
    row = await api.store.get_command(item["id"])
    assert row["status"] == "sent" and row["sent_at"] >= int(time.time()) - 5
    assert any(m["topic"] == "kk/v1/q-1/cmd" for m in api.bridge.cli.msgs)
    # 再触发一次 status online：无 queued 行，不重复投
    n_msgs = len(api.bridge.cli.msgs)
    await api.bridge._on_status("q-1", _status_frame("q-1"))
    assert len(api.bridge.cli.msgs) == n_msgs


async def test_status_online_flush_prefers_sse(api, monkeypatch):
    """QR1128-S11：status online 补投与 SSE 连接建立触发点同口径——SSE 连接在
    就直推队列，不经 MQTT（桥接经 create_app 注入的 sse_hub 引用路由）。"""
    await api.store.upsert_container("q-2", "img", "1.0", 60)
    h = await _login_admin(api)
    monkeypatch.setattr(api.bridge, "dispatch_command", lambda row: False)
    r = await api.client.post("/api/commands", headers=h,
                              json={"pods": ["q-2"], "kind": "shell", "argv": ["echo"]})
    cid = r.json()["items"][0]["id"]
    assert (await api.store.get_command(cid))["status"] == "queued"

    q = api.hub.connect("q-2")
    await api.bridge._on_status("q-2", _status_frame("q-2"))
    assert q.get_nowait()["id"] == cid
    assert (await api.store.get_command(cid))["status"] == "sent"
    assert not api.bridge.cli.msgs, "SSE 直推不得再经 MQTT"


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


async def test_reconnect_disconnect_is_identity_scoped(api):
    """QR1128-S7：同 host 重连后，旧生成器的 finally 按队列身份注销——
    不得摘掉新连接（旧实现按 host 无条件 pop，活跃主机被迟到收尾误判离线、
    后续命令推进死队列）。返回 False 即告知调用方「主机仍活跃，别置离线」。"""
    q1 = api.hub.connect("rc-1")
    q2 = api.hub.connect("rc-1")          # 重连：新队列覆盖旧条目
    assert q1 is not q2
    assert api.hub.disconnect("rc-1", q1) is False   # 旧生成器收尾：no-op
    assert api.hub.is_connected("rc-1")
    assert api.hub.push_command("rc-1", {"id": "c-x"}) is True
    assert q2.get_nowait()["id"] == "c-x"            # 命令进的是新队列
    assert api.hub.disconnect("rc-1", q2) is True    # 新连接自己收尾才摘
    assert not api.hub.is_connected("rc-1")


# ---- RoutingDownlink 路由 ----

async def test_routing_sse_first(api):
    api.hub.connect("rt-1")
    dl = RoutingDownlink(api.hub, api.bridge)
    ok = dl.dispatch_command({"id": "c-1", "pod": "rt-1", "kind": "shell",
                              "argv": json.dumps(["echo"]), "timeout": 30})
    assert ok is True
    # SSE 直推，不经 paho
    assert api.bridge.cli.msgs == []
    assert api.hub._conns["rt-1"]["queue"].get_nowait()["id"] == "c-1"


async def test_routing_mqtt_fallback(api):
    dl = RoutingDownlink(api.hub, api.bridge)
    ok = dl.dispatch_command({"id": "c-2", "pod": "rt-off", "kind": "shell",
                              "argv": json.dumps(["echo"]), "timeout": 30})
    assert ok is True
    assert len(api.bridge.cli.msgs) == 1       # 回落 paho
    assert api.bridge.cli.msgs[0]["topic"] == "kk/v1/rt-off/cmd"


async def test_routing_no_channel(api):
    dl = RoutingDownlink(api.hub, None)
    assert dl.dispatch_command({"id": "c-3", "pod": "x", "kind": "shell",
                                "argv": "[]", "timeout": 30}) is False


async def test_routing_publish_update_delegates_bridge(api):
    # Phase 0 升级帧仍走 MQTT
    dl = RoutingDownlink(api.hub, api.bridge)
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


async def test_sse_disconnect_offline_survives_cancellation(api):
    """QR1125 E2E 裁决回归锁：客户端断连时 uvicorn **cancel 请求任务**（非优雅 aclose），
    生成器 finally 内裸 await set_online 会被再次取消而中断落库（连接泄漏、主机永不
    离线）。finally 用 asyncio.shield 让离线落库作为独立任务完成——本用例模拟取消
    上下文（task.cancel 驱动生成器），断言主机仍被置 offline。aclose 路径见上一条。
    """
    await api.store.upsert_container("sse-c", "img", "1.0", 60)
    await api.store.set_online("sse-c", True)
    scope = {"type": "http", "app": api.app, "method": "GET", "path": "/",
             "headers": [], "query_string": b"", "client": ("127.0.0.1", 1)}

    async def receive():
        return {"type": "http.request"}

    resp = await command_stream(Request(scope, receive), "sse-c")
    it = resp.body_iterator
    await it.__anext__()                       # retry 帧；生成器停在 wait_for(queue.get)

    async def drive():
        await it.__anext__()                   # 无命令 → 阻塞等 ping，模拟长连接

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.05)
    task.cancel()                              # 模拟 uvicorn 断连取消
    try:
        await task
    except asyncio.CancelledError:
        pass
    # shield 内层任务独立完成离线落库（给事件循环 ticks）
    for _ in range(100):
        if not (await api.store.get_container("sse-c"))["online"]:
            break
        await asyncio.sleep(0.02)
    assert (await api.store.get_container("sse-c"))["online"] == 0
    assert not api.hub.is_connected("sse-c")


async def test_sse_requires_whitelist(api):
    api.app.state.agent_ips = parse_ip_whitelist("10.0.0.0/24")
    async with api.client.stream(
        "GET", "/api/v1/commands/stream?host=x"
    ) as resp:
        assert resp.status_code == 403       # 依赖先拒，不进流式


async def test_sse_bad_host_422(api):
    r = await api.client.get("/api/v1/commands/stream?host=" + "a" * 121)
    assert r.status_code == 422