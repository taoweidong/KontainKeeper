"""存储层单测。"""
import json
import time

import pytest

from kk_server.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_container_and_heartbeat_roundtrip(store):
    store.upsert_container("pod-a", "img:1", "0.1.0", 60)
    store.record_hb("pod-a", {"t": "hb", "interval": 60,
                              "metrics": {"cpu": 3.2, "mem_mb": 500.0},
                              "custom": {"p1": {"x": 1}}})
    rows = store.list_containers()
    assert len(rows) == 1 and rows[0]["pod"] == "pod-a"
    assert rows[0]["last_seen"] > 0

    series, source = store.metrics_series("pod-a", 24)
    assert source == "raw" and len(series) == 1
    assert series[0]["cpu"] == 3.2 and series[0]["mem_mb"] == 500.0

    detail = store.get_container("pod-a")
    hb = json.loads(detail["last_metrics"])
    assert hb["custom"] == {"p1": {"x": 1}}


def test_command_lifecycle(store):
    store.upsert_container("pod-b", "img", "0.1.0", 60)
    cid = store.create_command("pod-b", "shell", ["echo", "hi"], 30, "admin")
    assert store.get_command(cid)["status"] == "pending"
    assert [r["id"] for r in store.pending_for("pod-b")] == [cid]

    store.mark_sent(cid)
    assert store.get_command(cid)["status"] == "sent"
    assert store.pending_for("pod-b") == []

    store.append_result({"id": cid, "seq": 0, "data_b64": "aGVsbG8g", "done": False})
    mid = store.get_command(cid)
    assert mid["status"] == "running" and mid["out"] == "hello "

    store.append_result({"id": cid, "seq": 1, "data_b64": "d29ybGQ=", "done": True,
                         "rc": 0, "timed_out": False, "elapsed_ms": 12})
    done = store.get_command(cid)
    assert done["status"] == "done" and done["out"] == "hello world"
    assert done["rc"] == 0 and done["elapsed_ms"] == 12


def test_admin_sessions(store):
    assert store.ensure_admin("admin", "pw1") is True
    assert store.ensure_admin("admin", "pw2") is False  # 已存在不覆盖
    assert store.verify_admin("admin", "pw1") is True
    assert store.verify_admin("admin", "wrong") is False
    assert store.verify_admin("nobody", "pw") is False

    tok = store.create_session("admin")
    assert store.get_session(tok) == "admin"
    store.delete_session(tok)
    assert store.get_session(tok) is None


def test_cleanup_aggregates_and_prunes(store):
    store.upsert_container("pod-c", "img", "0.1.0", 60)
    now = int(time.time())
    # 3 小时前的心跳 → 应被聚合并清理
    for i in range(5):
        store._exec("INSERT INTO heartbeats(pod,ts,cpu,mem_mb,metrics) VALUES(?,?,?,?,?)",
                    ("pod-c", now - 3 * 3600 + i * 60, 1.0 + i, 100.0 + i, "{}"))

    store.cleanup(now)
    rows, source = store.metrics_series("pod-c", 24 * 7)
    assert source == "hourly" and len(rows) >= 1
    assert rows[-1]["cpu"] is not None
    # 原始明细已被清出 raw 窗口
    raw, _ = store.metrics_series("pod-c", 2)
    assert raw == []


def test_lost_command_marking(store):
    store.upsert_container("pod-d", "img", "0.1.0", 60)
    cid = store.create_command("pod-d", "shell", ["x"], 30, "admin")
    store.mark_sent(cid)
    # created_at 被改成 2 小时前 → cleanup 应标记 lost
    store._exec("UPDATE commands SET created_at=? WHERE id=?", (int(time.time()) - 7200, cid))
    store.cleanup()
    assert store.get_command(cid)["status"] == "lost"
