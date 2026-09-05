"""可观测面板：GET /api/system/stats（C5）。

只回答运维排障最常问的三个问题：链路是不是活的（Broker 与最近一帧消息）、
命令有没有积压（按状态分布 + 发布失败计数）、库有没有涨（各表行数）。

这里刻意不加百分位延迟统计：真要按帧测延迟就得在桥接里插桩并再建一张表，
而 500 台的规模下「计数在不在动」已经能定位绝大多数故障。
"""
import time

from fastapi import APIRouter, Request

from .deps import current_user

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
    return {
        "ok": True,
        "uptime_sec": int(time.time() - _started),
        "hosts": counts["hosts"],
        "commands": counts["commands"],
        "storage": counts["storage"],
        "broker": broker,
    }
