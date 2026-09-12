"""审计日志查询。"""
import asyncio

from fastapi import APIRouter, Request

from .deps import current_user

router = APIRouter(prefix="/api")


@router.get("/audit")
async def list_audit(request: Request, limit: int = 200, offset: int = 0):
    """审计也要能翻页：只靠条数切换时，早期记录永远看不到。"""
    await current_user(request)
    store = request.app.state.store
    limit = min(max(limit, 1), 1000)
    items, total = await asyncio.gather(
        store.list_audit(limit=limit, offset=offset), store.count_audit())
    return {"items": items, "total": total, "offset": offset, "limit": limit}
