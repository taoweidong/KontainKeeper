"""健康检查：GET /api/health（含桥接在线数与计数器，便于外部探活与排障）。"""
from fastapi import APIRouter, Request

from .. import PROTO_VER, __version__

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request):
    store, bridge = request.app.state.store, request.app.state.bridge
    return {
        "ok": True,
        "version": __version__,
        "proto_ver": PROTO_VER,
        "agents_online": await store.online_count(),
        "broker": "connected" if (bridge and bridge.connected.is_set()) else "disconnected",
        "bridge": bridge.stats if bridge else None,
    }
