"""连接压测：N 个模拟 Agent 并发连 Broker 上报 status，报告服务端在线数。

v3 模型（匿名 Broker + 服务端白名单）：模拟 Agent 只发 status 帧（携带自报 ip），
服务端未配 KK_AGENT_IPS 时放行全部。

前置：
  1) 本机 1883 端口有 Mosquitto（docker run -d -p 1883:1883 eclipse-mosquitto:2）
  2) 先起服务端: KK_MQTT_URL=mqtt://127.0.0.1:1883 python -m kk_server

用法:
  python scripts/loadtest.py [n=200] [broker_host=127.0.0.1] [broker_port=1883] [api_port=8443]
"""
import json
import sys
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

PREFIX = "kk/v1"
PROTO_VER = 3


def health(api_port):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % api_port, timeout=10) as r:
        return json.loads(r.read())


def make_agent(i, keep, errors):
    host = "load-%05d" % i
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lt-%d" % i,
                      protocol=mqtt.MQTTv311, clean_session=False)
    payload = json.dumps({
        "online": True, "host": host, "ip": "127.0.0.1",
        "agent_ver": "0.0.0", "proto_ver": PROTO_VER, "image": "loadtest",
        "interval": 60, "reason": "online", "ts": int(time.time()),
    }, separators=(",", ":"))

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            errors.append((host, str(reason_code)))
            return
        client.publish("%s/%s/status" % (PREFIX, host), payload, qos=1, retain=True)
        keep.append(client)

    cli.on_connect = on_connect
    return cli


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 200
    broker_host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    broker_port = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 1883
    api_port = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else 8443

    keep, errors = [], []
    t0 = time.monotonic()
    for i in range(n):
        cli = make_agent(i, keep, errors)
        try:
            cli.connect_async(broker_host, broker_port, keepalive=60)
            cli.loop_start()
        except Exception as e:
            errors.append((i, str(e)))
        if i % 100 == 99:   # 分批发起，避免本机瞬时压力失真
            time.sleep(0.2)
    elapsed = time.monotonic() - t0

    time.sleep(3)   # 留时间给服务端桥接收敛 retained status
    h = health(api_port)
    print("connect: %d/%d 成功，耗时 %.1fs，失败 %d" % (len(keep), n, elapsed, len(errors)))
    if errors:
        print("  失败示例:", errors[:3])
    print("服务端 /api/health: agents_online=%s (期望 >= %d)" % (h["agents_online"], len(keep)))

    for cli in keep:
        try:
            cli.disconnect()
            cli.loop_stop()
        except Exception:
            pass
    time.sleep(3)
    h = health(api_port)
    print("断开后 agents_online=%s (期望 0，retained 清理依赖各客户端主动下线)" % h["agents_online"])


if __name__ == "__main__":
    main()
