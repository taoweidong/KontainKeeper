"""跨端契约快照（第二轮 D.2，QR29-§5）：把字段级偏差钉成用例，装上漂移防护机制。

后端全部返回裸 dict（QR29-S19），response_model 缺位时契约漂移只能靠人工对齐——
评估报告 §5 的 9 处偏差就是那份账单。本文件用两条对账方向补上机制：

- 前端 → 后端：`web/src/api/*.ts` 里 TS 类型声明的每个字段，都必须真实出现在
  响应里。前端建模了而后端砍了/改名了，这里红灯。
- 后端 → 前端：响应字段集是快照。多出来的字段必须显式登记 `backend_only`
  白名单（附原因），否则红灯——「后端加了字段没人知道」就是 §5 前 6 行的成因。

TS 解析是行级的（只认 web/src/api 既有书写风格：`export type X = {`、
`  field: type;`、行注释、嵌套对象类型），不追求通用；解析器改坏时第一条
用例（解析 HostMeta）会先红。
"""
import re
from pathlib import Path

import httpx
import pytest

from kk_server.main import create_app

ADMIN = "admin"
PASS = "contract-pass"

WEB_API_DIR = Path(__file__).resolve().parents[2] / "web" / "src" / "api"


def ts_fields(type_name: str) -> list:
    """抠出 web/src/api/*.ts 里 `export type X = { ... }` 的顶层字段名清单。

    可选标记 `?` 剥掉（前端可选≠后端可缺）；`[k: string]: T` 索引签名跳过；
    嵌套对象类型只收 depth=1 的字段；`Base & { ... }` 交叉类型递归展开基类型。
    找不到类型名直接 AssertionError——前端改名/挪文件时这里先炸，而不是
    静默放空清单导致对账形同虚设。
    """
    for path in sorted(WEB_API_DIR.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"export type %s\s*=\s*(?:([\w]+)\s*&\s*)?\{"
                      % re.escape(type_name), text)
        if not m:
            continue
        fields = ts_fields(m.group(1)) if m.group(1) else []
        depth = 1
        for line in text[m.end():].splitlines():
            code = line.split("//")[0].strip()
            if code.startswith("/*") or code.startswith("*") or not code:
                continue
            fm = re.match(r"(\w+)\??\s*:", code)
            if fm and depth == 1:
                fields.append(fm.group(1))
            depth += code.count("{") - code.count("}")
            if depth <= 0:
                break
        return fields
    raise AssertionError("TS 类型 %s 在 %s 下找不到（前端改了名？）" % (type_name, WEB_API_DIR))


def assert_superset(actual: dict, ts_name: str, backend_only=(), where=""):
    """双向对账：TS 声明字段 ⊆ 响应字段；响应多出的必须登记在 backend_only。"""
    keys = set(actual)
    missing = [f for f in ts_fields(ts_name) if f not in keys]
    assert not missing, "%s：TS %s 声明了响应里没有的字段 %s" % (where, ts_name, missing)
    unlisted = sorted(keys - set(ts_fields(ts_name)) - set(backend_only))
    assert not unlisted, (
        "%s：响应多出 TS 未声明且未登记白名单的字段 %s"
        "（补 TS 类型，或带原因登记 backend_only）" % (where, unlisted))


