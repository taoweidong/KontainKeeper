"""QR1125 Phase 0 命令离线队列回归锁（store 层，泛化 updates 队列先例）。

queued 是 commands.status 的新取值：下发时主机离线 → queued；SSE 重连 flush 转 sent
（刷 sent_at 计时窗口）；超 KK_CMD_QUEUE_TTL → timeout。与在途命令的 timeout+slack
清扫（sweep_command_timeouts 扫 pending/sent/running）互不干扰——idx_cmd_inflight
部分索引谓词不变（test_dialects 锁）。
"""
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


async def _seed_host(store, pod="pod-q"):
    await store.upsert_container(pod, "img", "0.1.0", 60)


async def test_create_queued_and_list(store):
    """离线主机建 queued：queued_commands 按 created_at 升序返回补投输入。"""
    await _seed_host(store)
    ids, batch = await store.create_commands_batch(
        ["pod-q"], "shell", ["echo", "hi"], 30, "admin", status="queued")
    cid = ids[0]
    assert (await store.get_command(cid))["status"] == "queued"
    rows = await store.queued_commands("pod-q")
    assert len(rows) == 1 and rows[0]["id"] == cid
    assert rows[0]["kind"] == "shell" and rows[0]["timeout"] == 30
    assert rows[0]["batch_id"] == batch
    # 默认仍是 pending（既有路径行为不变）
    cid2 = await store.create_command("pod-q", "shell", ["echo"], 30, "admin")
    assert (await store.get_command(cid2))["status"] == "pending"


async def test_queued_commands_ordered_by_created(store):
    await _seed_host(store)
    ids, _ = await store.create_commands_batch(
        ["pod-q", "pod-q"], "shell", ["echo"], 30, "admin", status="queued")
    # 把第一条 created_at 拨早，验证升序（先排队先补投）
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                         {"a": int(time.time()) - 100, "b": ids[0]})
    rows = await store.queued_commands("pod-q")
    assert [r["id"] for r in rows] == ids


async def test_mark_command_queued_only_pending(store):
    """pending → queued；已 sent/done 的不拉回。"""
    await _seed_host(store)
    cid = await store.create_command("pod-q", "shell", ["echo"], 30, "admin")
    assert await store.mark_command_queued([cid]) == 1
    assert (await store.get_command(cid))["status"] == "queued"
    # 再调一次：已 queued 不是 pending，不重复转
    assert await store.mark_command_queued([cid]) == 0
    assert await store.mark_command_queued([]) == 0


async def test_mark_queued_sent_refreshes_timing_window(store):
    """queued → sent 且刷 sent_at：补投瞬间不被短 timeout 误判（仿 mark_update_pending）。"""
    await _seed_host(store)
    ids, _ = await store.create_commands_batch(
        ["pod-q"], "shell", ["echo"], 30, "admin", status="queued")
    cid = ids[0]
    # 排队很久（created_at 拨到 1h 前）
    old = int(time.time()) - 3600
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                         {"a": old, "b": cid})
    assert await store.mark_queued_commands_sent([cid]) == 1
    row = await store.get_command(cid)
    assert row["status"] == "sent"
    assert row["sent_at"] >= int(time.time()) - 5     # 计时窗口从补投时刻重新开始
    # 在途清扫拿 COALESCE(sent_at,created_at)+timeout：sent_at 新鲜 → 不会被扫
    assert await store.sweep_command_timeouts() == 0
    assert (await store.get_command(cid))["status"] == "sent"


async def test_mark_queued_sent_ignores_non_queued(store):
    await _seed_host(store)
    cid = await store.create_command("pod-q", "shell", ["echo"], 30, "admin")
    await store.mark_sent(cid)                        # sent
    assert await store.mark_queued_commands_sent([cid]) == 0
    assert (await store.get_command(cid))["status"] == "sent"


async def test_sweep_queued_ttl(store):
    """超 TTL 的 queued 收敛 timeout；未超的保持 queued。"""
    await _seed_host(store)
    ids, _ = await store.create_commands_batch(
        ["pod-q", "pod-q"], "shell", ["echo"], 30, "admin", status="queued")
    expired, fresh = ids
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                         {"a": int(time.time()) - 90000, "b": expired})  # > 24h
    n = await store.sweep_queued_command_timeouts(ttl=86400)
    assert n == 1
    assert (await store.get_command(expired))["status"] == "timeout"
    assert (await store.get_command(expired))["finished_at"]
    assert (await store.get_command(fresh))["status"] == "queued"


async def test_queued_not_swept_by_inflight_sweep(store):
    """queued 不在 sweep_command_timeouts 的 pending/sent/running 谓词里（互不干扰）。"""
    await _seed_host(store)
    ids, _ = await store.create_commands_batch(
        ["pod-q"], "shell", ["echo"], 30, "admin", status="queued")
    cid = ids[0]
    await store.exec_sql("UPDATE kk_commands SET created_at=:a WHERE id=:b",
                         {"a": int(time.time()) - 90000, "b": cid})
    # 在途清扫扫不到 queued（即使 created_at 很旧）
    assert await store.sweep_command_timeouts() == 0
    assert (await store.get_command(cid))["status"] == "queued"
    # 只有 queued 专用清扫能收敛它
    assert await store.sweep_queued_command_timeouts(ttl=86400) == 1