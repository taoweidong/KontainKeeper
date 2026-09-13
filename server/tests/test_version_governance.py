"""D1 版本治理：落后判定、最新版本可见性与协议文档一致性。

覆盖的三件事分别对应三类真实故障：
- **落后判定必须由服务端算**（D1.3）：前端没有 `version_lt` 的等价实现，让它自己比
  就是两套语义各自演化（多段比较 `1.10.0` vs `1.9.0` 最先出错）。同时锁死「KV 只读
  一次」——每行查一次就是与 P1-1 同型的 N+1。
- **「最新版本是什么」要对运维可见**（D1.2）：500 台规模下若客户端无从得知版本号，
  需求②的「升级到最新版本」在 UI 上根本无从表达。
- **文档不漂移**（D1.4）：`proto/messages.md` 的 update 示例版本必须始终高于
  `AGENT_VER`（它是「服务端推着 Agent 升级」的方向）。
"""
import re
from pathlib import Path

import httpx
import pytest

from kk_agent.config import AGENT_VER
from kk_server.main import create_app
from kk_server.models.version import count_outdated, version_lt

ADMIN = "admin"
PASS = "vg-pass"
ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "vg.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    token = await store.create_session(ADMIN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield type("Ctx", (), {"client": client, "store": store, "app": app})
    await store.close()


async def _upload_latest(store, ver):
    await store.set_agent_latest({"version": ver, "sha256": "x" * 8, "size": 1,
                                  "uploaded_at": 1690000000})


# ---- 纯函数：落后判定 ----

def test_count_outdated_treats_unknown_version_as_outdated():
    """空版本（从未上报过）也算落后：它不可能是最新，且确实需要被推升级。"""
    counts = {"0.2.0": 3, "": 2, "0.3.0": 1}
    assert count_outdated(counts, "0.3.0") == 5
    assert count_outdated(counts, "1.10.0") == 6   # 多段比较：0.3.0 < 1.10.0
    assert count_outdated(counts, "") == 0, "未上传任何版本时「落后」无从定义"


# ---- D1.3：列表带落后标记 ----

async def test_hosts_summary_carries_outdated_flag(api):
    await api.store.upsert_container("old-1", "img:1", "0.2.0", 60)
    await api.store.upsert_container("new-1", "img:1", "0.3.0", 60)
    await _upload_latest(api.store, "0.3.0")

    # KV 读次数替身：500 台逐行查一次即为 N+1，与 P1-1 同型
    calls = {"n": 0}
    real = api.store.get_agent_latest

    async def counting():
        calls["n"] += 1
        return await real()

    api.store.get_agent_latest = counting
    try:
        r = await api.client.get("/api/containers", params={"view": "summary"})
    finally:
        api.store.get_agent_latest = real
    assert r.status_code == 200, r.text

    body = r.json()
    rows = {i["pod"]: i for i in body["items"]}
    assert rows["old-1"]["agent_outdated"] is True
    assert rows["new-1"]["agent_outdated"] is False
    assert rows["old-1"]["latest_agent_ver"] == "0.3.0"
    assert body["latest_agent_ver"] == "0.3.0" and body["outdated"] == 1
    assert calls["n"] == 1, "500 台列表只允许读一次 KV（防 N+1 回归）"


async def test_no_uploaded_version_means_nobody_outdated(api):
    """未上传过版本时「落后」无从定义 —— 显示「未上传」而非「全都落后」。"""
    await api.store.upsert_container("old-1", "img:1", "0.2.0", 60)
    r = await api.client.get("/api/containers", params={"view": "summary"})
    row = r.json()["items"][0]
    assert row["latest_agent_ver"] == "" and row["agent_outdated"] is False


async def test_container_detail_carries_outdated_flag(api):
    await api.store.upsert_container("old-1", "img:1", "0.2.0", 60)
    await _upload_latest(api.store, "0.3.0")
    body = (await api.client.get("/api/containers/old-1")).json()
    assert body["agent_outdated"] is True and body["latest_agent_ver"] == "0.3.0"


# ---- D1.2：最新版本对运维可见 ----

async def test_agent_current_requires_admin(api):
    saved = api.client.headers.pop("Authorization")
    try:
        r = await api.client.get("/api/system/agent/current")
    finally:
        api.client.headers["Authorization"] = saved
    assert r.status_code in (401, 403), r.text


async def test_agent_current_reports_latest_and_outdated(api):
    await api.store.upsert_container("old-1", "img:1", "0.2.0", 60)
    await api.store.upsert_container("new-1", "img:1", "0.3.0", 60)
    await _upload_latest(api.store, "0.3.0")

    body = (await api.client.get("/api/system/agent/current")).json()
    assert body["version"] == "0.3.0"
    assert body["hosts_total"] == 2 and body["hosts_outdated"] == 1
    assert body["sha256"] == "x" * 8 and body["uploaded_at"] == 1690000000


async def test_agent_current_without_upload_is_not_an_error(api):
    """没上传过版本要回空串而不是报错：总览页据此显示「未上传」。"""
    body = (await api.client.get("/api/system/agent/current")).json()
    assert body["version"] == "" and body["hosts_outdated"] == 0


async def test_stats_carries_agent_version_counts(api):
    """总览页卡片直接消费这两个键，不在前端做版本比较。"""
    await api.store.upsert_container("old-1", "img:1", "0.2.0", 60)
    await _upload_latest(api.store, "0.3.0")
    body = (await api.client.get("/api/system/stats")).json()
    assert body["agent_latest_ver"] == "0.3.0" and body["agents_outdated"] == 1


# ---- D1.4：协议文档不漂移 ----

def test_proto_doc_example_version_in_sync():
    """`update` 帧示例的 version 必须**高于**当前 AGENT_VER。

    它是「服务端推着 Agent 升级」的方向，所以不能等于 AGENT_VER；而一旦 AGENT_VER
    涨过它，示例就变成了「推一个更旧的版本」，是纯粹的误导。
    """
    doc = (ROOT / "proto" / "messages.md").read_text(encoding="utf-8")
    m = re.search(r'"kind":"update","version":"([^"]+)"', doc)
    assert m, "协议文档里找不到 update 帧示例"
    example = m.group(1)
    assert version_lt(AGENT_VER, example), (
        "proto/messages.md 的 update 示例版本 %r 不高于 AGENT_VER %r —— 文档已漂移"
        % (example, AGENT_VER))