def assert_exact(actual: dict, expected, where=""):
    """平铺包装的精确快照：加字段是破坏性变更，必须有意识地改这里。"""
    assert set(actual) == set(expected), (
        "%s：契约快照漂移（实有 %s，快照 %s）" % (where, sorted(actual), sorted(expected)))


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "contract.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    # ASGITransport 不触发 lifespan：建表与管理员初始化要手工补齐（同 test_api）
    await store.ensure_admin(ADMIN, PASS)
    token = await store.create_session(ADMIN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield type("Ctx", (), {"client": client, "store": store, "app": app})
    await store.close()


async def _seed(api):
    """一次造齐五类代表数据：主机（含心跳指标）、命令（两种 argv 形态）、审计、
    升级台账、待分发版本清单。所有端点契约共用。"""
    store = api.store
    await store.upsert_container("h1", "img:1", "0.1.0", 60)
    await store.set_online("h1", True, image="img:1", agent_ver="0.1.0")
    await store.record_hb("h1", {"interval": 60, "metrics": {
        "cpu": 1.5, "mem_mb": 100, "disks": {"/": {"pct": 30}}}})
    await store.create_commands_batch(["h1"], "shell", ["df", "-h"], 30, "admin")
    await store.create_commands_batch(
        ["h1"], "shell", {"argv": ["ls -l"], "use_shell": True}, 30, "admin")
    await store.add_audit("admin", "contract_probe", {"k": "v"})
    await store.create_update("u1", "h1", "0.1.0", "0.2.0", "done")
    await store.finish_update("u1", "done", "")
    await store.set_agent_latest({"version": "0.2.0", "sha256": "ab" * 20,
                                  "size": 3, "uploaded_at": 1700000000})


async def test_ts_parser_reads_hostmeta(api):
    """解析器自检：HostMeta 的 9 个字段一个不能少——解析器坏了后面全空转。"""
    assert ts_fields("HostMeta") == [
        "host_type", "os_name", "ip", "group_name", "proto_ver",
        "docker_total", "docker_running", "docker_unhealthy", "caps"]


async def test_contract_containers_list(api):
    await _seed(api)
    r = await api.client.get("/api/containers", params={"view": "summary"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert_exact(body, ["items", "total", "online", "alerts",
                        "latest_agent_ver", "outdated"], "GET /containers 顶层")
    assert body["items"], "种子数据必须产出至少一行，否则对账空转"
    assert_superset(body["items"][0], "HostSummary", where="GET /containers 行")
    # 详情行比摘要多出的字段不允许混进摘要视图（B6 的性能契约）
    assert "metrics" not in body["items"][0] and "custom" not in body["items"][0]


async def test_contract_container_detail(api):
    await _seed(api)
    r = await api.client.get("/api/containers/h1")
    assert r.status_code == 200, r.text
    assert_superset(r.json(), "HostDetail", where="GET /containers/h1")
    detail = r.json()
    assert set(detail["metrics"]) >= {"cpu", "mem_mb"}, "metrics 必须是心跳帧内层"
    assert isinstance(detail["commands"], list), "详情附带最近命令"


async def test_contract_container_metrics(api):
    await _seed(api)
    r = await api.client.get("/api/containers/h1/metrics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_exact(body, ["pod", "hours", "source", "series",
                        "mem_limit", "disk_limit", "disk_limit_raw"],
                 "GET /metrics 顶层")
    assert body["source"] in ("raw", "hourly")
    assert body["series"], "种子心跳必须产出至少一个序列点"
    assert_superset(body["series"][0], "MetricPoint", where="metrics 序列点")
    # 上限语义：磁盘有效值（未自定义 = 全局 85）、内存原值（未设置 = null）
    assert body["disk_limit"] == 85 and body["disk_limit_raw"] is None
    assert body["mem_limit"] is None


async def test_contract_host_limits_put(api):
    """PUT /containers/{pod}/limits：单机告警上限的写入面（趋势页保存按钮）。"""
    await _seed(api)
    r = await api.client.put("/api/containers/h1/limits",
                             json={"mem_limit": 70, "disk_limit": 90})
    assert r.status_code == 200, r.text
    assert_exact(r.json(), ["ok", "mem_limit", "disk_limit"], "PUT limits 回执")
    assert (r.json()["ok"], r.json()["mem_limit"], r.json()["disk_limit"]) == (
        True, 70, 90)


async def test_contract_audit_list(api):
    await _seed(api)
    r = await api.client.get("/api/audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_exact(body, ["items", "total", "offset", "limit"], "GET /audit 顶层")
    assert body["items"], "种子审计必须产出至少一行"
    assert_superset(body["items"][0], "AuditRow", where="GET /audit 行")


async def test_contract_commands_list_and_detail(api):
    await _seed(api)
    r = await api.client.get("/api/commands")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_exact(body, ["items", "total", "offset", "limit"], "GET /commands 顶层")
    assert body["items"], "种子命令必须产出至少一行"
    assert_superset(body["items"][0], "CommandRow",
                    # last_seq：心跳序列号，服务端丢命令判定内部用，UI 无消费点
                    backend_only=["last_seq"], where="GET /commands 行")
    cid = body["items"][0]["id"]
    detail = (await api.client.get("/api/commands/%s" % cid)).json()
    assert_superset(detail, "CommandRow",
                    backend_only=["last_seq"], where="GET /commands/{id}")
    assert "out_b64" not in detail, "大字段不进响应体（列表与详情一致）"


async def test_contract_updates_ledger(api):
    """台账行是 §5 第 6 行的现存偏差：后端多回 finished_at（升级耗时），
    前端 UpdateRow 未建模——登记在 backend_only 而不是删掉后端字段，
    等「升级耗时」UI 立项时补 TS 即可摘掉。"""
    await _seed(api)
    r = await api.client.get("/api/system/updates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_exact(body, ["items", "summary", "limit"], "GET /system/updates 顶层")
    assert body["items"], "种子台账必须产出至少一行"
    assert_superset(body["items"][0], "UpdateRow",
                    backend_only=["finished_at"], where="GET /system/updates 行")


async def test_contract_agent_current(api):
    await _seed(api)
    r = await api.client.get("/api/system/agent/current")
    assert r.status_code == 200, r.text
    assert_exact(r.json(), ["version", "sha256", "size", "uploaded_at",
                            "hosts_total", "hosts_outdated"], "GET /agent/current")


async def test_contract_health_and_stats(api):
    await _seed(api)
    r = await api.client.get("/api/health")
    assert r.status_code == 200, r.text
    assert_exact(r.json(), ["ok", "version", "proto_ver", "agents_online",
                            "broker", "bridge"], "GET /health")

    r = await api.client.get("/api/system/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    stats_optional = ["agent_latest_ver", "agents_outdated", "updates"]
    declared = [f for f in ts_fields("StatsResult") if f not in stats_optional]
    missing = [f for f in declared if f not in body]
    assert not missing, "GET /system/stats 缺 TS 必填字段 %s" % missing
    for opt in stats_optional:
        assert opt in body, "StatsResult 的可选字段 %s 现网恒回，TS 上的 ? 可摘掉" % opt


async def test_contract_auth_shapes(api):
    """login/me 是前端会话模型的根：形状漂移=全站登录炸，精确快照。"""
    app_client = api.app.state  # noqa: F841 (占位说明：login 不带旧会话头，新开 client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://test"
    ) as bare:
        r = await bare.post("/api/login", json={"username": ADMIN, "password": PASS})
        assert r.status_code == 200, r.text
        assert_exact(r.json(), ["token", "username"], "POST /login")
        r = await bare.get("/api/me", headers={"Authorization": "Bearer " + r.json()["token"]})
        assert r.status_code == 200, r.text
        assert_exact(r.json(), ["username"], "GET /me")
