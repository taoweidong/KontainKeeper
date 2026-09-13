"""D2 受控批量升级：模式开关、人工触发端点与离线补投。

D2.1 默认 `manual`（自动推关闭）、`/agent/latest` 响应带 `policy` 自描述；
D2.2 选机升级端点（管理员会话），每个跳台账 + 发 QoS1 帧；
D2.3 离线主机 `queued`、重连补投、清零 `created_at` 的计时语义。

500 台规模下三条都是「不能 N+1」的强约束：
- 版本 / 在途台账 / 在线集合各一次查完，按 host 去重后逐台决定；
- 离线主机**只写台账不投帧**——此刻发出去等于让 Broker 缓一份，重连补投时白下 8–12MB。
"""
import json
import types

import httpx
import pytest

from kk_server.config import load_settings, normalize_update_mode
from kk_server.main import create_app
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge

ADMIN, PASS = "admin", "cu-pass"


class FakePublish:
    """paho `Client.publish` 的替身：把每条发出的消息记进 `msgs` 用于断言。"""
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return types.SimpleNamespace(rc=self.rc)


async def _seed(store, pod, ver, online=True):
    """seed 一台主机（不需真上报 status；通过 set_online 设 online=1）。"""
    await store.upsert_container(pod, "img:1", ver, 60)
    if online:
        await store.set_online(pod, True, image="img:1", agent_ver=ver)


