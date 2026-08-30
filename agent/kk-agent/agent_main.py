"""kk-agent 守护主循环：单线程 select 事件循环 + 一次性工作线程。

资源策略：
- 空闲时只有主线程阻塞在 select 上，0 CPU、常驻 RSS 目标 < 15MB
- 心跳/命令/插件采集在一次性 daemon 线程中执行，结果经队列回到主线程发帧
- 断线指数退避重连（1s→60s 封顶），成功后立即补发心跳
"""
import base64
import json
import queue
import random
import select
import threading
import time

import kk_collector
import kk_config
import kk_conn
import kk_executor
import kk_logutil
import kk_plugins
from kk_ws import WSClosed, WSError

CHUNK = 48 * 1024  # cmd_result 原始输出分块大小


def submit_heartbeat(cfg, log, q, state_box):
    def work():
        try:
            metrics, st = kk_collector.collect(cfg, state_box["state"])
            state_box["state"] = st
            custom = kk_plugins.collect_all(cfg["plugin_dir"], log)
            q.put(("hb", None, {"metrics": metrics, "custom": custom}))
        except Exception:
            log.exception("heartbeat collect failed")

    threading.Thread(target=work, daemon=True, name="kk-hb").start()


def send_cmd_result(ws, cmd_id, res):
    out = res.get("out") or b""
    chunks = [out[i:i + CHUNK] for i in range(0, len(out), CHUNK)] or [b""]
    for i, c in enumerate(chunks):
        last = i == len(chunks) - 1
        frame = {
            "t": "cmd_result", "id": cmd_id, "seq": i,
            "data_b64": base64.b64encode(c).decode("ascii"), "done": last,
        }
        if last:
            frame.update(rc=res.get("rc", -2), timed_out=bool(res.get("timed_out")),
                         elapsed_ms=int(res.get("elapsed_ms", 0)))
        kk_conn.send_json(ws, frame)


def handle_server_msg(msg, ws, runner, cfg, log, q):
    t = msg.get("t")
    if t == "cmd":
        kind = msg.get("kind", "shell")
        if kind == "shell":
            runner.submit(msg)
        elif kind == "plugin_reload":
            def fn():
                data = kk_plugins.collect_all(cfg["plugin_dir"], log)
                return {"rc": 0, "out": json.dumps({"plugins": data}, ensure_ascii=False).encode(),
                        "timed_out": False, "elapsed_ms": 0}
            runner.submit_fn("cmd_result", msg.get("id"), fn)
        else:
            send_cmd_result(ws, msg.get("id"), {"rc": 127, "out": b"unknown kind",
                                                "timed_out": False, "elapsed_ms": 0})
    elif t == "cfg":
        try:
            cfg["interval"] = min(max(int(msg["interval"]), 5), 3600)
            log.info("interval adjusted to %ss", cfg["interval"])
        except (KeyError, TypeError, ValueError):
            pass
    # hb_ack 等未知类型：忽略


def flush_outgoing(ws, q, cfg, log):
    """把工作线程结果全部发出去；发送失败时关闭连接并返回 False（触发重连）。"""
    while True:
        try:
            kind, ref, payload = q.get_nowait()
        except queue.Empty:
            return True
        try:
            if kind == "hb":
                frame = {"t": "hb", "ts": int(time.time()), "interval": cfg["interval"]}
                frame.update(payload)
                kk_conn.send_json(ws, frame)
            elif kind == "cmd_result":
                send_cmd_result(ws, ref, payload)
        except (WSClosed, WSError, OSError) as e:
            log.warning("send failed: %s", e)
            kk_conn.safe_close(ws)
            return False


def run(stop=None, cfg=None):
    stop = stop or threading.Event()
    cfg = cfg or kk_config.load()
    log = kk_logutil.get_logger(cfg["log_path"], cfg["log_level"])
    log.info("kk-agent %s starting: server=%s interval=%ss fs_root=%s",
             kk_config.AGENT_VER, cfg["server"], cfg["interval"], cfg["fs_root"])

    q = queue.Queue()
    runner = kk_executor.Runner(q, max_out=cfg["max_out_mb"] * 1024 * 1024)
    state_box = {"state": {"cpu": None, "procs": {}, "ts": time.time()}}
    ws = None
    backoff = 1.0
    next_hb = 0.0

    while not stop.is_set():
        if not cfg["server"] or not cfg["token"]:
            log.error("KK_SERVER/KK_TOKEN not configured, retry in 60s")
            stop.wait(60)
            continue

        if ws is None:
            try:
                ws = kk_conn.connect(cfg)
                backoff = 1.0
                next_hb = 0.0  # 连上立即上报首帧心跳
                log.info("connected to %s", cfg["server"])
            except Exception as e:
                log.warning("connect failed: %s; retry in %.0fs", e, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

        now = time.time()
        if now >= next_hb:
            submit_heartbeat(cfg, log, q, state_box)
            next_hb = now + cfg["interval"] * random.uniform(0.9, 1.1)

        # 等socket可读（最多 0.5s，保证 stop 响应及时），同时让出 CPU
        try:
            r, _, _ = select.select([ws.sock], [], [], 0.5)
            if r:
                for text in ws.drain():
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        log.warning("bad frame from server")
                        continue
                    handle_server_msg(msg, ws, runner, cfg, log, q)
        except (WSClosed, WSError, OSError) as e:
            log.warning("connection lost: %s", e)
            kk_conn.safe_close(ws)
            ws = None
            continue

        if not flush_outgoing(ws, q, cfg, log):
            ws = None

    kk_conn.safe_close(ws)
    log.info("kk-agent stopped")
