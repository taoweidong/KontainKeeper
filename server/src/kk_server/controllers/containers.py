"""主机列表 / 详情 / 指标序列 / 单机告警上限。"""
import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import CurrentUser, HostFilters, current_user
from ..models.helpers import clamp
from ..models.store import DISK_ALERT_PCT
from ..models.tables import ONLINE_GRACE
from ..models.version import version_lt

# 会话鉴权在 router 级收口（QR-S2）：新增端点默认受保护，不再依赖人工逐行调用
router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


def _parse_metrics(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _disk_alert(metrics):
    disks = (metrics or {}).get("disks") or {}
    return max((d.get("pct", 0) for d in disks.values()), default=0) >= DISK_ALERT_PCT


def _host_meta(row):
    """v4 机队视图的公共字段：列表与详情同形，前端一套类型两用。

    `caps` 在库里是 JSON 文本（`_jtext` 只写对象），接口给结构而不是字符串——
    让前端去 parse 等于把「解析失败怎么办」分发到每个页面各写一遍。
    `labels` 是大字段，摘要视图不带，所以在这里也不出现（详情侧单独补）。
    """
    return {
        "host_type": row.get("host_type") or "",
        "os_name": row.get("os_name") or "",
        "ip": row.get("ip") or "",
        "group_name": row.get("group_name") or "",
        "proto_ver": row.get("proto_ver"),
        "docker_total": row.get("docker_total") or 0,
        "docker_running": row.get("docker_running") or 0,
        "docker_unhealthy": row.get("docker_unhealthy") or 0,
        "caps": _parse_metrics(row.get("caps")),
    }


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
        # 单机告警上限（百分比）：NULL = 未自定义（磁盘回落全局 85，内存不画线）。
        # 详情回的是**原始值**——编辑表单的初始态要区分「没设过」与「设成过 85」
        "mem_limit": row.get("mem_limit"),
        "disk_limit": row.get("disk_limit"),
        "metrics": metrics,
        "custom": hb.get("custom") or {},
        "disk_alert": _disk_alert(metrics),
        "labels": _parse_metrics(row.get("labels")),
        **_host_meta(row),
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
        "disk_alert": disk_pct >= DISK_ALERT_PCT,
        **_host_meta(row),
    }


def _outdated(agent_ver, latest_ver):
    """未上传过任何版本时恒 False：此时「落后」无从定义，不是「全都落后」。"""
    return bool(latest_ver) and version_lt(agent_ver or "", latest_ver)


@router.get("/containers")
async def list_containers(request: Request, filters: HostFilters,
                          view: str = "full", limit: int = 0, offset: int = 0):
    if view not in ("full", "summary"):
        raise HTTPException(status_code=400, detail="view 需为 full 或 summary")
    store = request.app.state.store
    # 分页上限：full 视图拉完整指标（每行 2~4KB LONGTEXT），无 LIMIT 时全机队
    # 一次性进内存是现成的自伤面（QR29-S12）——缺省也封顶 5000；summary 行小，
    # 维持不限（导出走 /api/export/hosts 的 MAX_ROWS=20000 上限，不经这里）
    if limit and limit > 0:
        cap = min(limit, 5000)
    else:
        cap = 5000 if view == "full" else None
    off = offset if offset and offset > 0 else None
    # 一次查回在线集合与最新版本再逐行拼装：500 台只有几次往返，不在循环里打 500 次查询
    # （D1.3 明确要求 KV 只读一次——每行查一次就是与 P1-1 同型的 N+1）
    # total 与列表共用同一套筛选条件，分页控件才不会对着筛完的列表说谎（QR-S36）
    # 排序键与方向不算筛选条件，count 时要摘掉
    count_filters = {k: v for k, v in filters.items() if k not in ("sort", "order")}
    rows, online, total, latest = await asyncio.gather(
        store.list_containers(view, limit=cap, offset=off, **filters),
        store.online_set(ONLINE_GRACE),
        store.count_containers(**count_filters),
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
    hours = clamp(hours, 1, 24 * 90)
    store = request.app.state.store
    series, source = await store.metrics_series(pod, hours)
    # 上限语义在服务端算好下发（D1.3 判例：语义不下发就是两套实现各自演化）：
    # - disk_limit：**有效值**（单机覆盖 ?? 全局 85），趋势图画线用；
    # - mem_limit：单机覆盖原值（内存没有全局口径，未设置 = null = 不画线）；
    # - disk_limit_raw：单机覆盖原值，编辑表单的初始态要区分「没设过」与「设成 85」
    row = await store.get_container(pod)
    disk_raw = row.get("disk_limit") if row else None
    return {"pod": pod, "hours": hours, "source": source, "series": series,
            "mem_limit": row.get("mem_limit") if row else None,
            "disk_limit": disk_raw if disk_raw is not None else DISK_ALERT_PCT,
            "disk_limit_raw": disk_raw}


class LimitBody(BaseModel):
    """单机告警上限（百分比 1–100）；None = 清除自定义、回落全局口径。"""

    mem_limit: Optional[int] = None
    disk_limit: Optional[int] = None


def _limit_or_none(v: Optional[int], name: str) -> Optional[int]:
    if v is None:
        return None
    if not 1 <= v <= 100:
        raise HTTPException(status_code=400, detail="%s 需在 1–100 之间" % name)
    return v


@router.put("/containers/{pod}/limits")
async def set_host_limits(pod: str, body: LimitBody, request: Request,
                          user: CurrentUser):
    """单机内存 / 磁盘告警上限。总览页 disk_alert 的全局口径（85）不受影响——
    这里只服务趋势页的告警线，改口径不改列表语义。"""
    store = request.app.state.store
    if not await store.get_container(pod):
        raise HTTPException(status_code=404, detail="主机不存在")
    mem = _limit_or_none(body.mem_limit, "mem_limit")
    disk = _limit_or_none(body.disk_limit, "disk_limit")
    await store.set_host_limits(pod, mem, disk)
    await store.add_audit(user, "host_limits_set", {
        "pod": pod, "mem_limit": mem, "disk_limit": disk})
    return {"ok": True, "mem_limit": mem, "disk_limit": disk}
