"""QR1120-S13..S17：Agent 自更新接口的内存/竞态/可观测性/批量/版本校验修复锁。

- S13 上传流式落盘 + 增量哈希：整包不再双份驻留内存，拒绝路径清理临时文件；
- S14 Windows 共享冲突短退避重试（_replace_retry），下载路径不加锁；
- S15 .prev 保留失败必须 warning + agent_prev_lost 审计，不许静默；
- S16 批量升级台账走 create_updates_batch（按 from_version×在线态分组），
      逐台 dispatch_upgrade 的 N 次串行 DB 往返不再出现；
- S17 版本号全匹配白名单 ^\\d+(\\.\\d+)*$。

不依赖真实 Broker / 网络 / 真实时钟 / 执行顺序：桥接用 FakePublish 替身，
退避间隔 monkeypatch 归零，库用 tmp_path SQLite（或 KK_TEST_DB_URL 统一库）。
"""
import hashlib
import hmac
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from kk_server.controllers import agent_update as au

# 复用既有夹具助手（tests 目录在 sys.path 上，conftest 已注入 src 路径）
from test_agent_update import (ADMIN_PASS, ADMIN_USER, GOOD_CLIENT,
                               _admin_token, _make_app, _upload)
from test_controlled_upgrade import FakePublish, _seed, bridge_app  # noqa: F401


def _bin_dir(tmp_path):
    return str(tmp_path / "bin")


def _temp_residue(tmp_path):
    d = _bin_dir(tmp_path)
    if not os.path.isdir(d):
        return []
    return [f for f in os.listdir(d) if f.startswith(".kk-upload-")]


# ---- QR1120-S17：版本号白名单 ----

def test_upload_version_whitelist(tmp_path):
    """QR1120-S17：旧校验只查首字符是数字，"1abc" 可入库——全网 Agent 会拿到
    一个永远无法比较的升级目标。现全匹配 \\d+(\\.\\d+)*：上传是版本号唯一写入
    入口（回滚互换既有清单、批量升级目标必须等于清单版本），同口径收口在此。
    拒绝路径零副作用（不落盘、不写清单）。"""
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        for bad in ("1abc", "1.2abc", "1..2", "1.", ".5", "v1", " 1.0",
                    "1.0\n", "1.0-beta", "0x10"):
            r = _upload(client, token, b"\x7fELF-x", bad)
            assert r.status_code == 400, (bad, r.text)
        assert not os.path.exists(os.path.join(_bin_dir(tmp_path), "kk-agent"))
        for good in ("1", "1.2", "10.0.0"):
            assert _upload(client, token, b"\x7fELF-ok", good).status_code == 200, good


# ---- QR1120-S13：流式上传 ----

