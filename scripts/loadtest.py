"""连接压测：N 个模拟 Agent 并发连接服务端并保持，报告成功率与服务端在线数。

用法:
  1) 先起服务端: cd server && KK_PORT=8443 KK_AGENT_TOKENS=loadtest python -m kk_server
  2) python scripts/loadtest.py [n=200] [host=127.0.0.1] [port=8443] [token=loadtest]
"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from kk_agent.ws import WSClient  # noqa: E402


def health(host, port):
    with urllib.request.urlopen("http://%s:%d/api/health" % (host, port), timeout=10) as r:
        return json.loads(r.read())


def connect_one(i, host, port, token, conns, errors):
    try:
        ws = WSClient("ws://%s:%d/ws/agent" % (host, port))
        ws.connect(timeout=15)
        ws.send_text(json.dumps({
            "t": "hello", "id": "lt-%d" % i, "proto_ver": 1,
            "pod": "load-%05d" % i, "image": "loadtest", "agent_ver": "0.0.0",
            "token": token, "interval": 60}))
        conns.append(ws)
    except Exception as e:
        errors.append((i, str(e)))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 200
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 8443
    token = sys.argv[4] if len(sys.argv) > 4 else "loadtest"

    threading.stack_size(512 * 1024)
    conns, errors = [], []
    threads = []
    t0 = time.monotonic()
    for i in range(n):
        th = threading.Thread(target=connect_one, args=(i, host, port, token, conns, errors))
        th.start()
        threads.append(th)
        if len(threads) >= 100:  # 分批发起，避免本机瞬时压力失真
            threads.pop(0).join()
    for th in threads:
        th.join()
    elapsed = time.monotonic() - t0

    print("connect: %d/%d 成功，耗时 %.1fs，失败 %d" % (len(conns), n, elapsed, len(errors)))
    if errors:
        print("  失败示例:", errors[:3])

    time.sleep(2)
    h = health(host, port)
    print("服务端 /api/health: agents_online=%s (期望 >= %d)" % (h["agents_online"], len(conns)))

    for ws in conns:
        try:
            ws.close()
        except Exception:
            pass
    time.sleep(1)
    h = health(host, port)
    print("断开后 agents_online=%s (期望 0)" % h["agents_online"])


if __name__ == "__main__":
    main()
