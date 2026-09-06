"""MQTT 传输层：替代原先自研的 261 行 WebSocket 客户端（ws.py + conn.py）。

为什么用 MQTT 而不是继续自研长连接协议：
- **LWT（遗嘱消息）**：Agent 异常掉线时由 Broker 自动发布 offline，服务端无需靠
  心跳超时猜测，彻底消除半开连接误判
- **Retained 消息**：最新指标与在线状态保留在 Broker，服务端重启或水平扩容出新实例时
  立刻拿到全量现状，不必等一整个心跳周期
- **持久会话 + QoS1**：离线期间的命令由 Broker 排队，重连自动补发——原先手写的
  `pending_for` 补发逻辑（且存在 sent 状态丢命令的缺陷）直接被协议接管
- **QoS 分级**：心跳 QoS0（丢了无所谓）、命令与结果 QoS1（必须送达）

连接、重连退避、PINGREQ 保活、离线队列全部由 paho-mqtt 的后台线程负责，
Agent 主线程只做「定时采集 + 发布」。

主题布局（前缀可通过 KK_TOPIC_PREFIX 调整）：

    kk/v1/{host}/status    Agent → Server  在线状态，QoS1 + retain + LWT
    kk/v1/{host}/hb        Agent → Server  心跳指标，QoS0，不 retain
    kk/v1/{host}/result    Agent → Server  命令结果，QoS1
    kk/v1/{host}/cmd       Server → Agent  命令下发，QoS1

帧格式与语义以 proto/messages.md（协议 v3）为准。
"""
import json
import socket
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from . import config as kk_config

PROTO = mqtt.MQTTv311
QOS_HB = 0
QOS_CMD = 1
# 离线时最多缓存的结果/命令条数，防止长时间断网把内存撑爆
MAX_QUEUED = 512
# paho 内置指数退避重连的区间（秒）
RECONNECT_MIN, RECONNECT_MAX = 1, 60
# 结果分块无法送达（out-queue 溢出等）时回传的失败退出码：
# 让服务端把命令收敛成 failed，而不是永远停在 running。
RC_SEND_FAILED = -3


class TransportError(Exception):
    pass


def parse_broker(url, default_port=1883):
    """解析 mqtt://host[:port] → dict。mqtts:// 启用 TLS。

    v3 起不再支持 URL 内嵌凭据（Broker 匿名模式，权限管理由服务端
    KK_AGENT_IPS 白名单承担）；带 user:pass@ 的旧写法显式报错，
    避免凭据被当成主机名解析出难以理解的连接错误。
    """
    s = (url or "").strip()
    secure = s.startswith("mqtts://")
    if secure:
        s = "mqtt://" + s[len("mqtts://"):]
    elif not s.startswith("mqtt://"):
        raise TransportError("KK_SERVER 必须是 mqtt:// 或 mqtts:// 地址，当前为 %r" % url)
    rest = s[len("mqtt://"):]
    if "@" in rest:
        raise TransportError(
            "KK_SERVER 不再支持内嵌凭据（v3 起 Broker 匿名模式）：直接写地址即可，"
            "如 mqtt://broker:1883")
    if rest.startswith("["):
        host, _, port = rest[1:].partition("]")   # IPv6 字面量 [::1]:1883
        port = port.lstrip(":")
    else:
        host, _, port = rest.partition(":")
    host = host.strip("[]")
    if not host:
        raise TransportError("KK_SERVER 缺少主机名：%r" % url)
    return {
        "host": host,
        "port": int(port or (8883 if secure else default_port)),
        "secure": secure,
    }


