"""下行路由（QR1125 Phase 0）：SSE 优先、MQTT 兜底，控制器经 get_downlink 取用。

设计见 docs/design-transport.md §5.2。主机有 SSE 连接 → 命令直推 SSE 队列；否则
回落 bridge.dispatch_command（paho，MQTT 路径不变）。**get_downlink 按请求动态从
app.state.sse_hub + bridge 计算**，不缓存 RoutingDownlink——避免夹具覆盖 app.state.bridge
后路由仍指向旧 bridge（test_controlled_upgrade 的 bridge_app 夹具会换 bridge）。

Phase 0 范围：dispatch_command（shell/collect/plugin_reload）SSE 优先；publish_update
（升级帧）仍走 MQTT，SSE 升级推送留 Phase 1+（Agent 传输切换后）。控制器保留
bridge None→503 检查（Phase 0 命令通道仍需 bridge），仅把发布调用换成 get_downlink。
"""
import json


def cmd_payload(row):
    """把命令行还原成 Agent 认识的 cmd 帧（镜像 mqtt_bridge.dispatch_command 的
    payload 构造，单事实源在桥接；此处为 SSE 路径复用同款语义，改一处须同步）。

    commands.argv 列对 shell 存 argv 数组、对 collect 存 {"items":[...]} 结构。
    """
    raw = row["argv"]
    if isinstance(raw, str):
        try:
            argv = json.loads(raw or "null")
        except ValueError:
            argv = None
    else:
        argv = raw
    payload = {"id": row["id"], "kind": row["kind"], "timeout": row["timeout"]}
    if isinstance(argv, dict):
        payload["items"] = argv.get("items") or []
        if argv.get("use_shell"):
            payload["use_shell"] = True
        if argv.get("argv"):
            payload["argv"] = argv["argv"]
    else:
        payload["argv"] = argv or []
    return payload


class RoutingDownlink:
    """SSE 优先 + MQTT 兜底的下行组合器。鸭子类型兼容 bridge（dispatch_command/
    publish_update），控制器与 _FakeBridge 测试经 get_downlink 统一取用。"""

    def __init__(self, hub, bridge, store):
        self.hub = hub
        self.bridge = bridge
        self.store = store

    def dispatch_command(self, row):
        """同步（与 bridge.dispatch_command 同签名，控制器非阻塞调用）。"""
        host = row["pod"]
        if self.hub is not None and self.hub.is_connected(host):
            ok = self.hub.push_command(host, cmd_payload(row))
            if ok and self.bridge is not None:
                self.bridge._bump("cmd_published")   # 既有计数器，调用点在本模块
            return ok
        if self.bridge is not None:
            return self.bridge.dispatch_command(row)
        return False

    async def publish_update(self, host, uid, latest):
        # Phase 0：升级帧仍走 MQTT（SSE 升级推送留 Phase 1+）
        if self.bridge is not None:
            return await self.bridge.publish_update(host, uid, latest)
        return False


def get_downlink(request):
    """取下行通道：无 SSE hub 时直接是 bridge（保 _FakeBridge 鸭子类型测试）；
    有 hub 时组合路由。bridge None 且无 hub → None（控制器据此 503）。"""
    bridge = request.app.state.bridge
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        return bridge
    return RoutingDownlink(hub, bridge, request.app.state.store)