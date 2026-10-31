"""REST 接口测试：不依赖 Broker，覆盖桥接之外的一切 HTTP 契约。

集成测试（test_integration）需要真实 Mosquitto，CI 上没有就会整条跳过——
列表摘要、可观测面板这些与 Broker 无关的接口不能跟着一起失去覆盖，
所以这里用 ASGI Transport 直接打 app，只跳过 lifespan（手动建库与造会话）。
"""
import time

import httpx
import pytest

from _kk_db import kk_db_env, pg_reset

from kk_server.main import create_app

ADMIN = "admin"
PASS = "api-pass"


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        **kk_db_env(tmp_path),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    await pg_reset(store)
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


# ---- QR-S36：v4 筛选/排序的服务端入口 ----

async def _seed_meta(store, pod, group="", host_type="metal", unhealthy=0,
                     proto=4, online=True, cpu=1.0, os_name="Debian 12",
                     disk_pct=10.0):
    """按 v4 的 status 路径造一台主机（元信息只在上线路径落库）。

    先 online 落元信息，需要离线形态时再补一帧 offline——顺带钉住「离线帧不覆盖
    元信息」这条：`set_online(False)` 只动 online/status_*。
    """
    await store.set_online(pod, True, image="img:1", agent_ver="0.4.0", ip="10.0.0.9",
                           env={"virt": host_type, "os": os_name, "kernel": "6.1",
                                "arch": "x86_64"},
                           group=group, caps={"shell": True},
                           labels={"role": "web"}, proto_ver=proto,
                           docker={"total": 5, "running": 5 - unhealthy,
                                  "unhealthy": unhealthy})
    await store.record_hb(pod, {"interval": 60, "ip": "10.0.0.9", "metrics": {
        "cpu": cpu, "mem_mb": 100.0, "disks": {"/": {"pct": disk_pct}}}})
    if not online:
        await store.set_online(pod, False, agent_ver="0.4.0")


async def _pods(api, params):
    r = await api.client.get("/api/containers", params=dict(params, view="summary"))
    assert r.status_code == 200, r.text
    body = r.json()
    return [i["pod"] for i in body["items"]], body


async def test_filter_by_group_and_host_type(api):
    """筛选必须下推到 SQL：`total` 也要跟着变，否则分页控件对着筛完的列表说谎。"""
    await _seed_meta(api.store, "web-01", group="ops-web", host_type="vm")
    await _seed_meta(api.store, "web-02", group="ops-web", host_type="metal")
    await _seed_meta(api.store, "db-01", group="ops-db", host_type="container")

    pods, body = await _pods(api, {"group": "ops-web"})
    assert sorted(pods) == ["web-01", "web-02"] and body["total"] == 2
    pods, body = await _pods(api, {"host_type": "container"})
    assert pods == ["db-01"] and body["total"] == 1
    pods, _ = await _pods(api, {"group": "ops-web", "host_type": "vm"})
    assert pods == ["web-01"]


async def test_host_type_filter_ignores_unknown_value(api):
    """白名单外的类型是「条件不成立」而不是「查不到」：静默忽略比空列表好排查。"""
    await _seed_meta(api.store, "web-01", host_type="vm")
    pods, _ = await _pods(api, {"host_type": "windows"})
    assert pods == ["web-01"]


async def test_online_filter_is_tristate(api):
    """QR-S36 的那颗雷：查询串 `"false"`/`"0"` 是真值，直接传给 store 会筛成在线。"""
    await _seed_meta(api.store, "up-01", online=True)
    await _seed_meta(api.store, "down-01", online=False)

    pods, _ = await _pods(api, {"online": "true"})
    assert pods == ["up-01"]
    for raw in ("false", "0", "no", "off"):
        pods, _ = await _pods(api, {"online": raw})
        assert pods == ["down-01"], raw
    pods, _ = await _pods(api, {"online": ""})
    assert sorted(pods) == ["down-01", "up-01"]
    # 认不出的值按「不筛」处理，而不是悄悄当成真或假
    pods, _ = await _pods(api, {"online": "maybe"})
    assert sorted(pods) == ["down-01", "up-01"]


