"""Server 测试公共固件：注入 src 路径，提供 MqttBridge 假发版。

集成测试（test_integration）会启动真实服务端并驱动真实 Agent 主循环，
因此需要同时把 agent/src 与 server/src 注入路径。
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_SRC = ROOT / "agent" / "src"
SERVER_SRC = Path(__file__).resolve().parent.parent / "src"
for p in (str(SERVER_SRC), str(AGENT_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from kk_server.config import load_settings
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge


class FakePublish:
    """paho `Client.publish` 的替身：把每条发出的消息记进 `msgs` 用于断言。

    不会连真实 Broker —— MqttBridge 的 `start()` 需要 `connect_async + loop_start`，
    这两件对单元测都是负担，**所有测试路径都不调 start()**，把 `bridge.cli` 整把换掉。
    """
    def __init__(self, rc=0):
        self.msgs = []
        self.rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return types.SimpleNamespace(rc=self.rc)


@pytest.fixture
async def bridge(tmp_path):
    """默认 `update_mode=manual`、白名单空的桥：聚焦路由与策略门禁。"""
    store = Store(str(tmp_path / "b.db"))
    await store.setup()
    settings = load_settings({
        "KK_DB_PATH": str(tmp_path / "b.db"),
        "KK_MQTT_URL": "mqtt://broker:1883",
        "KK_TOPIC_PREFIX": "kk/v1",
        "KK_MQTT_CLIENT_ID": "kk-server",
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    b = MqttBridge(store, settings, settings.agent_ips, loop=None, proto_ver=3)
    b.cli = FakePublish()
    b.store = store
    yield b
    await store.close()
