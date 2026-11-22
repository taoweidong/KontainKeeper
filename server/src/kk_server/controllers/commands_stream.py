"""SSE 下行命令通道（QR1125 Phase 0）：Agent 出站 GET 长连接收命令。

设计见 docs/design-transport.md §5.2。鉴权挂 agent_ip_auth（真实 TCP 源 IP）。
用 starlette 原生 StreamingResponse（media_type=text/event-stream），零新依赖
（不引 sse-starlette）；async generator 每 15s ping 注释帧保活 + touch 续活，
request.is_disconnected() 检测断连，finally disconnect + set_online(False) 秒级离线
（sweep mark_stale_offline 180s 双保险）。连接建立即 flush_queued_commands 补投离线队列。

事件 id 用命令 id（客户端去重）；retry 字段告知重连延迟。Phase 0 续传以 DB queued
flush 为准（sent 未 ACK 命令的 Last-Event-ID 精确回放留后续，命令收敛靠 sweep + result 幂等）。
nginx 须 proxy_buffering off（X-Accel-Buffering: no 头告知）。
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .deps import agent_ip_auth
from ..services.mqtt_bridge import MAX_HOST_LEN

agent_router = APIRouter(prefix="/api/v1/commands",
                         dependencies=[Depends(agent_ip_auth)])

PING_INTERVAL = 15


@agent_router.get("/stream")
async def command_stream(request: Request, host: str):
    store = request.app.state.store
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="SSE 通道未装配")
    if not host or len(host) > MAX_HOST_LEN:
        raise HTTPException(status_code=422, detail="host 非法")

    queue = hub.connect(host)
    # 重连补投离线期间排队的命令（Last-Event-ID 续传的 Phase 0 形态：DB queued flush）
    await hub.flush_queued_commands(host, store)

    async def gen():
        try:
            yield "retry: 3000\n\n"        # 告知客户端断线 3s 后重连
            while True:
                if await request.is_disconnected():
                    break
                hub.touch(host)            # 续活，sweep_dead 据此判死连接
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"     # 注释帧保活，穿 nginx 不被缓冲攒住
                    continue
                yield "id: %s\ndata: %s\n\n" % (
                    payload.get("id"), json.dumps(payload, ensure_ascii=False))
        finally:
            hub.disconnect(host)
            # 秒级离线（SSE 断连即判离线，不等心跳超时）；sweep 180s 兜底
            await store.set_online(host, False)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})