"""存储层单测。"""
import json
import time

import pytest

from kk_server.models.store import Store


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


async def test_token_revocation(store):
    assert await store.is_token_revoked("t1") is False
    await store.revoke_token("t1")
    await store.revoke_token("t1")  # 幂等
    assert await store.is_token_revoked("t1") is True
    assert await store.revoked_tokens() == ["t1"]
    await store.restore_token("t1")
    assert await store.is_token_revoked("t1") is False
    assert await store.revoked_tokens() == []


async def test_lost_command_marking(store):
    await store.upsert_container("pod-d", "img", "0.1.0", 60)
    cid = await store.create_command("pod-d", "shell", ["x"], 30, "admin")
    await store.mark_sent(cid)
    # created_at 被改成 2 小时前 → cleanup 应标记 lost
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                           {"a": int(time.time()) - 7200, "b": cid})
    await store.cleanup()
    assert (await store.get_command(cid))["status"] == "lost"
