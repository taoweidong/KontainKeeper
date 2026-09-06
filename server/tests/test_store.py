"""存储层单测。"""
import json
import time

import pytest
from sqlalchemy import select

from kk_server.models.store import Store
from kk_server.models.tables import commands, containers, heartbeats, hourly


@pytest.fixture
async def store(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    await st.setup()
    yield st
    await st.close()


async def test_container_and_heartbeat_roundtrip(store):
    await store.upsert_container("pod-a", "img:1", "0.1.0", 60)
    await store.record_hb("pod-a", {"t": "hb", "interval": 60,
                              "metrics": {"cpu": 3.2, "mem_mb": 500.0},
                              "custom": {"p1": {"x": 1}}})
    rows = await store.list_containers()
    assert len(rows) == 1 and rows[0]["pod"] == "pod-a"
    assert rows[0]["last_seen"] > 0

    series, source = await store.metrics_series("pod-a", 24)
    assert source == "raw" and len(series) == 1
    assert series[0]["cpu"] == 3.2 and series[0]["mem_mb"] == 500.0

    detail = await store.get_container("pod-a")
    hb = json.loads(detail["last_metrics"])
    assert hb["custom"] == {"p1": {"x": 1}}


async def test_command_lifecycle(store):
    """协议 v2 结果帧：out_b64 分块累加，done 盖章终态；全量输出走 command_output。"""
    await store.upsert_container("pod-b", "img", "0.1.0", 60)
    cid = await store.create_command("pod-b", "shell", ["echo", "hi"], 30, "admin")
    assert (await store.get_command(cid))["status"] == "pending"

    await store.mark_sent(cid)
    assert (await store.get_command(cid))["status"] == "sent"

    await store.append_result({"id": cid, "seq": 0, "total": 2, "out_b64": "aGVsbG8g", "done": False})
    mid = await store.get_command(cid)
    assert mid["status"] == "running" and mid["out_chunks"] == 1
    assert await store.command_output(cid) == "hello "

    await store.append_result({"id": cid, "seq": 1, "total": 2, "out_b64": "d29ybGQ=", "done": True,
                         "rc": 0, "timed_out": False, "elapsed_ms": 12, "truncated": False})
    done = await store.get_command(cid)
    assert done["status"] == "done" and done["rc"] == 0 and done["elapsed_ms"] == 12
    assert await store.command_output(cid) == "hello world"
    assert [r["out_tail"] for r in await store.list_commands(pod="pod-b")] == ["hello world"]


async def test_command_binary_output_survives(store):
    """二进制输出不再被 utf-8/replace 污染（评审 L2）。"""
    import base64
    await store.upsert_container("pod-bin", "img", "0.1.0", 60)
    cid = await store.create_command("pod-bin", "shell", ["cat"], 30, "admin")
    raw = bytes(range(256))
    await store.append_result({"id": cid, "seq": 0, "total": 1, "done": True, "rc": 0,
                         "out_b64": base64.b64encode(raw).decode()})
    assert base64.b64decode(await store.command_output(cid, as_text=False)) == raw


async def test_rc_minus_three_marks_truncated(store):
    """Agent 回 rc=-3（分块没能全部送达）时，命令要盖 truncated 而不是静默当成功。"""
    await store.upsert_container("pod-fail", "img", "0.1.0", 60)
    cid = await store.create_command("pod-fail", "shell", ["yes"], 30, "admin")
    await store.append_result({"id": cid, "seq": 3, "total": 80, "out_b64": "", "done": True,
                         "rc": -3, "timed_out": False, "elapsed_ms": 900})
    row = await store.get_command(cid)
    assert row["status"] == "done" and row["truncated"] == 1


async def test_sweep_converges_stuck_commands(store):
    await store.upsert_container("pod-s", "img", "0.1.0", 60)
    cid = await store.create_command("pod-s", "shell", ["echo"], 30, "admin")
    await store.mark_sent(cid)
    await store.exec_sql("UPDATE kk_commands SET sent_at=:a WHERE id=:b",
                           {"a": int(time.time()) - 600, "b": cid})
    assert await store.sweep_command_timeouts() >= 1
    row = await store.get_command(cid)
    assert row["status"] == "timeout" and row["finished_at"]


async def test_pending_without_sent_at_still_swept(store):
    """publish 失败停在 pending（sent_at 为 NULL）的行也必须被扫到。"""
    await store.upsert_container("pod-p", "img", "0.1.0", 60)
    cid = await store.create_command("pod-p", "shell", ["echo"], 30, "admin")
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                           {"a": int(time.time()) - 600, "b": cid})
    assert await store.sweep_command_timeouts() >= 1
    assert (await store.get_command(cid))["status"] == "timeout"


