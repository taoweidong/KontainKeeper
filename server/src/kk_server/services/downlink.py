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
    """把命令行还原成 Agent 认识的 cmd 帧（QR1128-S3：单事实源）。

    MQTT 桥接的 dispatch_command 与 SSE 直推都经本函数构造 payload——此前
    两处各持一份逐字同构的实现，「改一处须同步」的注释正是漂移源。
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


async def flush_queued(host, store, dispatch):
    """重连补投 queued 命令的共用骨架（QR1128-S11，design-transport §5.2）。

    先 mark sent 刷计时再逐条补投——与 MQTT 先例同口径：若推之前进程重启，
    行已 sent 不会被重复 flush（避免重投），真丢的由 sweep_command_timeouts
    兜底收敛。dispatch(row)->bool 由调用方注入：SSE 连接建立触发点推自己的
    队列，status online 触发点经下行路由（SSE 优先、MQTT 兜底）。
    """
    rows = await store.queued_commands(host)
    if not rows:
        return 0
    await store.mark_queued_commands_sent([r["id"] for r in rows])
    n = 0
    for r in rows:
        if dispatch(r):
            n += 1
    return n


class RoutingDownlink:
    """SSE 优先 + MQTT 兜底的下行组合器。鸭子类型兼容 bridge（dispatch_command/
    publish_update），控制器与 _FakeBridge 测试经 get_downlink 统一取用。"""

    def __init__(self, hub, bridge):
        self.hub = hub
        self.bridge = bridge

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
    return RoutingDownlink(hub, bridge)