"""HTTP ingest 帧闸门（QR1125 Phase 0）：与 MQTT 入口同款校验，共享常量避免漂移。

MQTT 侧闸门在 mqtt_bridge._route_message（解析 paho msg）；HTTP 侧 body 是裸 JSON，
这里复用同一套尺寸/主机名口径（常量直接从 mqtt_bridge 导入，单事实源）。差异：
- IP 白名单由 agent_ip_auth 按**真实 TCP 源 IP** 把关（比 MQTT 自报 ip 更安全），
  故此处不再查 body 自报 ip；
- 帧归属：MQTT 比对 topic host vs body host，HTTP 只有 body host（处理器内部
  仍做跨主机结果拒收 cmd["pod"]!=host）；
- proto 兼容窗口在 bridge._on_status 内自然生效，不在此处重复。
"""
import json

from .mqtt_bridge import MAX_FRAME_BYTES, MAX_HOST_LEN


class FrameError(Exception):
    """帧校验失败：code 是 HTTP 状态码，控制器映射成 HTTPException。"""

    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def decode_frame(raw):
    """尺寸预检 + JSON 解析（json.loads 前预检，与 QR1120-S2 同口径）。"""
    if len(raw) > MAX_FRAME_BYTES:
        raise FrameError(413, "frame too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise FrameError(422, "invalid JSON frame")
    if not isinstance(body, dict):
        raise FrameError(422, "frame must be a JSON object")
    return body


def frame_host(body):
    """提取并校验主机名（与 tables.pod String(120) 同口径，超长入口拒收）。"""
    host = body.get("host")
    if not isinstance(host, str) or not host:
        raise FrameError(422, "missing host")
    if len(host) > MAX_HOST_LEN:
        raise FrameError(422, "host too long")
    return host