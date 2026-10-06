"""公共依赖：会话鉴权与 Agent IP 白名单。

鉴权统一走 FastAPI 依赖注入（QR-S2）：router 级 `dependencies=[Depends(...)]`
做默认拒绝，需要主体身份的端点用 `CurrentUser` / `AgentIp` 别名取值。
禁止再手写 `await current_user(request)`——漏写一行就是裸奔。
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from ..config import ip_in_whitelist


async def current_user(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    user = await request.app.state.store.get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


async def agent_ip_auth(request: Request) -> str:
    """Agent 自更新接口的 IP 白名单校验（v3：替代原 Bearer token）。

    这里拿到的是直连请求的真实 TCP 源 IP（request.client.host），比 MQTT 侧的
    自报 ip 可靠；注意经反向代理时源 IP 会变成代理地址——Agent 自更新地址
    应配置为内网直连地址（KK_UPDATE_URL），不走公网反代。
    """
    ip = request.client.host if request.client else ""
    networks = getattr(request.app.state, "agent_ips", [])
    if not ip_in_whitelist(networks, ip):
        raise HTTPException(status_code=403, detail="agent ip not allowed")
    return ip


CurrentUser = Annotated[str, Depends(current_user)]
AgentIp = Annotated[str, Depends(agent_ip_auth)]

