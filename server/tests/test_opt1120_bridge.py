"""QR1120-S1..S5（2026-11-20 评估）桥接加固的回归锁。

- S1：审计节流 key 收口为「按 action 全局」。旧 key 带 host（主题任选）/
  ip（帧内自报），匿名 Broker 下攻击者逐帧轮换维度即可绕过节流灌审计库，
  且 _audit_ts 自己成为无界增长点。新语义：同 action 60s 一条审计，
  detail 仍记首条事件的 host/ip，stats 计数逐帧递增不受节流影响。
- S2：入站帧 json.loads 前预检尺寸（1MB），超限丢弃 + oversized_dropped。
- S3：topic host 超过 pod 列宽 String(120) 入口拒收 + host_too_long_dropped，
  不再让 MySQL 1406 穿透成整帧失败。
- S4：KK_MQTT_URL 坏端口抛带配置项名的可读 ValueError，不再是裸 int() 错误。
- S5：Agent 把「良性不更新」（not_newer/backoff）从 rc=1 改为 rc=0 后，
  rc=0 回执落 done 且 reason 进台账、不写 agent_update_failed 审计；
  rc≠0 仍 failed（既有锁 test_bridge.py / test_qr29_remediation.py 不动）。

全部单测无 Broker/网络依赖：conftest 的 bridge 固件 + FakePublish，
节流窗口用直接回拨 _audit_ts 模拟，不依赖真实时钟流逝与执行顺序。
"""
import base64
import json
import time
import types

import pytest

from kk_server.services.mqtt_bridge import (
    AUDIT_THROTTLE, MAX_FRAME_BYTES, MAX_HOST_LEN, MqttBridge)


def _msg(topic, payload):
    return types.SimpleNamespace(topic=topic, payload=payload)


def _json_msg(topic, body):
    return _msg(topic, json.dumps(body).encode("utf-8"))


class LoopStub:
    """替身事件循环：记录 call_soon_threadsafe 的投递而不真跑（同 QR29 手法）。"""

    def __init__(self):
        self.posted = []

    def call_soon_threadsafe(self, cb, *args):
        self.posted.append((cb, args))


def _status(host, proto=3, ip="10.0.0.5"):
    return {"online": True, "host": host, "ip": ip, "proto_ver": proto,
            "agent_ver": "0.3.0", "image": "img:1", "interval": 60,
            "reason": "online", "ts": int(time.time())}


async def _audits(store, action):
    return [a for a in await store.list_audit(limit=200) if a["action"] == action]


# ---- QR1120-S1：节流 key 按 action 全局，轮换 host/ip 不再绕过 ----

async def test_ip_rejected_rotation_does_not_bypass_throttle(bridge):
    """QR1120-S1：旧 key=(ip_rejected, host, ip)，ip 是自报值——逐帧换 ip
    每个新 key 都过节流，审计表被灌。新语义：同 action 60s 只落第一条，
    detail 记首条事件的 host/ip，stats 逐帧递增。"""
    for i in range(5):
        await bridge._on_ip_rejected("evil-%d" % i, {"ip": "192.0.2.%d" % i,
                                                     "online": True})
    rows = await _audits(bridge.store, "ip_rejected")
    assert len(rows) == 1, "轮换 host/ip 不得绕过 action 级节流"
    detail = json.loads(rows[0]["detail"])
    assert detail["host"] == "evil-0" and detail["ip"] == "192.0.2.0", \
        "首条事件的 host/ip 必须留在 detail 里供溯源"


async def test_host_mismatch_rotation_throttled(bridge):
    """QR1120-S1：host 取自主题、发布者任选，旧 key 带 host 即轮换绕过。"""
    for i in range(5):
        await bridge._on_host_mismatch("topic-%d" % i, {"host": "frame-%d" % i})
    rows = await _audits(bridge.store, "host_mismatch")
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["topic_host"] == "topic-0" and detail["frame_host"] == "frame-0"


async def test_hb_unknown_host_rotation_throttled(bridge):
    """QR1120-S1：伪造心跳逐帧换 host 不得灌 hb_unknown_host 审计；
    rejected 计数仍逐帧递增。"""
    for i in range(5):
        await bridge._on_hb("ghost-%d" % i,
                            {"host": "ghost-%d" % i, "ts": 1,
                             "metrics": {"mem_mb": 1.0}})
    assert len(await _audits(bridge.store, "hb_unknown_host")) == 1
    assert bridge.stats["rejected"] == 5
    assert await bridge.store.list_containers() == []


