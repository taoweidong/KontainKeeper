"""接入 token 管理：查看与在线吊销（Agent 重连时生效）。"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import current_user

router = APIRouter(prefix="/api")


class TokenBody(BaseModel):
    token: str


def _mask(token):
    return token[:4] + "…" + token[-4:] if len(token) > 12 else "***"


@router.get("/tokens")
def list_tokens(request: Request):
    current_user(request)
    store, hub = request.app.state.store, request.app.state.hub
    revoked = set(store.revoked_tokens())
    items = [{"token": _mask(t), "revoked": t in revoked} for t in sorted(hub.tokens)]
    return {"items": items}


@router.post("/tokens/revoke")
def revoke_token(body: TokenBody, request: Request):
    user = current_user(request)
    store, hub = request.app.state.store, request.app.state.hub
    if body.token not in hub.tokens:
        raise HTTPException(status_code=404, detail="token 不在当前接入列表")
    if store.is_token_revoked(body.token):
        raise HTTPException(status_code=400, detail="token 已处于吊销状态")
    store.revoke_token(body.token)
    store.add_audit(user, "token_revoke", {"token": _mask(body.token)})
    return {"ok": True, "note": "已在线的连接保持到断开，重连即被拒绝"}


@router.post("/tokens/restore")
def restore_token(body: TokenBody, request: Request):
    user = current_user(request)
    store, hub = request.app.state.store, request.app.state.hub
    if body.token not in hub.tokens:
        raise HTTPException(status_code=404, detail="token 不在当前接入列表")
    store.restore_token(body.token)
    store.add_audit(user, "token_restore", {"token": _mask(body.token)})
    return {"ok": True}
