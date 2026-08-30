"""公共依赖：会话鉴权。"""
from fastapi import HTTPException, Request


def current_user(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    user = request.app.state.store.get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def agent_token_auth(request: Request) -> str:
    """校验 Agent 自更新接口用的 Bearer / X-KK-Token，与 WebSocket hello 同一令牌池。"""
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else request.headers.get("X-KK-Token", "").strip()
    tokens = getattr(request.app.state.hub, "tokens", set())
    if token not in tokens:
        raise HTTPException(status_code=401, detail="invalid agent token")
    return token

