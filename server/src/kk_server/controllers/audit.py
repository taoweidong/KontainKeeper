"""审计日志查询。"""
from fastapi import APIRouter, Request

from .deps import current_user

router = APIRouter(prefix="/api")


@router.get("/audit")
async def list_audit(request: Request, limit: int = 200):
    await current_user(request)
    return {"items": await request.app.state.store.list_audit(limit=min(limit, 1000))}
