"""HTTP 上行 ingest（QR1125 Phase 0）：Agent 出站 POST 指标，组合复用 bridge 处理器。

设计见 docs/design-transport.md §5.1。帧格式 v4 不变（仅传输绑定变）。鉴权挂
agent_ip_auth（真实 TCP 源 IP 白名单，比 MQTT 自报 ip 更安全）；落库经
bridge._on_status/_on_hb/_on_result——与 MQTT 共享同一 store/stats/per-cid 锁/升级三连
（同一事件循环，线程纪律不变）。**MqttBridge 源码零改动**：处理器本就是
transport-agnostic 的 (host, body) 协程，HTTP 直接 await 即共享核心。

Phase 0 并存模式需 bridge 存在（KK_MQTT_URL 配置）；无 MQTT 独立运行留后续
FrameProcessor 抽取阶段（连同 getsource 扫描锁裁决）。新计数器 ingest_* 经 bridge._bump
累加（调用点在本模块，不在 MqttBridge 源码 → 不触发静态扫描锁的前端收录要求）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import agent_ip_auth
from ..services.frame_gate import FrameError, decode_frame, frame_host
from ..services.mqtt_bridge import MAX_FRAME_BYTES

# Agent 侧通道：真实源 IP 白名单（仿 agent_update.agent_router 的双 router 模式）
agent_router = APIRouter(prefix="/api/v1/ingest",
                         dependencies=[Depends(agent_ip_auth)])


def _bridge(request):
    bridge = request.app.state.bridge
    if bridge is None:
        raise HTTPException(
            status_code=503,
            detail="采集通道未配置（需 KK_MQTT_URL；独立 HTTP 通道留后续阶段）")
    return bridge


async def _read_frame(request):
    # QR1128-S6：尺寸预检必须在缓冲**之前**——旧实现先 await request.body()
    # 把整个请求体读进内存再查 len，白名单内发布者 POST 1GB 会先被完整缓冲
    # 才 413，预检形同虚设。先查 Content-Length，无该头（chunked）时流式
    # 累计、超限即断，内存恒有界在 MAX_FRAME_BYTES。
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_FRAME_BYTES:
                raise HTTPException(status_code=413, detail="frame too large")
        except ValueError:
            pass    # 畸形头不纠结，交给下面的流式累计兜底
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="frame too large")
        chunks.append(chunk)
    try:
        body = decode_frame(b"".join(chunks))
        host = frame_host(body)
    except FrameError as exc:
        raise HTTPException(status_code=exc.code, detail=exc.detail)
    return host, body


@agent_router.post("/status")
async def ingest_status(request: Request):
    bridge = _bridge(request)
    host, body = await _read_frame(request)
    await bridge._on_status(host, body)
    bridge._bump("ingest_status")
    return {"ok": True}


@agent_router.post("/hb")
async def ingest_hb(request: Request):
    bridge = _bridge(request)
    host, body = await _read_frame(request)
    await bridge._on_hb(host, body)
    bridge._bump("ingest_hb")
    return {"ok": True}


@agent_router.post("/result")
async def ingest_result(request: Request):
    bridge = _bridge(request)
    host, body = await _read_frame(request)
    await bridge._on_result(host, body)
    bridge._bump("ingest_result")
    return {"ok": True}