"""QR1120-S6..S12（2026-11-20 存储层评估）修复项的回归锁。

每条测试对应一个缺陷编号，docstring 写明「旧行为为什么是缺陷」——
回滚某条修复时，这条测试必须跟着一起红。全部单测无 Broker/网络依赖，
时间相关断言用相对窗口（不依赖真实时钟的精确值），夹具与 test_store.py 同款。
"""
import json
import time

import pytest

from kk_server.models.store import Store
from _kk_db import kk_db_url, pg_reset


@pytest.fixture
async def store(tmp_path):
    st = Store(kk_db_url(tmp_path))
    await st.setup()
    await pg_reset(st)
    yield st
    await st.close()


# ---- QR1120-S6：迟到 LWT 后心跳必须拉回在线 ----

async def test_record_hb_restores_online_after_late_lwt(store):
    """半开连接：Agent 已重连发 online，Broker keepalive 超时后补发旧会话
    LWT 把 online 打回 0。旧 record_hb 只刷 last_seen/status_ts 不回置 online，
    主机「心跳在流却永显离线」。心跳是比任何 status 帧更强的在线证据。"""
    await store.set_online("pod-lwt", True, image="img", agent_ver="0.5.0")
    await store.set_online("pod-lwt", False)          # 迟到的旧会话 LWT
    assert (await store.get_container("pod-lwt"))["online"] == 0

    await store.record_hb("pod-lwt", {"interval": 60, "metrics": {"cpu": 2.0}})
    row = await store.get_container("pod-lwt")
    assert row["online"] == 1 and row["status_reason"] == ""
    assert await store.is_online("pod-lwt") is True


async def test_record_hb_clears_stale_offline_reason(store):
    """心跳到达说明 Agent 已活：陈旧的离线原因（含 updating）不该继续挂着。
    updating 宽限语义由 set_online 管，不在心跳路径保留。"""
    await store.set_online("pod-r", True, reason="online")
    await store.set_online("pod-r", False, reason="updating")
    await store.record_hb("pod-r", {"interval": 60, "metrics": {"cpu": 1.0}})
    row = await store.get_container("pod-r")
    assert row["online"] == 1 and row["status_reason"] == ""


# ---- QR1120-S7：离线分支恒用服务器时间 ----

async def test_offline_ignores_stale_lwt_timestamp(store):
    """LWT 载荷 ts 冻结在 Agent 启动时刻：运行数天后掉线带陈旧 ts，旧实现
    在 ±24h 钳制窗内照样把 status_ts 写回过去，离线时长失真。离线时刻的
    唯一真相是「服务端何时收到离线帧」。"""
    now = int(time.time())
    await store.set_online("p-stale", True)
    # 启动于 2h 前的 Agent 掉线：ts 在钳制窗内但早已陈旧
    await store.set_online("p-stale", False, ts=now - 7200)
    row = await store.get_container("p-stale")
    assert row["online"] == 0
    assert row["status_ts"] >= now, "离线 status_ts 必须是服务器时间，不吃帧 ts"

    # 越界 ts 同样回落服务器时间（而不是沿用在线分支的钳制语义）
    await store.set_online("p-stale", False, ts=now - 100000)
    assert (await store.get_container("p-stale"))["status_ts"] >= now


async def test_offline_stale_ts_does_not_pollute_updating_grace(store):
    """updating 宽限拿 status_ts 算年龄：若离线帧把陈旧 ts 写进 status_ts，
    紧随其后的空 reason LWT 会因「年龄 > 120s」误判宽限已过，抹掉 updating。
    离线恒用服务器时间后，宽限判定不受帧 ts 污染。"""
    now = int(time.time())
    await store.set_online("p-upd", True)
    await store.set_online("p-upd", False, reason="updating", ts=now - 7200)
    await store.set_online("p-upd", False, reason="", ts=now - 7200)
    assert (await store.get_container("p-upd"))["status_reason"] == "updating"


