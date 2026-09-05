"""管理员登录/登出/身份。"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import current_user

router = APIRouter(prefix="/api")


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request):
    store = request.app.state.store
    if not await store.verify_admin(body.username, body.password):
        await store.add_audit(body.username, "login_fail", {})
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = await store.create_session(body.username)
    await store.add_audit(body.username, "login_ok", {})
    return {"token": token, "username": body.username}


@router.post("/logout")
async def logout(request: Request):
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    if token:
        await request.app.state.store.delete_session(token)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = await current_user(request)
    return {"username": user}