def test_upload_multichunk_stream_hashes_and_no_temp_residue(tmp_path):
    """QR1120-S13：大于 _CHUNK 的包走多轮流式分块——sha256/HMAC 增量计算结果
    必须与整包一次算完全一致（API 契约不变），成功后临时文件零残留。"""
    payload = bytes(range(256)) * 4096   # 1MB，跨 4 个 256KB 分块
    assert len(payload) > au._CHUNK
    with TestClient(_make_app(tmp_path, extra_env={"KK_UPDATE_HMAC_KEY": "s3cret"}),
                    client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        r = _upload(client, token, payload, "3.0.0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["size"] == len(payload)
        assert body["sha256"] == hashlib.sha256(payload).hexdigest()
        assert body["sig"] == hmac.new(b"s3cret", payload, hashlib.sha256).hexdigest()
        assert client.get("/api/system/agent/download").content == payload
        assert _temp_residue(tmp_path) == []


def test_upload_reject_paths_clean_temp_and_keep_current(tmp_path, monkeypatch):
    """QR1120-S13：超限/空包在流式过程中即断——临时文件必须清理，且当前在架
    二进制与 .prev 不受任何影响（旧实现拒绝前已把整包读进内存）。"""
    monkeypatch.setattr(au, "MAX_BIN_BYTES", 8)
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        assert _upload(client, token, b"ok", "1.0.0").status_code == 200
        assert _upload(client, token, b"x" * 9, "2.0.0").status_code == 413
        assert _upload(client, token, b"", "2.0.0").status_code == 400
        dest = os.path.join(_bin_dir(tmp_path), "kk-agent")
        with open(dest, "rb") as f:
            assert f.read() == b"ok"
        assert not os.path.exists(dest + ".prev")
        assert _temp_residue(tmp_path) == []


# ---- QR1120-S14：Windows 共享冲突短退避 ----

def _patch_win(monkeypatch, replace):
    """模拟 Windows 共享冲突环境：os.name=nt + 退避归零（不依赖真实时钟）。"""
    monkeypatch.setattr(au.os, "name", "nt")
    monkeypatch.setattr(au, "_SWAP_BACKOFF", 0)
    monkeypatch.setattr(au.os, "replace", replace)


def test_replace_retry_recovers_from_transient_sharing_conflict(tmp_path, monkeypatch):
    """QR1120-S14：下载线程正读文件时 os.replace 撞 PermissionError，短退避
    重试后成功——不能一次冲突就把上传/回滚打成 500。"""
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    with open(src, "wb") as f:
        f.write(b"new")
    calls = []
    real = os.replace

    def flaky(s, d):
        calls.append((s, d))
        if len(calls) < 3:
            raise PermissionError(13, "sharing violation")
        real(s, d)

    _patch_win(monkeypatch, flaky)
    au._replace_retry(src, dst)
    assert len(calls) == 3
    with open(dst, "rb") as f:
        assert f.read() == b"new"


def test_replace_retry_gives_up_after_attempts(tmp_path, monkeypatch):
    """QR1120-S14：重试有界（_SWAP_ATTEMPTS 次），仍失败才报错——不无限重试。"""
    calls = []

    def always_busy(s, d):
        calls.append((s, d))
        raise PermissionError(13, "sharing violation")

    _patch_win(monkeypatch, always_busy)
    with pytest.raises(PermissionError):
        au._replace_retry(str(tmp_path / "a"), str(tmp_path / "b"))
    assert len(calls) == au._SWAP_ATTEMPTS


def test_replace_retry_no_retry_on_posix_or_other_errors(tmp_path, monkeypatch):
    """QR1120-S14：POSIX 读旧 inode 天然安全，PermissionError 是真权限故障，
    不重试；其余 OSError（如 FileNotFoundError）在任何平台都立即抛。"""
    calls = []

    def fail(s, d):
        calls.append((s, d))
        raise PermissionError(13, "denied")

    monkeypatch.setattr(au.os, "name", "posix")
    monkeypatch.setattr(au, "_SWAP_BACKOFF", 0)
    monkeypatch.setattr(au.os, "replace", fail)
    with pytest.raises(PermissionError):
        au._replace_retry("a", "b")
    assert len(calls) == 1

    calls.clear()

    def missing(s, d):
        calls.append((s, d))
        raise FileNotFoundError(2, "no such file")

    _patch_win(monkeypatch, missing)
    with pytest.raises(FileNotFoundError):
        au._replace_retry("a", "b")
    assert len(calls) == 1


def test_rollback_retries_when_download_holds_file(tmp_path, monkeypatch):
    """QR1120-S14：回滚 swap 的「另一版上位」一步撞上正在读的下载线程，
    短退避后完成互换——二进制与清单一起换回上一版（语义与无冲突时一致）。"""
    with TestClient(_make_app(tmp_path), client=GOOD_CLIENT) as client:
        token = _admin_token(client)
        v1, v2 = b"\x7fELF-v1", b"\x7fELF-v2-longer"
        assert _upload(client, token, v1, "0.3.0").status_code == 200
        assert _upload(client, token, v2, "0.4.0").status_code == 200

        dest = os.path.join(_bin_dir(tmp_path), "kk-agent")
        real = os.replace
        hits = []

        def busy_dest(s, d):
            if d == dest:
                hits.append(d)
                if len(hits) < 3:
                    raise PermissionError(13, "being read by download")
            return real(s, d)

        _patch_win(monkeypatch, busy_dest)
        r = client.post("/api/system/agent/rollback",
                        headers={"Authorization": "Bearer %s" % token})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == "0.3.0"
        assert len(hits) == 3
        assert client.get("/api/system/agent/download").content == v1


# ---- QR1120-S15：.prev 保留失败必须留痕 ----

async def test_prev_retention_failure_audited_and_logged(tmp_path, monkeypatch):
    """QR1120-S15：旧实现 `except OSError: pass` → had_prev=False → 清空
    agent_prev，无日志无审计，回滚能力悄悄消失、下次 rollback 404 无从排查。
    现失败必须 log.warning + agent_prev_lost 审计（detail 带版本/路径/原因），
    agent_upload 审计照常（审计红线不可绕过）。"""
    app = _make_app(tmp_path)
    store = app.state.store
    await store.setup()
    await store.ensure_admin(ADMIN_USER, ADMIN_PASS)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            tok = (await c.post("/api/login",
                                json={"username": ADMIN_USER, "password": ADMIN_PASS})
                   ).json()["token"]
            h = {"Authorization": "Bearer %s" % tok}

            def up(payload, ver):
                return c.post("/api/system/agent", headers=h,
                              files={"file": ("kk-agent", payload)},
                              data={"version": ver})

            assert (await up(b"v1", "0.3.0")).status_code == 200
            assert (await up(b"v2", "0.4.0")).status_code == 200
            assert await store.get_agent_prev()   # 正常路径 .prev 清单在

            # 让「换 .prev」永久失败（模拟下载线程长期占用 / 权限故障）
            real = os.replace

            def fail_prev(s, d):
                if d.endswith(".prev"):
                    raise PermissionError(13, "prev being read")
                return real(s, d)

            _patch_win(monkeypatch, fail_prev)
            records = []
            # loguru 的 sink 收到的是格式化字符串，取 .record 才拿到结构化记录
            sink = logger.add(lambda m: records.append(m.record), level="WARNING")
            try:
                r = await up(b"v3", "0.5.0")
            finally:
                logger.remove(sink)
            assert r.status_code == 200, r.text

            audits = await store.list_audit(limit=50)
            lost = [a for a in audits if a["action"] == "agent_prev_lost"]
            assert len(lost) == 1
            detail = json.loads(lost[0]["detail"])
            assert detail["version"] == "0.5.0"
            assert "prev being read" in detail["reason"]
            assert detail["path"].endswith(".prev")
            assert [a for a in audits if a["action"] == "agent_upload"]
            assert any("保留上一版" in rec["message"] for rec in records)

            # 清单口径不变：prev 被清（fail-closed），回滚 404 而非 sha 错配
            assert await store.get_agent_prev() is None
            assert (await c.post("/api/system/agent/rollback",
                                 headers=h)).status_code == 404
    finally:
        await store.close()


# ---- QR1120-S16：批量升级台账单事务化 ----

async def test_upgrade_ledger_batched_not_per_host(bridge_app):  # noqa: F811
    """QR1120-S16：旧实现循环内逐台 dispatch_upgrade（每台一次 create_update
    事务 + publish），500 台 = 500 次串行 DB 往返。现台账按 (from_version,
    在线态) 分组走 create_updates_batch——组数与主机数无关；逐台 create_update
    不再出现。在线/离线分流、顺序、queued 标记、帧 id=台账主键、审计语义不变。"""
    store, bridge = bridge_app.store, bridge_app.bridge
    await _seed(store, "h1", "0.1.0", online=True)
    await _seed(store, "h2", "0.1.0", online=True)
    await _seed(store, "h3", "0.2.0", online=False)
    await _seed(store, "h4", "0.1.0", online=True)
    await store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})

    batch_calls, single_calls = [], []
    orig_batch, orig_single = store.create_updates_batch, store.create_update

    async def spy_batch(pods, from_version, to_version, status="pending"):
        batch_calls.append((list(pods), from_version, to_version, status))
        return await orig_batch(pods, from_version, to_version, status=status)

    async def spy_single(*a, **k):
        single_calls.append(a)
        return await orig_single(*a, **k)

    store.create_updates_batch, store.create_update = spy_batch, spy_single

    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["h1", "h2", "h3", "h4"]})
    body = r.json()
    assert body["ok"] and not body["skipped"]
    assert [a["host"] for a in body["accepted"]] == ["h1", "h2", "h3", "h4"]
    assert [a["queued"] for a in body["accepted"]] == [False, False, True, False]

    # 两个分组：(0.1.0, 在线) 与 (0.2.0, 离线)——而不是 4 次单条事务
    assert single_calls == []
    assert len(batch_calls) == 2
    by_key = {(fv, st): pods for pods, fv, _tv, st in batch_calls}
    assert by_key[("0.1.0", "pending")] == ["h1", "h2", "h4"]
    assert by_key[("0.2.0", "queued")] == ["h3"]
    assert all(tv == "9.9.9" for _p, _fv, tv, _st in batch_calls)

    rows = {row["pod"]: row for row in await store.list_updates()}
    assert len(rows) == 4
    assert rows["h3"]["status"] == "queued"
    assert rows["h3"]["from_version"] == "0.2.0"
    for pod in ("h1", "h2", "h4"):
        assert rows[pod]["status"] == "pending"
        assert rows[pod]["from_version"] == "0.1.0"
    for a in body["accepted"]:
        assert rows[a["host"]]["id"] == a["ledger_id"]
        assert a["ledger_id"].startswith("up-")

    # 只有在线主机投帧，且帧 id == 台账主键（回执才有地方落）
    sent = {json.loads(m["payload"])["id"]: m["topic"]
            for m in bridge.cli.msgs if m["topic"].endswith("/cmd")}
    assert set(sent) == {rows[p]["id"] for p in ("h1", "h2", "h4")}
    assert not [m for m in bridge.cli.msgs if m["topic"].endswith("/h3/cmd")]

    audit = await store.list_audit(limit=20)
    batch_audit = [a for a in audit if a["action"] == "agent_upgrade_batch"]
    assert len(batch_audit) == 1
    assert json.loads(batch_audit[0]["detail"])["accepted"] == ["h1", "h2", "h3", "h4"]


async def test_upgrade_publish_failure_keeps_dispatch_semantics(bridge_app):  # noqa: F811
    """QR1120-S16：改走 publish_update 后，发布失败语义必须与 dispatch_upgrade
    完全一致——台账落 failed(publish_failed)，主机仍出现在 accepted（受理口径
    不变），不静默吞掉。"""
    store, bridge = bridge_app.store, bridge_app.bridge
    bridge.cli = FakePublish(rc=1)   # 非 MQTT_ERR_SUCCESS / NO_CONN → 入队失败
    await _seed(store, "h1", "0.1.0", online=True)
    await store.set_agent_latest(
        {"version": "9.9.9", "sha256": "x", "size": 1, "uploaded_at": 0})
    r = await bridge_app.client.post("/api/system/agent/upgrade",
                                     json={"hosts": ["h1"]})
    body = r.json()
    assert body["ok"] and len(body["accepted"]) == 1
    rows = await store.list_updates()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["reason"] == "publish_failed"