async def test_online_path_keeps_frame_ts_clamp(store):
    """在线路径不动（QR29-S5 既有锁的同款口径）：合法窗内帧 ts 原样采纳，
    越界回落服务器时间——S7 只改离线分支。"""
    now = int(time.time())
    await store.set_online("p-on-ok", True, ts=now - 60)
    assert (await store.get_container("p-on-ok"))["status_ts"] == now - 60
    await store.set_online("p-on-future", True, ts=now + 100000)
    assert (await store.get_container("p-on-future"))["status_ts"] <= now + 300


# ---- QR1120-S8：append_result 水位 CASE 绑规整后的 seq_n ----

async def test_append_result_string_seq_is_idempotent(store):
    """帧带字符串 seq（"3"）时旧实现把原始 seq 绑进 SQL CASE：PG 类型报错
    丢帧、SQLite/MySQL 隐式转换分叉。规整后的 seq_n 必须贯穿水位判定与写回。"""
    await store.upsert_container("pod-sseq", "img", "0.1.0", 60)
    cid = await store.create_command("pod-sseq", "shell", ["echo"], 30, "admin")
    base = {"id": cid, "total": 2}

    await store.append_result({**base, "seq": "0", "out_b64": "QUJD", "done": False})
    await store.append_result({**base, "seq": "1", "out_b64": "REVG", "done": True, "rc": 0})
    assert await store.command_output(cid) == "ABCDEF"

    # 字符串 seq 重投：水位幂等必须与 int seq 完全同语义
    await store.append_result({**base, "seq": "0", "out_b64": "QUJD", "done": False})
    await store.append_result({**base, "seq": "1", "out_b64": "REVG", "done": True, "rc": 0})
    row = await store.get_command(cid)
    assert await store.command_output(cid) == "ABCDEF"
    assert row["out_chunks"] == 2 and row["last_seq"] == 1
    assert row["status"] == "done", "迟到字符串 seq 重投不得回退终态"


async def test_append_result_mixed_int_and_string_seq(store):
    """int 与字符串 seq 混用（Agent 版本混杂的真实形态）水位必须连续。"""
    await store.upsert_container("pod-mix", "img", "0.1.0", 60)
    cid = await store.create_command("pod-mix", "shell", ["echo"], 30, "admin")
    await store.append_result({"id": cid, "seq": 0, "out_b64": "QUJD"})
    await store.append_result({"id": cid, "seq": "1", "out_b64": "REVG"})
    await store.append_result({"id": cid, "seq": "1", "out_b64": "REVG"})   # 重投
    await store.append_result({"id": cid, "seq": 2, "out_b64": "R0hJ", "done": True, "rc": 0})
    row = await store.get_command(cid)
    assert await store.command_output(cid) == "ABCDEFGHI"
    assert row["out_chunks"] == 3 and row["last_seq"] == 2


async def test_append_result_seq_bind_is_int_across_dialects(store):
    """字符串 seq 规整后，UPDATE 里所有 seq 相关绑定值必须是 int——
    三库编译口径一致（PG 不做隐式转换，绑 str 直接类型报错丢帧）。
    捕获真实下发的语句分别按三库方言编译检查绑定参数。"""
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    await store.upsert_container("pod-bind", "img", "0.1.0", 60)
    cid = await store.create_command("pod-bind", "shell", ["echo"], 30, "admin")
    captured = []
    orig = store._run

    async def spy(*stmts):
        captured.extend(stmts)
        return await orig(*stmts)

    store._run = spy
    try:
        await store.append_result({"id": cid, "seq": "3", "out_b64": "QUJD"})
    finally:
        store._run = orig
    stmt = captured[-1]
    for dialect in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
        params = stmt.compile(dialect=dialect).params
        assert "3" not in params.values(), "原始字符串 seq 不得进绑定参数"
        assert 3 in params.values(), "规整后的 int seq_n 必须进绑定参数"


# ---- QR1120-S9：batch_summary 查询级时间窗 ----