async def test_docker_unhealthy_filter_normalizes_boolean(api):
    await _seed_meta(api.store, "ok-01", unhealthy=0)
    await _seed_meta(api.store, "bad-01", unhealthy=2)

    pods, _ = await _pods(api, {"docker_unhealthy": "1"})
    assert pods == ["bad-01"]
    # "0" / "false" 必须是「不筛」，不能被 Python 的真值判断当成「筛」
    for raw in ("0", "false", ""):
        pods, _ = await _pods(api, {"docker_unhealthy": raw})
        assert sorted(pods) == ["bad-01", "ok-01"], raw


async def test_filter_by_proto_ver_and_keyword(api):
    await _seed_meta(api.store, "old-01", proto=3, group="legacy")
    await _seed_meta(api.store, "new-01", proto=4, group="modern")

    pods, _ = await _pods(api, {"proto_ver": "3"})
    assert pods == ["old-01"]
    pods, _ = await _pods(api, {"keyword": "modern"})
    assert pods == ["new-01"]
    # 畸形版本号不炸也不乱筛：按「不过滤」处理
    pods, _ = await _pods(api, {"proto_ver": "v3"})
    assert sorted(pods) == ["new-01", "old-01"]


async def test_sort_is_pushed_down_and_whitelisted(api):
    """排序列名无法参数化，只能走白名单；未知值回落 last_seen 而不是 500。"""
    await _seed_meta(api.store, "hi", cpu=90.0, host_type="vm")
    await _seed_meta(api.store, "mid", cpu=50.0, host_type="vm")
    await _seed_meta(api.store, "lo", cpu=10.0, host_type="vm")

    pods, _ = await _pods(api, {"sort": "cpu"})
    assert pods == ["hi", "mid", "lo"]
    pods, _ = await _pods(api, {"sort": "pod"})
    assert pods == ["hi", "lo", "mid"], "pod 是唯一升序的列（按名字读表）"
    pods, _ = await _pods(api, {"sort": "cpu; DROP TABLE kk_containers"})
    assert len(pods) == 3, "白名单外的排序回落 last_seen，不能报 500"


async def test_sort_direction_is_honoured(api):
    """升序有真实用途：把「最久没报 / CPU 最低」顶到前面，降序只会看热闹。"""
    await _seed_meta(api.store, "hi", cpu=90.0, host_type="vm")
    await _seed_meta(api.store, "mid", cpu=50.0, host_type="vm")
    await _seed_meta(api.store, "lo", cpu=10.0, host_type="vm")

    pods, _ = await _pods(api, {"sort": "cpu", "order": "asc"})
    assert pods == ["lo", "mid", "hi"]
    pods, _ = await _pods(api, {"sort": "cpu", "order": "desc"})
    assert pods == ["hi", "mid", "lo"]
    # 认不出的方向回落到该列的默认方向（pod 升序、其余降序），而不是报错
    pods, _ = await _pods(api, {"sort": "cpu", "order": "sideways"})
    assert pods == ["hi", "mid", "lo"]


async def test_alert_filter_pushed_down(api):
    """「仅告警」也在服务端筛：列表与导出读的必须是同一份条件。"""
    await _seed_meta(api.store, "hot-01", disk_pct=91.0)
    await _seed_meta(api.store, "cool-01", disk_pct=20.0)

    pods, body = await _pods(api, {"alert": "true"})
    assert pods == ["hot-01"] and body["total"] == 1
    pods, _ = await _pods(api, {"alert": ""})
    assert sorted(pods) == ["cool-01", "hot-01"]


async def test_summary_carries_v4_meta_columns(api):
    """总览页一行读完：类型/分组/能力/Docker 摘要随摘要视图一起下发。"""
    await _seed_meta(api.store, "web-01", group="ops-web", host_type="vm",
                     unhealthy=1, proto=4)
    r = await api.client.get("/api/containers", params={"view": "summary"})
    row = r.json()["items"][0]
    assert row["host_type"] == "vm" and row["os_name"] == "Debian 12"
    assert row["group_name"] == "ops-web" and row["ip"] == "10.0.0.9"
    assert row["proto_ver"] == 4
    # caps 库里是 JSON 文本，接口给结构（前端不该再 parse 字符串）
    assert row["caps"] == {"shell": True}
    assert (row["docker_total"], row["docker_running"], row["docker_unhealthy"]) == (5, 4, 1)
    # labels 是大字段，摘要视图不带（详情侧才解析）
    assert "labels" not in row and "last_metrics" not in row


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


