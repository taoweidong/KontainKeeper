"""MQTT 桥接：替代原 Hub 的内存连接表，把连接可靠性整体交给 Broker。

服务端在这里变成「无状态桥」——只负责把上行消息路由进库、把命令发布到主题：

| 原来自己做的                | 现在谁在做                                |
|-----------------------------|-------------------------------------------|
| 内存 conn dict + 在线判定   | Broker 的 retained status + LWT           |
| 心跳超时判半开连接（4404）  | MQTT keepalive，由 Broker 强制断开        |
| 离线命令 pending_for 补发   | 持久会话 + QoS1，Broker 排队重连自动补投  |
| hello 的 token 校验         | Broker 的 password_file + pattern ACL     |

线程模型：paho 回调跑在它自己的网络线程，而 SQLite 连接与 Store 的锁都不是
跨线程共享设计，所以这里一律 `loop.call_soon_threadsafe` 把处理调度回事件循环，
在事件循环里同步落库。多实例扩容只需改 `_sub_topics`（加 `$share/{group}/` 前缀）
并保证每个实例 client_id 唯一——共用 client_id 会被 Broker 判为重复会话而互相踢下线。
"""
import json
import logging
import ssl
import threading

import paho.mqtt.client as mqtt

from ..models.version import version_lt

log = logging.getLogger("kk.bridge")

QOS_STATUS = 1
QOS_HB = 0
QOS_RESULT = 1
QOS_CMD = 1
SWEEP_INTERVAL = 30
# 心跳丢失宽限：status 的 retained online 只有在 Broker 崩溃且没发 LWT 时才会失真，
# 用「N 倍心跳周期」兜底把这种僵尸在线判成离线。
OFFLINE_GRACE = 180


