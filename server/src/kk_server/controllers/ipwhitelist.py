"""IP 白名单管理 + 拦截记录查询（QR1124）。

- 白名单 CRUD 挂 AdminUser（登录 + admin 角色，安全配置）；每个变更落审计
  agent_ips_update + 即时刷新快照（refresh_agent_ips），运行时生效无需重启。
- 拦截记录列表 AdminUser（含源 IP，管理员可见；operator 经审计页 ip_rejected
  仍能看到拦截事件，权限口径与审计页一致）。
- env KK_AGENT_IPS 为启动种子，DB 为运行时真相源（见 models/_store_ipwhitelist）。
  条目校验复用 config.parse_ip_whitelist（与启动解析同一语义）。
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import AdminUser, refresh_agent_ips
from ..models.helpers import clamp, offset_or_none

router = APIRouter(prefix="/api")


class IpRuleCreate(BaseModel):
    cidr: str
    remark: str = ""


class IpRuleUpdate(BaseModel):
    enabled: bool | None = None
    remark: str | None = None


async def _refresh(request: Request):
    store = request.app.state.store
    await refresh_agent_ips(request.app, store, request.app.state.bridge)


@router.get("/system/ip-whitelist")
async def list_rules(request: Request, _: AdminUser):
    store = request.app.state.store
    return {"items": await store.list_ip_rules()}


@router.post("/system/ip-whitelist")
async def add_rule(request: Request, body: IpRuleCreate, admin: AdminUser):
    store = request.app.state.store
    try:
        cidr = await store.add_ip_rule(body.cidr, body.remark)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"IP/CIDR 格式非法：{e}")
    except KeyError:
        raise HTTPException(status_code=409, detail="该网段已在白名单中")
    await store.add_audit(admin, "agent_ips_update",
                          {"op": "add", "cidr": cidr, "remark": body.remark})
    await _refresh(request)
    return {"ok": True, "cidr": cidr}


@router.put("/system/ip-whitelist/{rule_id}")
async def update_rule(request: Request, rule_id: int, body: IpRuleUpdate,
                      admin: AdminUser):
    store = request.app.state.store
    n = await store.update_ip_rule(rule_id, enabled=body.enabled, remark=body.remark)
    if not n:
        raise HTTPException(status_code=404, detail="白名单条目不存在")
    await store.add_audit(admin, "agent_ips_update",
                          {"op": "update", "id": rule_id,
                           "enabled": body.enabled, "remark": body.remark})
    await _refresh(request)
    return {"ok": True}


@router.delete("/system/ip-whitelist/{rule_id}")
async def delete_rule(request: Request, rule_id: int, admin: AdminUser):
    store = request.app.state.store
    n = await store.delete_ip_rule(rule_id)
    if not n:
        raise HTTPException(status_code=404, detail="白名单条目不存在")
    await store.add_audit(admin, "agent_ips_update", {"op": "delete", "id": rule_id})
    await _refresh(request)
    return {"ok": True}


@router.get("/ip-rejects")
async def list_rejects(request: Request, _: AdminUser, limit: int = 200,
                       offset: int = 0, ip: str = ""):
    """拦截记录按 IP 聚合：last_seen 倒序，ip 模糊筛选下推（与导出同语义）。"""
    store = request.app.state.store
    limit = clamp(limit, 1, 1000)
    off = offset_or_none(offset)
    items, total = await asyncio.gather(
        store.list_ip_rejects(limit=limit, offset=off, ip=ip or None),
        store.count_ip_rejects(ip=ip or None))
    return {"items": items, "total": total, "offset": offset, "limit": limit}