"""数据导出：命令 / 审计 / 主机 / 指标四类导成 CSV（P0-1）。

设计取舍：

- **只用标准库 `csv` + `io`**，内存生成后一次性返回。全量 zip（500 × 4MB ≈ 2GB）
  必须走临时文件 + 流式打包，复杂度与收益不匹配，登记在二期备选。
- **默认只导元数据 + 末 2KB 输出**（`include_tail=1` 时才带 `out_tail`）。
  2000 行 × 2KB = 4MB，默认不背这份流量；单条全量输出走 `/api/commands/{cid}/out`。
- **行数硬上限 20000** 并显式 400：静默截断会让报表缺数据且无人知晓。
- **筛选条件与页面完全一致** —— 走 `store._command_filters`，导出的就是看到的。
"""
import csv
import io
import json
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .deps import CurrentUser, current_user

# 会话鉴权在 router 级收口（QR-S2）；导出属批量数据外带，每个端点落一条审计（QR-S7）
router = APIRouter(prefix="/api/export", dependencies=[Depends(current_user)])

MAX_ROWS = 20000
# CSV 注入防护：Excel 会把以这些字符开头的单元格当公式执行。
# argv / detail 都是用户可控内容（`=cmd|'/c calc'!A1` 是真实攻击载荷）。
_INJECT_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _cell(v):
    """单格取值：None → 空串；危险前缀加前导单引号。"""
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    if s[:1] in _INJECT_PREFIX:
        return "'" + s
    return s


def _ts(v):
    """Unix 秒 → 可读时间（服务端本地时区）。报表直接可读优先于时区严谨。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))


def _limit(limit, default=2000):
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        n = default
    if n > MAX_ROWS:
        raise HTTPException(status_code=400,
                            detail="导出行数上限 %d，请缩小时间范围或筛选条件" % MAX_ROWS)
    return n


def _csv_response(header, rows, filename):
    """rows 为 list[list]，内存生成 CSV 并带 BOM 返回。

    BOM 是 Windows/Excel 场景的刚需：不加则中文全乱码。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(c) for c in r])
    body = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quote(filename)},
    )


@router.get("/commands")
async def export_commands(request: Request, user: CurrentUser, pod: str = "",
                          kind: str = "", status: str = "", batch: str = "",
                          since: int = 0, until: int = 0, keyword: str = "",
                          limit: int = 2000, include_tail: int = 0):
    store = request.app.state.store
    limit = _limit(limit)
    rows = await store.list_commands(
        pod=pod or None, kind=kind or None, status=status or None, batch=batch or None,
        since=since or None, until=until or None, keyword=keyword or None,
        limit=limit, tail=True)
    await store.add_audit(user, "export", {
        "kind": "commands", "rows": len(rows), "pod": pod, "status": status,
        "batch": batch, "keyword": keyword})
    header = ["id", "pod", "kind", "argv", "status", "rc", "timed_out", "truncated",
              "elapsed_ms", "created_at", "finished_at", "created_by", "out_purged"]
    if include_tail:
        header.append("out_tail")
    out = []
    for r in rows:
        out.append([
            r.get("id"), r.get("pod"), r.get("kind"), _argv_text(r.get("argv")),
            r.get("status"), r.get("rc"), r.get("timed_out"), r.get("truncated"),
            r.get("elapsed_ms"), _ts(r.get("created_at")), _ts(r.get("finished_at")),
            r.get("created_by"), r.get("out_purged"),
        ] + ([r.get("out_tail", "")] if include_tail else []))
    name = "命令历史_%s.csv" % time.strftime("%Y%m%d-%H%M%S")
    return _csv_response(header, out, name)


def _argv_text(raw):
    """argv 列对 shell 存 JSON 数组、对 collect 存 {"items": [...]}，导出时统一人读。"""
    try:
        v = json.loads(raw or "null")
    except ValueError:
        return raw or ""
    if isinstance(v, dict):
        if v.get("items"):
            return "collect: " + ",".join(v["items"])
        return " ".join(v.get("argv") or [])
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v) if v is not None else ""


@router.get("/audit")
async def export_audit(request: Request, user: CurrentUser, actor: str = "",
                       action: str = "", keyword: str = "", limit: int = 5000):
    store = request.app.state.store
    limit = _limit(limit)
    # 筛选下推到 SQL（store._audit_filters）：先取最近 N 条再内存过滤的话，
    # 命中行会被 LIMIT 截掉，导出就不再是页面所见（评审 P1）
    rows = await store.list_audit(limit=limit, actor=actor or None,
                                  action=action or None, keyword=keyword or None)
    await store.add_audit(user, "export", {
        "kind": "audit", "rows": len(rows), "actor": actor, "action": action})
    out = []
    for r in rows:
        out.append([r.get("id"), _ts(r.get("ts")), r.get("actor"),
                    r.get("action"), r.get("detail")])
    return _csv_response(["id", "ts", "actor", "action", "detail"],
                         out, "审计日志_%s.csv" % time.strftime("%Y%m%d-%H%M%S"))


@router.get("/hosts")
async def export_hosts(request: Request, user: CurrentUser, view: str = "summary"):
    store = request.app.state.store
    if view not in ("full", "summary"):
        raise HTTPException(status_code=400, detail="view 需为 full 或 summary")
    # 资产盘点场景：不分页，一次性导出全量摘要列
    rows = await store.list_containers("summary", limit=MAX_ROWS)
    online = await store.online_set()
    await store.add_audit(user, "export", {"kind": "hosts", "rows": len(rows)})
    now = int(time.time())
    out = []
    for r in rows:
        disk = r.get("disk_pct") or 0.0
        out.append([r.get("pod"), r.get("image"), r.get("agent_ver"),
                    1 if r.get("pod") in online else 0, r.get("hb_interval"),
                    r.get("cpu"), r.get("mem_mb"), disk, 1 if disk >= 85 else 0,
                    _ts(r.get("last_seen")), max(0, now - (r.get("last_seen") or now))])
    return _csv_response(
        ["pod", "image", "agent_ver", "online", "hb_interval", "cpu", "mem_mb",
         "disk_pct", "disk_alert", "last_seen", "age_sec"],
        out, "主机清单_%s.csv" % time.strftime("%Y%m%d-%H%M%S"))


@router.get("/metrics")
async def export_metrics(request: Request, user: CurrentUser, pod: str = "",
                         hours: int = 24):
    if not pod:
        raise HTTPException(status_code=400, detail="pod 为必填")
    store = request.app.state.store
    hours = min(max(int(hours or 24), 1), 24 * 90)
    # 与 metrics_series 同源：>24h 自动走 hourly 聚合表，导出与曲线语义一致
    series, _ = await store.metrics_series(pod, hours)
    await store.add_audit(user, "export", {
        "kind": "metrics", "rows": len(series), "pod": pod, "hours": hours})
    out = [[_ts(r.get("ts")), r.get("cpu"), r.get("mem_mb")] for r in series]
    return _csv_response(["ts", "cpu", "mem_mb"], out,
                         "指标_%s_%sh.csv" % (pod, hours))
