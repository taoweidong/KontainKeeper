#!/usr/bin/env python
"""真实 Broker 端到端冒烟：Agent ↔ Mosquitto ↔ 假服务端订阅者。

补单元测试证不到的那段：paho 的实际排队/重试语义、LWT 触发、持久会话离线补发、
命令结果分块在真实网络上的可拼装性。

前置：本机可达的 Mosquitto（开发环境用 WSL：
  wsl -d Ubuntu-22.04 -- sudo apt-get install -y mosquitto mosquitto-clients
  默认 0.0.0.0:1883、允许匿名；Windows 侧经 WSL2 localhost 转发直连 127.0.0.1:1883）

用法：
    python scripts/mqtt_e2e.py                     # 默认连 127.0.0.1:1883
    KK_MQTT_URL=mqtt://10.0.0.5:1883 python scripts/mqtt_e2e.py
退出码 0 = 全部通过。
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent" / "src"))

BROKER = (os.environ.get("KK_MQTT_URL") or "mqtt://127.0.0.1:1883")
_prefix, _hostport = BROKER.split("://", 1)
HOST, _, PORT = _hostport.partition(":")
PORT = int(PORT or (8883 if _prefix == "mqtts" else 1883))
PREFIX = "kk/e2e"
TIMEOUT = 25

_checks = []


def connect_with_retry(cli, tries=10, delay=1.5):
    """WSL2 的 localhost 转发偶发瞬时 connection-refused，重连几次再判失败。"""
    last = None
    for _ in range(tries):
        try:
            cli.connect(HOST, PORT, keepalive=60)
            return
        except OSError as e:
            last = e
            time.sleep(delay)
    raise last


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))


class Recorder:
    """假服务端：订阅全部上行主题，按主机与主题分桶存帧。"""

    def __init__(self, client_id):
        self.msgs = []
        self.lock = threading.Lock()
        self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                               protocol=mqtt.MQTTv311)
        self.cli.on_connect = lambda c, u, f, rc, p=None: c.subscribe(PREFIX + "/#", qos=1)
        self.cli.on_message = self._on_msg

    def _on_msg(self, _c, _u, msg):
        try:
            body = json.loads(msg.payload.decode("utf-8", "replace"))
        except ValueError:
            body = {"_raw": msg.payload[:64].decode("latin-1")}
        with self.lock:
            self.msgs.append({"topic": msg.topic, "ts": time.time(), "body": body})

    def start(self):
        connect_with_retry(self.cli)
        self.cli.loop_start()

    def stop(self):
        self.cli.loop_stop()
        self.cli.disconnect()

    def find(self, host, suffix, pred=lambda b: True):
        with self.lock:
            return [m for m in self.msgs
                    if m["topic"] == "%s/%s/%s" % (PREFIX, host, suffix)
                    and pred(m["body"])]

    def wait_for(self, host, suffix, pred=lambda b: True, timeout=TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = self.find(host, suffix, pred)
            if hits:
                return hits[-1]["body"]
            time.sleep(0.1)
        return None

    def clear(self):
        with self.lock:
            self.msgs.clear()


def agent_env(host, interval=1):
    env = dict(os.environ)
    env.update({
        "KK_SERVER": BROKER, "KK_HOST_NAME": host, "KK_INTERVAL": str(interval),
        "KK_TOPIC_PREFIX": PREFIX, "KK_UPDATE_DISABLED": "1",
        "KK_KEEPALIVE": "20", "KK_LOG_LEVEL": "WARNING",
    })
    return env


def start_agent_proc(host, interval=1):
    """用独立进程跑 Agent，才能用强制杀进程模拟未干净断开（触发 LWT）。"""
    exe = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "kk-agent"
    return subprocess.Popen([str(exe)], env=agent_env(host, interval),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_proc(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        p.kill()
    p.wait(timeout=15)


def publish_cmd(host, payload, client_id="kk-e2e-server"):
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                      protocol=mqtt.MQTTv311)
    connect_with_retry(cli)
    cli.loop_start()
    info = cli.publish("%s/%s/cmd" % (PREFIX, host), json.dumps(payload), qos=1)
    info.wait_for_publish(timeout=10)
    cli.loop_stop()
    cli.disconnect()


def collect_result(rec, host, cmd_id, timeout=TIMEOUT):
    """把同一命令的分块按 seq 拼装成完整输出。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        frames = rec.find(host, "result", lambda b: b.get("id") == cmd_id)
        done = [f for f in frames if f["body"].get("done")]
        if done:
            byseq = {f["body"]["seq"]: f["body"] for f in frames}
            total = done[-1]["body"].get("total", len(byseq))
            out = b"".join(base64.b64decode(byseq[i].get("out_b64", ""))
                           for i in range(total) if i in byseq)
            return out, done[-1]["body"], len(frames)
        time.sleep(0.15)
    return None, None, 0


