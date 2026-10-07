"""v4 协议兼容窗口（P1）的回归锁。

单开一个文件的原因：这条链路错了不是「少个字段」，是**一次升级全网闪断**
（方案 R1）。窗口内外的取舍必须能一眼读完整：

- 窗口内：存量 v3 帧照常落库，`proto_ver` 留下 3 供总览标「待升级」；
- 窗口外（`KK_DROP_PROTO_V3=1`）：v3 帧拒收并审计，关窗前要先用统计确认存量升完；
- v4 新增的 env/group/labels/caps/docker 是**可选字段**，落库与「离线不覆盖」都要钉住。
"""
import json
import time

import pytest

from kk_server import PROTO_VER
from kk_server.config import load_settings
from kk_server.models.store import Store
from kk_server.services.mqtt_bridge import MqttBridge

GOOD_IP = "10.0.0.5"
BASE_ENV = {
    "KK_MQTT_URL": "mqtt://broker:1883",
    "KK_TOPIC_PREFIX": "kk/v1",
    "KK_MQTT_CLIENT_ID": "kk-server",
}


def frame(host, proto, online=True, **meta):
    """status 帧：v4 的 env/group/labels/caps/docker 全部可选，缺省即不传。"""
    body = {"online": online, "host": host, "ip": GOOD_IP, "proto_ver": proto,
            "agent_ver": "0.4.0", "image": "", "interval": 60,
            "reason": "online" if online else "", "ts": int(time.time())}
    body.update(meta)
    return body


@pytest.fixture
async def mk(tmp_path):
    """按 main.py 的装配方式造桥：窗口集合来自 settings，不另起一套默认值。"""
    stores = []

    async def _mk(extra=None, drop_v3=False):
        db = str(tmp_path / ("pw-%d.db" % len(stores)))
        store = Store(db)
        await store.setup()
        stores.append(store)
        env = dict(BASE_ENV, KK_DB_PATH=db, KK_WEB_DIR=str(tmp_path / "noweb"))
        if drop_v3:
            env["KK_DROP_PROTO_V3"] = "1"
        env.update(extra or {})
        settings = load_settings(env)
        return MqttBridge(store, settings, settings.agent_ips, loop=None,
                          proto_ver=PROTO_VER,
                          accept_proto_vers=settings.accept_proto_vers)

    yield _mk
    for st in stores:
        await st.close()


async def test_v3_frame_still_lands_inside_window(mk):
    """窗口存在的意义就是这条：抬版本不能让存量 Agent 掉线。"""
    bridge = await mk()
    await bridge._on_status("old-01", frame("old-01", 3, agent_ver="0.3.0"))
    row = await bridge.store.get_container("old-01")
    assert row is not None and row["online"] == 1
    assert row["proto_ver"] == 3, "落库的必须是帧内版本，否则「待升级」角标永远标不出来"
    assert bridge.stats["rejected"] == 0


async def test_v4_meta_lands_in_summary_view(mk):
    """v4 机队视图的数据来源：总览页一行读完，不回详情、不做第二次查询。"""
    bridge = await mk()
    await bridge._on_status("new-01", frame(
        "new-01", 4,
        env={"virt": "vm", "os": "Ubuntu 22.04.3 LTS", "kernel": "5.15.0-118",
             "arch": "x86_64"},
        group="ops-web", labels={"role": "web"},
        caps={"shell": True, "docker": True, "docker_ver": "27.0.3"},
        docker={"total": 12, "running": 11, "unhealthy": 1}))
    rows = await bridge.store.list_containers(view="summary")
    assert len(rows) == 1
    row = rows[0]
    assert row["host_type"] == "vm" and row["os_name"] == "Ubuntu 22.04.3 LTS"
    assert row["group_name"] == "ops-web" and row["ip"] == GOOD_IP
    assert json.loads(row["caps"])["docker_ver"] == "27.0.3"
    assert (row["docker_total"], row["docker_running"], row["docker_unhealthy"]) == (12, 11, 1)
    assert row["proto_ver"] == 4
    # labels 是运维可写大的字段，摘要视图必须不带它（详情侧才解析）
    assert "labels" not in row and "last_metrics" not in row


async def test_out_of_window_proto_rejected_and_audited(mk):
    """按新语义解析旧协议帧会得到错的在线状态，比丢帧更危险 → 拒收 + 审计。"""
    bridge = await mk()
    await bridge._on_status("ancient-01", frame("ancient-01", 2))
    assert bridge.stats["rejected"] == 1
    assert await bridge.store.get_container("ancient-01") is None
    audit = await bridge.store.list_audit()
    assert audit[0]["action"] == "proto_mismatch"


async def test_drop_v3_window_closes_the_gate(mk):
    """关窗口是单向门：配了 KK_DROP_PROTO_V3 之后 v3 帧必须被拒，而不是继续吃。"""
    bridge = await mk(drop_v3=True)
    assert bridge.accept_proto_vers == (PROTO_VER,)
    await bridge._on_status("old-02", frame("old-02", 3))
    assert bridge.stats["rejected"] == 1
    assert await bridge.store.get_container("old-02") is None
    # v4 仍照常受理，否则关窗口等于把自己协议的 Agent 也关掉
    await bridge._on_status("new-02", frame("new-02", 4))
    assert (await bridge.store.get_container("new-02"))["proto_ver"] == 4


async def test_malformed_proto_ver_rejects_instead_of_crashing(mk):
    """QR-S29：`"proto_ver": "v3"` 这类畸形帧过去会炸掉整个派发 task，
    而 QoS1 已被 paho 线程 ACK —— 帧从此永久丢失，retained 状态也不落库。
    闸门必须按「窗口外」处理：拒收 + 审计，不抛。"""
    bridge = await mk()
    await bridge._on_status("bad-01", frame("bad-01", "v3"))
    await bridge._on_status("bad-02", frame("bad-02", None))
    await bridge._on_status("bad-03", frame("bad-03", "4"))   # 数字字符串仍应受理
    assert bridge.stats["rejected"] == 2
    assert (await bridge.store.get_container("bad-03"))["proto_ver"] == 4
    actions = [a["action"] for a in await bridge.store.list_audit()]
    assert actions.count("proto_mismatch") == 2


async def test_offline_frame_does_not_clobber_v4_meta(mk):
    """离线帧可能是 Broker 补发的 LWT（字段缺失或来自旧 Agent）：只改在线态。"""
    bridge = await mk()
    await bridge._on_status("mix-01", frame(
        "mix-01", 4, env={"virt": "metal", "os": "Debian 12"},
        group="edge", caps={"shell": True}))
    await bridge._on_status("mix-01", frame("mix-01", 4, online=False, reason=""))
    row = await bridge.store.get_container("mix-01")
    assert row["online"] == 0
    assert row["host_type"] == "metal" and row["os_name"] == "Debian 12"
    assert row["group_name"] == "edge"
