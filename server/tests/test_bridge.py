"""MQTT 桥接单测：不连真实 Broker，用假 publish 与同步派发钉住路由与校验。

覆盖原 test_hub.py 关心、但改由 Broker 承担后仍需服务端把关的部分：
归属校验、token/协议闸门、命令帧字段还原、URL 解析、主题解析。
"""
import base64
import json
import time
import types

import pytest

from kk_server.config import load_settings
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge

TOKEN = "bridge-token"


class FakePublish:
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return types.SimpleNamespace(rc=self.rc)


@pytest.fixture
async def bridge(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    await store.setup()
    settings = load_settings({
        "KK_DB_PATH": str(tmp_path / "b.db"),
        "KK_AGENT_TOKENS": TOKEN,
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_TOPIC_PREFIX": "kk/v1",
        "KK_MQTT_CLIENT_ID": "kk-server",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    b = MqttBridge(store, settings, settings.agent_tokens, loop=None, proto_ver=2)
    b.cli = FakePublish()
    b.store = store
    yield b
    await store.close()


def status_frame(host, online=True, token=TOKEN, proto=2, ver="0.2.0"):
    return {"online": online, "host": host, "token": token, "proto_ver": proto,
            "agent_ver": ver, "image": "img:1", "interval": 60, "reason": "online",
            "ts": int(time.time())}


# ---- 主题与地址解析 ----

@pytest.mark.parametrize("url,expect", [
    ("mqtt://b:1883", ("b", 1883, False)),
    ("mqtts://b", ("b", 8883, True)),
    ("mqtt://u:p@b:1884", ("b", 1884, False)),
    ("mqtt://[fe80::1]:1883", ("fe80::1", 1883, False)),
])
async def test_parse_url(url, expect):
    assert MqttBridge._parse_url(url) == expect


async def test_parse_url_rejects_ws():
    with pytest.raises(ValueError):
        MqttBridge._parse_url("ws://b/ws/agent")


async def test_sub_topics_are_plain_subscriptions(bridge):
    """默认不开共享订阅（单实例用不到）；扩容才改成 $share/{group}/ 前缀。"""
    topics = [t for t, _ in bridge._sub_topics()]
    assert topics == ["kk/v1/+/status", "kk/v1/+/hb", "kk/v1/+/result"]


async def test_on_message_routes_by_topic(bridge):
    import asyncio
    bridge.loop = asyncio.get_running_loop()
    seen = {}
    bridge._on_status = lambda host, body: seen.__setitem__("status", host)
    bridge._on_hb = lambda host, body: seen.__setitem__("hb", host)
    bridge._on_result = lambda host, body: seen.__setitem__("result", host)
    for suffix, key in (("status", "status"), ("hb", "hb"), ("result", "result")):
        bridge._on_message(None, None, types.SimpleNamespace(
            topic="kk/v1/h-1/" + suffix,
            payload=json.dumps({"id": "c1", "host": "h-1"}).encode()))
        await asyncio.sleep(0.05)      # 帧是经 call_soon_threadsafe 派发的
        assert seen[key] == "h-1", suffix


async def test_on_message_ignores_garbage(bridge):
    import asyncio
    bridge.loop = asyncio.get_running_loop()
    called = []
    bridge._on_hb = lambda h, b: called.append(h)
    bridge._on_message(None, None, types.SimpleNamespace(topic="kk/v1/h1/hb",
                                                         payload=b"{not json"))
    bridge._on_message(None, None, types.SimpleNamespace(topic="kk/v1/hb",
                                                         payload=b"{}"))
    await asyncio.sleep(0.05)
    assert called == []


# ---- status：鉴权闸门与在线真相 ----

async def test_status_registers_host_online(bridge):
    await bridge._on_status("web-01", status_frame("web-01"))
    assert await bridge.store.is_online("web-01") is True
    row = await bridge.store.get_container("web-01")
    assert row["image"] == "img:1" and row["agent_ver"] == "0.2.0"


async def test_status_without_valid_token_rejected(bridge):
    await bridge._on_status("evil", status_frame("evil", token="nope"))
    assert await bridge.store.get_container("evil") is None
    assert bridge.stats["rejected"] == 1
    audit = (await bridge.store.list_audit())[0]
    assert audit["action"] == "status_rejected"


async def test_status_revoked_token_rejected(bridge):
    await bridge.store.revoke_token(TOKEN)
    await bridge._on_status("web-02", status_frame("web-02"))
    assert await bridge.store.get_container("web-02") is None


async def test_status_proto_mismatch_ignored(bridge):
    await bridge._on_status("web-03", status_frame("web-03", proto=1))
    assert await bridge.store.get_container("web-03") is None
    assert (await bridge.store.list_audit())[0]["action"] == "proto_mismatch"


async def test_lwt_offline_marks_offline(bridge):
    await bridge._on_status("web-04", status_frame("web-04"))
    assert await bridge.store.is_online("web-04") is True
    await bridge._on_status("web-04", status_frame("web-04", online=False))
    assert await bridge.store.is_online("web-04") is False
    assert await bridge.store.online_count() == 0


async def test_status_pushes_upgrade_when_agent_behind(bridge):
    await bridge.store.set_agent_latest({"version": "99.0.0", "sha256": "ab", "size": 8})
    await bridge._on_status("web-05", status_frame("web-05", ver="0.0.1"))
    pushed = [m for m in bridge.cli.msgs if m["topic"] == "kk/v1/web-05/cmd"]
    assert pushed, "落后的 Agent 上线即应收到升级推送"
    body = json.loads(pushed[-1]["payload"])
    assert body["kind"] == "update" and body["version"] == "99.0.0"
    assert body["url"].endswith("/api/system/agent/download")


async def test_status_no_upgrade_when_up_to_date(bridge):
    await bridge.store.set_agent_latest({"version": "0.2.0", "sha256": "ab", "size": 8})
    await bridge._on_status("web-06", status_frame("web-06", ver="0.2.0"))
    assert [m for m in bridge.cli.msgs if m["topic"].endswith("/cmd")] == []


# ---- hb / result 归属 ----

async def test_hb_from_unknown_host_not_recorded(bridge):
    await bridge._on_hb("ghost", {"host": "ghost", "ts": 1, "metrics": {"mem_mb": 1.0}})
    assert await bridge.store.list_containers() == []
    assert (await bridge.store.list_audit())[0]["action"] == "hb_unknown_host"


async def test_hb_recorded_after_status(bridge):
    await bridge._on_status("web-07", status_frame("web-07"))
    await bridge._on_hb("web-07", {"host": "web-07", "ts": int(time.time()), "interval": 60,
                             "metrics": {"cpu": 3.0, "mem_mb": 400.0}})
    row = await bridge.store.get_container("web-07")
    assert "400.0" in row["last_metrics"] or "mem_mb" in row["last_metrics"]
    series, _ = await bridge.store.metrics_series("web-07", hours=24)
    assert len(series) == 1


async def test_result_cross_host_rejected(bridge):
    """评审 P0-3：A 主机不得替 B 主机回传命令结果。"""
    await bridge._on_status("pod-a", status_frame("pod-a"))
    await bridge._on_status("pod-b", status_frame("pod-b"))
    cid = (await bridge.store.create_commands_batch(["pod-a"], "shell", ["echo"], 30, "admin"))[0]
    await bridge._on_result("pod-b", {"id": cid, "seq": 0, "total": 1, "out_b64": "aGk=",
                                "done": True, "rc": 0})
    row = await bridge.store.get_command(cid)
    assert row["status"] == "pending", "跨主机结果不得改写命令"
    assert await bridge.store.command_output(cid) == ""
    assert (await bridge.store.list_audit())[0]["action"] == "result_mismatch"


async def test_result_unknown_command_dropped(bridge):
    await bridge._on_result("pod-a", {"id": "c-none", "seq": 0, "out_b64": "", "done": True})
    assert (await bridge.store.list_audit())[0]["action"] == "result_unknown_cmd"


async def test_result_appends_and_completes(bridge):
    await bridge._on_status("pod-c", status_frame("pod-c"))
    cid = (await bridge.store.create_commands_batch(["pod-c"], "shell", ["echo"], 30, "admin"))[0]
    await bridge._on_result("pod-c", {"id": cid, "seq": 0, "total": 2,
                                "out_b64": base64.b64encode(b"part1-").decode()})
    assert (await bridge.store.get_command(cid))["status"] == "running"
    await bridge._on_result("pod-c", {"id": cid, "seq": 1, "total": 2, "done": True, "rc": 0,
                                "out_b64": base64.b64encode(b"part2").decode(),
                                "timed_out": False, "elapsed_ms": 7, "truncated": False})
    row = await bridge.store.get_command(cid)
    assert row["status"] == "done" and row["rc"] == 0
    assert await bridge.store.command_output(cid) == "part1-part2"


# ---- 下行命令帧 ----

async def test_dispatch_shell_payload(bridge):
    await bridge._on_status("pod-d", status_frame("pod-d"))
    cid = (await bridge.store.create_commands_batch(["pod-d"], "shell", ["du", "-sh", "/"], 30, "a"))[0]
    assert bridge.dispatch_command(await bridge.store.get_command(cid)) is True
    msg = bridge.cli.msgs[-1]
    assert msg["topic"] == "kk/v1/pod-d/cmd" and msg["qos"] == 1
    body = json.loads(msg["payload"])
    assert body["kind"] == "shell" and body["argv"] == ["du", "-sh", "/"]
    assert "items" not in body


async def test_dispatch_collect_payload_carries_items(bridge):
    """R4：collect 命令必须把 items 带给 Agent，否则采集通道形同虚设。"""
    await bridge._on_status("pod-e", status_frame("pod-e"))
    cid = (await bridge.store.create_commands_batch(
        ["pod-e"], "collect", {"items": ["cpu", "net"]}, 30, "a"))[0]
    bridge.dispatch_command(await bridge.store.get_command(cid))
    body = json.loads(bridge.cli.msgs[-1]["payload"])
    assert body["kind"] == "collect" and body["items"] == ["cpu", "net"]
    assert "argv" not in body, "collect 不该带 argv"


async def test_dispatch_carries_use_shell(bridge):
    await bridge._on_status("pod-f", status_frame("pod-f"))
    cid = (await bridge.store.create_commands_batch(
        ["pod-f"], "shell", {"argv": ["ls | wc -l"], "use_shell": True}, 30, "a"))[0]
    bridge.dispatch_command(await bridge.store.get_command(cid))
    body = json.loads(bridge.cli.msgs[-1]["payload"])
    assert body["use_shell"] is True and body["argv"] == ["ls | wc -l"]


async def test_dispatch_reports_queued_when_disconnected(bridge):
    """未连上 Broker 时 paho 会入队（rc=NO_CONN），这算已尽责，不能判失败。"""
    bridge.cli.rc = 4  # MQTT_ERR_NO_CONN
    await bridge._on_status("pod-g", status_frame("pod-g"))
    cid = (await bridge.store.create_commands_batch(["pod-g"], "shell", ["echo"], 30, "a"))[0]
    assert bridge.dispatch_command(await bridge.store.get_command(cid)) is True


async def test_dispatch_fails_on_queue_overflow(bridge):
    bridge.cli.rc = 15  # MQTT_ERR_QUEUE_SIZE
    await bridge._on_status("pod-h", status_frame("pod-h"))
    cid = (await bridge.store.create_commands_batch(["pod-h"], "shell", ["echo"], 30, "a"))[0]
    assert bridge.dispatch_command(await bridge.store.get_command(cid)) is False


# ---- 周期收敛 ----

async def test_sweep_converges_stuck_commands(bridge):
    import time as _t
    await bridge._on_status("pod-i", status_frame("pod-i"))
    cid = (await bridge.store.create_commands_batch(["pod-i"], "shell", ["echo"], 30, "a"))[0]
    await bridge.store.mark_sent(cid)
    # 人为把时间推到超时之后
    await bridge.store.exec_sql(
        "UPDATE kk_commands SET sent_at=:a WHERE id=:b",
        {"a": int(_t.time()) - 600, "b": cid})
    n, _ = await bridge.sweep()
    assert n >= 1
    row = await bridge.store.get_command(cid)
    assert row["status"] == "timeout" and row["finished_at"], "超时命令必须盖章可被回收"


async def test_sweep_marks_zombie_hosts_offline(bridge):
    await bridge._on_status("pod-j", status_frame("pod-j"))
    await bridge.store.exec_sql(
        "UPDATE kk_containers SET status_ts=:a WHERE pod=:b", {"a": 1000, "b": "pod-j"})
    _, stale = await bridge.sweep()
    assert stale >= 1 and await bridge.store.is_online("pod-j") is False