async def test_online_column_and_grace(store):
    """在线真相来自 retained status / LWT，不再查内存连接表。"""
    await store.set_online("pod-o", True, image="img", agent_ver="0.2.0")
    assert await store.is_online("pod-o") is True and await store.online_count() == 1
    row = await store.get_container("pod-o")
    assert row["image"] == "img" and row["last_seen"], "last_seen 必须是时间戳，列表要算 age"

    await store.set_online("pod-o", False)
    assert await store.is_online("pod-o") is False and await store.online_count() == 0

    # retained online 失真（Broker 崩了没发 LWT）时靠宽限兜底
    await store.set_online("pod-o", True)
    await store.exec_sql("UPDATE kk_containers SET status_ts=:a WHERE pod=:b",
                           {"a": 1000, "b": "pod-o"})
    assert await store.is_online("pod-o") is False
    assert await store.mark_stale_offline() >= 0


async def test_containers_exist_is_one_query(store):
    for p in ("a", "b", "c"):
        await store.upsert_container(p, "img", "0.2.0", 60)
    assert await store.containers_exist(["a", "c", "ghost"]) == {"a", "c"}


async def test_batch_create_is_single_transaction(store):
    await store.upsert_container("x", "img", "0.2.0", 60)
    ids = await store.create_commands_batch(["x"] * 50, "collect", {"items": ["cpu"]}, 30, "admin")
    assert len(set(ids)) == 50
    assert (await store.get_command(ids[0]))["kind"] == "collect"


async def test_admin_sessions(store):
    assert await store.ensure_admin("admin", "pw1") is True       # 首次创建
    assert await store.ensure_admin("admin", "pw1") is False      # 口令一致，不改动
    assert await store.verify_admin("admin", "pw1") is True
    # 密码轮换：KK_ADMIN_PASS 变化应被应用（覆盖旧口令）
    assert await store.ensure_admin("admin", "pw2") is True
    assert await store.verify_admin("admin", "pw2") is True
    assert await store.verify_admin("admin", "pw1") is False     # 旧口令失效
    assert await store.verify_admin("admin", "wrong") is False
    assert await store.verify_admin("nobody", "pw") is False

    tok = await store.create_session("admin")
    assert await store.get_session(tok) == "admin"
    await store.delete_session(tok)
    assert await store.get_session(tok) is None


async def test_cleanup_aggregates_and_prunes(store):
    await store.upsert_container("pod-c", "img", "0.1.0", 60)
    now = int(time.time())
    # 3 小时前的心跳 → 应被聚合并清理
    for i in range(5):
        await store.exec_sql(
            "INSERT INTO kk_heartbeats(pod,ts,cpu,mem_mb,metrics)"
            " VALUES(:p,:t,:c,:m,:x)",
            {"p": "pod-c", "t": now - 3 * 3600 + i * 60, "c": 1.0 + i,
             "m": 100.0 + i, "x": "{}"})

    await store.cleanup(now)
    rows, source = await store.metrics_series("pod-c", 24 * 7)
    assert source == "hourly" and len(rows) >= 1
    assert rows[-1]["cpu"] is not None
    # 原始明细已被清出 raw 窗口
    raw, _ = await store.metrics_series("pod-c", 2)
    assert raw == []


async def test_summary_view_written_with_heartbeat(store):
    """B6：心跳顺手落摘要列，列表页不必再解析完整 last_metrics。"""
    await store.upsert_container("pod-sum", "img:1", "0.1.0", 60)
    await store.record_hb("pod-sum", {
        "interval": 60, "metrics": {"cpu": 12.5, "mem_mb": 800.0,
                                    "disks": {"/": {"pct": 91.0}, "/data": {"pct": 40.0}}}})
    rows = await store.list_containers(view="summary")
    assert len(rows) == 1
    row = rows[0]
    assert row["cpu"] == 12.5 and row["mem_mb"] == 800.0
    # 磁盘告警取的是「最满的那块盘」，不是第一块
    assert row["disk_pct"] == 91.0
    assert set(row) == {"pod", "image", "agent_ver", "hb_interval", "online",
                        "last_seen", "cpu", "mem_mb", "disk_pct"}, \
        "摘要视图不该把 last_metrics 这种大字段带出来"


async def test_summary_tolerates_broken_metrics(store):
    """坏数据不能让整帧心跳落不了库：摘不到的标量落 None / 0。"""
    await store.upsert_container("pod-bad", "img", "0.1.0", 60)
    await store.record_hb("pod-bad", {"metrics": {"cpu": "oops", "disks": "nope"}})
    row = (await store.list_containers(view="summary"))[0]
    assert row["cpu"] is None and row["disk_pct"] == 0.0
    # 完整视图仍然可用：last_metrics 原样保留
    assert "oops" in (await store.get_container("pod-bad"))["last_metrics"]