async def test_batch_summary_window_matches_full_scan_semantics(store):
    """时间窗下推后返回形状/语义不变：最近 limit 个批次、按 created_at 倒序、
    状态分布计数。用不同 created_at 的批次验证「挑的是最近的 N 个」。"""
    await store.upsert_container("h", "img", "0.1.0", 60)
    now = int(time.time())
    batches = []
    for i in range(5):
        ids, bid = await store.create_commands_batch(["h"] * 3, "shell", ["echo"], 30, "admin")
        batches.append((bid, ids))
        # 批次 i 的 created_at = now - (4-i)*100：i=4 最新（恰好 now）
        await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE batch_id=:b",
                             {"a": now - (4 - i) * 100, "b": bid})
    # 最新批次（i=4）：2 done + 1 pending
    for cid in batches[4][1][:2]:
        await store.append_result({"id": cid, "done": True, "rc": 0})

    top2 = await store.batch_summary(limit=2)
    assert [b["batch_id"] for b in top2] == [batches[4][0], batches[3][0]]
    assert top2[0]["total"] == 3 and top2[0].get("done") == 2 and top2[0].get("pending") == 1
    assert top2[0]["created_at"] == now

    # limit=0/None = 全量（既有语义保留）
    full = await store.batch_summary(limit=0)
    assert len(full) == 5
    assert [b["batch_id"] for b in full] == [b for b, _ in reversed(batches)]


async def test_batch_summary_excludes_non_batch_rows(store):
    """batch_id="" 的行（补列前的历史单条命令）不进批次汇总——
    窗口子查询不得把它们带进来。"""
    await store.upsert_container("h", "img", "0.1.0", 60)
    cid = await store.create_command("h", "shell", ["echo"], 30, "admin")
    await store.exec_sql("UPDATE kk_commands SET batch_id='' WHERE id=:a", {"a": cid})
    assert await store.batch_summary() == []


# ---- QR1120-S10：hb raw 入库尺寸收敛 ----

async def test_hb_oversized_custom_is_sanitised_not_dropped(store):
    """插件 custom 撑到任意大时旧实现直写 last_metrics：MySQL TEXT 64KB 静默
    截断成半截 JSON，详情页 json.loads 直接炸。超限帧必须 sanitise 成合法
    JSON 且 ≤ 256KB，标准指标标量保住（曲线不能因插件失控出空洞）。"""
    await store.upsert_container("pod-big", "img", "0.1.0", 60)
    huge = "x" * (300 * 1024)
    await store.record_hb("pod-big", {
        "interval": 60, "metrics": {"cpu": 7.5, "mem_mb": 123.0, "mem_pct": 42.0},
        "custom": {"p1": {"blob": huge}}})
    row = await store.get_container("pod-big")
    raw = row["last_metrics"]
    assert len(raw) <= 256 * 1024
    hb = json.loads(raw)                      # 必须是合法 JSON
    assert hb["_truncated"] is True
    assert hb["metrics"]["cpu"] == 7.5 and hb["metrics"]["mem_mb"] == 123.0
    assert "<truncated" in hb["custom"]["p1"]["blob"]
    # 摘要列与序列点不受影响
    assert row["cpu"] == 7.5 and row["mem_mb"] == 123.0
    series, _ = await store.metrics_series("pod-big", 24)
    assert len(series) == 1 and series[0]["cpu"] == 7.5


async def test_hb_oversized_many_small_keys_still_bounded(store):
    """海量小键（字符串截断救不了）也必须收敛到封顶内：二级回落只留标准项。"""
    await store.upsert_container("pod-keys", "img", "0.1.0", 60)
    await store.record_hb("pod-keys", {
        "interval": 60, "metrics": {"cpu": 1.0},
        "custom": {"k%d" % i: i for i in range(60000)}})
    raw = (await store.get_container("pod-keys"))["last_metrics"]
    assert len(raw) <= 256 * 1024
    hb = json.loads(raw)
    assert hb["_truncated"] is True and hb["metrics"]["cpu"] == 1.0


