"""MQTT 传输层单测：不连真实 Broker，用假 Client 钉住主题/QoS/retain/排队语义。

这些用例是阶段 0 三处协议缺陷的回归锁：
- R1 心跳不得 retain（否则服务端重启会整批回放出幽灵心跳）
- R2 QoS1 发布不得被 is_connected 短路（否则断线窗口内命令结果静默丢失）
- LWT / 持久会话 / client_id 稳定性（Broker 靠它认人并保留离线队列）
"""
import json
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from kk_agent import transport as tp
from kk_agent.config import AGENT_VER


CFG = {
    "server": "mqtt://broker.test:1883",
    "host": "web-01",
    "topic_prefix": "kk/v1",
    "interval": 60,
    "keepalive": 45,
    "image": "img:1",
    "client_id": "kk",
    "max_queued": 999,
    "advertise_ip": "10.0.0.5",
}


class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _RecLog:
    """记录 warning 的日志替身：退出路径的静默失败是本批次要修的缺陷，得有东西能看见。"""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Info:
    """paho publish() 返回的 MQTTMessageInfo 的替身：记录 wait_for_publish 的调用。"""

    def __init__(self, rc, owner):
        self.rc = rc
        self._owner = owner

    def wait_for_publish(self, timeout=None):
        self._owner.waited = timeout
        return None


class FakeClient:
    """记录所有对外调用；publish 的返回码由测试指定。"""

    def __init__(self, publish_rc=mqtt.MQTT_ERR_SUCCESS, connected=False,
                 publish_exc=None):
        self.published = []
        self.subscribed = []
        self.will = None
        self.publish_rc = publish_rc
        self.publish_exc = publish_exc
        self._connected = connected
        self.tls_calls = []
        self.max_queued = None
        self.reconnect_delay = None
        self.clean_session = None
        self.client_id = None
        self.waited = None          # 进程消失前的 PUBACK 等待超时值（B6.1 / QR-A14）
        self.disconnected = False   # stop() 是否真的走到了 disconnect
        self.stopped = False        # stop() 是否关掉了网络循环
        self.events = []            # publish / disconnect / loop_stop 的先后顺序

    def tls_set(self, **kw):
        self.tls_calls.append(kw)

    def tls_insecure_set(self, flag):
        self.tls_calls.append({"insecure": flag})

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = {"topic": topic, "payload": payload, "qos": qos, "retain": retain}

    def max_queued_messages_set(self, n):
        self.max_queued = n

    def reconnect_delay_set(self, min_delay=None, max_delay=None):
        self.reconnect_delay = (min_delay, max_delay)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.events.append("publish")
        if self.publish_exc is not None:
            raise self.publish_exc
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return _Info(self.publish_rc, self)

    def disconnect(self):
        self.events.append("disconnect")
        self.disconnected = True

    def loop_stop(self):
        self.events.append("loop_stop")
        self.stopped = True

    def is_connected(self):
        return self._connected


def make_transport(monkeypatch, cfg=None, log=None, **fake_kw):
    fake = FakeClient(**fake_kw)
    monkeypatch.setattr(tp.mqtt, "Client", lambda *a, **k: fake)
    tr = tp.Transport(dict(CFG, **(cfg or {})), log or _NullLog())
    return tr, fake


# ---- broker 地址解析 ----

@pytest.mark.parametrize("url,expect", [
    ("mqtt://h", ("h", 1883, False)),
    ("mqtt://h:1884", ("h", 1884, False)),
    ("mqtts://h", ("h", 8883, True)),
    ("mqtt://[::1]:1883", ("::1", 1883, False)),
])
def test_parse_broker(url, expect):
    got = tp.parse_broker(url)
    assert (got["host"], got["port"], got["secure"]) == expect


def test_parse_broker_rejects_embedded_credentials():
    """v3 起 Broker 匿名模式：URL 内嵌凭据必须显式报错，而不是被当成主机名。"""
    with pytest.raises(tp.TransportError, match="内嵌凭据"):
        tp.parse_broker("mqtt://u:p@h:1883")


@pytest.mark.parametrize("bad", ["ws://h/ws/agent", "http://h", "", None])
def test_parse_broker_rejects_non_mqtt(bad):
    with pytest.raises(tp.TransportError):
        tp.parse_broker(bad)


# ---- 会话与遗嘱 ----

