"""管理员登录/登出/身份。"""
import threading
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import current_user

router = APIRouter(prefix="/api")

# 登录失败限流（P1-5）：按用户名记连续失败次数，达到阈值后临时锁定。
# 内存态，重启即清零——足以拖慢在线爆破，不依赖外部存储。
_LOGIN_FAILS: dict[str, int] = defaultdict(int)
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}
_LOGIN_LIMIT_LOCK = threading.Lock()
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request):
    store = request.app.state.store
    username = body.username
    now = time.time()
    with _LOGIN_LIMIT_LOCK:
        until = _LOGIN_LOCKED_UNTIL.get(username, 0)
        locked = until > now
    if locked:
        await store.add_audit(username, "login_locked",
                              {"retry_after": int(until - now)})
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    ok = await store.verify_admin(username, body.password)
    locked_now = False
    with _LOGIN_LIMIT_LOCK:
        if not ok:
            _LOGIN_FAILS[username] += 1
            fails = _LOGIN_FAILS[username]
            locked_now = fails >= MAX_LOGIN_FAILS
            if locked_now:
                # 达到阈值：立即锁定，本次失败直接回 429（而非等下一次）
                _LOGIN_LOCKED_UNTIL[username] = now + LOGIN_LOCK_SECONDS
                _LOGIN_FAILS[username] = 0
        else:
            # 成功：清零该用户失败计数与锁定
            _LOGIN_FAILS.pop(username, None)
            _LOGIN_LOCKED_UNTIL.pop(username, None)
    if not ok:
        if locked_now:
            await store.add_audit(username, "login_locked",
                                  {"retry_after": LOGIN_LOCK_SECONDS})
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
        await store.add_audit(username, "login_fail", {"fails": _LOGIN_FAILS.get(username, 0)})
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = await store.create_session(username)
    await store.add_audit(username, "login_ok", {})
    return {"token": token, "username": username}


@router.post("/logout")
async def logout(request: Request):
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    actor = None
    if token:
        # 先解析出用户名再删会话，便于审计登出主体
        actor = await request.app.state.store.get_session(token)
        await request.app.state.store.delete_session(token)
    # P2 审计：登出动作此前未留痕，安全事件不可追溯
    await request.app.state.store.add_audit(actor or "unknown", "logout", {})
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = await current_user(request)
    return {"username": user}