def detect_outbound_ip(host, port=1883):
    """探测本机到 Broker 方向的出口 IP。

    UDP connect 只在内核里选路由（不会实际发包），getsockname 拿到的即是
    Broker 可达网卡上的本地地址——多网卡环境自动选中正确一侧。探测失败
    （如无网络栈）返回空串，帧里 ip 留空由服务端按白名单拒绝。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, port))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


class Transport:
    """MQTT 客户端封装。

    回调运行在 paho 的网络线程上，因此 on_cmd 回调里**只做入队**，
    实际命令执行交给 executor 的线程池，避免阻塞网络循环。
    """

    def __init__(self, cfg, log, on_cmd=None):
        self.cfg = cfg
        self.log = log
        self.on_cmd = on_cmd
        self.host = cfg["host"]
        self.prefix = cfg["topic_prefix"].strip("/") or "kk/v1"
        self.base = "%s/%s" % (self.prefix, self.host)

        broker = parse_broker(cfg["server"])
        self.broker = broker
        self.connected = threading.Event()
        self._stopping = threading.Event()
        # 出口 IP 自报（v3）：KK_ADVERTISE_IP 显式覆盖 > 自动探测。
        # 服务端据 KK_AGENT_IPS 白名单校验该值（MQTT 经 Broker 中转拿不到
        # 发布者真实 TCP 源 IP，自报是协议约束下的务实解，适合内网可信环境）
        self.ip = (cfg.get("advertise_ip") or
                   detect_outbound_ip(broker["host"], broker["port"]))

        # client_id 必须稳定：Broker 靠它识别「同一个 Agent」并保留离线命令队列
        client_id = "%s-%s" % (cfg.get("client_id") or "kk", self.host)
        self.cli = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id[:128],
            protocol=PROTO,
            clean_session=False,  # 持久会话：离线命令由 Broker 排队
        )
        if broker["secure"]:
            ca = cfg.get("tls_ca") or None
            if ca:
                # 已配 CA 时**绝不**调 tls_insecure_set(True)：paho 会连带把
                # verify_mode 置成 CERT_NONE，让 KK_TLS_CA 完全失效（代码审查 P1-3）。
                # 因此 KK_TLS_INSECURE 在配了 CA 的情况下被显式忽略并告警。
                if cfg.get("tls_insecure"):
                    self.log.warning("已配置 KK_TLS_CA，忽略 KK_TLS_INSECURE"
                                     "（不会为兼容主机名而关闭证书校验）")
                self.cli.tls_set(ca_certs=ca, cert_reqs=ssl.CERT_REQUIRED)
            else:
                self.log.warning("mqtts:// 未配置 KK_TLS_CA：仅加密、不校验对端证书（中间人风险）")
                self.cli.tls_set(cert_reqs=ssl.CERT_NONE)
                self.cli.tls_insecure_set(True)

        # 遗嘱：异常断开时由 Broker 代为发布 offline（retain，服务端立刻可见）
        self.cli.will_set(self.topic("status"), self._status_payload(False),
                          qos=QOS_CMD, retain=True)
        self.cli.max_queued_messages_set(int(self.cfg.get("max_queued") or MAX_QUEUED))
        self.cli.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect
        self.cli.on_message = self._on_message

    # ---- 主题 ----
    def topic(self, name):
        return "%s/%s" % (self.base, name)

    @property
    def cmd_topic(self):
        return self.topic("cmd")

    # ---- 载荷 ----
    def _status_payload(self, online, reason=""):
        # 带 ip（v3）：服务端据 KK_AGENT_IPS 白名单校验，白名单外的上报全部
        # 拒绝并审计。Broker 匿名模式下这是唯一的接入管控手段。
        return json.dumps({
            "online": online,
            "host": self.host,
            "ip": self.ip,
            "agent_ver": kk_config.AGENT_VER,
            "proto_ver": kk_config.PROTO_VER,
            "image": self.cfg.get("image", ""),
            "interval": self.cfg.get("interval", 60),
            "reason": reason,
            "ts": int(time.time()),
        }, ensure_ascii=False, separators=(",", ":"))

    # ---- 生命周期 ----
    def start(self):
        b = self.broker
        self.cli.connect_async(b["host"], b["port"], keepalive=int(self.cfg.get("keepalive") or 60))
        self.cli.loop_start()  # 后台网络线程：收发 + 自动重连
        self.log.info("mqtt connecting to %s:%s as %s", b["host"], b["port"], self.base)

    def wait_ready(self, timeout=15, stop=None):
        """等首次连接就绪。

        必须对 stop 敏感：Broker 不可达时若一路等满 timeout，容器停止信号
        就被卡在启动等待里，最终被 SIGKILL 而不是优雅退出。
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.connected.wait(0.5):
                return True
            if stop is not None and stop.is_set():
                return False
            if time.monotonic() >= deadline:
                return self.connected.is_set()

    def stop(self, reason="stopping"):
        self._stopping.set()
        try:
            if self.cli.is_connected():
                self.publish_status(False, reason)
        except Exception:
            pass
        try:
            self.cli.disconnect()
        except Exception:
            pass
        try:
            self.cli.loop_stop()
        except Exception:
            pass
        self.connected.clear()

    # ---- 发布 ----
    def _pub(self, suffix, payload, qos=QOS_HB, retain=False):
        """QoS1 交给 paho 的 out-queue 做离线排队，QoS0 才在断线时直接跳过。

        不要在入口判 is_connected：paho 在未连接时仍会把 QoS1 消息入队（返回
        MQTT_ERR_NO_CONN），重连后自动补发——这正是选 MQTT 而不自研长连接的目的。
        判了就等于把离线排队能力自己短路掉，命令结果会在重连窗口内静默丢失。
        """
        if qos == QOS_HB and not self.cli.is_connected():
            return False  # 心跳积压无意义，等下一帧
        info = self.cli.publish(self.topic(suffix), payload, qos=qos, retain=retain)
        # NO_CONN = 已入队待重连补发，同样算尽责；QUEUE_SIZE 等真失败才返回 False
        return info.rc in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN)

    def publish_status(self, online, reason=""):
        return self._pub("status", self._status_payload(online, reason), QOS_CMD, True)

    def publish_hb(self, metrics, custom=None):
        payload = json.dumps({
            "host": self.host,
            "ip": self.ip,
            "ts": int(time.time()),
            "interval": self.cfg["interval"],
            "agent_ver": kk_config.AGENT_VER,
            "metrics": metrics,
            "custom": custom or {},
        }, ensure_ascii=False, separators=(",", ":"))
        # 不 retain：retained 心跳会在服务端每次建立订阅时整批回放，
        # 而指标真相在数据库里，Broker 只该做搬运而非存档。
        return self._pub("hb", payload, QOS_HB, False)

    def publish_result(self, result):
        """命令结果分块回传；每块 QoS1，末块带 done 标记。"""
        frame = dict(result)
        frame["ip"] = self.ip   # v3：上行帧统一携带自报 ip 供白名单校验
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        return self._pub("result", payload, QOS_CMD, False)

    # ---- 回调（运行在 paho 网络线程）----
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.log.warning("mqtt connect failed: %s", reason_code)
            return
        self.connected.set()
        client.subscribe(self.cmd_topic, qos=QOS_CMD)
        self.publish_status(True, "online")
        self.log.info("mqtt connected (session_present=%s), subscribed %s",
                      getattr(flags, "session_present", False), self.cmd_topic)

    def _on_disconnect(self, client, userdata, flags, reason_code=0, properties=None):
        self.connected.clear()
        if not self._stopping.is_set():
            self.log.warning("mqtt disconnected (rc=%s), paho will retry with backoff", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            self.log.warning("bad command frame ignored")
            return
        if not isinstance(payload, dict):
            return
        if self.on_cmd:
            try:
                self.on_cmd(payload)
            except Exception:
                self.log.exception("command dispatch failed")
