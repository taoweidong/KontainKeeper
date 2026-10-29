"""API 全链路用例（模块：API）。

覆盖「数据上报 → 采集 → 命令 → 输出提取 → 审计 → 导出 → 统计」这条主链，
全部打真实服务端 + 真实 Broker + 真实 WSL Agent，不 mock 任何一跳。

刻意不做的事：**不测登录锁定**。锁定是按用户名/源 IP 计的（5 次 → 429 锁 300s），
在本进程里打满就再也登不进来，后面所有用例连带假失败；锁定语义由
`server/tests/test_auth.py` 单测锁定，这里只验一条错误口令的 401。
"""
from __future__ import annotations

import base64
import csv
import io
import time
import urllib.parse

from .core import (Env, api, check, hdr, http, http_bytes, login, trunc, wait_api)

MOD = "API"
COLLECT_ITEMS = ["cpu", "mem", "disk", "net", "proc"]
SUCCESS = ("done", "failed", "timeout")   # 命令终态


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _hdr(headers: dict, name: str, default: str = "") -> str:
    """历史入口，实现统一在 core.hdr（响应头大小写不可依赖）。"""
    return hdr(headers, name, default)


def _summary(env: Env) -> tuple[int, dict]:
    st, body = api(env, "GET", "/api/containers", params={"view": "summary"})
    return st, body if isinstance(body, dict) else {}


def _pods(env: Env) -> list[str]:
    _, body = _summary(env)
    return [i.get("pod", "") for i in body.get("items") or []]


def _wait_cmd(env: Env, cid: str, timeout: float = 90) -> dict:
    body, last = wait_api(env, "/api/commands/%s" % cid,
                          lambda b: b.get("status") in SUCCESS, timeout=timeout,
                          interval=0.5)
    if body:
        return body
    return {"_last": last, "status": "timeout-wait"}