async def test_hb_normal_frame_is_verbatim(store):
    """正常帧零开销路径：不超限原样入库，不得加 _truncated 标记。"""
    await store.upsert_container("pod-ok", "img", "0.1.0", 60)
    msg = {"t": "hb", "interval": 60, "metrics": {"cpu": 3.2}, "custom": {"p1": {"x": 1}}}
    await store.record_hb("pod-ok", msg)
    raw = (await store.get_container("pod-ok"))["last_metrics"]
    assert json.loads(raw) == msg


# ---- QR1120-S11：upsert_container 的 image/agent_ver 对齐列宽 ----

async def test_upsert_container_clips_image_and_agent_ver(store):
    """首次建表路径与 set_online 同款 _clip：超长自报值在 MySQL 严格模式
    报 1406，整行 upsert 失败——主机永远建不出来。"""
    await store.upsert_container("pod-clip", "i" * 500, "v" * 100, 60)
    row = await store.get_container("pod-clip")
    assert len(row["image"]) == 200 and len(row["agent_ver"]) == 40


# ---- QR1120-S12：批量收敛 + 批量建台账 ----

async def test_finish_updates_reaching_converges_in_one_pass(store):
    """状态帧佐证收敛：目标版本不高于上报版本的在途台账全部 done，
    高于的保持不动；终态行不被改写（与逐条 finish_update 同语义）。"""
    await store.create_update("up-a", "h", "0.1.0", "0.2.0")
    await store.create_update("up-b", "h", "0.1.0", "0.3.0", status="queued")
    await store.create_update("up-c", "h", "0.1.0", "9.9.9")
    await store.create_update("up-d", "h", "0.1.0", "0.2.0", status="done")
    n = await store.finish_updates_reaching("h", "0.3.0")
    assert n == 2                                   # up-a / up-b
    assert (await store.get_update("up-a"))["status"] == "done"
    assert (await store.get_update("up-b"))["status"] == "done"
    assert (await store.get_update("up-c"))["status"] == "pending"
    assert (await store.get_update("up-d"))["status"] == "done"
    # 空收敛短路：没有可收敛的行时返回 0
    assert await store.finish_updates_reaching("h", "0.1.0") == 0
    assert await store.finish_updates_reaching("ghost", "9.9.9") == 0


async def test_create_updates_batch_fields_match_create_update(store):
    """批量版字段与 create_update 完全一致；返回 id 列表与 pods 一一对应。"""
    ids = await store.create_updates_batch(["h1", "h2", "h3"], "0.1.0", "0.2.0")
    assert len(ids) == 3
    for uid, pod in zip(ids, ["h1", "h2", "h3"]):
        row = await store.get_update(uid)
        assert row["pod"] == pod and row["from_version"] == "0.1.0"
        assert row["to_version"] == "0.2.0" and row["status"] == "pending"
        assert row["reason"] == "" and row["finished_at"] is None
        assert row["created_at"] > 0
    # 与单条版逐字段对齐（同一时刻建的两行除 id/pod 外完全一致）
    single = await store.create_update("up-single", "h1", "0.1.0", "0.2.0")
    a, b = await store.get_update(ids[0]), await store.get_update(single)
    assert {k: v for k, v in a.items() if k not in ("id", "created_at")} == \
           {k: v for k, v in b.items() if k not in ("id", "created_at")}


async def test_create_updates_batch_same_second_ids_unique(store):
    """批量内同秒同主机多行不得撞 id（QR-S46 同秒唯一性锁的批量版）：
    随机 token 区分，且总长 ≤ updates.id 的 String(48) 列宽。"""
    ids = await store.create_updates_batch(["dup-host"] * 50, "0.1.0", "9.9.9",
                                           status="queued")
    assert len(set(ids)) == 50
    for uid in ids:
        assert uid.startswith("up-") and len(uid) <= 48
        assert (await store.get_update(uid))["status"] == "queued"


async def test_create_updates_batch_empty_short_circuits(store):
    assert await store.create_updates_batch([], "0.1.0", "0.2.0") == []
    assert await store.create_updates_batch(None, "0.1.0", "0.2.0") == []