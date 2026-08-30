"""连接管理：hello 构建、发送 JSON、安全关闭。"""
import json
import uuid

import kk_config
from kk_ws import WSClient


def build_hello(cfg):
    return {
        "t": "hello",
        "id": uuid.uuid4().hex[:12],
        "proto_ver": kk_config.PROTO_VER,
        "pod": cfg["hostname"],
        "image": cfg["image"],
        "agent_ver": kk_config.AGENT_VER,
        "token": cfg["token"],
        "interval": cfg["interval"],
    }


def connect(cfg):
    """建立 WebSocket 并发送 hello；失败抛异常，由主循环退避重试。"""
    ws = WSClient(cfg["server"])
    ws.connect(timeout=10)
    send_json(ws, build_hello(cfg))
    return ws


def send_json(ws, obj):
    ws.send_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def safe_close(ws):
    if ws is None:
        return
    try:
        ws.close()
    except Exception:
        pass
