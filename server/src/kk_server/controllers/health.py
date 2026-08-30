"""健康检查：GET /api/health。"""
from fastapi import APIRouter, Request

from .. import __version__

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request):
    hub = request.app.state.hub
    return {"ok": True, "version": __version__, "agents_online": len(hub.conns)}