def _csv_parse(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    return (rows[0] if rows else []), rows[1:]


def _save(env: Env, name: str, data: bytes) -> str:
    """把页面/接口拿到的文件真落盘到产物目录（「文件提取」必须可复现翻查）。"""
    path = env.artifacts / "downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _filename(headers: dict) -> str:
    cd = _hdr(headers, "content-disposition")
    if "filename*=UTF-8''" in cd:
        return urllib.parse.unquote(cd.split("filename*=UTF-8''", 1)[1].strip())
    return ""


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def case_health(env: Env) -> dict:
    st, body = api(env, "GET", "/api/health", token="")
    ok = (st == 200 and body.get("ok") is True
          and body.get("broker") == "connected"
          and body.get("agents_online") == len(env.agents))
    check("API-01", MOD, "健康检查：服务端就绪 + Broker 已连接 + 3 台 Agent 在线",
          ok, "status=%s body=%s" % (st, trunc(body, 260)),
          precond="栈已启动，3 台 WSL Agent 已连上专用 Broker",
          steps="GET /api/health（免鉴权）",
          expect="200；ok=true；broker=connected；agents_online=3")
    return body


def case_auth(env: Env) -> dict:
    st1, b1 = api(env, "POST", "/api/login", token="",
                  json_body={"username": env.admin_user, "password": "wrong-pass"})
    st2, b2 = api(env, "GET", "/api/containers", token="")
    st3, b3 = api(env, "POST", "/api/login", token="",
                  json_body={"username": env.admin_user, "password": env.admin_pass})
    tok = b3.get("token", "") if st3 == 200 else ""
    st4, b4 = api(env, "GET", "/api/me", token=tok) if tok else (-1, {})
    env.token = tok or env.token
    ok = (st1 == 401 and "用户名或密码错误" in str(b1.get("detail", ""))
          and st2 == 401 and tok and b4.get("username") == env.admin_user)
    check("API-02", MOD, "鉴权：错误口令 401、无 token 401、正确登录后可读身份",
          ok, "wrong=%s/%s anon=%s login=%s me=%s"
          % (st1, trunc(b1.get("detail", ""), 40), st2, st3, trunc(b4, 80)),
          precond="服务端 admin/admin123（e2e 专用库）",
          steps="POST /api/login（错口令）→ GET /api/containers（无 token）→ "
                "POST /api/login（对口令）→ GET /api/me",
          expect="401 用户名或密码错误 / 401 / 200 带 token / me.username=admin")
    return {"token": env.token}


def case_fleet(env: Env) -> dict:
    st, body = _summary(env)
    items = body.get("items") or []
    names = sorted(i.get("pod", "") for i in items)
    ok_fields = all(
        i.get("group_name") == "e2e-group" and i.get("proto_ver") == 4
        and (i.get("ip") or "") and i.get("agent_ver") == "0.3.0"
        and i.get("host_type") in ("metal", "vm", "container")
        and isinstance(i.get("caps"), dict) and i.get("cpu") is not None
        and i.get("mem_mb") is not None
        for i in items)
    ok = (st == 200 and body.get("total") == len(env.agents)
          and body.get("online") == len(env.agents) and ok_fields
          and names == sorted(env.agents))
    check("API-03", MOD, "机队视图：3 台主机在线且 v4 元信息（分组/协议/IP/能力）齐全",
          ok, "status=%s total=%s online=%s pods=%s 首行=%s"
          % (st, body.get("total"), body.get("online"), names,
             trunc(items[0] if items else {}, 300)),
          precond="Agent 已发 status 帧（retain）",
          steps="GET /api/containers?view=summary",
          expect="total=online=3；group_name=e2e-group；proto_ver=4；ip 非空；cpu/mem 非空")
    return {"pods": names}


def case_reporting(env: Env, pods: list[str]) -> dict:
    """数据上报：full 视图的指标 + 7 秒后 last_seen 前进（心跳是持续的，不是一次性）。"""
    st, body = api(env, "GET", "/api/containers",
                   params={"view": "full", "keyword": pods[0]})
    one = (body.get("items") or [{}])[0] if isinstance(body, dict) else {}
    m = one.get("metrics") or {}
    first_seen = one.get("last_seen", 0)
    wait_s = max(6.0, float(one.get("hb_interval") or 3) * 2)
    base_ok = (st == 200 and (m.get("mem_mb") or 0) > 0
               and (m.get("cpu") is not None)
               and isinstance(m.get("disks"), dict) and bool(m.get("disks"))
               and isinstance(m.get("net"), dict) and bool(m.get("net"))
               and (m.get("uptime_sec") or 0) > 0)
    time.sleep(wait_s)
    st2, body2 = api(env, "GET", "/api/containers/%s" % pods[0])
    second = body2.get("last_seen", 0) if st2 == 200 else 0
    labels = body2.get("labels") or {}
    ok = bool(base_ok and second > first_seen
              and labels.get("role") == "e2e" and labels.get("zone") == "wsl")
    check("API-04", MOD, "数据上报：心跳指标持续更新（%ds 后 last_seen 前进）+ 标签落库"
          % int(wait_s), ok,
          "status=%s mem_mb=%s disks=%s net=%s last_seen %s→%s labels=%s"
          % (st, m.get("mem_mb"), len(m.get("disks") or {}),
             list((m.get("net") or {}))[:2], first_seen, second, labels),
          precond="Agent KK_INTERVAL=3，采集项由 psutil 真机读取",
          steps="GET /api/containers?view=full → 等待一个心跳周期 → GET /api/containers/{pod}",
          expect="metrics.mem_mb>0、disks/net 非空；last_seen 严格执行；labels=role=e2e,zone=wsl")
    return {"sample": m}


def case_filters(env: Env, pods: list[str]) -> None:
    pod = pods[0]
    st_k, b_k = api(env, "GET", "/api/containers",
                    params={"view": "summary", "keyword": pod})
    st_g, b_g = api(env, "GET", "/api/containers",
                    params={"view": "summary", "group": "e2e-group"})
    st_v, b_v = api(env, "GET", "/api/containers",
                    params={"view": "summary", "proto_ver": "4"})
    st_v3, b_v3 = api(env, "GET", "/api/containers",
                      params={"view": "summary", "proto_ver": "3"})
    st_off, b_off = api(env, "GET", "/api/containers",
                        params={"view": "summary", "online": "false"})
    st_bad, b_bad = api(env, "GET", "/api/containers", params={"view": "nope"})
    ok = (st_k == 200 and b_k.get("total") == 1 and (b_k["items"][0]["pod"] == pod)
          and st_g == 200 and b_g.get("total") == len(env.agents)
          and st_v == 200 and b_v.get("total") == len(env.agents)
          and st_v3 == 200 and b_v3.get("total") == 0
          and st_off == 200 and b_off.get("total") == 0
          and st_bad == 400 and "view 需为" in str(b_bad.get("detail", "")))
    check("API-05", MOD, "筛选：关键字/分组/协议/仅在线/非法视图参数",
          ok, "key=%s/%s group=%s proto4=%s proto3=%s online=false→%s view=nope→%s"
          % (st_k, b_k.get("total"), b_g.get("total"), b_v.get("total"),
             b_v3.get("total"), b_off.get("total"), st_bad),
          precond="机队 3 台，全部 v4、分组 e2e-group、在线",
          steps="GET /api/containers 依次带 keyword / group / proto_ver=4 / proto_ver=3 / "
                "online=false / view=nope",
          expect="1 / 3 / 3 / 0 / 0；view 非法返回 400")


def case_detail(env: Env, pods: list[str]) -> None:
    pod = pods[0]
    st, body = api(env, "GET", "/api/containers/%s" % pod)
    st404, b404 = api(env, "GET", "/api/containers/not-exist-host")
    st_m, b_m = api(env, "GET", "/api/containers/%s/metrics" % pod,
                    params={"hours": 1})
    series = b_m.get("series") or []
    st_zero, b_zero = api(env, "GET", "/api/containers/%s/metrics" % pod,
                          params={"hours": 0})
    asc = all(series[i]["ts"] <= series[i + 1]["ts"] for i in range(len(series) - 1))
    ok = (st == 200 and body.get("pod") == pod and "commands" in body
          and st404 == 404 and "容器不存在" in str(b404.get("detail", ""))
          and st_m == 200 and len(series) >= 2 and asc
          and all("cpu" in r and "mem_mb" in r for r in series)
          and b_zero.get("hours") == 1)
    check("API-06", MOD, "主机详情 + 指标序列（升序、hours 下限钳到 1）+ 404 语义",
          ok, "detail=%s metrics=%s/%d 点 asc=%s hours=0→%s 404=%s"
          % (st, st_m, len(series), asc, b_zero.get("hours"), st404),
          precond="该主机已有多个心跳序列点",
          steps="GET /api/containers/{pod} → GET .../metrics?hours=1 → hours=0 → 不存在的主机",
          expect="详情含 commands；序列 ≥2 点且 ts 升序；hours=0 钳到 1；404 容器不存在")


def case_collect(env: Env, pods: list[str]) -> dict:
    st, body = api(env, "POST", "/api/commands", json_body={
        "pods": pods, "kind": "collect", "items": COLLECT_ITEMS})
    items = body.get("items") or []
    batch = body.get("batch_id", "")
    results = [_wait_cmd(env, i.get("id", "")) for i in items]
    done = [r for r in results if r.get("status") == "done" and r.get("rc") == 0]
    outs = [r.get("out_tail") or "" for r in results]
    ok = (st == 200 and len(items) == len(pods) and batch
          and len(done) == len(pods) and all(len(o) > 20 for o in outs))
    check("API-07", MOD, "模拟数据采集：5 个采集项 × 3 台主机全部执行成功",
          ok, "status=%s accepted=%d batch=%s done=%d rc=%s 首条输出=%s"
          % (st, len(items), batch, len(done), [r.get("rc") for r in results],
             trunc(outs[0] if outs else "", 200)),
          precond="命令通道已连通（Broker + 3 台 Agent 在线）",
          steps="POST /api/commands {kind:collect, items:[cpu,mem,disk,net,proc]} → 轮询每条命令",
          expect="3 条命令 status=done、rc=0、输出非空")
    return {"batch": batch, "ids": [i.get("id") for i in items]}


def case_shell(env: Env, pods: list[str]) -> dict:
    st, body = api(env, "POST", "/api/commands", json_body={
        "pods": [pods[0]], "kind": "shell",
        "cmdline": "echo KK_E2E_OK; uname -s; id -u; echo 中文输出正常",
        "use_shell": True, "timeout": 30})
    cid = ((body.get("items") or [{}])[0] or {}).get("id", "")
    row = _wait_cmd(env, cid) if cid else {}
    tail = row.get("out_tail") or ""
    st_out, text, _ = http(env, "GET", "/api/commands/%s/out" % cid)
    st_b64, b64 = api(env, "GET", "/api/commands/%s/out" % cid,
                      params={"format": "base64"})
    decoded = ""
    if st_b64 == 200:
        try:
            decoded = base64.b64decode(b64.get("out", "")).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 解码失败本身就是用例结果
            decoded = ""
    st_bad, b_bad = api(env, "GET", "/api/commands/%s/out" % cid,
                        params={"format": "xml"})
    ok = (st == 200 and row.get("status") == "done" and "KK_E2E_OK" in tail
          and "Linux" in tail and "中文输出正常" in text
          and decoded == text and st_bad == 400)
    check("API-08", MOD, "Shell 命令执行与输出提取（text / base64 / 非法格式）",
          ok, "submit=%s status=%s rc=%s tail=%s text=%dB base64==text:%s xml=%s"
          % (st, row.get("status"), row.get("rc"), trunc(tail, 120),
             len(text), decoded == text, st_bad),
          precond="KK_ALLOW_SHELL 默认放行（e2e 环境未关）",
          steps="POST /api/commands use_shell=true → GET /api/commands/{id} → "
                "GET .../out?format=text → ?format=base64 → ?format=xml",
          expect="done/rc=0；tail 含 KK_E2E_OK 与 uname；base64 解码与 text 一致；xml→400")
    if text:
        _save(env, "cmd-%s.txt" % cid[:8], text.encode("utf-8"))
    return {"id": cid, "text": text}


def case_guard(env: Env, pods: list[str]) -> None:
    st_b, b_b = api(env, "POST", "/api/commands", json_body={
        "pods": [pods[0]], "kind": "shell", "cmdline": "rm -rf /", "use_shell": True})
    st_i, b_i = api(env, "POST", "/api/commands", json_body={
        "pods": [pods[0]], "kind": "collect", "items": ["cpu", "nope"]})
    st_k, b_k = api(env, "POST", "/api/commands", json_body={
        "pods": [pods[0]], "kind": "rm", "cmdline": "ls"})
    st_p, b_p = api(env, "POST", "/api/commands", json_body={
        "pods": ["ghost"], "kind": "shell", "cmdline": "ls"})
    st_e, b_e = api(env, "POST", "/api/commands", json_body={
        "pods": [pods[0]], "kind": "shell", "argv": []})
    ok = (st_b == 400 and "黑名单" in str(b_b.get("detail", ""))
          and st_i == 400 and "采集项不存在" in str(b_i.get("detail", ""))
          and st_k == 400 and "kind 需为" in str(b_k.get("detail", ""))
          and st_p == 404 and "容器不存在" in str(b_p.get("detail", ""))
          and st_e == 400)
    check("API-09", MOD, "安全红线：黑名单拦截 / 非法采集项 / 非法 kind / 不存在主机 / 空命令",
          ok, "blacklist=%s/%s collect=%s kind=%s pod=%s empty=%s"
          % (st_b, trunc(b_b.get("detail", ""), 40), st_i, st_k, st_p, st_e),
          precond="KK_CMD_BLACKLIST 默认值（含 rm -rf /）",
          steps="POST /api/commands 依次发 rm -rf /、非法采集项、kind=rm、pod=ghost、空 argv",
          expect="400 黑名单 / 400 采集项不存在 / 400 kind / 404 容器不存在 / 400 命令不能为空")


def case_command_views(env: Env, pods: list[str], batch: str, cid: str) -> None:
    st_l, b_l = api(env, "GET", "/api/commands", params={"batch": batch})
    st_p, b_p = api(env, "GET", "/api/commands", params={"pod": pods[1], "kind": "collect"})
    st_s, b_s = api(env, "GET", "/api/commands", params={"status": "done", "limit": 5})
    st_k, b_k = api(env, "GET", "/api/commands", params={"keyword": "KK_E2E_OK"})
    st_b, b_b = api(env, "GET", "/api/commands/batches")
    batches = {r.get("batch_id"): r for r in b_b.get("items") or []}
    st_d, b_d = api(env, "GET", "/api/commands/%s" % cid)
    st_404, _ = api(env, "GET", "/api/commands/no-such-command")
    # argv 的形状取决于下发方式：cmdline 走 shell 时服务端存的是
    # `{"argv": [...], "use_shell": true}` 载荷（controllers/commands.py::_normalize），
    # 只有 argv 模式才是裸数组。这里断言「两种形状之一且内层确实是 argv 列表」，
    # 不要写死 list —— 那会把 shell 命令判成失败（本用例第一版就踩了）。
    argv = b_d.get("argv")
    argv_ok = ((isinstance(argv, dict) and argv.get("use_shell") is True
                and isinstance(argv.get("argv"), list))
               or (isinstance(argv, list) and argv != []))
    ok = (st_l == 200 and b_l.get("total") == len(env.agents)
          and st_p == 200 and b_p.get("total", 0) >= 1
          and st_s == 200 and 0 < b_s.get("total", 0) <= 5
          and st_k == 200 and b_k.get("total", 0) >= 1
          and st_b == 200 and bool(batches.get(batch))
          and batches[batch].get("total") == len(env.agents)
          and st_d == 200 and argv_ok
          and st_404 == 404)
    check("API-10", MOD, "命令检索面：按批次/主机/状态/关键字、批次汇总、单条与 404",
          ok, "batch=%s pods=%s done=%s kw=%s batches=%s detail.argv=%s 404=%s"
          % (b_l.get("total"), b_p.get("total"), b_s.get("total"),
             b_k.get("total"), trunc(batches.get(batch), 160),
             trunc(argv, 80), st_404),
          precond="已下发 1 个采集批次 + 1 条 shell 命令",
          steps="GET /api/commands?batch=|pod=&kind=|status=|keyword= → /commands/batches → "
                "/commands/{id} → /commands/不存在",
          expect="批次 3 条、按主机≥1、status 生效、关键字命中、批次汇总 total=3、"
                 "详情 argv 为命令载荷（shell 模式 {argv,use_shell}）、404")


def case_audit(env: Env) -> None:
    st, body = api(env, "GET", "/api/audit", params={"limit": 200})
    items = body.get("items") or []
    actions = [i.get("action") for i in items]
    st_a, b_a = api(env, "GET", "/api/audit", params={"action": "command_blocked"})
    st_f, b_f = api(env, "GET", "/api/audit", params={"actor": "admin", "limit": 5})
    need = {"login_ok", "command_create", "command_blocked", "agent_upload"}
    ok = (st == 200 and body.get("total", 0) > 0
          and {"login_ok", "command_create", "command_blocked"} <= set(actions)
          and st_a == 200 and b_a.get("total", 0) >= 1
          and st_f == 200 and len(b_f.get("items") or []) > 0)
    check("API-11", MOD, "审计：登录/下发/拦截全部留痕，且可按动作与操作者筛选",
          ok, "total=%s 含=%s blocked=%s actor=admin→%s 待补=%s"
          % (body.get("total"), sorted(set(actions) & need), b_a.get("total"),
             b_f.get("total"), sorted(need - set(actions))),
          precond="前面的登录、下发、黑名单用例已产生审计",
          steps="GET /api/audit?limit=200 → ?action=command_blocked → ?actor=admin",
          expect="至少含 login_ok / command_create / command_blocked；筛选生效")


def case_export(env: Env, pods: list[str]) -> None:
    specs = [
        ("commands", "/api/export/commands", {}, "命令历史_", "id"),
        ("audit", "/api/export/audit", {}, "审计日志_", "action"),
        ("hosts", "/api/export/hosts", {}, "主机清单_", "pod"),
        ("metrics", "/api/export/metrics", {"pod": pods[0], "hours": 1}, "指标_", "cpu"),
    ]
    detail = []
    ok = True
    for name, path, params, prefix, col in specs:
        url = path + ("?" + urllib.parse.urlencode(params) if params else "")
        st, data, headers = http_bytes(env, url)
        header, rows = _csv_parse(data)
        fn = _filename(headers)
        saved = _save(env, fn or ("%s.csv" % name), data)
        good = (st == 200 and data.startswith(b"\xef\xbb\xbf") and col in header
                and fn.startswith(prefix) and saved)
        if name == "hosts":
            good = good and len(rows) == len(env.agents)
        ok = ok and good
        detail.append("%s=%s/%d行/%s" % (name, st, len(rows), fn))
    st_lim, b_lim = api(env, "GET", "/api/export/commands", params={"limit": 20001})
    st_nopod, b_nopod = api(env, "GET", "/api/export/metrics")
    ok = ok and st_lim == 400 and st_nopod == 400
    check("API-12", MOD, "文件提取：四类 CSV 导出（BOM + 中文文件名 + 落盘）+ 上限/必填校验",
          ok, " ; ".join(detail) + " ; limit=20001→%s nopod→%s" % (st_lim, st_nopod),
          precond="已有命令/审计/主机/指标数据",
          steps="GET /api/export/{commands,audit,hosts,metrics}（带 token）→ 校验 BOM、表头、"
                "Content-Disposition 文件名 → 落盘 artifacts/downloads/",
          expect="200 + UTF-8 BOM + 表头正确 + 文件名以中文前缀开头；主机清单 3 行；"
                 "limit>20000→400；metrics 缺 pod→400")


def case_stats(env: Env) -> None:
    st, body = api(env, "GET", "/api/system/stats")
    hosts = body.get("hosts") or {}
    storage = body.get("storage") or {}
    broker = body.get("broker") or {}
    cmds = (body.get("commands") or {}).get("status") or {}
    ok = (st == 200 and hosts.get("total") == len(env.agents)
          and hosts.get("online") == len(env.agents)
          and (storage.get("heartbeats") or 0) > 0
          and broker.get("connected") is True
          and isinstance(cmds, dict))
    check("API-13", MOD, "系统统计：主机数 / 心跳明细行数 / Broker 连接 / 命令积压",
          ok, "status=%s hosts=%s storage=%s broker=%s commands=%s"
          % (st, hosts, storage, trunc(broker, 120), trunc(cmds, 160)),
          precond="链路已跑过命令与心跳",
          steps="GET /api/system/stats",
          expect="hosts.total=online=3；storage.heartbeats>0；broker.connected=true")


def run(env: Env, ctx: dict | None = None) -> dict:
    """执行全部 API 用例；返回后续模块（升级/页面）要复用的上下文。"""
    login(env, password=env.admin_pass)
    case_health(env)
    case_auth(env)
    ctx = case_fleet(env)
    pods = ctx.get("pods") or list(env.agents)
    case_reporting(env, pods)
    case_filters(env, pods)
    case_detail(env, pods)
    collected = case_collect(env, pods)
    shell = case_shell(env, pods)
    case_guard(env, pods)
    case_command_views(env, pods, collected.get("batch", ""), shell.get("id", ""))
    case_audit(env)
    case_export(env, pods)
    case_stats(env)
    return {"pods": pods, "batch": collected.get("batch", ""),
            "cmd_id": shell.get("id", "")}