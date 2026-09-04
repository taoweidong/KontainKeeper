"""容器列表 / 详情 / 指标序列。"""
import json
import time

from fastapi import APIRouter, HTTPException, Request

from .deps import current_user

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


def _container_view(row, store):
    """online 直读 store 的在线列——由桥接按 retained status / LWT 维护，不再查内存连接表。"""
    now = int(time.time())
    hb = _parse_metrics(row["last_metrics"])
    metrics = hb.get("metrics") or {}
    return {
        "pod": row["pod"],
        "image": row["image"],
        "agent_ver": row["agent_ver"],
        "hb_interval": row["hb_interval"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "online": store.is_online(row["pod"], ONLINE_GRACE),
        "age_sec": max(0, now - row["last_seen"]),
        "metrics": metrics,
        "custom": hb.get("custom") or {},
        "disk_alert": _disk_alert(metrics),
    }


@router.get("/containers")
def list_containers(request: Request):
    current_user(request)
    store = request.app.state.store
    items = [_container_view(r, store) for r in store.list_containers()]
    return {
        "items": items,
        "total": len(items),
        "online": sum(1 for i in items if i["online"]),
        "alerts": sum(1 for i in items if i["disk_alert"]),
    }


@router.get("/containers/{pod}")
def container_detail(pod: str, request: Request):
    current_user(request)
    store = request.app.state.store
    row = store.get_container(pod)
    if not row:
        raise HTTPException(status_code=404, detail="容器不存在")
    view = _container_view(row, store)
    view["commands"] = store.list_commands(pod=pod, limit=20)
    return view


@router.get("/containers/{pod}/metrics")
def container_metrics(pod: str, request: Request, hours: int = 24):
    current_user(request)
    hours = min(max(hours, 1), 24 * 90)
    series, source = request.app.state.store.metrics_series(pod, hours)
    return {"pod": pod, "hours": hours, "source": source, "series": series}
