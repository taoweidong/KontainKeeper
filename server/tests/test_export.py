"""数据导出测试（A1 / P0-1）：CSV 契约、注入防护、行数上限、鉴权。

导出是「批量核验 + 生成报表」的落点，因此测试重点是**契约稳定性**：
列名顺序、BOM、注入前缀、与页面筛选的一致性。
"""
import base64
import csv
import io

import httpx
import pytest

from kk_server.main import create_app

ADMIN = "admin"
PASS = "export-pass"


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "export.db"),
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


def parse(text):
    """去掉 BOM 后按 csv 解析，返回 (表头, 数据行)。"""
    assert text.startswith("﻿"), "CSV 必须带 UTF-8 BOM，否则 Excel 中文乱码"
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    return rows[0], rows[1:]


async def _seed(store):
    await store.upsert_container("web-01", "img:1", "0.3.0", 60)
    await store.set_online("web-01", True, image="img:1", agent_ver="0.3.0")
    await store.record_hb("web-01", {"interval": 60, "metrics": {
        "cpu": 12.5, "mem_mb": 800.0, "disks": {"/": {"pct": 91.0}}}})
    ids, batch = await store.create_commands_batch(
        ["web-01"], "shell", ["echo", "hi"], 30, "admin")
    await store.mark_sent(ids[0])
    await store.append_result({"id": ids[0], "seq": 0, "done": True, "rc": 0,
                               "out_b64": base64.b64encode(b"hi").decode()})
    await store.add_audit("admin", "command_create", {"kind": "shell"})
    return ids, batch


async def test_export_commands_csv(api):
    ids, _ = await _seed(api.store)
    r = await api.client.get("/api/export/commands")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    header, rows = parse(r.text)
    assert header[0] == "id" and "argv" in header and "status" in header
    assert len(rows) == 1
    assert rows[0][0] == ids[0] and rows[0][1] == "web-01"
    assert rows[0][4] == "done"          # status
    assert "echo hi" == rows[0][3]       # argv 已归一为人读文本
    assert "out_tail" not in header      # 默认不带大字段


async def test_export_commands_include_tail(api):
    await _seed(api.store)
    r = await api.client.get("/api/export/commands", params={"include_tail": 1})
    header, rows = parse(r.text)
    assert header[-1] == "out_tail" and rows[0][-1] == "hi"


async def test_export_csv_injection(api):
    """argv 是用户可控内容：Excel 公式注入是真实攻击面，必须加前导单引号。"""
    ids, _ = await api.store.create_commands_batch(
        ["web-01"], "shell", ["=cmd|'/c calc'!A1"], 30, "admin")
    await _seed(api.store)
    r = await api.client.get("/api/export/commands", params={"pod": "web-01"})
    _, rows = parse(r.text)
    hit = [x for x in rows if x[0] == ids[0]]
    assert hit, rows
    assert hit[0][3].startswith("'="), hit[0][3]


async def test_export_limit_guard(api):
    r = await api.client.get("/api/export/commands", params={"limit": 99999})
    assert r.status_code == 400 and "上限" in r.json()["detail"]


async def test_export_requires_auth(tmp_path):
    app = create_app({"KK_ADMIN_USER": ADMIN, "KK_ADMIN_PASS": PASS,
                      "KK_DB_PATH": str(tmp_path / "noauth.db"),
                      "KK_WEB_DIR": str(tmp_path / "noweb")})
    await app.state.store.setup()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for path in ("/api/export/commands", "/api/export/audit",
                     "/api/export/hosts", "/api/export/metrics"):
            r = await client.get(path)
            assert r.status_code == 401, path
    await app.state.store.close()


async def test_export_hosts_and_metrics(api):
    await _seed(api.store)
    r = await api.client.get("/api/export/hosts")
    assert r.status_code == 200
    header, rows = parse(r.text)
    assert header[0] == "pod" and "disk_alert" in header
    assert len(rows) == 1 and rows[0][0] == "web-01"
    assert rows[0][8] == "1"            # disk 91% → 告警

    r = await api.client.get("/api/export/metrics", params={"pod": "web-01"})
    header, rows = parse(r.text)
    assert header == ["ts", "cpu", "mem_mb"] and len(rows) == 1
    assert rows[0][1] == "12.5"

    r = await api.client.get("/api/export/metrics")
    assert r.status_code == 400 and "pod" in r.json()["detail"]


async def test_export_audit(api):
    await _seed(api.store)
    r = await api.client.get("/api/export/audit")
    header, rows = parse(r.text)
    assert header == ["id", "ts", "actor", "action", "detail"]
    assert any(x[3] == "command_create" for x in rows)
    # actor 过滤生效
    r = await api.client.get("/api/export/audit", params={"actor": "nobody"})
    assert len(parse(r.text)[1]) == 0


async def test_export_filter_matches_page(api):
    """导出结果必须与页面筛选一致（P0-1 验收项）。"""
    await _seed(api.store)
    ids2, _ = await api.store.create_commands_batch(
        ["web-01"], "collect", {"items": ["cpu"]}, 30, "admin")
    r = await api.client.get("/api/export/commands", params={"kind": "collect"})
    _, rows = parse(r.text)
    assert len(rows) == 1 and rows[0][0] == ids2[0]


async def test_export_metrics_hourly_source(api):
    """>24h 走 hourly 聚合表：与 metrics_series 同源，导出与曲线语义必须一致。"""
    await _seed(api.store)
    # hour 必须是真实的当前小时桶：metrics_series 按 since//3600 过滤
    hour = int(__import__("time").time()) // 3600
    await api.store.exec_sql(
        "INSERT INTO kk_hourly (pod, hour, samples, cpu_avg, cpu_max, mem_avg,"
        " mem_max, last_metrics) VALUES ('web-01', %d, 3, 5.0, 9.0, 100.0, 200.0, '')"
        % hour)
    r = await api.client.get("/api/export/metrics",
                             params={"pod": "web-01", "hours": 48})
    _, rows = parse(r.text)
    assert len(rows) == 1 and rows[0][1] == "5.0"
