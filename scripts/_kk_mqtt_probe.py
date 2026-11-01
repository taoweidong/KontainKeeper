import json, sys, time
import paho.mqtt.client as mqtt

url = "127.0.0.1"
port = 1883
got = []

def on_connect(c, u, rc, props=None):
    print("connect rc=%s" % rc, flush=True)
    if rc == 0:
        c.subscribe("#", qos=1)

def on_message(c, u, m):
    got.append((m.topic, m.payload[:60]))
    print("msg topic=%s payload=%r" % (m.topic, m.payload[:60]), flush=True)

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kk-probe", protocol=mqtt.MQTTv311)
c.on_connect = on_connect
c.on_message = on_message
try:
    c.connect(url, port, 30)
except Exception as e:
    print("CONNECT FAIL: %s" % e, flush=True)
    sys.exit(1)
c.loop_start()
time.sleep(1.5)
c.publish("kk-probe/test", json.dumps({"ok": True}), qos=1)
time.sleep(1.5)
c.loop_stop()
c.disconnect()
print("PROBE OK, received=%d" % len(got), flush=True)