def main():
    rec = Recorder("kk-e2e-sub-" + str(int(time.time())))
    rec.start()
    time.sleep(1.0)

    # ---- 1. 上线：retain 的 status + 不 retain 的 hb ----
    host = "e2e-live"
    proc = start_agent_proc(host)
    try:
        st = rec.wait_for(host, "status", lambda b: b.get("online") is True)
        check("Agent 上线并上报 retained status", st is not None, str(st)[:120])
        hb = rec.wait_for(host, "hb")
        check("收到心跳且带 psutil 指标", bool(hb and hb.get("metrics", {}).get("mem_mb")),
              "mem_mb=%s cpu=%s" % ((hb or {}).get("metrics", {}).get("mem_mb"),
                                    (hb or {}).get("metrics", {}).get("cpu")))

        # Broker 上应只有 status 被 retain，hb 没有（R1 回归）
        retained = retained_topics(host)
        check("R1：Broker 只 retain status，hb 未被 retain",
              retained == ["%s/%s/status" % (PREFIX, host)], "retained=%s" % retained)

        # ---- 2. 命令结果真实回传（R6 主功能）----
        cid = "c-echo-1"
        publish_cmd(host, {"id": cid, "kind": "shell",
                           "argv": ["echo", "kk-e2e-ok"], "timeout": 20})
        out, body, n = collect_result(rec, host, cid)
        check("R6：shell 命令结果经真实 Broker 回传", out is not None and b"kk-e2e-ok" in out,
              "frames=%d out=%r" % (n, (out or b"")[:40]))

        # ---- 3. 大输出多块可拼装 ----
        cid2 = "c-big-1"
        publish_cmd(host, {"id": cid2, "kind": "shell",
                           "argv": ["python", "-c",
                                    "import sys;sys.stdout.write('Q'*300000)"],
                           "timeout": 30})
        out2, body2, n2 = collect_result(rec, host, cid2, timeout=TIMEOUT)
        check("大输出分块后按 seq 完整重组", out2 is not None and len(out2) == 300000,
              "frames=%d bytes=%s" % (n2, len(out2 or b"")))

        # ---- 4. collect 按项采集（R4 的 Agent 半边）----
        cid3 = "c-collect-1"
        publish_cmd(host, {"id": cid3, "kind": "collect", "items": ["cpu", "mem"]})
        out3, _, _ = collect_result(rec, host, cid3)
        ok3 = False
        if out3:
            try:
                data = json.loads(out3.decode())
                ok3 = "cpu" in data["data"] and "mem_mb" in data["data"]
            except ValueError:
                ok3 = False
        check("kind=collect 按项采集返回结构化数据", ok3, str(out3)[:80])
    finally:
        # ---- 5. 强杀进程 → Broker 代发 LWT offline ----
        rec.clear()
        kill_proc(proc)
        off = rec.wait_for(host, "status", lambda b: b.get("online") is False, timeout=40)
        check("LWT：强杀进程后 Broker 代发 offline", off is not None, str(off)[:100])

    # ---- 6. 持久会话离线补发：Agent 不在线时下发的命令，重连后自动到达 ----
    host2 = "e2e-queue"
    seed = start_agent_proc(host2)
    try:
        got = rec.wait_for(host2, "status", lambda b: b.get("online") is True)
        check("离线路测：Agent 先上线一次以在 Broker 建立会话与订阅", got is not None)
    finally:
        kill_proc(seed)
        rec.wait_for(host2, "status", lambda b: b.get("online") is False, timeout=40)
        rec.clear()

    cid4 = "c-offline-1"
    publish_cmd(host2, {"id": cid4, "kind": "shell", "argv": ["echo", "from-queue"],
                        "timeout": 30}, client_id="kk-e2e-server-2")
    check("命令在 Agent 离线期间下发（应由 Broker 排队而非丢弃）",
          not rec.find(host2, "result", lambda b: b.get("id") == cid4))

    revived = start_agent_proc(host2)
    try:
        out4, body4, _ = collect_result(rec, host2, cid4, timeout=TIMEOUT)
        check("离线路测：Agent 重连后收到 Broker 排队的命令并回传结果",
              out4 is not None and b"from-queue" in out4, repr((out4 or b"")[:40]))
    finally:
        kill_proc(revived)

    rec.stop()
    bad = [n for n, ok in _checks if not ok]
    print("\n%d/%d 通过" % (len(_checks) - len(bad), len(_checks)))
    if bad:
        print("失败项: " + ", ".join(bad))
    return 1 if bad else 0


def retained_topics(host):
    """新开一个 clean_session 订阅者，只有 retained 消息会立刻回放。"""
    seen = []
    done = threading.Event()
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kk-e2e-retain-" + str(
        int(time.time() * 1000)), clean_session=True, protocol=mqtt.MQTTv311)
    cli.on_connect = lambda c, u, f, rc, p=None: c.subscribe(PREFIX + "/" + host + "/#", qos=1)
    cli.on_message = lambda c, u, m: (seen.append(m.topic), done.set())
    connect_with_retry(cli)
    cli.loop_start()
    done.wait(6)
    time.sleep(0.6)  # 让其它 retained 帧也到齐
    cli.loop_stop()
    cli.disconnect()
    return sorted(set(seen))


if __name__ == "__main__":
    sys.exit(main())
