"""公共依赖：会话鉴权。"""
from fastapi import HTTPException, Request


async def current_user(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    user = await request.app.state.store.get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


async def agent_token_auth(request: Request) -> str:
    """校验 Agent 自更新接口用的 Bearer / X-KK-Token，与 status 帧同一令牌池。

    注意：MQTT 3.1.1 下服务端看不到发布者的认证身份，主机级隔离由 Broker 的
    password_file + pattern ACL 保证；这里只保护 HTTP 侧的更新接口。
    """
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else request.headers.get("X-KK-Token", "").strip()
    tokens = getattr(request.app.state, "agent_tokens", set())
    if token not in tokens:
        raise HTTPException(status_code=401, detail="invalid agent token")
    # 吊销校验：旧 WS hello 是查的，迁移到 MQTT 时差点丢掉这道闸门
    if await request.app.state.store.is_token_revoked(token):
        raise HTTPException(status_code=401, detail="agent token revoked")
    return token

