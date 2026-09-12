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


async def test_login_lock_by_ip(api, monkeypatch):
    """B3 / P2-3：同一 IP 换着用户名试，也必须在 5 次内被锁。

    只按用户名计数时，攻击者拿一个 IP 遍历「admin / root / ops …」，每个名字
    各错一次，任何一个都到不了 5 次 —— 用户名枚举这条路等于不设防。
    """
    from kk_server.controllers import auth as auth_mod

    src = {"ip": "203.0.113.7"}
    monkeypatch.setattr(auth_mod, "_client_ip", lambda request: src["ip"])
    auth_mod._LOGIN_FAILS.clear()
    auth_mod._LOGIN_LOCKED_UNTIL.clear()
    try:
        await api.store.ensure_admin(ADMIN, PASS)
        await api.store.ensure_admin("ops", "ops-pass")

        # 4 个不同用户名各错一次：用户名维度一次都没到 5，IP 维度累加到 4
        for i in range(4):
            r = await api.client.post("/api/login",
                                      json={"username": "u%d" % i, "password": "nope"})
            assert r.status_code == 401, r.text

        # 第 5 次仍错（换回真实用户名）→ IP 维度达阈值，直接 429
        r = await api.client.post("/api/login",
                                  json={"username": ADMIN, "password": "nope"})
        assert r.status_code == 429, r.text

        # 换一个来源 IP 且换一个未被锁的用户名：不应被上一个 IP 连坐
        src["ip"] = "198.51.100.9"
        r = await api.client.post("/api/login",
                                  json={"username": "ops", "password": "ops-pass"})
        assert r.status_code == 200 and "token" in r.json(), r.text
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


# ---- A2：批量下发回路去 N+1 ----

class _FakeBridge:
    """只记录入参的假桥接：用来断言发布所需的字段全都由调用方就地提供。"""

    def __init__(self):
        self.dispatched = []
        self.stats = {"cmd_published": 0, "cmd_failed": 0}

    def dispatch_command(self, row):
        self.dispatched.append(dict(row))
        return True


async def test_create_commands_batch_dispatch_without_lookup(api):
    """P1-1：500 台一次点击，发布回路不得有任何 get_command 回查。"""
    store = api.store
    pods = ["h-%03d" % i for i in range(500)]
    for p in pods:
        await store.upsert_container(p, "img", "0.3.0", 60)
    bridge = _FakeBridge()
    api.app.state.bridge = bridge

    calls = {"get_command": 0}
    orig = store.get_command

    async def counting(cid):
        calls["get_command"] += 1
        return await orig(cid)

    store.get_command = counting
    r = await api.client.post("/api/commands", json={
        "pods": pods, "kind": "shell", "argv": ["echo", "hi"], "timeout": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 500
    assert body["batch_id"].startswith("b-")
    assert calls["get_command"] == 0, "发布回路不得回查数据库"
    # 就地组装的行必须带齐桥接要的五个字段
    assert len(bridge.dispatched) == 500
    assert {c["pod"] for c in bridge.dispatched} == set(pods)
    assert all(c["kind"] == "shell" and c["timeout"] == 30 for c in bridge.dispatched)
    # mark_sent_batch 生效：全部为 sent，且只走批量 UPDATE
    rows = await store.list_commands(batch=body["batch_id"], limit=500)
    assert len(rows) == 500 and all(r["status"] == "sent" for r in rows)


async def test_mark_sent_batch_shards_and_ignores_non_pending(api):
    """批量置 sent 走 IN（分片），且只翻 pending 行——done 行不得被拉回 sent。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids, _ = await store.create_commands_batch(["h1"] * 3, "shell", ["echo"], 30, "admin")
    await store.append_result({"id": ids[0], "done": True, "rc": 0})
    n = await store.mark_sent_batch(ids)
    assert n == 2
    assert (await store.get_command(ids[0]))["status"] == "done"
    assert (await store.get_command(ids[1]))["status"] == "sent"
    assert await store.mark_sent_batch([]) == 0


# ---- A3：结果分页 + 批次聚合 ----

async def test_list_commands_pagination(api):
    """P1-2：offset/limit 生效，total 是全量而非当页条数。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids, _ = await store.create_commands_batch(["h1"] * 5, "shell", ["echo"], 30, "admin")
    # created_at 相同（同一秒建），按 id 侧的稳定顺序断言：翻页总数为 5 且两页不重叠
    p1 = (await api.client.get("/api/commands", params={"limit": 2, "offset": 0})).json()
    p2 = (await api.client.get("/api/commands", params={"limit": 2, "offset": 2})).json()
    assert p1["total"] == 5 and p1["offset"] == 0 and p1["limit"] == 2
    assert len(p1["items"]) == 2 and len(p2["items"]) == 2
    assert {i["id"] for i in p1["items"]}.isdisjoint({i["id"] for i in p2["items"]})
    assert {i["id"] for i in p1["items"] + p2["items"]} <= set(ids)


async def test_list_commands_batch_filter(api):
    """两个批次各自筛选互不污染（P1-3 的前提）。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids1, b1 = await store.create_commands_batch(["h1"] * 2, "shell", ["echo"], 30, "admin")
    ids2, b2 = await store.create_commands_batch(["h1"] * 3, "shell", ["ls"], 30, "admin")
    assert b1 != b2
    r1 = (await api.client.get("/api/commands", params={"batch": b1})).json()
    r2 = (await api.client.get("/api/commands", params={"batch": b2})).json()
    assert r1["total"] == 2 and {i["id"] for i in r1["items"]} == set(ids1)
    assert r2["total"] == 3 and {i["id"] for i in r2["items"]} == set(ids2)


async def test_batch_id_assigned_to_all_rows(api):
    """一次批量 = 一个批次号，全部行共享（500 台聚合核验的唯一抓手）。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids, batch = await store.create_commands_batch(["h1"] * 3, "shell", ["echo"], 30, "admin")
    assert batch.startswith("b-") and len(ids) == 3
    rows = await store.list_commands(batch=batch, limit=10)
    assert len(rows) == 3 and {r["batch_id"] for r in rows} == {batch}


async def test_batch_summary_groups_by_status(api):
    """批次汇总按状态分布计数：让一次下发可以整体核验。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids, batch = await store.create_commands_batch(["h1"] * 4, "shell", ["echo"], 30, "admin")
    for cid in ids[:2]:
        await store.append_result({"id": cid, "done": True, "rc": 0})
    await store.append_result({"id": ids[2], "done": True, "rc": 1})
    r = await api.client.get("/api/commands/batches")
    assert r.status_code == 200, r.text
    hit = [b for b in r.json()["items"] if b["batch_id"] == batch]
    assert hit, r.json()
    # status 表示「结果是否收全」，退出码看 rc：rc=1 的命令状态仍是 done
    assert hit[0]["total"] == 4 and hit[0].get("done") == 3
    assert hit[0].get("pending") == 1


async def test_list_commands_keyword_pushed_to_backend(api):
    """关键字必须下推：否则导出与页面所见不一致。"""
    store = api.store
    await store.upsert_container("alpha-01", "img", "0.3.0", 60)
    await store.upsert_container("beta-02", "img", "0.3.0", 60)
    await store.create_commands_batch(["alpha-01"], "shell", ["echo"], 30, "admin")
    await store.create_commands_batch(["beta-02"], "shell", ["ls"], 30, "admin")
    r = (await api.client.get("/api/commands", params={"keyword": "alpha"})).json()
    assert r["total"] == 1 and r["items"][0]["pod"] == "alpha-01"