def test_session_identity_and_will(monkeypatch):
    _tr, fake = make_transport(monkeypatch)
    assert fake.will["topic"] == "kk/v1/web-01/status"
    assert fake.will["retain"] is True and fake.will["qos"] == tp.QOS_CMD
    will = json.loads(fake.will["payload"])
    assert will["online"] is False and will["host"] == "web-01"
    assert will["ip"] == "10.0.0.5", "LWT 与 status 必须同形（都带自报 ip），服务端都要能认主机"
    assert fake.max_queued == 999, "KK_MAX_QUEUED 必须传给 paho，否则大输出断线仍会被静默淘汰"
    assert fake.reconnect_delay == (tp.RECONNECT_MIN, tp.RECONNECT_MAX)


def test_client_id_is_stable_and_host_scoped(monkeypatch):
    """client_id 是 Broker 识别「同一台机器」的凭据，必须由主机名派生且稳定。"""
    captured = {}

    def spy(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return FakeClient()
    monkeypatch.setattr(tp.mqtt, "Client", spy)
    tp.Transport(dict(CFG, client_id="kk"), _NullLog())
    assert captured["kwargs"]["client_id"] == "kk-web-01"
    assert captured["kwargs"]["clean_session"] is False
    assert captured["kwargs"]["protocol"] == tp.PROTO


def test_tls_applied_for_mqtts(monkeypatch):
    """mqtts:// + CA：TLS 按配置生效；已配 CA 时 KK_TLS_INSECURE 必须被忽略——
    paho 的 tls_insecure_set(True) 会把 verify_mode 一并置成 CERT_NONE，
    让 CA 校验形同虚设（代码审查 P1-3）。"""
    tr, fake = make_transport(monkeypatch, cfg={"server": "mqtts://h:8883",
                                                "tls_ca": "/etc/ca.pem",
                                                "tls_insecure": True})
    assert fake.tls_calls[0]["ca_certs"] == "/etc/ca.pem"
    assert all(c.get("insecure") is not True for c in fake.tls_calls), \
        "配了 CA 就不能再降校验，否则 KK_TLS_CA 白配"


# ---- 出口 IP 自报（v3）----

def test_advertise_ip_explicit_wins_over_probe(monkeypatch):
    """KK_ADVERTISE_IP 显式配置优先于自动探测：多网卡/NAT 下探测不准时的人工覆盖。"""
    monkeypatch.setattr(tp, "detect_outbound_ip", lambda h, p=1883: "192.0.2.9")
    tr, _ = make_transport(monkeypatch, cfg={"advertise_ip": "10.9.9.9"})
    assert tr.ip == "10.9.9.9"


def test_ip_auto_detected_when_not_configured(monkeypatch):
    monkeypatch.setattr(tp, "detect_outbound_ip", lambda h, p=1883: "192.0.2.7")
    tr, _ = make_transport(monkeypatch, cfg={"advertise_ip": ""})
    assert tr.ip == "192.0.2.7"


def test_ip_probe_failure_leaves_empty(monkeypatch):
    """探测失败（无网络栈）时 ip 留空，由服务端按白名单拒绝——不能编造地址。"""
    monkeypatch.setattr(tp, "detect_outbound_ip", lambda h, p=1883: "")
    tr, _ = make_transport(monkeypatch, cfg={"advertise_ip": ""})
    assert tr.ip == ""


# ---- 主题布局 ----

def test_topic_layout(monkeypatch):
    tr, _ = make_transport(monkeypatch)
    assert tr.topic("hb") == "kk/v1/web-01/hb"
    assert tr.cmd_topic == "kk/v1/web-01/cmd"


def test_topic_prefix_is_normalised(monkeypatch):
    tr, _ = make_transport(monkeypatch, cfg={"topic_prefix": "/kk/custom/"})
    assert tr.topic("status") == "kk/custom/web-01/status"


# ---- QoS 与 retain 语义（R1/R2 回归锁）----

def test_heartbeat_is_qos0_and_not_retained(monkeypatch):
    """R1：retained 心跳会在服务端每次订阅时整批回放，污染指标表。"""
    tr, fake = make_transport(monkeypatch, connected=True)
    assert tr.publish_hb({"mem_mb": 1.0}, {"plug": {"v": 1}}) is True
    frame = fake.published[-1]
    assert frame["topic"] == "kk/v1/web-01/hb"
    assert frame["qos"] == 0
    assert frame["retain"] is False, "心跳绝不能 retain"
    body = json.loads(frame["payload"])
    assert body["metrics"]["mem_mb"] == 1.0 and body["custom"]["plug"]["v"] == 1
    assert body["host"] == "web-01" and body["ip"] == "10.0.0.5" and "ts" in body


def test_status_is_retained_qos1(monkeypatch):
    """在线状态必须 retain：服务端重启后要能立刻拿到全量现状。"""
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.publish_status(True, "online")
    frame = fake.published[-1]
    assert frame["qos"] == tp.QOS_CMD and frame["retain"] is True
    body = json.loads(frame["payload"])
    assert body["online"] is True and body["ip"] == "10.0.0.5"


def test_qos1_publish_is_queued_even_while_disconnected(monkeypatch):
    """R2：不能因为没连上就把结果丢掉——交给 paho out-queue 重连后补发。"""
    tr, fake = make_transport(monkeypatch, connected=False)
    assert tr.publish_result({"id": "c1", "done": True}) is True
    assert fake.published, "断线时也必须把 QoS1 消息交给 paho 入队"
    assert fake.published[-1]["qos"] == 1


def test_qos0_heartbeat_is_skipped_while_disconnected(monkeypatch):
    """心跳反过来不该入队积压：断线期间的旧指标没有价值。"""
    tr, fake = make_transport(monkeypatch, connected=False)
    assert tr.publish_hb({"mem_mb": 1.0}) is False
    assert fake.published == []


def test_queue_overflow_reports_failure(monkeypatch):
    """out-queue 挤爆时必须报 False，好让上层补发失败终态。"""
    tr, fake = make_transport(monkeypatch, publish_rc=mqtt.MQTT_ERR_QUEUE_SIZE)
    assert tr.publish_result({"id": "c1"}) is False


# ---- B6.1：自更新前的离线宣告 ----

def test_announce_update_sends_retained_offline_reason_and_waits(monkeypatch):
    """execv 前发 reason=updating 的 retained 离线帧，并**等 PUBACK**。

    只 publish 不等：execv 会掐断 paho 的发送队列，帧随进程一起消失，
    服务端只能看到 Broker 补发的 LWT（reason 为空）。
    """
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.announce_update()
    assert fake.published, "必须发出离线状态帧"
    frame = fake.published[-1]
    assert frame["retain"] is True and frame["qos"] == tp.QOS_CMD
    body = json.loads(frame["payload"])
    assert body["online"] is False and body["reason"] == "updating"
    assert fake.waited == 1.0, "必须等 PUBACK，否则帧会随 execv 丢失"


def test_announce_update_is_silent_when_disconnected(monkeypatch):
    """钩子绝不能抛错：未连接时直接返回，且不影响更新主流程。"""
    tr, fake = make_transport(monkeypatch, connected=False)
    tr.announce_update()          # 不抛即通过
    assert fake.published == []


def test_connect_subscribes_cmd_and_announces_online(monkeypatch):
    tr, fake = make_transport(monkeypatch, connected=True)
    tr._on_connect(fake, None, {}, 0)
    assert fake.subscribed == [("kk/v1/web-01/cmd", tp.QOS_CMD)]
    assert json.loads(fake.published[-1]["payload"])["online"] is True


def test_connect_failure_leaves_not_ready(monkeypatch):
    tr, fake = make_transport(monkeypatch)
    tr._on_connect(fake, None, {}, 5, None)  # reason code != 0
    assert tr.connected.is_set() is False
    assert fake.published == []


def test_wait_ready_returns_false_while_broker_unreachable(monkeypatch):
    """Broker 不可达时不得无限等待；到点即返回，交给 paho 后台重连。"""
    tr, _ = make_transport(monkeypatch)
    t0 = time.monotonic()
    assert tr.wait_ready(timeout=1, stop=threading.Event()) is False
    assert time.monotonic() - t0 < 2.5


def test_wait_ready_aborts_promptly_on_stop(monkeypatch):
    """停止信号必须能打断启动等待，否则容器停止会被 SIGKILL 而不是优雅退出。"""
    tr, _ = make_transport(monkeypatch)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    assert tr.wait_ready(timeout=30, stop=stop) is False
    assert time.monotonic() - t0 < 2.0, "stop 置位后不应继续等满 timeout"


def test_wait_ready_true_when_connected(monkeypatch):
    tr, _ = make_transport(monkeypatch)
    tr.connected.set()
    assert tr.wait_ready(timeout=5, stop=threading.Event()) is True


def test_stop_announces_offline_before_disconnect(monkeypatch):
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.stop()
    body = json.loads(fake.published[-1]["payload"])
    assert body["online"] is False and body["reason"] == "stopping"
    assert tr.connected.is_set() is False


# ---- 退出路径（QR-A14）----

def test_stop_waits_for_puback_before_disconnecting(monkeypatch):
    """优雅退出的三连：发 offline 帧 → 等 PUBACK → 才 disconnect。

    干净的 DISCONNECT 不触发 LWT，服务端只能靠这一帧判下线；而 disconnect 会掐断
    还在发送队列里的帧。不等 PUBACK 就等于发了个寂寞 —— 顺序 + 等待都是回归锁。
    """
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.stop()
    assert fake.waited == 1.0, "必须等 PUBACK，否则 offline 帧会随 disconnect 丢失"
    assert fake.events == ["publish", "disconnect", "loop_stop"], \
        "收尾顺序不能颠倒：publish → disconnect → loop_stop"
    assert fake.disconnected and fake.stopped


def test_stop_is_idempotent_when_disconnected(monkeypatch):
    """已断线时别再发状态帧（否则 rc 分支掩盖真实原因），但仍要收尾。"""
    tr, fake = make_transport(monkeypatch, connected=False)
    tr.stop()
    assert fake.published == []
    assert fake.events == ["disconnect", "loop_stop"]


def test_stop_logs_failures_instead_of_swallowing_them(monkeypatch):
    """三段收尾过去全静默 `except: pass`：退出路径炸了现场什么都没有。

    现在 publish 抛错必须留下 warning，且后续 disconnect/loop_stop 照常执行
    ——不能因为一帧没发出去就卡住退出。
    """
    rec = _RecLog()
    tr, fake = make_transport(monkeypatch, connected=True, log=rec,
                              publish_exc=RuntimeError("broker went away"))
    tr.stop()
    assert any("offline frame failed" in w for w in rec.warnings), rec.warnings
    assert fake.disconnected and fake.stopped, "收尾不能被单点失败打断"


# ---- 失败终态的绕行通道（QR-A6）----

def test_publish_result_urgent_bypasses_offline_queue(monkeypatch):
    """urgent=True 走 QoS0：out-queue 被大输出塞满时，QoS1 终态会一起被拒收。"""
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.publish_result({"id": "c1", "done": True})
    assert fake.published[-1]["qos"] == tp.QOS_CMD
    tr.publish_result({"id": "c1", "done": True, "rc": tp.RC_SEND_FAILED}, urgent=True)
    assert fake.published[-1]["qos"] == tp.QOS_HB, "失败终态必须绕开排队，当场发出"


def test_publish_result_default_is_qos1(monkeypatch):
    """默认路径不许被 urgent 优化带跑偏：常规分块仍然必须保证送达。"""
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.publish_result({"id": "c1"})
    assert fake.published[-1]["qos"] == tp.QOS_CMD


def test_max_queued_budget_stays_inside_agent_envelope():
    """离线排队的最坏内存占用是显式预算（QR-A22），不能靠「反正一般用不满」。

    单块 base64 后 ≈64KB：队列上限 × 64KB 就是断线窗口内 Agent 能撑到的额外 RSS。
    调大它必须先调这条预算，否则 25–35MB 的常驻口径会被一条长断网击穿。
    """
    worst_mb = tp.MAX_QUEUED * 64 * 1024 / (1024 * 1024)
    assert worst_mb <= 10, "out-queue 最坏占用 %.1fMB，超出主机额外预算" % worst_mb


# ---- 版本治理契约（D1.4）----

def test_hb_frame_always_carries_agent_ver(monkeypatch):
    """版本是**默认上报数据**：两帧都带，且不受 `KK_HB_ITEMS` 精简影响。

    实现里一直如此，但协议文档只在 status 帧列了 `agent_ver`。这条用例把「默认上报」
    从实现细节提升为契约 —— 否则日后有人优化帧体积时，最容易先砍的就是它。
    """
    tr, fake = make_transport(monkeypatch, connected=True)

    tr.publish_hb({"cpu": 1.0})            # 只采一项也不影响版本字段
    hb = json.loads(fake.published[-1]["payload"])
    assert hb["agent_ver"] == AGENT_VER and hb["agent_ver"]

    tr.publish_status(True, "online")
    st = json.loads(fake.published[-1]["payload"])
    assert st["agent_ver"] == AGENT_VER, "status 与 hb 必须同源同值"
