"""公共依赖：会话鉴权、Agent IP 白名单与主机列表的 v4 筛选参数。

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


# ---- v4 主机筛选参数（/api/containers 与 /api/export/hosts 共用同一套）----

_TRUEISH = ("1", "true", "yes", "on")
_FALSEISH = ("0", "false", "no", "off")


def _tri(raw):
    """查询串 → 三态布尔：认不出的一律 `None`（= 不筛）。

    绝不能把 query 原值丢给 store：`"false"`、`"0"` 在 Python 里都是真值，
    `?online=false` 会被筛成「只看在线」、`?docker_unhealthy=0` 会被筛成「只看有
    异常容器」——两个方向都错，而且页面上看不出任何异常（QR-S36 点名的那颗雷）。
    """
    s = str(raw or "").strip().lower()
    if s in _TRUEISH:
        return True
    if s in _FALSEISH:
        return False
    return None


async def host_filter_params(group: str = "", host_type: str = "",
                             docker_unhealthy: str = "", online: str = "",
                             proto_ver: str = "", keyword: str = "",
                             sort: str = "last_seen") -> dict:
    """把主机列表的查询参数归一成 store 的筛选 kwargs。

    枚举白名单（`host_type`、`sort`）由 store 侧统一兜，这里只做类型归一，避免
    两个地方各写一份白名单再各自演化。
    """
    return {
        "group": group,
        "host_type": host_type,
        "keyword": keyword,
        # store 用真值判断决定是否加这条条件，所以只有明确的「真」才置位
        "docker_unhealthy": _tri(docker_unhealthy) is True,
        "online": _tri(online),
        "proto_ver": proto_ver or None,
        "sort": sort,
    }


HostFilters = Annotated[dict, Depends(host_filter_params)]