async def test_result_unknown_cmd_rotation_throttled(bridge):
    """QR1120-S1：未知命令回执逐帧换 (host, id) 不得灌审计。"""
    for i in range(5):
        await bridge._on_result("h-%d" % i,
                                {"id": "c-fake-%d" % i, "seq": 0,
                                 "out_b64": "", "done": True})
    rows = await _audits(bridge.store, "result_unknown_cmd")
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"])["host"] == "h-0"
    assert bridge.stats["rejected"] == 5


async def test_update_result_mismatch_rotation_throttled(bridge):
    """QR1120-S1：旧 key=(pod, got_host)，got_host 取自主题可逐帧轮换——
    重放 up- 帧换 host 即绕过 QR-S46 的节流。新语义按 action 全局。"""
    await bridge.store.create_update("up-mx-r", "real-host", "0.1.0", "9.9.9")
    frame = {"id": "up-mx-r", "done": True, "rc": 1, "out_b64": ""}
    for i in range(5):
        await bridge._on_update_result("spoof-%d" % i, "up-mx-r", frame)
    rows = await _audits(bridge.store, "update_result_mismatch")
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["expect"] == "real-host" and detail["got"] == "spoof-0"
    assert bridge.stats["rejected"] == 5


async def test_proto_mismatch_rotation_throttled(bridge):
    """QR1120-S1：窗口外协议帧逐帧换 host 不得灌 proto_mismatch 审计。"""
    for i in range(3):
        await bridge._on_status("old-%d" % i, _status("old-%d" % i, proto=1))
    assert len(await _audits(bridge.store, "proto_mismatch")) == 1
    assert bridge.stats["rejected"] == 3


async def test_throttle_window_expiry_still_allows_next_audit(bridge):
    """QR1120-S1：收口 key 不改变窗口语义——过了 AUDIT_THROTTLE 仍能落下一条。
    直接回拨 _audit_ts 模拟时间流逝，不 sleep 真实时钟。"""
    await bridge._on_ip_rejected("h1", {"ip": "192.0.2.1"})
    bridge._audit_ts[("ip_rejected",)] = int(time.time()) - AUDIT_THROTTLE - 1
    await bridge._on_ip_rejected("h2", {"ip": "192.0.2.2"})
    rows = await _audits(bridge.store, "ip_rejected")
    assert len(rows) == 2
    assert json.loads(rows[0]["detail"])["host"] == "h2", "新窗口记新首条"


async def test_audit_ts_keys_stay_bounded(bridge):
    """QR1120-S1：_audit_ts 的 key 全部是有界常量维度（action 单元素 /
    drop_log 双元素常量），不再含攻击者可随机的 host/ip——dict 不可能被灌大。"""
    for i in range(20):
        await bridge._on_ip_rejected("h%d" % i, {"ip": "192.0.2.%d" % i})
        await bridge._on_host_mismatch("t%d" % i, {"host": "f%d" % i})
    bridge._drop_log("oversized", "x %s", 1)
    bridge._drop_log("host_too_long", "x %s", 1)
    assert len(bridge._audit_ts) <= 4, "20 轮轮换只该留下 action 级 key"
    for key in bridge._audit_ts:
        assert key in {("ip_rejected",), ("host_mismatch",),
                       ("drop_log", "oversized"), ("drop_log", "host_too_long")}


# ---- QR1120-S2：入站帧尺寸上限 ----

async def test_oversized_frame_dropped_before_parse(bridge):
    """QR1120-S2：超过 MAX_FRAME_BYTES 的帧在 json.loads 前丢弃——
    计数 oversized_dropped，不派发、不进解析错误路径。"""
    bridge.loop = LoopStub()
    bridge._on_message(None, None, _msg("kk/v1/h1/hb", b"x" * (MAX_FRAME_BYTES + 1)))
    assert bridge.stats["oversized_dropped"] == 1
    assert bridge.loop.posted == []
    assert bridge.stats["bridge_errors"] == 0