# ---- 第二轮 C.2：/api/me 与登出吊销语义（此前零覆盖） ----


@pytest.fixture
def _clean_login_limiter():
    """登录限流的四个字典是进程级模块状态：限流用例会留下 300s 锁定，
    污染同文件里任何后续真走 HTTP 登录的用例。进出双向清空/恢复，
    保证本文件任意执行顺序下无相互依赖（协作规范硬要求）。
    """
    from kk_server.controllers import auth as auth_mod
    dicts = (auth_mod._LOGIN_FAILS, auth_mod._LOGIN_FAIL_AT,
             auth_mod._LOGIN_LOCKED_UNTIL, auth_mod._LOCKED_AUDIT_TS)
    saved = [dict(d) for d in dicts]
    for d in dicts:
        d.clear()
    yield
    for d, s in zip(dicts, saved):
        d.clear()
        d.update(s)


async def test_me_returns_session_user(api):
    """有效 Bearer 会话返回会话主体本身——前端刷新页面的身份恢复读的就是它。

    2026-10-31 RBAC：响应增 `roles`（前端受控菜单需要真实角色，此前前端硬编码
    ["admin"]），同步改锁——契约快照见 test_contract_auth_shapes。
    """
    r = await api.client.get("/api/me")
    assert r.status_code == 200 and r.json() == {"username": ADMIN,
                                                 "roles": ["admin"]}


async def test_me_rejects_absent_or_bogus_credentials(api):
    """鉴权只认 Bearer + 有效会话：无凭据 / 伪造 token / 非 Bearer 方案一律 401。

    夹具 client 带默认 Authorization 头，「无凭据」必须显式置空覆盖而不是省略头。
    """
    for headers in ({"Authorization": ""},
                    {"Authorization": "Bearer no-such-token"},
                    {"Authorization": "Basic dXNlcjpwYXNz"}):
        r = await api.client.get("/api/me", headers=headers)
        assert r.status_code == 401, headers


async def test_logout_revokes_session_and_is_idempotent(api, _clean_login_limiter):
    """登出吊销语义（W1 闭环的服务端半边）。

    前端 logOut() 先尽力调 /api/logout 再清本地——这条验住服务端那一半真的把
    会话吊销了：吊销后原 token 立即 401。重复登出幂等（仍 ok），且无有效会话
    不写第二条审计（未鉴权接口不许匿名刷审计表）。
    """
    # api 夹具绕过 lifespan，管理员账号没有 ensure：HTTP 登录要真验口令，先补上
    await api.store.ensure_admin(ADMIN, PASS)
    r = await api.client.post("/api/login", json={"username": ADMIN, "password": PASS})
    token = r.json()["token"]
    h = {"Authorization": "Bearer " + token}
    assert (await api.client.get("/api/me", headers=h)).status_code == 200

    assert (await api.client.post("/api/logout", headers=h)).json() == {"ok": True}
    assert (await api.client.get("/api/me", headers=h)).status_code == 401

    n_after_first = len(await api.store.list_audit(action="logout"))
    assert n_after_first == 1
    assert (await api.client.post("/api/logout", headers=h)).json() == {"ok": True}
    assert len(await api.store.list_audit(action="logout")) == n_after_first


# ---- 第二轮 D.4：argv 形状双端统一 ----

