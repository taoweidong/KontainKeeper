"""QR29（2026-10-29 评估）修复项的回归锁。

每条测试对应 remediation-plan（docs/remediation-plan-2026-10-29.md）里的一个编号，
docstring 写明「旧行为为什么是缺陷」——回滚某条修复时，这条测试必须跟着一起红。
全部单测无 Broker 依赖、无相互依赖（模块级状态在用例内自建自清）。
"""
import asyncio
import json
import threading
import time
import types

import httpx
import pytest
from sqlalchemy import select, update

import kk_server.controllers.auth as auth_mod
import kk_server.services.mqtt_bridge as bridge_mod
from kk_server.config import load_settings
from kk_server.main import create_app
from kk_server.models.helpers import _PWDF_ITERS, _PWDF_ITERS_LEGACY, _pwdf
from kk_server.models.store import Store, admins
from kk_server.models.tables import hourly


@pytest.fixture
async def store(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    await st.setup()
    yield st
    await st.close()


@pytest.fixture
async def api(tmp_path):
    """REST 夹具（与 test_api.py 同款自足版）：ASGI Transport + 手工补 lifespan
    的等价初始化，不依赖真实端口与 Broker。"""
    app = create_app({
        "KK_ADMIN_USER": "admin",
        "KK_ADMIN_PASS": "qr29-pass",
        "KK_DB_PATH": str(tmp_path / "qr29.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    token = await store.create_session("admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield types.SimpleNamespace(client=client, store=store, app=app)
    await store.close()


class FakePublish:
    """paho `Client.publish` 的替身（与 conftest 同款）：rc 可指定，
    供 S14 断言「返回码非 0 = 未入队」。"""

    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos})
        return types.SimpleNamespace(rc=self.rc)


class LoopStub:
    """替身事件循环：记录 call_soon_threadsafe 的投递而不真跑——
    让 _dispatch 路径「可达但可断言」，不必起真实 loop。"""

    def __init__(self):
        self.posted = []

    def call_soon_threadsafe(self, cb, *args):
        self.posted.append((cb, args))


def _msg(topic, body):
    return types.SimpleNamespace(topic=topic,
                                 payload=json.dumps(body).encode("utf-8"))


# ---- QR29-S1：verify_admin 的 PBKDF2 必须离开事件循环 ----

async def test_verify_admin_paths_unchanged(store):
    """三路返回值与旧实现逐字一致：对 → True，错 → False，
    legacy 迭代数哈希 → True 且原地升级到新迭代数。"""
    await store.ensure_admin("admin", "right-pass")
    assert await store.verify_admin("admin", "right-pass") is True
    assert await store.verify_admin("admin", "wrong-pass") is False

    # 人工把口令哈希降级成 legacy 迭代数（120k），走「首次成功登录原地升级」
    row = await store._one(select(admins).where(admins.c.username == "admin"))
    await store._run(update(admins).where(admins.c.username == "admin")
                     .values(pw_hash=_pwdf(row["salt"], "legacy-pass",
                                           _PWDF_ITERS_LEGACY)))
    assert await store.verify_admin("admin", "legacy-pass") is True
    row2 = await store._one(select(admins).where(admins.c.username == "admin"))
    assert row2["pw_hash"] == _pwdf(row2["salt"], "legacy-pass", _PWDF_ITERS)


async def test_verify_admin_offloads_pbkdf2_from_event_loop(store, monkeypatch):
    """登录哈希（310k 迭代同步 CPU 活）在 to_thread 里跑：哈希进行中事件循环
    仍能按时醒来。旧实现在协程里同步哈希，MQTT 桥接的心跳落库会被一次未鉴权的
    /api/login 阻塞数十毫秒。"""
    await store.ensure_admin("admin", "pw")     # 先建行，别让慢哈希拖慢建行
    from kk_server.models import store as store_mod

    def slow_pwdf(*args, **kwargs):
        time.sleep(0.5)
        return "0" * 64

    monkeypatch.setattr(store_mod, "_pwdf", slow_pwdf)
    t0 = asyncio.get_running_loop().time()
    task = asyncio.create_task(store.verify_admin("admin", "whatever"))
    await asyncio.sleep(0.05)     # 旧实现这里要等满 0.5s 的同步哈希
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.3, "事件循环被 verify_admin 阻塞 %.2fs（PBKDF2 没进线程池）" % elapsed
    assert await task is False


# ---- QR29-S2：paho 网络线程回调必须自兜底 ----

def test_on_message_swallows_callback_exceptions(bridge, monkeypatch):
    """回调路径抛异常不能外溢到 paho 网络线程（会把线程打死、桥接静默停摆），
    而是计入 stats["bridge_errors"] 并放行后续帧。"""
    def boom(*args, **kwargs):
        raise RuntimeError("白名单校验炸了")

    monkeypatch.setattr(bridge_mod, "ip_in_whitelist", boom)
    bridge._on_message(None, None, _msg("kk/v1/web-01/status",
                                        {"online": True, "ip": "10.0.0.1"}))
    assert bridge.stats["bridge_errors"] == 1


async def test_dispatch_without_loop_is_counted_not_fatal(bridge):
    """loop 未绑定（装配漏了 lifespan）时显式拒帧并计数：这是既有语义，
    这里锁它不被 S2 的兜底壳重构改坏。"""
    bridge.loop = None
    bridge._on_message(None, None, _msg("kk/v1/web-01/status",
                                        {"online": True, "ip": "10.0.0.1"}))
    assert bridge.stats["rejected"] == 1


# ---- QR29-S3：stats 读改写加锁 ----

def test_stats_bump_is_thread_safe(bridge):
    """4 线程 × 500 次 bump 一个不丢：dict 的 += 不是原子，安全计数（rejected）
    偏低会让安全事件追溯失真。"""
    def hammer():
        for _ in range(500):
            bridge._bump("rejected")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bridge.stats["rejected"] == 2000


# ---- QR29-S4：status/hb 帧归属校验 ----

async def test_frame_with_foreign_host_is_rejected(bridge):
    """白名单内的主机不能替他机覆写在线状态：topic=web-01 的帧自报 host=other
    必须被拒（_on_status 不被投递），只投 host_mismatch 审计，且计数 +1。"""
    bridge.loop = LoopStub()
    bridge._on_message(None, None, _msg(
        "kk/v1/web-01/status",
        {"online": True, "host": "other-host", "ip": "10.0.0.1", "proto_ver": 4}))
    dispatched = [inner[0] for _, inner in bridge.loop.posted]
    assert dispatched == [bridge._on_host_mismatch]
    assert bridge.stats["rejected"] == 1

    await bridge._on_host_mismatch("web-01", {"host": "other-host"})
    rows = await bridge.store.list_audit(limit=10)
    assert any(r["action"] == "host_mismatch" and
               json.loads(r["detail"])["topic_host"] == "web-01" for r in rows)


async def test_frame_without_host_field_still_routes(bridge):
    """LWT 补发与存量 Agent 可能不带 host 字段：缺失放行（照常投递处理函数），
    不能把兼容窗口收窄成硬性要求。"""
    bridge.loop = LoopStub()
    bridge._on_message(None, None, _msg(
        "kk/v1/web-01/status", {"online": True, "ip": "10.0.0.1", "proto_ver": 4}))
    assert len(bridge.loop.posted) == 1
    assert bridge.stats["rejected"] == 0


# ---- QR29-S5：set_online 的时间戳钳制 ----

async def test_set_online_clamps_out_of_range_timestamp(store):
    """时钟超前的主机不能把 status_ts 写进未来：写进未来的行在 is_online 的
    负差值上恒判「在线」，离线兜底（mark_stale_offline）永不生效。"""
    now = int(time.time())
    await store.set_online("p-future", True, ts=now + 100000)
    assert (await store.get_container("p-future"))["status_ts"] <= now + 300

    await store.set_online("p-past", True, ts=now - 100000)
    assert (await store.get_container("p-past"))["status_ts"] >= now - 86400

    # 合法范围内的时间戳原样采纳（测试回拨等合法用途不受影响）
    await store.set_online("p-ok", True, ts=now - 60)
    assert (await store.get_container("p-ok"))["status_ts"] == now - 60


# ---- QR29-S7：登录限流字典的回收 ----

def test_reap_evicts_cooled_fail_counts():
    """未达阈值的失败计数在冷却 TTL 后被回收：一次用户名枚举不再能把
    _LOGIN_FAILS/_LOGIN_FAIL_AT 撑成无界增长。"""
    now = time.time()
    auth_mod._LOGIN_FAILS["u:cooled"] = 3
    auth_mod._LOGIN_FAIL_AT["u:cooled"] = now - auth_mod.FAIL_COUNT_TTL - 1
    auth_mod._LOGIN_FAILS["u:active"] = 1
    auth_mod._LOGIN_FAIL_AT["u:active"] = now
    auth_mod._LOGIN_LOCKED_UNTIL["u:expired"] = now - 1
    auth_mod._LOCKED_AUDIT_TS["u:stale-ts"] = now - auth_mod.LOCKED_AUDIT_INTERVAL * 5
    try:
        auth_mod._reap(now)
        assert "u:cooled" not in auth_mod._LOGIN_FAILS
        assert "u:cooled" not in auth_mod._LOGIN_FAIL_AT
        assert "u:expired" not in auth_mod._LOGIN_LOCKED_UNTIL
        assert "u:stale-ts" not in auth_mod._LOCKED_AUDIT_TS
        # 活跃攻击的计数不被误清
        assert auth_mod._LOGIN_FAILS["u:active"] == 1
    finally:
        for d in (auth_mod._LOGIN_FAILS, auth_mod._LOGIN_FAIL_AT,
                  auth_mod._LOGIN_LOCKED_UNTIL, auth_mod._LOCKED_AUDIT_TS):
            d.clear()


def test_locked_audit_is_throttled(monkeypatch):
    """锁定期审计 60s 一条：未鉴权路径不能逐请求灌审计表（audit 只增不减）。"""
    monkeypatch.setattr(auth_mod, "_LOCKED_AUDIT_TS", {})
    assert auth_mod._locked_audit_allowed("u:a", "i:b", 1000.0) is True
    assert auth_mod._locked_audit_allowed("u:a", "i:b", 1030.0) is False
    assert auth_mod._locked_audit_allowed(
        "u:a", "i:b", 1000.0 + auth_mod.LOCKED_AUDIT_INTERVAL) is True


# ---- QR29-S9：小时聚合追赶窗口的单位 ----

async def test_aggregate_hours_catches_up_after_downtime(store):
    """宕机数小时后追赶聚合不得跳段：心跳落在 5 小时前（保留期 2 天内），
    聚合后 hourly 必须有那个小时桶。旧实现把「天」当「小时」（(raw_days+1)*3600），
    水位直接跳进 now-3h，这段历史永久缺曲线。"""
    now = int(time.time())
    old_ts = now - 5 * 3600
    await store.upsert_container("pod-agg", "img", "0.1.0", 60)
    await store.record_hb("pod-agg", {"ts": old_ts, "interval": 60,
                                      "metrics": {"cpu": 7.5, "mem_mb": 100.0}})
    n = await store._aggregate_hours(now, raw_days=2)
    assert n > 0
    rows = await store._all(select(hourly).where(hourly.c.pod == "pod-agg"))
    assert any(r["hour"] == old_ts // 3600 and r["samples"] == 1 for r in rows), rows


# ---- QR29-D.3（第二轮）：hourly.last_metrics 砍写入 ----

async def test_aggregate_hours_stops_writing_last_metrics(store):
    """hourly.last_metrics 全仓零消费（唯一读方 metrics_series 只取 cpu/mem 均值、
    统计页只数行数），写入侧的逐主机逐小时 lastm 反查是聚合路径的 N+1。

    砍写入后的语义：聚合行照常产出（cpu/mem 聚合不受影响）；last_metrics 列
    只留建行时的空串，重复聚合（upsert 走 update_cols）不复活它。未来要消费
    这个字段，必须先恢复写入并同步更新本锁——否则列里永远只有空串，读了个寂寞。
    """
    now = int(time.time())
    old_ts = now - 5 * 3600
    await store.upsert_container("pod-lm", "img", "0.1.0", 60)
    await store.record_hb("pod-lm", {"ts": old_ts, "interval": 60,
                                     "metrics": {"cpu": 7.5, "mem_mb": 100.0}})
    assert await store._aggregate_hours(now, raw_days=2) > 0
    rows = await store._all(select(hourly).where(hourly.c.pod == "pod-lm"))
    assert rows and all(r["last_metrics"] == "" for r in rows)
    # 重复聚合走 update 分支：cpu 聚合照常刷新，last_metrics 不被写入
    await store.record_hb("pod-lm", {"ts": old_ts + 60, "interval": 60,
                                     "metrics": {"cpu": 15.0, "mem_mb": 120.0}})
    await store.kv_set("agg_hour", (old_ts // 3600))
    await store._aggregate_hours(now, raw_days=2)
    rows = await store._all(select(hourly).where(hourly.c.pod == "pod-lm"))
    bucket = [r for r in rows if r["hour"] == old_ts // 3600][0]
    assert bucket["samples"] == 2
    assert bucket["last_metrics"] == ""


# ---- QR29-S12：full 视图缺省封顶 ----

async def test_full_view_without_limit_is_capped(api, monkeypatch):
    """/api/containers?view=full 不带 limit 时封顶 5000：全机队 LONGTEXT
    一次性进内存是现成的自伤面（admin 即可触发）。5001 台的规模无法在单测里
    造出来，直接断言控制器传给 store 的 limit。"""
    captured = {}
    orig = api.store.list_containers

    async def spy(*args, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return await orig(*args, **kwargs)

    monkeypatch.setattr(api.store, "list_containers", spy)
    r = await api.client.get("/api/containers", params={"view": "full"})
    assert r.status_code == 200
    assert captured["limit"] == 5000


# ---- QR29-S13：审计 detail 入库截断 ----

async def test_audit_detail_is_capped_not_dropped(store):
    """超长 detail 截断落库而不是让 INSERT 炸 1406：批量下发的审计发生在
    命令发布之后，炸了就是「命令已出去、台账却 500」的部分写入。"""
    await store.add_audit("tester", "command_create", {"blob": "A" * 30000})
    rows = await store.list_audit(limit=5)
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert len(detail) <= 8192 and detail.endswith("(truncated)")

    # 小 detail 逐字保留
    await store.add_audit("tester", "login_ok", {"k": "v"})
    rows = await store.list_audit(limit=5)
    assert json.loads(rows[0]["detail"]) == {"k": "v"}


# ---- QR29-S14：升级帧发布返回码 ----

async def test_publish_update_treats_bad_rc_as_failure(tmp_path):
    """队列满/未连接（rc 非 0）时升级帧没真正入队：台账要置 failed、
    upgrade_pushed 不得虚高。旧实现只看抛不抛异常，rc 静默吞掉后
    台账卡 pending 30 分钟。"""
    store = Store(str(tmp_path / "s14.db"))
    await store.setup()
    try:
        settings = load_settings({
            "KK_DB_PATH": str(tmp_path / "s14.db"),
            "KK_MQTT_URL": "mqtt://broker:1883",
            "KK_WEB_DIR": str(tmp_path / "noweb"),
        })
        b = bridge_mod.MqttBridge(store, settings, [], loop=None, proto_ver=3)
        b.cli = FakePublish(rc=5)     # 任何非 SUCCESS/NO_CONN 的返回码
        await store.create_update("up-p-1", "p", "0.1.0", "0.2.0")
        assert await b.publish_update("p", "up-p-1", {"version": "0.2.0"}) is False
        row = await store.get_update("up-p-1")
        assert row["status"] == "failed" and row["reason"] == "publish_failed"
        assert b.stats["upgrade_pushed"] == 0
        assert b.stats["cmd_failed"] == 1

        # rc=0 正常投出：台账保持 pending（等回执）、pushed 计数 +1
        b2 = bridge_mod.MqttBridge(store, settings, [], loop=None, proto_ver=3)
        b2.cli = FakePublish(rc=0)
        await store.create_update("up-p-2", "p", "0.1.0", "0.2.0")
        assert await b2.publish_update("p", "up-p-2", {"version": "0.2.0"}) is True
        assert (await store.get_update("up-p-2"))["status"] == "pending"
        assert b2.stats["upgrade_pushed"] == 1
    finally:
        await store.close()


# ---- QR29-S15：online 筛选与渲染同口径 ----

async def test_online_filter_matches_grace_semantics(store):
    """?online=true/false 必须与列表渲染、online_set 三处一致：裸 online 列
    只反映 retained/LWT，宽限兜底要在筛选里再现，否则「筛出来的」含渲染为
    离线的行。"""
    await store.set_online("fresh", True)
    await store.set_online("stale", True)
    # 离线帧只更新既有行（LWT 的既有语义），先 upsert 再置离线
    await store.upsert_container("marked-off", "img", "0.1.0", 60)
    await store.set_online("marked-off", False)
    # stale：声称在线但 status_ts 远超宽限（Broker 崩溃没发 LWT 的僵尸在线）
    await store.exec_sql("UPDATE kk_containers SET status_ts = ? WHERE pod = 'stale'",
                         (int(time.time()) - 100000,))

    assert await store.online_set() == {"fresh"}

    rows_t = {r["pod"] for r in await store.list_containers(online=True)}
    rows_f = {r["pod"] for r in await store.list_containers(online=False)}
    assert rows_t == {"fresh"}
    assert "stale" in rows_f and "marked-off" in rows_f and "fresh" not in rows_f


# ---- QR29-S18：keyword LIKE 的 autoescape ----

async def test_command_keyword_escapes_like_wildcards(store):
    """搜 "100%" 只命中字面量：旧实现裸拼 %...% 通配，"100%" 退化成 "100"
    还能造尾随 % 的慢扫描（主机侧早就是 autoescape，这里对齐）。"""
    await store.upsert_container("p1", "img", "0.1.0", 60)
    await store.create_command("p1", "shell", ["echo", "rate 100%"], 30, "t")
    await store.create_command("p1", "shell", ["echo", "100x"], 30, "t")

    rows = await store.list_commands(keyword="100%")
    assert len(rows) == 1 and "100%" in rows[0]["argv"]

    rows = await store.list_commands(keyword="100_")
    assert rows == []


async def test_audit_keyword_escapes_like_wildcards(store):
    """审计关键字同款：下划线不再当「任意一字符」通配。"""
    await store.add_audit("t", "act", {"msg": "save 100% now"})
    await store.add_audit("t", "act", {"msg": "save 100x now"})

    rows = await store.list_audit(keyword="100%")
    assert len(rows) == 1 and "100%" in rows[0]["detail"]

    rows = await store.list_audit(keyword="save_100")
    assert rows == []