async def test_list_containers_view_param_guarded(store):
    await store.upsert_container("pod-v", "img", "0.1.0", 60)
    assert len(await store.list_containers()) == 1
    assert len(await store.list_containers("summary")) == 1
    # 未知 view 不能静默退化成全列查询——那会让前端以为拿到了摘要
    with pytest.raises(ValueError):
        await store.list_containers("nope")


async def test_schema_migration_adds_missing_columns(tmp_path):
    """既有库升级：create_all 不加列，setup() 必须自己 ALTER 补上。"""
    import sqlalchemy as sa

    path = str(tmp_path / "legacy.db")
    # 先造一个「旧版本」的库：containers 没有摘要列，commands 没有 out_purged
    legacy = sa.MetaData()
    sa.Table("kk_containers", legacy,
             sa.Column("pod", sa.String(120), primary_key=True),
             sa.Column("image", sa.String(200), nullable=False, server_default=""),
             sa.Column("agent_ver", sa.String(40), nullable=False, server_default=""),
             sa.Column("hb_interval", sa.Integer, nullable=False, server_default="60"),
             sa.Column("first_seen", sa.BigInteger, nullable=False),
             sa.Column("last_seen", sa.BigInteger, nullable=False),
             sa.Column("last_metrics", sa.Text, nullable=False),
             sa.Column("online", sa.Integer, nullable=False, server_default="0"),
             sa.Column("status_ts", sa.BigInteger, nullable=False, server_default="0"))
    eng = sa.create_engine("sqlite:///" + path)
    legacy.create_all(eng)
    eng.dispose()

    st = Store(path)
    await st.setup()
    async with st.engine.begin() as conn:
        cols = await st._table_columns(conn, "kk_containers")
        cmd_cols = await st._table_columns(conn, "kk_commands")
    assert {"cpu", "mem_mb", "disk_pct"} <= cols, cols
    assert "out_purged" in cmd_cols, cmd_cols
    # 补列后功能照常：心跳能写、命令能建
    await st.upsert_container("old-pod", "img", "0.1.0", 60)
    await st.record_hb("old-pod", {"metrics": {"cpu": 1.0}})
    assert (await st.list_containers(view="summary"))[0]["cpu"] == 1.0
    await st.close()


async def test_cleanup_purges_stale_command_output(store):
    """B3：命令输出按 7 天清，状态行保留 30 天——两条保留期互不影响。"""
    await store.upsert_container("pod-out", "img", "0.1.0", 60)
    cid = await store.create_command("pod-out", "shell", ["echo"], 30, "admin")
    await store.append_result({"id": cid, "seq": 0, "total": 1, "done": True, "rc": 0,
                         "out_b64": "aGVsbG8="})
    now = int(time.time())
    await store.exec_sql("UPDATE kk_commands SET finished_at=:a WHERE id=:b",
                         {"a": now - 8 * 86400, "b": cid})
    stats = await store.cleanup(now)
    assert stats["outputs_purged"] == 1
    row = await store.get_command(cid)
    # 状态行还在（8 天 < 30 天），但输出已清理且留下可解释的标记
    assert row["status"] == "done" and row["rc"] == 0
    assert row["out_purged"] == 1 and await store.command_output(cid) == ""
    # 再跑一次不能重复计数
    assert (await store.cleanup(now))["outputs_purged"] == 0
    # 满 30 天后整行才被删
    assert (await store.cleanup(now + 31 * 86400))["commands_deleted"] == 1
    assert await store.get_command(cid) is None


async def test_cleanup_prunes_hourly_beyond_retention(store):
    """hourly 只增不减会让表无限膨胀：90 天外的聚合行必须回收。"""
    now = int(time.time())
    old_hour = (now - 120 * 86400) // 3600
    fresh_hour = (now - 3600) // 3600
    for h in (old_hour, fresh_hour):
        await store.exec_sql(
            "INSERT INTO kk_hourly(pod,hour,samples,cpu_avg,cpu_max,mem_avg,mem_max,"
            "last_metrics) VALUES(:p,:h,1,1.0,2.0,3.0,4.0,'{}')",
            {"p": "pod-h", "h": h})
    stats = await store.cleanup(now)
    assert stats["hourly_deleted"] == 1
    left = await store._all(select(hourly.c.hour))
    assert [r["hour"] for r in left] == [fresh_hour]