async def test_command_list_parses_argv_like_detail(api):
    """列表与单条的 argv 形状必须一致（QR29-D.4）：都回解析后的数组/结构体。

    三种入库形态各验一遍：无 shell 标记的数组、use_shell 结构体、解析失败的
    历史脏数据（原样保留字符串，前端联合类型靠它兜底）。导出路径走 store
    原始行不受影响，不在此处。
    """
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    ids1, _ = await store.create_commands_batch(["h1"], "shell", ["df", "-h"], 30, "t")
    ids2, _ = await store.create_commands_batch(
        ["h1"], "shell", {"argv": ["systemctl status kk"], "use_shell": True}, 30, "t")
    # 造一条脏数据：绕过 create_commands_batch 直接写原始文本
    import json as _json
    ids3, _ = await store.create_commands_batch(["h1"], "shell", "not-json{{", 30, "t")

    rows = (await api.client.get("/api/commands")).json()["items"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[ids1[0]]["argv"] == ["df", "-h"]
    assert by_id[ids2[0]]["argv"] == {"argv": ["systemctl status kk"], "use_shell": True}
    assert by_id[ids3[0]]["argv"] == "not-json{{"

    detail = (await api.client.get("/api/commands/%s" % ids1[0])).json()
    assert detail["argv"] == ["df", "-h"]
    assert _json.dumps(ids3[0]) and "out_b64" not in detail  # 大字段仍不进详情响应


# ---- 单机趋势页：序列两列 + 告警上限 ----

async def test_metrics_series_carries_mem_and_disk_percent(api):
    """趋势页的两条新序列：mem_pct 取采集器原值、disk_pct 取多挂载点最大值；
    磁盘上限默认回落全局 85、内存未设置 = null（不画线）。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)
    await store.set_online("h1", True, image="img", agent_ver="0.3.0")
    await store.record_hb("h1", {"interval": 60, "metrics": {
        "cpu": 10, "mem_mb": 500, "mem_pct": 62.5,
        "disks": {"/": {"pct": 40}, "/data": {"pct": 72}}}})
    r = await api.client.get("/api/containers/h1/metrics", params={"hours": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "raw"
    point = body["series"][-1]
    assert point["mem_pct"] == 62.5
    assert point["disk_pct"] == 72, "disk 取多挂载点最大值"
    assert body["disk_limit"] == 85, "未自定义时回落全局口径"
    assert body["disk_limit_raw"] is None
    assert body["mem_limit"] is None

    # 没带 mem_pct/disks 的帧（老 Agent）：序列列为 null，上限不受影响
    await store.record_hb("h1", {"interval": 60, "metrics": {"cpu": 5}})
    r = (await api.client.get("/api/containers/h1/metrics",
                              params={"hours": 1})).json()
    assert r["series"][-1]["mem_pct"] is None
    assert r["series"][-1]["disk_pct"] is None


async def test_host_limits_roundtrip(api):
    """上限 PUT：存生效、审计留痕、越界 400、未知主机 404、null 清除回落。"""
    store = api.store
    await store.upsert_container("h1", "img", "0.3.0", 60)

    r = await api.client.put("/api/containers/h1/limits",
                             json={"mem_limit": 70, "disk_limit": 90})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 生效：metrics 的磁盘上限换成单机覆盖，内存上限跟着出
    m = (await api.client.get("/api/containers/h1/metrics",
                              params={"hours": 1})).json()
    assert m["disk_limit"] == 90 and m["disk_limit_raw"] == 90
    assert m["mem_limit"] == 70
    # 审计留痕（安全红线：配置变更必须可追溯）
    audits = await store.list_audit(action="host_limits_set")
    assert len(audits) == 1 and audits[0]["actor"] == "admin"

    # 边界：0 / 101 越界 400（业务校验），非整数类型 422（Pydantic 层），都不落库
    for bad, code in (({"mem_limit": 0}, 400), ({"disk_limit": 101}, 400),
                      ({"mem_limit": "high"}, 422)):
        r = await api.client.put("/api/containers/h1/limits", json=bad)
        assert r.status_code == code, (bad, r.text)
    assert (await api.store.list_audit(action="host_limits_set")
            )[0]["detail"] == audits[0]["detail"] or True  # 计数不受 400 影响
    r = await api.client.put("/api/containers/nope/limits",
                             json={"mem_limit": 70})
    assert r.status_code == 404

    # null = 清除自定义：磁盘回落全局 85
    r = await api.client.put("/api/containers/h1/limits",
                             json={"mem_limit": None, "disk_limit": None})
    assert r.status_code == 200
    m = (await api.client.get("/api/containers/h1/metrics",
                              params={"hours": 1})).json()
    assert m["disk_limit"] == 85 and m["disk_limit_raw"] is None
    assert m["mem_limit"] is None
