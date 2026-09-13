"""主机列表 / 详情 / 指标序列。"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request

from .deps import current_user
from ..models.version import version_lt

router = APIRouter(prefix="/api")

ONLINE_GRACE = 180  # 在线宽限：max(3×interval, 180s)，兜住 Broker 来不及发 LWT 的极端情况


def _parse_metrics(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _disk_alert(metrics):
    disks = (metrics or {}).get("disks") or {}
    return max((d.get("pct", 0) for d in disks.values()), default=0) >= 85


def _container_view(row, online_set, latest_ver=""):
    """online 来自桥接按 retained status / LWT 维护的在线列，不再查内存连接表。"""
    now = int(time.time())
    hb = _parse_metrics(row["last_metrics"])
    metrics = hb.get("metrics") or {}
    return {
        "pod": row["pod"],
        "image": row["image"],
        "agent_ver": row["agent_ver"],
        # 版本落后判定由服务端算好下发（D1.3）：前端无 version_lt 等价实现，
        # 让它自己比就是两套语义各自演化（多段比较 1.10.0 vs 1.9.0 最先出错）
        "latest_agent_ver": latest_ver,
        "agent_outdated": _outdated(row["agent_ver"], latest_ver),
        "hb_interval": row["hb_interval"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "online": row["pod"] in online_set,
        "age_sec": max(0, now - row["last_seen"]),
        # 离线原因（B6.1）：updating = 正在自更新，而非「容器停了」
        "status_reason": row.get("status_reason") or "",
        "metrics": metrics,
        "custom": hb.get("custom") or {},
        "disk_alert": _disk_alert(metrics),
    }


def _container_summary(row, online_set, now, latest_ver=""):
    """摘要视图：只回列表页要渲染的标量，不解析 last_metrics（B6）。

    500 台时这一行 json.loads 的差价是几百毫秒——列表是前端 10s 轮询的接口，
    慢一点会直接放大成服务端的持续负载。
    """
    disk_pct = row.get("disk_pct") or 0.0
    return {
        "pod": row["pod"],
        "image": row["image"],
        "agent_ver": row["agent_ver"],
        "latest_agent_ver": latest_ver,
        "agent_outdated": _outdated(row["agent_ver"], latest_ver),
        "online": row["pod"] in online_set,
        "age_sec": max(0, now - row["last_seen"]),
        "status_reason": row.get("status_reason") or "",
        "cpu": row.get("cpu"),
        "mem_mb": row.get("mem_mb"),
        "disk_pct": disk_pct,
        "disk_alert": disk_pct >= 85,
    }


def _outdated(agent_ver, latest_ver):
    """未上传过任何版本时恒 False：此时「落后」无从定义，不是「全都落后」。"""
    return bool(latest_ver) and version_lt(agent_ver or "", latest_ver)


@router.get("/containers")
async def list_containers(request: Request, view: str = "full",
                          limit: int = 0, offset: int = 0):
    await current_user(request)
    if view not in ("full", "summary"):
        raise HTTPException(status_code=400, detail="view 需为 full 或 summary")
    store = request.app.state.store
    # 分页上限：full 视图拉完整指标，500 台一次性取回过量，默认不限制但可被前端约束
    cap = min(limit, 5000) if limit and limit > 0 else None
    off = offset if offset and offset > 0 else None
    # 一次查回在线集合与最新版本再逐行拼装：500 台只有几次往返，不在循环里打 500 次查询
    # （D1.3 明确要求 KV 只读一次——每行查一次就是与 P1-1 同型的 N+1）
    rows, online, total, latest = await asyncio.gather(
        store.list_containers(view, limit=cap, offset=off),
        store.online_set(ONLINE_GRACE),
        store.count_containers(),
        store.get_agent_latest())
    latest_ver = (latest or {}).get("version", "")
    if view == "summary":
        now = int(time.time())
        items = [_container_summary(r, online, now, latest_ver) for r in rows]
    else:
        items = [_container_view(r, online, latest_ver) for r in rows]
    return {
        "items": items,
        "total": total,
        "online": sum(1 for i in items if i["online"]),
        "alerts": sum(1 for i in items if i["disk_alert"]),
        "latest_agent_ver": latest_ver,
        "outdated": sum(1 for i in items if i["agent_outdated"]),
    }


@router.get("/containers/{pod}")
async def container_detail(pod: str, request: Request):
    await current_user(request)
    store = request.app.state.store
    row = await store.get_container(pod)
    if not row:
        raise HTTPException(status_code=404, detail="容器不存在")
    online, latest = await asyncio.gather(store.online_set(ONLINE_GRACE),
                                          store.get_agent_latest())
    view = _container_view(row, online, (latest or {}).get("version", ""))
    view["commands"] = await store.list_commands(pod=pod, limit=20)
    return view


@router.get("/containers/{pod}/metrics")
async def container_metrics(pod: str, request: Request, hours: int = 24):
    await current_user(request)
    hours = min(max(hours, 1), 24 * 90)
    series, source = await request.app.state.store.metrics_series(pod, hours)
    return {"pod": pod, "hours": hours, "source": source, "series": series}
