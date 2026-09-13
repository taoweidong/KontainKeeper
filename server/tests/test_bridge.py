"""MQTT 桥接单测：不连真实 Broker，用假 publish 与同步派发钉住路由与校验。

覆盖原 test_hub.py 关心、但改由 Broker 承担后仍需服务端把关的部分：
归属校验、IP 白名单/协议闸门、命令帧字段还原、URL 解析、主题解析。
"""
import base64
import json
import time
import types

import pytest

from kk_server.config import load_settings
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge

# v3 接入管控：Agent 自报 ip 按此白名单校验（10.0.0.0/24 网段 + 单点 IPv6）
WHITELIST = "10.0.0.0/24,fd00::1/128"
GOOD_IP = "10.0.0.5"
BAD_IP = "192.0.2.66"


class FakePublish:
    """paho `Client.publish` 的替身：把每条发出的消息记进 `msgs` 用于断言。"""
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return types.SimpleNamespace(rc=self.rc)


@pytest.fixture
async def wbridge(tmp_path):
    """配了 KK_AGENT_IPS 白名单的桥：白名单闸门（_on_message 入口）的回归锁。"""
    store = Store(str(tmp_path / "wb.db"))
    await store.setup()
    settings = load_settings({
        "KK_DB_PATH": str(tmp_path / "wb.db"),
        "KK_AGENT_IPS": WHITELIST,
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_TOPIC_PREFIX": "kk/v1",
        "KK_MQTT_CLIENT_ID": "kk-server",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    b = MqttBridge(store, settings, settings.agent_ips, loop=None, proto_ver=3)
    b.cli = FakePublish()
    b.store = store
    yield b
    await store.close()


def status_frame(host, online=True, ip=GOOD_IP, proto=3, ver="0.3.0"):
    return {"online": online, "host": host, "ip": ip, "proto_ver": proto,
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


# ---- status：在线真相 ----

async def test_status_registers_host_online(bridge):
    await bridge._on_status("web-01", status_frame("web-01"))
    assert await bridge.store.is_online("web-01") is True
    row = await bridge.store.get_container("web-01")
    assert row["image"] == "img:1" and row["agent_ver"] == "0.3.0"


async def test_status_proto_mismatch_ignored(bridge):
    await bridge._on_status("web-03", status_frame("web-03", proto=1))
    assert await bridge.store.get_container("web-03") is None
    assert (await bridge.store.list_audit())[0]["action"] == "proto_mismatch"


# ---- v3 白名单闸门（_on_message 入口，三类上行帧统一拦截）----

def _frame(wb, suffix, body):
    wb._on_message(None, None, types.SimpleNamespace(
        topic="kk/v1/%s/%s" % (body.get("host") or "h", suffix),
        payload=json.dumps(body).encode()))


async def test_status_from_whitelisted_ip_passes(wbridge):
    import asyncio
    wbridge.loop = asyncio.get_running_loop()
    _frame(wbridge, "status", status_frame("web-01"))
    await asyncio.sleep(0.05)
    assert await wbridge.store.is_online("web-01") is True


async def test_status_ipv6_whitelist_entry(wbridge):
    import asyncio
    wbridge.loop = asyncio.get_running_loop()
    _frame(wbridge, "status", status_frame("web-v6", ip="fd00::1"))
    await asyncio.sleep(0.05)
    assert await wbridge.store.is_online("web-v6") is True


async def test_status_from_non_whitelisted_ip_rejected(wbridge):
    """白名单外的上报：主机不注册、计数拒收、审计 ip_rejected。"""
    import asyncio
    wbridge.loop = asyncio.get_running_loop()
    _frame(wbridge, "status", status_frame("evil", ip=BAD_IP))
    await asyncio.sleep(0.05)
    assert await wbridge.store.get_container("evil") is None
    assert wbridge.stats["rejected"] == 1
    audit = (await wbridge.store.list_audit())[0]
    assert audit["action"] == "ip_rejected"


async def test_hb_from_non_whitelisted_ip_rejected(wbridge):
    """闸门在 _on_message 入口：已注册主机的白名单外心跳同样拦下。"""
    import asyncio
    wbridge.loop = asyncio.get_running_loop()
    await wbridge._on_status("web-07", status_frame("web-07"))
    _frame(wbridge, "hb", {"host": "web-07", "ip": BAD_IP, "ts": int(time.time()),
                           "metrics": {"mem_mb": 1.0}})
    await asyncio.sleep(0.05)
    assert wbridge.stats["hb"] == 0, "白名单外心跳不得入库"
    assert (await wbridge.store.list_audit())[0]["action"] == "ip_rejected"


async def test_missing_or_bogus_ip_rejected_when_whitelist_set(wbridge):
    """白名单配置后，缺 ip / 格式非法 ip 的帧一律拒绝，不能只靠「有字段」放行。"""
    import asyncio
    wbridge.loop = asyncio.get_running_loop()
    for bad in ("", "not-an-ip", None):
        _frame(wbridge, "status", status_frame("bogus", ip=bad))
    await asyncio.sleep(0.05)
    assert await wbridge.store.get_container("bogus") is None
    assert wbridge.stats["rejected"] == 3


async def test_no_whitelist_means_no_restriction(bridge):
    """未配置白名单（空）= 放行全部：开发/测试模式不加门槛。"""
    import asyncio
    bridge.loop = asyncio.get_running_loop()
    _frame(bridge, "status", status_frame("any-host", ip=BAD_IP))
    await asyncio.sleep(0.05)
    assert await bridge.store.is_online("any-host") is True


async def test_lwt_offline_marks_offline(bridge):
    await bridge._on_status("web-04", status_frame("web-04"))
    assert await bridge.store.is_online("web-04") is True
    await bridge._on_status("web-04", status_frame("web-04", online=False))
    assert await bridge.store.is_online("web-04") is False
    assert await bridge.store.online_count() == 0


async def test_status_pushes_upgrade_when_agent_behind(bridge):
    # 旧测试默认在 auto 模式触发推送（D2.1 之后默认 manual 关闭推送）
    bridge.s.update_mode = "auto"
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
    cid = (await bridge.store.create_commands_batch(["pod-a"], "shell", ["echo"], 30, "admin"))[0][0]
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
    cid = (await bridge.store.create_commands_batch(["pod-c"], "shell", ["echo"], 30, "admin"))[0][0]
    await bridge._on_result("pod-c", {"id": cid, "seq": 0, "total": 2,
                                "out_b64": base64.b64encode(b"part1-").decode()})
    assert (await bridge.store.get_command(cid))["status"] == "running"
    await bridge._on_result("pod-c", {"id": cid, "seq": 1, "total": 2, "done": True, "rc": 0,
                                "out_b64": base64.b64encode(b"part2").decode(),
                                "timed_out": False, "elapsed_ms": 7, "truncated": False})
    row = await bridge.store.get_command(cid)
    assert row["status"] == "done" and row["rc"] == 0
    assert await bridge.store.command_output(cid) == "part1-part2"


async def test_result_chunks_concurrent_no_data_loss(bridge):
    """回归：分块帧并发处理时不得丢中间块（水位去重的乱序防线）。

    _spawn 用 create_task 并发调度各 result 帧，帧内首个 await 让出后
    DB 完成顺序与到达顺序不一致；首帧被人为放慢时，无串行锁的实现
    会让后到的 done 帧先落库（last_seq 跳到末块），中间块全被
    「严格递增水位」丢弃——E2E 实测 200KB 输出 5 块只落 2-4 块。
    """
    import asyncio

    await bridge._on_status("pod-e", status_frame("pod-e"))
    cid = (await bridge.store.create_commands_batch(["pod-e"], "shell", ["cat", "big"], 30, "admin"))[0][0]
    data = b"x" * 204800                       # 5 块：4×48KB + 8KB
    chunk = 48 * 1024
    frames = []
    for i in range(0, len(data), chunk):
        last = i + chunk >= len(data)
        frames.append({"id": cid, "seq": len(frames), "total": 5,
                       "out_b64": base64.b64encode(data[i:i + chunk]).decode(),
                       "done": last, **({"rc": 0, "timed_out": False,
                                         "elapsed_ms": 9, "truncated": False} if last else {})})
    orig = bridge.store.append_result

    async def slow_first(msg, host=None):
        if msg.get("seq") == 0:
            await asyncio.sleep(0.05)          # 模拟首帧 DB 慢，放大并发交错
        return await orig(msg, host=host)

    bridge.store.append_result = slow_first
    # 模拟 _spawn 的 create_task 并发：按到达顺序同时调度全部分块帧
    await asyncio.gather(*[bridge._on_result("pod-e", f) for f in frames])
    row = await bridge.store.get_command(cid)
    assert row["status"] == "done" and row["out_chunks"] == 5
    out = base64.b64decode(row["out_b64"])
    assert len(out) == len(data) and out == data


# ---- 下行命令帧 ----

async def test_dispatch_shell_payload(bridge):
    await bridge._on_status("pod-d", status_frame("pod-d"))
    cid = (await bridge.store.create_commands_batch(["pod-d"], "shell", ["du", "-sh", "/"], 30, "a"))[0][0]
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
        ["pod-e"], "collect", {"items": ["cpu", "net"]}, 30, "a"))[0][0]
    bridge.dispatch_command(await bridge.store.get_command(cid))
    body = json.loads(bridge.cli.msgs[-1]["payload"])
    assert body["kind"] == "collect" and body["items"] == ["cpu", "net"]
    assert "argv" not in body, "collect 不该带 argv"


async def test_dispatch_carries_use_shell(bridge):
    await bridge._on_status("pod-f", status_frame("pod-f"))
    cid = (await bridge.store.create_commands_batch(
        ["pod-f"], "shell", {"argv": ["ls | wc -l"], "use_shell": True}, 30, "a"))[0][0]
    bridge.dispatch_command(await bridge.store.get_command(cid))
    body = json.loads(bridge.cli.msgs[-1]["payload"])
    assert body["use_shell"] is True and body["argv"] == ["ls | wc -l"]


async def test_dispatch_reports_queued_when_disconnected(bridge):
    """未连上 Broker 时 paho 会入队（rc=NO_CONN），这算已尽责，不能判失败。"""
    bridge.cli.rc = 4  # MQTT_ERR_NO_CONN
    await bridge._on_status("pod-g", status_frame("pod-g"))
    cid = (await bridge.store.create_commands_batch(["pod-g"], "shell", ["echo"], 30, "a"))[0][0]
    assert bridge.dispatch_command(await bridge.store.get_command(cid)) is True


async def test_dispatch_fails_on_queue_overflow(bridge):
    bridge.cli.rc = 15  # MQTT_ERR_QUEUE_SIZE
    await bridge._on_status("pod-h", status_frame("pod-h"))
    cid = (await bridge.store.create_commands_batch(["pod-h"], "shell", ["echo"], 30, "a"))[0][0]
    assert bridge.dispatch_command(await bridge.store.get_command(cid)) is False


# ---- 周期收敛 ----

async def test_sweep_converges_stuck_commands(bridge):
    import time as _t
    await bridge._on_status("pod-i", status_frame("pod-i"))
    cid = (await bridge.store.create_commands_batch(["pod-i"], "shell", ["echo"], 30, "a"))[0][0]
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


# ---- KK_AGENT_IPS 解析与 production 自检 ----

def test_parse_ip_whitelist_mixed_formats():
    from kk_server.config import parse_ip_whitelist
    nets = parse_ip_whitelist("10.0.0.0/24, 192.168.1.5 ,fd00::1/128, 172.16.0.7")
    assert len(nets) == 4
    assert any("10.0.0.0/24" in str(n) for n in nets)
    # 单个 IP 自动按主机位全 1 处理（strict=False）
    assert any("192.168.1.5/32" in str(n) for n in nets)
    assert any("172.16.0.7/32" in str(n) for n in nets)


def test_parse_ip_whitelist_empty_is_empty():
    from kk_server.config import parse_ip_whitelist
    assert parse_ip_whitelist("") == []
    assert parse_ip_whitelist(" , ") == []


def test_parse_ip_whitelist_rejects_garbage():
    """白名单写错时启动即失败，好过静默放行或全拒。"""
    from kk_server.config import parse_ip_whitelist
    with pytest.raises(ValueError):
        parse_ip_whitelist("10.0.0.0/24,not-an-ip")


def test_production_requires_whitelist():
    """KK_ENV=production 必须显式配置 KK_AGENT_IPS：不配等于对所有上报放行。"""
    with pytest.raises(RuntimeError, match="KK_AGENT_IPS"):
        load_settings({"KK_ENV": "production",
                       "KK_ADMIN_PASS": "x" * 20,
                       "KK_WEB_DIR": "/tmp/noweb"})
    # 配了就能过
    s = load_settings({"KK_ENV": "production", "KK_AGENT_IPS": "10.0.0.0/24",
                       "KK_ADMIN_PASS": "x" * 20,
                       "KK_WEB_DIR": "/tmp/noweb"})
    assert len(s.agent_ips) == 1


# ---- A4：上报间隔下限检测（P1-4）----

def hb_frame(host, interval, ip=GOOD_IP):
    return {"host": host, "ip": ip, "proto_ver": 3, "agent_ver": "0.3.0",
            "interval": interval, "ts": int(time.time()),
            "metrics": {"cpu": 1.0, "mem_mb": 10.0}}


async def _mk_bridge(tmp_path, env_extra=None):
    store = Store(str(tmp_path / "iv.db"))
    await store.setup()
    env = {
        "KK_DB_PATH": str(tmp_path / "iv.db"),
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    }
    env.update(env_extra or {})
    settings = load_settings(env)
    b = MqttBridge(store, settings, settings.agent_ips, loop=None, proto_ver=3)
    b.cli = FakePublish()
    return b, store


async def test_interval_violation_audited(tmp_path):
    """低于下限 → 审计 + stats 计数，但**照常落库**（检测不阻断，避免丢指标）。"""
    b, store = await _mk_bridge(tmp_path, {"KK_INTERVAL_MIN": "10"})
    await b._on_status("web-01", status_frame("web-01"))
    await b._on_hb("web-01", hb_frame("web-01", 1))

    rows = await store.metrics_series("web-01", hours=24)
    assert len(rows[0]) == 1, "违规心跳也必须落库"
    audit = await store.list_audit(limit=10)
    hit = [a for a in audit if a["action"] == "interval_violation"]
    assert hit and hit[0]["detail"]
    assert b.stats["interval_violation"] == 1


async def test_interval_within_limit_no_audit(tmp_path):
    """等于或高于下限不告警：阈值边界不能差一。"""
    b, store = await _mk_bridge(tmp_path, {"KK_INTERVAL_MIN": "10"})
    await b._on_status("web-02", status_frame("web-02"))
    await b._on_hb("web-02", hb_frame("web-02", 10))
    await b._on_hb("web-02", hb_frame("web-02", 60))
    assert b.stats["interval_violation"] == 0
    assert not [a for a in await store.list_audit(limit=10)
                if a["action"] == "interval_violation"]


async def test_interval_check_disabled_when_unset(tmp_path):
    """KK_INTERVAL_MIN 未配 = 不检查（原死配置的默认行为保持不变）。"""
    b, store = await _mk_bridge(tmp_path)
    assert b.s.interval_min is None
    await b._on_status("web-03", status_frame("web-03"))
    await b._on_hb("web-03", hb_frame("web-03", 1))
    assert b.stats["interval_violation"] == 0
    assert not [a for a in await store.list_audit(limit=10)
                if a["action"] == "interval_violation"]
    await store.close()


# ---- A6.2：自更新台账与结果回执 ----

import base64 as _b64


def _result_frame(cid, done=True, rc=0, out=b""):
    return {"id": cid, "seq": 0, "total": 1, "done": done, "rc": rc,
            "out_b64": _b64.b64encode(out).decode()}


async def test_update_receipt_marks_ledger_done(bridge):
    """Agent 回执 rc=0 → 台账置 done，不再停在 pending。"""
    # 旧测试直接调 _maybe_push_upgrade；该方法在 manual 模式已不动作，
    # 这里显式置 auto 并改走 dispatch_upgrade（_maybe_push_upgrade 仍走它）。
    bridge.s.update_mode = "auto"
    await bridge._on_status("up-1", status_frame("up-1", ver="0.1.0"))
    await bridge.store.set_agent_latest({"version": "9.9.9", "sha256": "s", "size": 1})
    await bridge._maybe_push_upgrade("up-1", "0.1.0")

    rows = await bridge.store.list_updates()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    uid = rows[0]["id"]
    assert uid.startswith("up-")
    # 帧 id 必须与台账主键一致，否则回执无处可落
    assert json.loads(bridge.cli.msgs[-1]["payload"])["id"] == uid

    await bridge._on_result("up-1", _result_frame(uid, rc=0))
    row = await bridge.store.get_update(uid)
    assert row["status"] == "done" and row["finished_at"]


async def test_result_frame_for_update_not_rejected(bridge):
    """up- 前缀结果帧不落入 result_unknown_cmd 审计，而是更新台账。"""
    await bridge._on_status("up-2", status_frame("up-2", ver="0.1.0"))
    uid = "up-up-2-1"
    await bridge.store.create_update(uid, "up-2", "0.1.0", "9.9.9")
    await bridge._on_result("up-2", _result_frame(uid, rc=1, out=b"sha256_mismatch"))

    row = await bridge.store.get_update(uid)
    assert row["status"] == "failed" and row["reason"] == "sha256_mismatch"
    audit = await bridge.store.list_audit(limit=20)
    assert not [a for a in audit if a["action"] == "result_unknown_cmd"], audit
    assert [a for a in audit if a["action"] == "agent_update_failed"]


async def test_updates_ledger_done_by_status_evidence(bridge):
    """回执帧可能来不及发出：主机上报新 agent_ver 时台账补成 done。"""
    uid = "up-up-3-1"
    await bridge.store.upsert_container("up-3", "img", "0.1.0", 60)
    await bridge.store.create_update(uid, "up-3", "0.1.0", "0.3.0")
    await bridge._on_status("up-3", status_frame("up-3", ver="0.3.0"))
    assert (await bridge.store.get_update(uid))["status"] == "done"

    # 仍然落后的主机不得被误判为已升级
    uid2 = "up-up-3-2"
    await bridge.store.create_update(uid2, "up-3", "0.1.0", "0.9.0")
    await bridge._on_status("up-3", status_frame("up-3", ver="0.3.0"))
    assert (await bridge.store.get_update(uid2))["status"] == "pending"


async def test_updates_ledger_swept_timeout(bridge):
    """在途超过 30min 未终结 → timeout（可见的降级，不是静默卡住）。"""
    uid = "up-up-4-1"
    await bridge.store.create_update(uid, "up-4", "0.1.0", "9.9.9")
    await bridge.store.exec_sql("UPDATE kk_updates SET created_at=:t WHERE id=:i",
                                {"t": int(time.time()) - 3600, "i": uid})
    n = await bridge.store.sweep_update_timeouts()
    assert n == 1
    assert (await bridge.store.get_update(uid))["status"] == "timeout"


async def test_queued_upgrade_not_swept_by_pending_ttl(bridge):
    """queued（离线排队）行受 7d 阈值约束，不被 30min 的 pending 阈值误伤。"""
    uid = "up-up-5-1"
    await bridge.store.create_update(uid, "up-5", "0.1.0", "9.9.9", status="queued")
    await bridge.store.exec_sql("UPDATE kk_updates SET created_at=:t WHERE id=:i",
                                {"t": int(time.time()) - 3600, "i": uid})
    assert await bridge.store.sweep_update_timeouts() == 0
    assert (await bridge.store.get_update(uid))["status"] == "queued"


async def test_in_flight_update_dedupe(bridge):
    """有未终结台账 → in_flight_update 能查到（D2.3 去重的依据）。"""
    await bridge.store.create_update("up-up-6-1", "up-6", "0.1.0", "9.9.9")
    row = await bridge.store.in_flight_update("up-6")
    assert row and row["id"] == "up-up-6-1"
    assert await bridge.store.in_flight_update("up-404") is None