@pytest.fixture
async def bridge_app(tmp_path):
    """一组 bridge + 注入 bridge 的 FastAPI app，端点测试用。

    create_app 会基于 KK_MQTT_URL 自己建一个 bridge + store；测试只关心路由 + 我们的
    bridge，所以建完直接关闭原 store 并把 state 指向**我们的**实例（这样 dispatch 走的
    是我们注入的 FakePublish 客户端，每条发出的帧都能在 `bridge.cli.msgs` 里查到）。
    """
    store = Store(str(tmp_path / "cu.db"))
    await store.setup()
    settings = load_settings({
        "KK_DB_PATH": str(tmp_path / "cu.db"),
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_TOPIC_PREFIX": "kk/v1",
        "KK_MQTT_CLIENT_ID": "kk-server",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    bridge = MqttBridge(store, settings, settings.agent_ips, loop=None, proto_ver=3)
    bridge.cli = FakePublish()
    bridge.store = store

    app = create_app({
        "KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "cu.db"),
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    # 替换 create_app 自带的 store/bridge：端点走 `state.bridge.dispatch_upgrade`，
    # 我们必须让它看到注入的 FakePublish
    await app.state.store.close()
    app.state.store = store
    app.state.bridge = bridge

    token = await store.create_session(ADMIN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield type("Ctx", (), {"client": client, "store": store,
                               "bridge": bridge, "app": app, "token": token})
    await store.close()


def _cmd_msgs(bridge, host):
    """桥接上发给该主机的命令帧（按出现顺序）。"""
    return [m for m in bridge.cli.msgs if m["topic"] == f"kk/v1/{host}/cmd"]


# ---- 配置：模式归一 ----

def test_normalize_update_mode_falls_back_to_manual_on_typo():
    """未知/typo 回落 manual 是「安全方向」：写错成 `atuo` 不应悄悄恢复自动全网推。"""
    assert normalize_update_mode("") == "manual"
    assert normalize_update_mode("manual") == "manual"
    assert normalize_update_mode("MANUAL") == "manual"
    assert normalize_update_mode("auto") == "auto"
    assert normalize_update_mode("atuo") == "manual"
    assert normalize_update_mode(" yes ") == "manual"


# ---- D2.1：模式门禁 ----

async def test_update_mode_manual_disables_auto_push(bridge_app):
    """manual 模式：落后的 Agent 上线**不**收到升级推送（默认行为变更）。"""
    await _seed(bridge_app.store, "web-05", "0.0.1", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "99.0.0", "sha256": "ab", "size": 8, "uploaded_at": 0})
    await bridge_app.bridge._on_status(
        "web-05", {"online": True, "host": "web-05", "ip": "10.0.0.5",
                   "proto_ver": 3, "agent_ver": "0.0.1", "image": "img:1",
                   "interval": 60, "reason": "online", "ts": 0})
    assert _cmd_msgs(bridge_app.bridge, "web-05") == [], "manual 下不应自动推"


async def test_update_mode_auto_pushes_upgrade(bridge_app):
    """auto 模式保留旧行为：落后的 Agent 上线即收到推送（D2.1 的对照组）。"""
    bridge_app.bridge.s.update_mode = "auto"
    await _seed(bridge_app.store, "web-05", "0.0.1", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "99.0.0", "sha256": "ab", "size": 8, "uploaded_at": 0})
    await bridge_app.bridge._on_status(
        "web-05", {"online": True, "host": "web-05", "ip": "10.0.0.5",
                   "proto_ver": 3, "agent_ver": "0.0.1", "image": "img:1",
                   "interval": 60, "reason": "online", "ts": 0})
    pushed = _cmd_msgs(bridge_app.bridge, "web-05")
    assert pushed, "auto 模式必须保留旧自动推行为"
    assert json.loads(pushed[-1]["payload"])["version"] == "99.0.0"


async def test_update_mode_manual_hides_poll_path(bridge_app):
    """`/agent/latest` 在 manual 下答「不升级」+ `policy` 字段自描述。

    字段是排障用的：没有它，「返回 available=false」会被误读成端点坏了。
    """
    await bridge_app.store.set_agent_latest(
        {"version": "99.0.0", "sha256": "ab", "size": 8, "uploaded_at": 0})
    r = await bridge_app.client.get("/api/system/agent/latest",
                                    params={"ver": "0.0.1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False and body["policy"] == "manual"


# ---- D2.3：离线排队 + 重连补投 ----

async def test_offline_host_dispatched_as_queued(bridge_app):
    """下发时离线 → 只写 `queued` 台账，**不**投帧（避免重连时白下 8–12MB）。"""
    await _seed(bridge_app.store, "web-off", "0.0.1", online=False)   # 没调 set_online
    latest = {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0}
    await bridge_app.store.set_agent_latest(latest)
    uid = await bridge_app.bridge.dispatch_upgrade("web-off", "0.0.1", latest, online=False)
    assert uid
    assert _cmd_msgs(bridge_app.bridge, "web-off") == [], "离线时不应投帧"
    rows = await bridge_app.store.list_updates()
    assert len(rows) == 1 and rows[0]["status"] == "queued"


async def test_flush_queued_upgrades_republishes_on_reconnect(bridge_app):
    """重连补投：把 `queued` 行重投一次并转 `pending`（刷新计时窗口）。"""
    await _seed(bridge_app.store, "web-1", "0.0.1", online=False)
    latest = {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0}
    await bridge_app.store.set_agent_latest(latest)
    await bridge_app.bridge.dispatch_upgrade("web-1", "0.0.1", latest, online=False)
    bridge_app.cli_msgs_before = list(bridge_app.bridge.cli.msgs)

    # 模拟主机上线（用 flush_queued_upgrades 自身——_on_status 内部就调它）
    n = await bridge_app.bridge.flush_queued_upgrades("web-1", "0.0.1")
    assert n == 1
    pushed = _cmd_msgs(bridge_app.bridge, "web-1")
    assert pushed, "补投必须真发帧"
    assert json.loads(pushed[-1]["payload"])["version"] == "9.9.9"
    assert (await bridge_app.store.list_updates())[0]["status"] == "pending"


async def test_flush_drops_queued_when_target_superseded(bridge_app):
    """期间又上传了更新版本：旧 queued 行**不**补投（让它白下 8–12MB 没意义）。"""
    await _seed(bridge_app.store, "web-1", "0.0.1", online=False)
    await bridge_app.store.set_agent_latest(
        {"version": "0.5.0", "sha256": "x", "size": 1, "uploaded_at": 0})
    await bridge_app.bridge.dispatch_upgrade(
        "web-1", "0.0.1", {"version": "0.5.0", "sha256": "x", "size": 1, "uploaded_at": 0},
        online=False)
    # 中途上传了 0.6.0
    await bridge_app.store.set_agent_latest(
        {"version": "0.6.0", "sha256": "y", "size": 1, "uploaded_at": 0})
    n = await bridge_app.bridge.flush_queued_upgrades("web-1", "0.0.1")
    assert n == 0
    assert _cmd_msgs(bridge_app.bridge, "web-1") == []
    assert (await bridge_app.store.list_updates())[0]["status"] == "queued"


async def test_mark_update_pending_resets_created_at(bridge_app):
    """`mark_update_pending` 必须刷新 created_at：sweep 是按 created_at 算 pending 的 30min。

    否则 3 天前下发的 queued 行在重连补投的瞬间就被 sweep 判 timeout —— 刚投出去
    就被记失败。
    """
    await bridge_app.store.create_update("up-1", "web-1", "0.0.1", "9.9.9", status="queued")
    # 把 created_at 倒回一个很早的时刻，模拟「3 天前下发的 queued」
    old = 100
    await bridge_app.store.exec_sql(
        "UPDATE kk_updates SET created_at = :t WHERE id = :i", {"t": old, "i": "up-1"})
    await bridge_app.store.mark_update_pending("up-1")
    row = (await bridge_app.store.list_updates())[0]
    assert row["status"] == "pending"
    assert row["created_at"] > old, "created_at 必须被刷新到当前时刻"
    assert (row["created_at"] - int(__import__("time").time())) < 5


# ---- D2.2：受控批量升级端点 ----

async def test_upgrade_endpoint_requires_admin(bridge_app):
    saved = bridge_app.client.headers.pop("Authorization")
    try:
        r = await bridge_app.client.post("/api/system/agent/upgrade",
                                         json={"hosts": ["web-1"]})
    finally:
        bridge_app.client.headers["Authorization"] = saved
    assert r.status_code in (401, 403), r.text


async def test_upgrade_no_binary_returns_all_skipped(bridge_app):
    """未上传过任何版本：所有 host 一律 skipped reason=no_binary，不抛异常。"""
    await _seed(bridge_app.store, "web-1", "0.0.1", online=True)
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-1", "web-2"]})
    body = r.json()
    assert body["ok"] and not body["accepted"]
    assert {h["host"] for h in body["skipped"]} == {"web-1", "web-2"}
    assert all(s["reason"] == "no_binary" for s in body["skipped"])


async def test_upgrade_accepts_and_writes_ledger(bridge_app):
    """在线落后主机：建台账 pending + 投 `kind=update` 帧（id=台账主键）。"""
    await _seed(bridge_app.store, "web-1", "0.1.0", online=True)
    await _seed(bridge_app.store, "web-2", "0.1.0", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})

    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-1", "web-2"]})
    body = r.json()
    assert body["ok"] and len(body["accepted"]) == 2 and not body["skipped"]
    assert body["batch_id"].startswith("ug-")
    a0, a1 = body["accepted"]
    assert a0["from_version"] == "0.1.0" and a0["to_version"] == "9.9.9"
    assert a0["ledger_id"].startswith("up-")
    assert a0["queued"] is False

    rows = await bridge_app.store.list_updates()
    assert {r_["id"] for r_ in rows} == {a0["ledger_id"], a1["ledger_id"]}
    assert all(r_["status"] == "pending" for r_ in rows)
    # 帧 id 必须等于台账主键：回执才有地方落（A6.2）
    sent_ids = {json.loads(m["payload"])["id"] for m in bridge_app.bridge.cli.msgs
                if m["topic"].endswith("/cmd")}
    assert sent_ids == {a0["ledger_id"], a1["ledger_id"]}


async def test_upgrade_skips_already_latest(bridge_app):
    """已经最新的主机 → skipped.already_latest，且**不**产生台账行。"""
    await _seed(bridge_app.store, "web-latest", "9.9.9", online=True)
    await _seed(bridge_app.store, "web-old", "0.1.0", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-latest", "web-old"]})
    body = r.json()
    assert len(body["accepted"]) == 1 and body["accepted"][0]["host"] == "web-old"
    skips = {s["host"]: s["reason"] for s in body["skipped"]}
    assert skips == {"web-latest": "already_latest"}


async def test_upgrade_skips_in_flight(bridge_app):
    """该主机已有未终结台账 → skipped.in_flight（避免重复下载 8–12MB）。"""
    await _seed(bridge_app.store, "web-1", "0.1.0", online=True)
    await bridge_app.store.create_update("up-pre-1", "web-1", "0.0.1", "9.9.9",
                                          status="pending")
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-1"]})
    body = r.json()
    assert not body["accepted"] and body["skipped"][0]["reason"] == "in_flight"
    rows = await bridge_app.store.list_updates()
    assert len(rows) == 1, "in_flight 命中时不应再写台账（避免把旧台账覆盖或加新行）"


async def test_upgrade_offline_host_marked_queued(bridge_app):
    """离线主机：受理成功，**queued=true**，**不**投帧（避免 Broker 缓一份再下第二次）。"""
    await _seed(bridge_app.store, "web-off", "0.1.0", online=False)   # 未调 set_online
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-off"]})
    body = r.json()
    assert body["accepted"][0]["queued"] is True
    assert _cmd_msgs(bridge_app.bridge, "web-off") == []
    assert (await bridge_app.store.list_updates())[0]["status"] == "queued"


async def test_upgrade_dedupes_repeated_hosts_in_one_batch(bridge_app):
    """同一批里重复传同一台按一次算（保持传入顺序便于前端逐条对齐）。"""
    await _seed(bridge_app.store, "web-1", "0.1.0", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-1", "web-1", "web-1"]})
    body = r.json()
    assert len(body["accepted"]) == 1
    rows = await bridge_app.store.list_updates()
    assert len(rows) == 1, "去重后只写一条台账"


async def test_upgrade_rejects_bad_version(bridge_app):
    """指定一个不存在的版本号（服务端单槽位）→ 全部 skipped.bad_version。"""
    await _seed(bridge_app.store, "web-1", "0.1.0", online=True)
    await bridge_app.store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["web-1"], "version": "8.8.8"})
    body = r.json()
    assert not body["accepted"]
    assert body["skipped"][0]["reason"] == "bad_version"
