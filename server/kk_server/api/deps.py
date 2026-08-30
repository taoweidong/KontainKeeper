"""公共依赖：会话鉴权。"""
from fastapi import HTTPException, Request


def current_user(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    user = request.app.state.store.get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user