class MqttBridge:
    def __init__(self, store, settings, tokens, loop=None, proto_ver=2):
        self.store = store
        self.s = settings
        self.tokens = set(tokens)
        self.proto_ver = proto_ver
        self.loop = loop
        self.prefix = (settings.mqtt_prefix or "kk/v1").strip("/")
        self.connected = threading.Event()
        self._stopping = threading.Event()
        self._sched = None          # 事件循环里跑，用于 to_thread 派发
        self.stats = {"status": 0, "hb": 0, "result": 0, "rejected": 0}

        self.cli = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            protocol=mqtt.MQTTv311,
            clean_session=False,   # 服务端重连后仍保留订阅；命令排队是 Agent 侧的事
        )
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect
        self.cli.on_message = self._on_message
        if settings.mqtt_username:
            self.cli.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_tls_ca or settings.mqtt_url.startswith("mqtts://"):
            ca = settings.mqtt_tls_ca or None
            self.cli.tls_set(ca_certs=ca, cert_reqs=ssl.CERT_REQUIRED if ca else ssl.CERT_NONE)
            self.cli.tls_insecure_set(bool(settings.mqtt_tls_insecure))
        self.cli.reconnect_delay_set(min_delay=1, max_delay=60)

    # ---- 生命周期 ----
    def start(self):
        host, port, secure = self._parse_url(self.s.mqtt_url)
        self.cli.connect_async(host, port, keepalive=self.s.mqtt_keepalive)
        self.cli.loop_start()
        log.info("mqtt bridge connecting to %s:%s (prefix=%s client_id=%s)",
                 host, port, self.prefix, self.s.mqtt_client_id)

    def stop(self):
        self._stopping.set()
        try:
            self.cli.loop_stop()
            self.cli.disconnect()
        except Exception:
            log.debug("bridge stop ignored", exc_info=True)
        self.connected.clear()

    @staticmethod
    def _parse_url(url):
        s = (url or "").strip()
        secure = s.startswith("mqtts://")
        if not (secure or s.startswith("mqtt://")):
            raise ValueError("KK_MQTT_URL 必须是 mqtt:// 或 mqtts:// 地址，当前为 %r" % url)
        rest = s.split("://", 1)[1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        if rest.startswith("["):
            host, _, port = rest[1:].partition("]")   # IPv6 字面量 [fe80::1]:1883
            port = port.lstrip(":")
        else:
            host, _, port = rest.partition(":")
        if not host:
            raise ValueError("KK_MQTT_URL 缺少主机名：%r" % url)
        return host, int(port or (8883 if secure else 1883)), secure

    def _sub_topics(self):
        """唯一需要改的地方：多实例时改成 $share/{group}/... 并逐实例换 client_id。"""
        p = self.prefix
        return [(p + "/+/status", QOS_STATUS), (p + "/+/hb", QOS_HB), (p + "/+/result", QOS_RESULT)]

    # ---- paho 回调（网络线程）----
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.warning("broker connect failed: %s", reason_code)
            return
        self.connected.set()
        for topic, qos in self._sub_topics():
            client.subscribe(topic, qos=qos)
        log.info("mqtt bridge ready, subscribed %s", [t for t, _ in self._sub_topics()])

    def _on_disconnect(self, client, userdata, flags, reason_code=0, properties=None):
        self.connected.clear()
        if not self._stopping.is_set():
            log.warning("broker disconnected (rc=%s), paho 将退避重连", reason_code)

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        depth = len(self.prefix.split("/"))
        try:
            host, suffix = parts[depth], parts[depth + 1]
            body = json.loads(msg.payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, IndexError):
            log.warning("忽略无法解析的帧 topic=%s", msg.topic)
            return
        if not isinstance(body, dict) or not host:
            return
        fn = {"status": self._on_status, "hb": self._on_hb,
              "result": self._on_result}.get(suffix)
        if fn is None:
            return
        self._dispatch(fn, host, body)

    def _dispatch(self, fn, host, body):
        """把处理调度回事件循环：Store 的连接与锁不做跨线程共享。"""
        if self.loop is None:
            fn(host, body)      # 单测/无循环时同步执行
            return
        self.loop.call_soon_threadsafe(fn, host, body)

    # ---- 三类上行帧（事件循环线程）----
    def _token_ok(self, body):
        token = str(body.get("token") or "")
        return bool(token) and token in self.tokens and not self.store.is_token_revoked(token)

    def _on_status(self, host, body):
        if not self._token_ok(body):
            self.stats["rejected"] += 1
            self.store.add_audit("mqtt", "status_rejected", {"host": host})
            log.warning("拒绝非法 status：host=%s（token 缺失或不认识）", host)
            return
        if int(body.get("proto_ver") or 0) != self.proto_ver:
            self.stats["rejected"] += 1
            self.store.add_audit("mqtt", "proto_mismatch",
                                 {"host": host, "proto_ver": body.get("proto_ver")})
            log.warning("协议版本不匹配 host=%s got=%s want=%s，忽略该帧",
                        host, body.get("proto_ver"), self.proto_ver)
            return
        online = bool(body.get("online"))
        self.store.set_online(host, online, ts=body.get("ts"),
                              image=str(body.get("image") or ""),
                              agent_ver=str(body.get("agent_ver") or ""))
        self.stats["status"] += 1
        if online:
            self._maybe_push_upgrade(host, str(body.get("agent_ver") or ""))

    def _on_hb(self, host, body):
        if not self.store.get_container(host):
            # 没上线过的主机直接发心跳：多半是伪造或 status 丢了，不入库
            self.stats["rejected"] += 1
            self.store.add_audit("mqtt", "hb_unknown_host", {"host": host})
            return
        self.store.record_hb(host, body)
        self.stats["hb"] += 1

    def _on_result(self, host, body):
        cid = body.get("id")
        cmd = self.store.get_command(cid) if cid else None
        if cmd is None:
            self.stats["rejected"] += 1
            self.store.add_audit("mqtt", "result_unknown_cmd", {"host": host, "id": cid})
            return
        if cmd["pod"] != host:
            # 归属校验：A 主机不能替 B 主机回传结果（评审 P0-3）
            self.stats["rejected"] += 1
            self.store.add_audit("mqtt", "result_mismatch",
                                 {"expect": cmd["pod"], "got": host, "id": cid})
            log.warning("丢弃跨主机结果 cmd=%s expect=%s got=%s", cid, cmd["pod"], host)
            return
        self.store.append_result(body, host=host)
        self.stats["result"] += 1

    # ---- 下行 ----
    def _cmd_topic(self, host):
        return "%s/%s/cmd" % (self.prefix, host)

    def dispatch_command(self, row):
        """发布命令到该主机的 cmd 主题；返回 True = 已交给 Broker（QoS1 会排队送达）。

        commands.argv 列对 shell 存 argv 数组、对 collect 存 {"items": [...]} 结构，
        这里按 kind 还原成 Agent 认识的帧字段（items / use_shell 见协议 v2）。
        """
        try:
            argv = json.loads(row["argv"] or "null")
        except ValueError:
            argv = None
        payload = {"id": row["id"], "kind": row["kind"], "timeout": row["timeout"]}
        if isinstance(argv, dict):
            payload["items"] = argv.get("items") or []
            if argv.get("use_shell"):
                payload["use_shell"] = True
            if argv.get("argv"):
                payload["argv"] = argv["argv"]
        else:
            payload["argv"] = argv or []
        try:
            info = self.cli.publish(self._cmd_topic(row["pod"]),
                                    json.dumps(payload, ensure_ascii=False), qos=QOS_CMD)
        except Exception:
            log.exception("发布命令失败 id=%s", row["id"])
            return False
        return info.rc in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN)

    def _maybe_push_upgrade(self, host, agent_ver):
        latest = self.store.get_agent_latest()
        if not latest or not version_lt(agent_ver or "", latest.get("version", "")):
            return
        payload = {"id": "u-" + host, "kind": "update",
                   "version": latest["version"], "sha256": latest.get("sha256", ""),
                   "size": latest.get("size", 0), "url": "/api/system/agent/download"}
        try:
            self.cli.publish(self._cmd_topic(host), json.dumps(payload), qos=QOS_CMD)
            log.info("pushed upgrade %s -> %s to %s", agent_ver, latest["version"], host)
        except Exception:
            log.debug("push upgrade failed", exc_info=True)

    # ---- 周期任务 ----
    def sweep(self):
        """命令超时收敛 + 僵尸在线判定，交给一个周期任务而不是每连接一个定时器。"""
        n = self.store.sweep_command_timeouts()
        stale = self.store.mark_stale_offline(OFFLINE_GRACE)
        if n or stale:
            log.info("sweep: %d 条命令置 timeout, %d 台判离线", n, stale)
        return n, stale
