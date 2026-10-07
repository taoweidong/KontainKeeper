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

    kk/v1/{host}/status    Agent → Server  在线状态 + 主机元信息（v4），QoS1 + retain + LWT
    kk/v1/{host}/hb        Agent → Server  心跳指标，QoS0，不 retain
    kk/v1/{host}/result    Agent → Server  命令结果，QoS1
    kk/v1/{host}/cmd       Server → Agent  命令下发，QoS1

帧格式与语义以 proto/messages.md（协议 v4）为准。
"""
import json
import socket
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from . import config as kk_config
from . import hostinfo

PROTO = mqtt.MQTTv311
QOS_HB = 0
QOS_CMD = 1
# 离线时最多缓存的 QoS1 帧数（QR-A22 的显式预算）。心跳是 QoS0 且断线时直接丢弃
# （见 _pub），所以这条队列里只有 status 帧与命令结果分块：单块 base64 后 ≈64KB，
# 512 块就是 ≈32MB，一台 Agent 光靠排队就能越过 25–35MB 的常驻口径。128 块把最坏
# 情况压到 ≈8MB；断网期超长时宁可让后排分块溢出（`send_result` 会补发截断终态，
# 服务端拿到 failed+truncated 而不是永远 running），也不吃内存。
MAX_QUEUED = 128
# paho 内置指数退避重连的区间（秒）
RECONNECT_MIN, RECONNECT_MAX = 1, 60
# 结果分块无法送达（out-queue 溢出等）时回传的失败退出码：
# 让服务端把命令收敛成 failed，而不是永远停在 running。
RC_SEND_FAILED = -3


class TransportError(Exception):
    pass


def parse_broker(url, default_port=1883):
    """解析 mqtt://host[:port] → dict。mqtts:// 启用 TLS。

    URL 内不再支持 user:pass@ 内嵌凭据：Broker 鉴权走独立的
    KK_MQTT_USERNAME / KK_MQTT_PASSWORD 环境变量（可选，配合 Broker 端
    password_file + ACL，见 docs/deployment.md「启用 MQTT 鉴权」），
    带内嵌凭据的旧写法显式报错，避免被当成主机名解析出难以理解的连接错误。
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
            "KK_SERVER 不支持内嵌凭据：Broker 鉴权请改用 KK_MQTT_USERNAME / "
            "KK_MQTT_PASSWORD 环境变量，地址直接写 mqtt://broker:1883")
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
        # v4 主机元信息：只在启动时探测一次并缓存。status 帧在重连/优雅停机时都会
        # 发，而 /proc 与 platform 探测虽然便宜，重试循环里反复做也是纯浪费；
        # 且整帧进程生命周期内几乎不可能变（OS 不会在运行时换）。
        self.env = hostinfo.host_env()
        self.group = str(cfg.get("group") or "")
        self.labels = dict(cfg.get("labels") or {})
        # 能力声明：v4 先只**上报**，服务端那侧的「这台机器干不了就别发」门禁尚未接入
        # （眼下 shell 由 Agent 侧 allow_shell 自己拦）。目前只声明 shell；docker
        # 能力随 v4 之后的容器阶段接入。
        self.caps = {"shell": bool(cfg.get("allow_shell", True))}

        # client_id 必须稳定：Broker 靠它识别「同一个 Agent」并保留离线命令队列
        client_id = "%s-%s" % (cfg.get("client_id") or "kk", self.host)
        self.cli = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id[:128],
            protocol=PROTO,
            clean_session=False,  # 持久会话：离线命令由 Broker 排队
        )
        # 可选 Broker 鉴权（默认匿名）：配置了用户名才启用，匿名部署零改动
        if cfg.get("mqtt_username"):
            self.cli.username_pw_set(cfg["mqtt_username"],
                                     cfg.get("mqtt_password") or "")
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
        # v4 起另带 env/group/labels/caps：管理面据此把「K8S 容器 IDE 专用」
        # 扩成「任意 Linux 主机」的机队视图与能力门禁。
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
            "env": self.env,
            "group": self.group,
            "labels": self.labels,
            "caps": self.caps,
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
        """优雅退出：先把 offline 状态帧送到 Broker 并等它落网，再断开。

        为什么必须等（QR-A7/A14）：干净的 DISCONNECT **不会触发 LWT**，服务端只能
        靠这一帧判下线；而紧接着的 `disconnect()` 会掐断还在发送队列里的帧——
        不等就等于发了个寂寞。三段收尾过去全静默 `except: pass`，退出路径出问题
        时现场什么都没有，现在至少留一行 warning。
        """
        self._stopping.set()
        try:
            if self.cli.is_connected():
                self.publish_status(False, reason, wait=True)
        except Exception as e:
            self.log.warning("stop: offline frame failed: %s", e)
        try:
            self.cli.disconnect()
        except Exception as e:
            self.log.warning("stop: disconnect failed: %s", e)
        try:
            self.cli.loop_stop()
        except Exception as e:
            self.log.warning("stop: loop_stop failed: %s", e)
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

    def publish_status(self, online, reason="", wait=False):
        """发布状态帧（QoS1 + retain）。

        `wait=True` 用于**进程即将消失**的路径（优雅停止、execv 自更新）：不等
        PUBACK 的话，紧随其后的断开会把还在队列里的这一帧一起带走，服务端就只剩
        一条空 reason 的记录可看。常规上线不需要等，别默认打开。
        此时返回值口径是「Broker 已 ack」，不是「已入队」。
        """
        payload = self._status_payload(online, reason)
        if not wait:
            return self._pub("status", payload, QOS_CMD, True)
        info = self.cli.publish(self.topic("status"), payload, qos=QOS_CMD, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            info.wait_for_publish(1.0)
        except Exception as e:
            self.log.warning("status frame PUBACK wait failed: %s", e)
            return False
        # rc==SUCCESS 只证明帧进了发送队列；wait_for_publish 超时是**静默返回**
        # （paho 只在 rc>0 才 raise），所以送达与否只能问 is_published。
        # 不等这个确认就报成功，紧随其后的 disconnect/execv 会把队列里的帧一起掐掉。
        if not info.is_published():
            self.log.warning("status frame not acknowledged before timeout: %s",
                             reason or "online")
            return False
        return True

    def announce_update(self):
        """自更新前的优雅离线宣告（B6.1）：发 reason=updating 并**等它真正送达**。

        为什么必须等到 PUBACK，而不是 publish 了就算：execv 会直接替换进程，
        paho 发送队列里还没出网的帧会随进程一起消失 —— 只 publish 不等，服务端
        可能既收不到 updating、又只看到 Broker 补发的 LWT（reason 为空），等于白做。

        本方法是钩子，异常一律吞掉：宣告失败绝不允许阻断更新主流程。
        """
        try:
            if not self.cli.is_connected():
                return
            self.publish_status(False, "updating", wait=True)
        except Exception:
            pass

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

    def publish_result(self, result, urgent=False):
        """命令结果分块回传；每块 QoS1，末块带 done 标记。

        `urgent=True` 只留给**失败终态**（QR-A6）：out-queue 被大输出的分块塞满时，
        QoS1 的终态帧会被一起拒收（MQTT_ERR_QUEUE_SIZE），服务端那一行就永远停在
        running。QoS0 不入队、连着就当场发出——用「这一帧可能丢」换「不被自己的
        积压挡在门外」，真丢了还有服务端 30min 超时清扫兜底。
        """
        frame = dict(result)
        frame["ip"] = self.ip   # v3：上行帧统一携带自报 ip 供白名单校验
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        return self._pub("result", payload, QOS_HB if urgent else QOS_CMD, False)

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
