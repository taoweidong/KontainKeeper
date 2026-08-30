"""Hub 单测：伪造 WebSocket 脚本驱动连接生命周期。"""
import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from kk_server.hub import Hub
from kk_server.store import Store

STOP = object()


class FakeWS:
    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = None

    async def accept(self):
        pass

    async def receive_json(self):
        if self.script:
            item = self.script.pop(0)
            if item is STOP:
                raise WebSocketDisconnect()
            return item
        raise WebSocketDisconnect()

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, code=1000):
        self.closed = code


def hello(token="tok", pod="pod-a", proto=1):
    return {"t": "hello", "id": "x", "proto_ver": proto, "pod": pod,
            "image": "img:1", "agent_ver": "0.1.0", "token": token, "interval": 60}


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "hub.db"))


def run(coro):
    return asyncio.run(coro)


def test_reject_bad_token(store):
    hub = Hub(store, {"tok"})
    ws = FakeWS([hello(token="wrong")])
    run(hub.agent_endpoint(ws))
    assert ws.closed == 4401
    assert hub.conns == {}
    assert store.list_audit()[0]["action"] == "hello_rejected"


def test_reject_proto_mismatch(store):
    hub = Hub(store, {"tok"})
    ws = FakeWS([hello(proto=99)])
    run(hub.agent_endpoint(ws))
    assert ws.closed == 4402


def test_lifecycle_pending_flush_and_hb(store):
    hub = Hub(store, {"tok"})
    # Agent 离线期间积累的 pending 命令
    store.upsert_container("pod-a", "img:1", "0.1.0", 60)
    cid = store.create_command("pod-a", "shell", ["du", "-sh", "/w"], 30, "admin")

    ws = FakeWS([hello(), {"t": "hb", "interval": 60,
                           "metrics": {"cpu": 1.0, "mem_mb": 2.0}, "custom": {}}])
    run(hub.agent_endpoint(ws))

    assert hub.conns == {}  # 断开后连接表应清空
    assert ws.closed is None  # 对端主动断开，非服务端关闭

    sent = [m for m in ws.sent if m.get("t") == "cmd"]
    assert sent == [{"t": "cmd", "id": cid, "kind": "shell",
                     "argv": ["du", "-sh", "/w"], "timeout": 30}]
    assert store.get_command(cid)["status"] == "sent"

    row = store.get_container("pod-a")
    assert json.loads(row["last_metrics"])["metrics"]["cpu"] == 1.0
    assert store.metrics_series("pod-a", 1)[1] == "raw"


def test_cmd_result_routed_to_store(store):
    hub = Hub(store, {"tok"})
    store.upsert_container("pod-a", "i", "v", 60)
    cid = store.create_command("pod-a", "shell", ["x"], 30, "admin")
    store.mark_sent(cid)
    ws = FakeWS([hello(),
                 {"t": "cmd_result", "id": cid, "seq": 0, "data_b64": "b2s=",
                  "done": True, "rc": 0, "timed_out": False, "elapsed_ms": 5},
                 ])
    run(hub.agent_endpoint(ws))
    row = store.get_command(cid)
    assert row["status"] == "done" and row["out"] == "ok" and row["rc"] == 0


def test_try_dispatch_offline_returns_false(store):
    hub = Hub(store, {"tok"})
    store.upsert_container("pod-x", "i", "v", 60)
    cid = store.create_command("pod-x", "shell", ["x"], 30, "admin")
    row = store.get_command(cid)
    assert run(hub.try_dispatch(row)) is False
    assert store.get_command(cid)["status"] == "pending"
