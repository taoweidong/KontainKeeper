"""REST 接口测试：不依赖 Broker，覆盖桥接之外的一切 HTTP 契约。

集成测试（test_integration）需要真实 Mosquitto，CI 上没有就会整条跳过——
列表摘要、可观测面板这些与 Broker 无关的接口不能跟着一起失去覆盖，
所以这里用 ASGI Transport 直接打 app，只跳过 lifespan（手动建库与造会话）。
"""
import time

import httpx
import pytest

from kk_server.main import create_app

ADMIN = "admin"
PASS = "api-pass"


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "api.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    # 绕过 lifespan：ASGI Transport 不触发启动事件，这里手工补上等价初始化
    token = await store.create_session(ADMIN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield type("Ctx", (), {"client": client, "store": store, "app": app})
    await store.close()


async def _seed(store, pod, cpu, mem, disk_pct, online=True):
    await store.upsert_container(pod, "img:1", "0.3.0", 60)
    if online:
        # 上线走 status 路径（ retained status / LWT 维护 online 列）
        await store.set_online(pod, True, image="img:1", agent_ver="0.3.0")
    await store.record_hb(pod, {"interval": 60, "metrics": {
        "cpu": cpu, "mem_mb": mem, "disks": {"/": {"pct": disk_pct}}}})


async def test_summary_view_excludes_heavy_fields(api):
    await _seed(api.store, "host-a", 12.5, 800.0, 91.0)
    await _seed(api.store, "host-b", 1.0, 200.0, 30.0, online=False)

    r = await api.client.get("/api/containers", params={"view": "summary"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["online"] == 1 and body["alerts"] == 1

    rows = {i["pod"]: i for i in body["items"]}
    a = rows["host-a"]
    assert a["cpu"] == 12.5 and a["mem_mb"] == 800.0 and a["disk_pct"] == 91.0
    assert a["disk_alert"] is True and a["online"] is True
    # 摘要视图的立身之本：完整指标与大字段都不进列表响应
    for forbidden in ("last_metrics", "metrics", "custom"):
        assert forbidden not in a, forbidden
    assert rows["host-b"]["disk_alert"] is False


async def test_full_view_still_returns_metrics(api):
    await _seed(api.store, "host-a", 3.0, 100.0, 10.0)
    r = await api.client.get("/api/containers")
    assert r.status_code == 200
    row = [i for i in r.json()["items"] if i["pod"] == "host-a"][0]
    assert row["metrics"]["cpu"] == 3.0 and row["hb_interval"] == 60


async def test_unknown_view_is_rejected(api):
    r = await api.client.get("/api/containers", params={"view": "nope"})
    assert r.status_code == 400 and "view" in r.json()["detail"]


async def test_stats_endpoint_shape(api):
    await _seed(api.store, "host-a", 3.0, 100.0, 10.0)
    cid = await api.store.create_command("host-a", "shell", ["echo"], 30, ADMIN)
    await api.store.mark_sent(cid)

    r = await api.client.get("/api/system/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["hosts"] == {"total": 1, "online": 1}
    assert body["commands"] == {"sent": 1}
    assert body["storage"]["heartbeats"] == 1
    # 没配 KK_MQTT_URL：broker 段要如实报告未连接，而不是整段消失
    assert body["broker"]["connected"] is False
    assert body["broker"]["stats"] is None
    assert body["uptime_sec"] >= 0


async def test_stats_requires_auth(api):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                               base_url="http://test")
    async with client:
        r = await client.get("/api/system/stats")
        assert r.status_code == 401


async def test_purged_output_is_explained_in_list(api):
    """输出被保留策略清掉后，列表要能看出「不是命令没执行，是输出已清理」。"""
    await _seed(api.store, "host-a", 1.0, 10.0, 10.0)
    cid = await api.store.create_command("host-a", "shell", ["echo"], 30, ADMIN)
    await api.store.append_result({"id": cid, "seq": 0, "total": 1, "done": True,
                                   "rc": 0, "out_b64": "aGVsbG8="})
    r = await api.client.get("/api/commands")
    row = [c for c in r.json()["items"] if c["id"] == cid][0]
    assert row["out_tail"] == "hello" and row["out_purged"] == 0

    now = int(time.time())
    await api.store.exec_sql("UPDATE kk_commands SET finished_at=:a WHERE id=:b",
                             {"a": now - 10 * 86400, "b": cid})
    await api.store.cleanup(now)
    r = await api.client.get("/api/commands")
    row = [c for c in r.json()["items"] if c["id"] == cid][0]
    assert row["status"] == "done" and row["rc"] == 0
    assert row["out_purged"] == 1 and row["out_tail"] == ""


async def test_command_channel_unavailable_without_broker(api):
    """没配 MQTT 时下发命令必须显式 503，而不是静默入库后永远停在 pending。"""
    await _seed(api.store, "host-a", 1.0, 10.0, 10.0)
    r = await api.client.post("/api/commands",
                              json={"pods": ["host-a"], "argv": ["echo", "hi"]})
    assert r.status_code == 503 and "KK_MQTT_URL" in r.json()["detail"]


async def test_login_rate_limit_blocks_brute_force(api):
    """P1-5：连续失败达阈值后临时锁定，正确口令也被拒，直至锁定到期。"""
    from kk_server.controllers import auth as auth_mod
    auth_mod._LOGIN_FAILS.clear()
    auth_mod._LOGIN_LOCKED_UNTIL.clear()
    try:
        await api.store.ensure_admin(ADMIN, PASS)  # 确保账号存在
        # 前 4 次失败 → 401，第 5 次触发锁定 → 429
        codes = []
        for _ in range(5):
            r = await api.client.post("/api/login",
                                      json={"username": ADMIN, "password": "nope"})
            codes.append(r.status_code)
        assert codes[:4] == [401, 401, 401, 401], codes
        assert codes[4] == 429, codes

        # 锁定窗口内即便口令正确也被拒
        r = await api.client.post("/api/login",
                                  json={"username": ADMIN, "password": PASS})
        assert r.status_code == 429

        # 解除锁定后恢复正常
        auth_mod._LOGIN_LOCKED_UNTIL.clear()
        r = await api.client.post("/api/login",
                                  json={"username": ADMIN, "password": PASS})
        assert r.status_code == 200 and "token" in r.json()
    finally:
        auth_mod._LOGIN_FAILS.clear()
        auth_mod._LOGIN_LOCKED_UNTIL.clear()


async def test_logout_writes_audit(api):
    """P2 审计：登出动作须留痕（带用户名与动作 'logout'），便于安全事件追溯。"""
    # api 客户端已带 Bearer（由 fixture 注入）
    r = await api.client.post("/api/logout")
    assert r.status_code == 200
    audits = await api.store.list_audit()
    actions = [(a["actor"], a["action"]) for a in audits]
    assert (ADMIN, "logout") in actions
