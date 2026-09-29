"""审计日志查询。"""
import asyncio

from fastapi import APIRouter, Request

from .deps import current_user

router = APIRouter(prefix="/api")


@router.get("/audit")
async def list_audit(request: Request, limit: int = 200, offset: int = 0,
                     keyword: str = "", actor: str = "", action: str = ""):
    """审计也要能翻页：只靠条数切换时，早期记录永远看不到。

    筛选全部下推到后端（store._audit_filters）：与导出共用同一套语义，
    页面所见即导出所得；total 同条件计算，前端过滤后页内条数才会对得上。
    """
    await current_user(request)
    store = request.app.state.store
    limit = min(max(limit, 1), 1000)
    # 负 offset 直传 SQLite 之外的库会 500（评审 P2）：containers 的写法收口成惯例
    off = offset if offset and offset > 0 else None
    items, total = await asyncio.gather(
        store.list_audit(limit=limit, offset=off, actor=actor or None,
                         action=action or None, keyword=keyword or None),
        store.count_audit(actor=actor or None, action=action or None,
                          keyword=keyword or None))
    return {"items": items, "total": total, "offset": offset, "limit": limit}