async def test_frame_at_size_limit_still_routes(bridge):
    """QR1120-S2：上限是「>」不是「>=」——恰好 1MB 的合法帧照常受理，
    边界差一会把正常大 result 帧（48KB 分块留余量）误杀。"""
    bridge.loop = LoopStub()
    body = {"host": "h1", "ip": "10.0.0.5", "pad": ""}
    pad = MAX_FRAME_BYTES - len(json.dumps(body).encode("utf-8"))
    body["pad"] = "a" * pad
    payload = json.dumps(body).encode("utf-8")
    assert len(payload) == MAX_FRAME_BYTES
    bridge._on_message(None, None, _msg("kk/v1/h1/hb", payload))
    assert bridge.stats["oversized_dropped"] == 0
    assert len(bridge.loop.posted) == 1


async def test_oversized_drop_log_is_throttled(bridge):
    """QR1120-S2：丢弃痕迹走节流日志而非逐帧审计——持续超大帧攻击
    不得变成持续灌审计库；计数仍逐帧递增。"""
    from loguru import logger as _lu

    records = []
    hid = _lu.add(lambda m: records.append(m), level="WARNING")
    try:
        for _ in range(5):
            bridge._on_message(None, None,
                               _msg("kk/v1/h1/status", b"y" * (MAX_FRAME_BYTES + 1)))
    finally:
        _lu.remove(hid)
    assert bridge.stats["oversized_dropped"] == 5
    assert len(records) == 1, "60s 窗口内只留一条丢弃日志"
    assert await _audits(bridge.store, "oversized") == []


# ---- QR1120-S3：topic host 超长入口拒收 ----

async def test_overlong_topic_host_dropped(bridge):
    """QR1120-S3：host 取自主题，超过 pod 列宽 String(120) 的帧落 MySQL
    会炸 1406 整帧失败——入口拒收 + 计数，不派发任何处理器。"""
    bridge.loop = LoopStub()
    long_host = "h" * (MAX_HOST_LEN + 1)
    bridge._on_message(None, None,
                       _json_msg("kk/v1/%s/status" % long_host, _status(long_host)))
    assert bridge.stats["host_too_long_dropped"] == 1
    assert bridge.loop.posted == []
    assert await bridge.store.list_containers() == []


async def test_host_at_column_width_still_routes(bridge):
    """QR1120-S3：恰好 120 字符（列宽内）照常受理，边界不得差一。"""
    bridge.loop = LoopStub()
    host = "h" * MAX_HOST_LEN
    bridge._on_message(None, None, _json_msg("kk/v1/%s/hb" % host,
                                             {"host": host, "ip": "10.0.0.5"}))
    assert bridge.stats["host_too_long_dropped"] == 0
    assert len(bridge.loop.posted) == 1


async def test_overlong_host_drop_log_is_throttled(bridge):
    """QR1120-S3：超长 host 逐帧轮换不得刷出逐条日志/审计。"""
    from loguru import logger as _lu

    records = []
    hid = _lu.add(lambda m: records.append(m), level="WARNING")
    try:
        for i in range(5):
            host = ("h%d" % i) * MAX_HOST_LEN   # 每帧不同且超长
            bridge._on_message(None, None,
                               _json_msg("kk/v1/%s/hb" % host, {"host": host}))
    finally:
        _lu.remove(hid)
    assert bridge.stats["host_too_long_dropped"] == 5
    assert len(records) == 1


# ---- QR1120-S4：坏端口的可读启动错误 ----

@pytest.mark.parametrize("url", [
    "mqtt://b:abc",
    "mqtts://b:99999",
    "mqtt://b:0",
    "mqtt://[fe80::1]:notaport",
])
async def test_parse_url_bad_port_readable_error(url):
    """QR1120-S4：坏端口必须抛带 KK_MQTT_URL 字样的可读 ValueError，
    而不是裸 int() 的 invalid literal——启动排障要能一眼定位配置项。
    空端口（mqtt://b:）回落默认值是既有合法语义，见下方合法用例。"""
    with pytest.raises(ValueError, match="KK_MQTT_URL"):
        MqttBridge._parse_url(url)


