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


CFG = {
    "server": "mqtt://broker.test:1883",
    "host": "web-01",
    "topic_prefix": "kk/v1",
    "interval": 60,
    "keepalive": 45,
    "image": "img:1",
    "client_id": "kk",
    "max_queued": 999,
}


class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeClient:
    """记录所有对外调用；publish 的返回码由测试指定。"""

    def __init__(self, publish_rc=mqtt.MQTT_ERR_SUCCESS, connected=False):
        self.published = []
        self.subscribed = []
        self.will = None
        self.publish_rc = publish_rc
        self._connected = connected
        self.username = self.password = None
        self.tls_calls = []
        self.max_queued = None
        self.reconnect_delay = None
        self.clean_session = None
        self.client_id = None

    def username_pw_set(self, user, password):
        self.username, self.password = user, password

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
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return type("Info", (), {"rc": self.publish_rc})()

    def is_connected(self):
        return self._connected


def make_transport(monkeypatch, cfg=None, **fake_kw):
    fake = FakeClient(**fake_kw)
    monkeypatch.setattr(tp.mqtt, "Client", lambda *a, **k: fake)
    tr = tp.Transport(dict(CFG, **(cfg or {})), _NullLog())
    return tr, fake


# ---- broker 地址解析 ----

@pytest.mark.parametrize("url,expect", [
    ("mqtt://h", ("h", 1883, False)),
    ("mqtt://h:1884", ("h", 1884, False)),
    ("mqtts://h", ("h", 8883, True)),
    ("mqtts://u:p@h:8884", ("h", 8884, True)),
    ("mqtt://[::1]:1883", ("::1", 1883, False)),
])
def test_parse_broker(url, expect):
    got = tp.parse_broker(url)
    assert (got["host"], got["port"], got["secure"]) == expect


def test_parse_broker_credentials_are_urlsplit_but_password_may_hold_at():
    got = tp.parse_broker("mqtt://kk:pa@ss@h")
    assert got["host"] == "h"
    assert got["username"] == "kk"
    assert got["password"] == "pa@ss"


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


def test_tls_and_credentials_applied_from_broker_url(monkeypatch):
    tr, fake = make_transport(monkeypatch, cfg={"server": "mqtts://u:p@h:8883",
                                                "tls_ca": "/etc/ca.pem",
                                                "tls_insecure": True})
    assert (fake.username, fake.password) == ("u", "p")
    assert fake.tls_calls[0]["ca_certs"] == "/etc/ca.pem"
    assert fake.tls_calls[1] == {"insecure": True}


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
    assert body["host"] == "web-01" and "ts" in body


def test_status_is_retained_qos1(monkeypatch):
    """在线状态必须 retain：服务端重启后要能立刻拿到全量现状。"""
    tr, fake = make_transport(monkeypatch, connected=True)
    tr.publish_status(True, "online")
    frame = fake.published[-1]
    assert frame["qos"] == tp.QOS_CMD and frame["retain"] is True
    assert json.loads(frame["payload"])["online"] is True


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
