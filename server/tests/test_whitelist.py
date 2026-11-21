"""QR1124 动态 IP 白名单 + 拦截记录回归锁。

覆盖：
- REST CRUD 契约 + AdminUser 鉴权（401/403）+ 422/409 + 变更审计 + 即时刷新快照。
- 动态生效不重启：store 改名单 + refresh_agent_ips 后，桥接闸门放行/拒绝翻转。
- 拦截记录按 IP 聚合：hits 累加、按 IP 60s 节流（回拨 _reject_ts 模拟时间）、
  轮换 IP 建多行、kind 精确推断（status/hb/result）。
- env 种子幂等（删空不回填）+ 保留期清理。
不连真实 Broker（假 publish + 同步派发），落单元层不顶 skipped 门禁。
"""
import asyncio
import json
import time
import types

import httpx
import pytest

from _kk_db import kk_db_env, kk_db_url, pg_reset

from kk_server.config import load_settings
from kk_server.controllers.deps import refresh_agent_ips
from kk_server.main import create_app
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge

ADMIN = "admin"
PASS = "wl-pass"
GOOD_IP = "10.0.0.5"
BAD_IP = "192.0.2.66"


class FakePublish:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload})
        return types.SimpleNamespace(rc=0)


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
        **kk_db_env(tmp_path), "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    await pg_reset(store)
    await store.ensure_admin(ADMIN, PASS)
    token = await store.create_session(ADMIN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield types.SimpleNamespace(client=client, store=store, app=app)
    await store.close()


async def _operator_headers(api):
    await api.client.post("/api/system/users",
                          json={"username": "op", "password": "op-pass-1",
                                "role": "operator"})
    r = await api.client.post("/api/login",
                              json={"username": "op", "password": "op-pass-1"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def _frame(b, suffix, body):
    b._on_message(None, None, types.SimpleNamespace(
        topic="kk/v1/%s/%s" % (body.get("host") or "h", suffix),
        payload=json.dumps(body).encode()))


def _status(host, ip):
    return {"online": True, "host": host, "ip": ip, "proto_ver": 4,
            "agent_ver": "1.0.0", "image": "img:1", "interval": 60,
            "reason": "online", "ts": int(time.time())}


# ---- REST CRUD + 鉴权 + 审计 + 即时刷新 ----

async def test_crud_and_auth(api):
    # 未登录 401
    r = await api.client.get("/api/system/ip-whitelist",
                             headers={"Authorization": ""})
    assert r.status_code == 401
    # operator 403（AdminUser）
    op = await _operator_headers(api)
    r = await api.client.get("/api/system/ip-whitelist", headers=op)
    assert r.status_code == 403

    # 空列表
    r = await api.client.get("/api/system/ip-whitelist")
    assert r.status_code == 200 and r.json()["items"] == []
    # 新增 + 归一化（单 IP→/32）+ 即时刷新快照
    r = await api.client.post("/api/system/ip-whitelist",
                              json={"cidr": "10.0.0.0/24", "remark": "dev"})
    assert r.status_code == 200 and r.json()["cidr"] == "10.0.0.0/24"
    assert [str(n) for n in api.app.state.agent_ips] == ["10.0.0.0/24"]
    r = await api.client.post("/api/system/ip-whitelist",
                              json={"cidr": "192.168.1.5"})
    assert r.json()["cidr"] == "192.168.1.5/32"
    # 非法 422 / 重复 409
    assert (await api.client.post("/api/system/ip-whitelist",
                                  json={"cidr": "not-an-ip"})).status_code == 422
    assert (await api.client.post("/api/system/ip-whitelist",
                                  json={"cidr": "10.0.0.0/24"})).status_code == 409
    # 变更落审计
    audit = await api.store.list_audit(action="agent_ips_update")
    assert len(audit) == 2 and audit[0]["actor"] == ADMIN

    items = (await api.client.get("/api/system/ip-whitelist")).json()["items"]
    rid = items[0]["id"]
    # 停用 → 快照即时收敛
    r = await api.client.put(f"/api/system/ip-whitelist/{rid}",
                             json={"enabled": False})
    assert r.status_code == 200
    assert [str(n) for n in api.app.state.agent_ips] == ["192.168.1.5/32"]
    # 404
    assert (await api.client.put("/api/system/ip-whitelist/9999",
                                 json={"enabled": True})).status_code == 404
    # 删除
    assert (await api.client.delete(f"/api/system/ip-whitelist/{rid}")).status_code == 200
    assert (await api.client.delete(f"/api/system/ip-whitelist/{rid}")).status_code == 404


async def test_rejects_list_and_export(api):
    await api.store.record_ip_reject(BAD_IP, "evil", "hb")
    await api.store.record_ip_reject("192.0.2.77", "evil2", "status")
    r = await api.client.get("/api/ip-rejects")
    assert r.status_code == 200 and r.json()["total"] == 2
    # ip 筛选下推
    r = await api.client.get("/api/ip-rejects", params={"ip": "192.0.2.77"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["ip"] == "192.0.2.77"
    # 导出 CSV（管理员）+ 导出审计
    r = await api.client.get("/api/export/ip-rejects")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert BAD_IP in r.text
    assert await api.store.count_audit(action="export") >= 1
    # operator 403
    op = await _operator_headers(api)
    assert (await api.client.get("/api/ip-rejects", headers=op)).status_code == 403


# ---- 动态生效不重启（桥接闸门翻转）----

async def test_dynamic_enforcement_no_restart(tmp_path):
    store = Store(kk_db_url(tmp_path))
    await store.setup()
    await pg_reset(store)
    settings = load_settings({
        **kk_db_env(tmp_path), "KK_MQTT_URL": "mqtt://b:1883",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    b = MqttBridge(store, settings, [], loop=None, proto_ver=4)
    b.cli = FakePublish()
    b.loop = asyncio.get_running_loop()
    app = types.SimpleNamespace(state=types.SimpleNamespace())

    # 空白名单 = 放行全部（开发语义）
    _frame(b, "status", _status("h-open", BAD_IP))
    await asyncio.sleep(0.05)
    assert await store.is_online("h-open") is True

    # 界面新增规则 + 刷新快照 → 白名单外被拒、内放行，无需重启
    await store.add_ip_rule("10.0.0.0/24")
    await refresh_agent_ips(app, store, b)
    assert b.agent_ips == app.state.agent_ips  # 两处快照收口一致
    _frame(b, "status", _status("h-evil", BAD_IP))
    _frame(b, "status", _status("h-good", GOOD_IP))
    await asyncio.sleep(0.05)
    assert await store.get_container("h-evil") is None
    assert await store.is_online("h-good") is True

    # 停用规则 + 刷新 → 又放行全部
    rules = await store.list_ip_rules()
    await store.update_ip_rule(rules[0]["id"], enabled=False)
    await refresh_agent_ips(app, store, b)
    assert b.agent_ips == []
    _frame(b, "status", _status("h-again", BAD_IP))
    await asyncio.sleep(0.05)
    assert await store.is_online("h-again") is True
    await store.close()


# ---- 拦截记录聚合 + 节流 + kind ----

async def test_reject_aggregation_throttle_and_kind(tmp_path):
    store = Store(kk_db_url(tmp_path))
    await store.setup()
    await pg_reset(store)
    settings = load_settings({
        **kk_db_env(tmp_path), "KK_AGENT_IPS": "10.0.0.0/24",
        "KK_MQTT_URL": "mqtt://b:1883", "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    b = MqttBridge(store, settings, settings.agent_ips, loop=None, proto_ver=4)
    b.cli = FakePublish()
    b.loop = asyncio.get_running_loop()

    # 同 IP 三帧（status/hb/result）60s 内只 record 一次，kind 取首帧
    _frame(b, "status", _status("evil", BAD_IP))
    _frame(b, "hb", {"host": "evil", "ip": BAD_IP, "ts": int(time.time()),
                     "metrics": {"mem_mb": 1}})
    _frame(b, "result", {"host": "evil", "ip": BAD_IP, "id": "c1", "rc": 0})
    await asyncio.sleep(0.05)
    rows = await store.list_ip_rejects()
    assert len(rows) == 1
    assert rows[0]["ip"] == BAD_IP and rows[0]["hits"] == 1
    assert rows[0]["kind"] == "status"  # 首帧 online 键 → status（修原误标 hb）
    assert b.stats["rejected"] == 3     # 计数逐帧，不受节流影响

    # 回拨节流时间窗 → 同 IP 再 record，hits 累加
    b._reject_ts[BAD_IP] = 0
    _frame(b, "hb", {"host": "evil", "ip": BAD_IP, "ts": int(time.time()),
                     "metrics": {"mem_mb": 2}})
    await asyncio.sleep(0.05)
    rows = await store.list_ip_rejects()
    assert rows[0]["hits"] == 2 and rows[0]["kind"] == "hb"

    # 轮换 IP → 新行（按 IP 聚合有界，cleanup 兜底）
    _frame(b, "status", _status("evil2", "192.0.2.99"))
    await asyncio.sleep(0.05)
    assert await store.count_ip_rejects() == 2
    # ip_rejected 审计仍并行保留（全局节流）
    assert await store.count_audit(action="ip_rejected") >= 1
    await store.close()


async def test_reject_kind_result_inference(tmp_path):
    store = Store(kk_db_url(tmp_path))
    await store.setup()
    await pg_reset(store)
    await store.record_ip_reject("192.0.2.1", "h", "result")
    row = (await store.list_ip_rejects())[0]
    assert row["kind"] == "result"
    await store.close()


# ---- env 种子幂等 + 保留清理 ----

async def test_env_seed_idempotent(tmp_path):
    store = Store(kk_db_url(tmp_path))
    await store.setup()
    await pg_reset(store)
    from kk_server.config import parse_ip_whitelist
    nets = parse_ip_whitelist("10.0.0.0/24,192.168.1.5")
    assert await store.seed_ip_rules_from_env(nets) == 2
    # 二次播种不重复（kv 标记）
    assert await store.seed_ip_rules_from_env(nets) == 0
    assert len(await store.list_ip_rules()) == 2
    # 管理员删空后重启不再回填（DB 为真相）
    for r in await store.list_ip_rules():
        await store.delete_ip_rule(r["id"])
    assert await store.seed_ip_rules_from_env(nets) == 0
    assert await store.list_ip_rules() == []
    await store.close()


async def test_reject_cleanup_retention(tmp_path):
    store = Store(kk_db_url(tmp_path))
    await store.setup()
    await pg_reset(store)
    await store.record_ip_reject("192.0.2.1", "h", "hb")
    # 回拨 last_seen 到 31 天前 → 超 30 天保留期被清
    old = int(time.time()) - 31 * 86400
    await store.exec_sql("UPDATE kk_ip_rejects SET last_seen = :t", {"t": old})
    stats = await store.cleanup_ip_rejects()
    assert stats["ip_rejects_deleted"] == 1
    assert await store.count_ip_rejects() == 0
    await store.close()