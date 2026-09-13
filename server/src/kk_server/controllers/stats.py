"""可观测面板：GET /api/system/stats（C5）。

只回答运维排障最常问的三个问题：链路是不是活的（Broker 与最近一帧消息）、
命令有没有积压（按状态分布 + 发布失败计数）、库有没有涨（各表行数）。

这里刻意不加百分位延迟统计：真要按帧测延迟就得在桥接里插桩并再建一张表，
而 500 台的规模下「计数在不在动」已经能定位绝大多数故障。
"""
import asyncio
import time

from fastapi import APIRouter, Request

from .deps import current_user
from ..models.version import count_outdated

router = APIRouter(prefix="/api/system")

_started = time.time()


@router.get("/stats")
async def stats(request: Request):
    await current_user(request)
    store, bridge = request.app.state.store, request.app.state.bridge
    counts = await store.counts()
    broker = {
        "connected": bool(bridge and bridge.connected.is_set()),
        "stats": dict(bridge.stats) if bridge else None,
        "last_msg_age_sec": (int(time.time()) - bridge.stats["last_msg_ts"]
                             if bridge and bridge.stats["last_msg_ts"] else None),
    }
    # 升级在途/失败计数：500 台的升级结果必须先有个总量口径，再去看台账明细
    # 版本治理（D1.2）：总览页要能直接渲染「Agent 版本 vX.Y.Z · 落后 N 台」，
    # 因此这里把最新版本与落后台数一并给出，前端不做任何版本比较。
    updates, latest, versions = await asyncio.gather(
        store.updates_summary(), store.get_agent_latest(),
        store.agent_version_counts())
    latest_ver = (latest or {}).get("version", "")
    return {
        "ok": True,
        "uptime_sec": int(time.time() - _started),
        "hosts": counts["hosts"],
        "commands": counts["commands"],
        "storage": counts["storage"],
        "agent_latest_ver": latest_ver,
        "agents_outdated": count_outdated(versions, latest_ver),
        "updates": {
            "in_flight": updates.get("pending", 0) + updates.get("queued", 0),
            "done": updates.get("done", 0),
            "failed": updates.get("failed", 0),
            "timeout": updates.get("timeout", 0),
        },
        "broker": broker,
    }
