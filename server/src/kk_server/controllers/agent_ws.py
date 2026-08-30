"""Agent WebSocket 接入端点：WS /ws/agent（对外契约，协议见 proto/messages.md）。"""
from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    await ws.app.state.hub.agent_endpoint(ws)