@pytest.mark.parametrize("url,expect", [
    ("mqtt://b:1883", ("b", 1883, False)),
    ("mqtts://b", ("b", 8883, True)),
    ("mqtt://b:", ("b", 1883, False)),
    ("mqtt://u:p@b:1884", ("b", 1884, False)),
    ("mqtt://[fe80::1]:1883", ("fe80::1", 1883, False)),
    ("mqtt://b:65535", ("b", 65535, False)),
])
async def test_parse_url_valid_ports_unchanged(url, expect):
    """QR1120-S4：合法解析路径逐字不变（含缺省端口与 IPv6 字面量）。"""
    assert MqttBridge._parse_url(url) == expect


# ---- QR1120-S5：良性跳过 rc=0 落 done，reason 进台账 ----

def _receipt(cid, rc, reason=b"", done=True):
    return {"id": cid, "seq": 0, "total": 1, "done": done, "rc": rc,
            "out_b64": base64.b64encode(reason).decode()}


async def test_benign_skip_rc0_lands_done_with_reason(bridge):
    """QR1120-S5：Agent 侧把 not_newer/backoff 等良性不更新从 rc=1 改为
    rc=0（reason 保留）。服务端必须落 done、reason 进台账、不写
    agent_update_failed 审计——良性跳过被记 failed 会污染升级失败率。"""
    await bridge.store.create_update("up-benign-1", "h1", "0.3.0", "0.3.0")
    await bridge._on_result("h1", _receipt("up-benign-1", 0, b"not_newer"))
    row = await bridge.store.get_update("up-benign-1")
    assert row["status"] == "done" and row["finished_at"]
    assert row["reason"] == "not_newer", "良性跳过的 reason 必须进台账"
    assert await _audits(bridge.store, "agent_update_failed") == []
    assert bridge.stats["upgrade_done"] == 1
    assert bridge.stats["upgrade_failed"] == 0


async def test_benign_skip_backoff_rc0_also_done(bridge):
    """QR1120-S5：backoff 同型——rc=0 + reason 一律 done，不另立分支。"""
    await bridge.store.create_update("up-benign-2", "h1", "0.3.0", "9.9.9")
    await bridge._on_result("h1", _receipt("up-benign-2", 0, b"backoff"))
    row = await bridge.store.get_update("up-benign-2")
    assert row["status"] == "done" and row["reason"] == "backoff"
    assert await _audits(bridge.store, "agent_update_failed") == []


async def test_rc0_without_reason_still_done(bridge):
    """QR1120-S5：真升级成功的回执 out 可能为空——rc=0 缺 reason 仍落 done，
    reason 留空串（不得因缺 reason 掉进 failed 分支）。"""
    await bridge.store.create_update("up-ok-1", "h1", "0.3.0", "9.9.9")
    await bridge._on_result("h1", _receipt("up-ok-1", 0))
    row = await bridge.store.get_update("up-ok-1")
    assert row["status"] == "done" and row["reason"] == ""
    assert await _audits(bridge.store, "agent_update_failed") == []


async def test_rc_nonzero_still_failed_with_audit(bridge):
    """QR1120-S5 的另一半：rc≠0 仍是真失败——failed + reason + 审计，
    与既有锁（test_bridge.test_result_frame_for_update_not_rejected、
    test_qr29_remediation 的 QR-A19 幂等锁）同一语义，不得被良性跳过改动波及。"""
    await bridge.store.create_update("up-bad-1", "h1", "0.3.0", "9.9.9")
    await bridge._on_result("h1", _receipt("up-bad-1", 1, b"sha256_mismatch"))
    row = await bridge.store.get_update("up-bad-1")
    assert row["status"] == "failed" and row["reason"] == "sha256_mismatch"
    assert len(await _audits(bridge.store, "agent_update_failed")) == 1
    assert bridge.stats["upgrade_failed"] == 1
    assert bridge.stats["upgrade_done"] == 0


async def test_benign_skip_terminal_idempotent(bridge):
    """QR1120-S5 × QR-A19：rc=0 终态帧被 QoS1 重投不得翻倍 upgrade_done。"""
    await bridge.store.create_update("up-benign-3", "h1", "0.3.0", "0.3.0")
    frame = _receipt("up-benign-3", 0, b"not_newer")
    for _ in range(3):
        await bridge._on_result("h1", frame)
    assert bridge.stats["upgrade_done"] == 1
    assert (await bridge.store.get_update("up-benign-3"))["status"] == "done"