async def test_cleanup_deletes_heartbeats_in_batches(store):
    """分批删除：一次清掉远超 batch 的行，不能因为 LIMIT 只删一批就返回。"""
    await store.upsert_container("pod-bulk", "img", "0.1.0", 60)
    now = int(time.time())
    old = now - 5 * 86400
    rows = [{"p": "pod-bulk", "t": old + i, "c": 1.0, "m": 1.0, "x": "{}"}
            for i in range(23)]        # 23 > 默认 batch 5 的倍数，逼出多轮循环
    await store.exec_sql(
        "INSERT INTO kk_heartbeats(pod,ts,cpu,mem_mb,metrics)"
        " VALUES(:p,:t,:c,:m,:x)", rows)
    # 新心跳要留下
    await store.record_hb("pod-bulk", {"metrics": {"cpu": 9.0}})
    stats = await store.cleanup(now, raw_days=2)
    assert stats["heartbeats_deleted"] == 23
    left = await store._all(select(heartbeats.c.cpu))
    assert [r["cpu"] for r in left] == [9.0]


async def test_counts_feeds_stats_endpoint(store):
    """C5：stats 面板要的行数与状态分布。"""
    await store.upsert_container("c1", "img", "0.1.0", 60)
    await store.set_online("c1", True)
    await store.upsert_container("c2", "img", "0.1.0", 60)
    cid = await store.create_command("c1", "shell", ["echo"], 30, "admin")
    await store.record_hb("c1", {"metrics": {"cpu": 1.0}})
    counts = await store.counts()
    assert counts["hosts"] == {"total": 2, "online": 1}
    assert counts["commands"] == {"pending": 1}
    assert counts["storage"]["heartbeats"] == 1
    await store.mark_sent(cid)
    assert (await store.counts())["commands"] == {"sent": 1}


async def test_token_revocation(store):
    assert await store.is_token_revoked("t1") is False
    await store.revoke_token("t1")
    await store.revoke_token("t1")  # 幂等
    assert await store.is_token_revoked("t1") is True
    assert await store.revoked_tokens() == ["t1"]
    await store.restore_token("t1")
    assert await store.is_token_revoked("t1") is False
    assert await store.revoked_tokens() == []


async def test_record_hb_refreshes_status_ts(store):
    """P1-4：长驻主机仅靠心跳也要保持在线。record_hb 必须刷新 status_ts，
    否则 mark_stale_offline 会在数个周期后把它误判离线（列表抖动）。"""
    await store.upsert_container("pod-hb", "img", "0.1.0", 60)
    await store.set_online("pod-hb", True)
    # 模拟「早已上线、很久没发 status 帧」：把 status_ts 推到很久以前
    await store.exec_sql("UPDATE kk_containers SET status_ts=:a WHERE pod=:b",
                         {"a": 1000, "b": "pod-hb"})
    assert await store.is_online("pod-hb") is False  # 宽限兜底已判离线

    # 持续心跳应当把它重新拉回在线（仅 last_seen 不刷新 status_ts 时做不到）
    await store.record_hb("pod-hb", {"interval": 60, "metrics": {"cpu": 1.0}})
    assert await store.is_online("pod-hb") is True
    assert (await store.get_container("pod-hb"))["status_ts"] > 1000


async def test_append_result_is_idempotent_on_redelivery(store):
    """P1-6：QoS1「至少一次」，Broker 重投同 seq 块会重复拼接导致输出翻倍。
    以 last_seq 为水位，同 seq 重投必须被幂等丢弃。"""
    await store.upsert_container("pod-dup", "img", "0.1.0", 60)
    cid = await store.create_command("pod-dup", "shell", ["echo"], 30, "admin")
    base = {"id": cid, "total": 2}

    await store.append_result({**base, "seq": 0, "out_b64": "QUJD", "done": False})
    await store.append_result({**base, "seq": 1, "out_b64": "REVG", "done": True, "rc": 0})
    assert await store.command_output(cid) == "ABCDEF"

    # 重投：同 seq 块再发一次，输出不得翻倍
    await store.append_result({**base, "seq": 0, "out_b64": "QUJD", "done": False})
    await store.append_result({**base, "seq": 1, "out_b64": "REVG", "done": True, "rc": 0})
    assert await store.command_output(cid) == "ABCDEF"
    row = await store.get_command(cid)
    assert row["out_chunks"] == 2 and row["last_seq"] == 1

    # 无 seq 的帧仍按原语义无条件追加（向后兼容）
    await store.append_result({"id": cid, "out_b64": "R0hJ", "done": True})
    assert await store.command_output(cid) == "ABCDEFGHI"


async def test_lost_command_marking(store):
    await store.upsert_container("pod-d", "img", "0.1.0", 60)
    cid = await store.create_command("pod-d", "shell", ["x"], 30, "admin")
    await store.mark_sent(cid)
    # created_at 被改成 2 小时前 → cleanup 应标记 lost
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                           {"a": int(time.time()) - 7200, "b": cid})
    await store.cleanup()
    assert (await store.get_command(cid))["status"] == "lost"
