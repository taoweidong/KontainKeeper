"""kk-agent 主循环：MQTT 事件驱动 + 一次性工作线程。

与旧版（自研 select 循环 + 手写 WebSocket）的区别：
- 连接管理、重连退避、保活、离线命令队列全部交给 paho-mqtt 的后台线程，
  主线程不再轮询 socket，空闲时只阻塞在 `stop.wait()` 上
- 命令由 MQTT 回调进入，立即派发到有限线程池执行；结果直接经 Transport 回传
- paho 的 publish 是线程安全的，工作线程采集完即发布，无需再经队列绕回主线程

资源策略不变：空闲零线程、采集/命令在 daemon 工作线程跑、常驻开销以 MB 计。
"""
import base64
import json
import random
import signal
import threading
import time

from . import collector as kk_collector
from . import config as kk_config
from . import executor as kk_executor
from . import logutil as kk_logutil
from . import plugin_loader as kk_plugins
from . import updater as kk_updater
from .transport import RC_SEND_FAILED, Transport, TransportError

CHUNK = 48 * 1024  # 命令输出分块大小（base64 前）


def _fail_frame(cmd_id, seq, total, res):
    """分块没能进 Broker 队列时补发的失败终态。"""
    return {"id": cmd_id, "seq": seq, "total": total, "out_b64": "", "done": True,
            "rc": RC_SEND_FAILED, "timed_out": False,
            "elapsed_ms": int(res.get("elapsed_ms", 0)), "truncated": True}


def send_result(tr, cmd_id, res):
    """把执行结果分块回传，末块带 done/rc/timed_out。

    任一分块发送失败即改发失败终态：宁可丢部分输出，也不能让服务端那一行
    永远停在 running（评审 P0-4「石沉大海」的 Agent 侧防线）。
    """
    out = res.get("out") or b""
    if isinstance(out, str):
        out = out.encode("utf-8", "replace")
    chunks = [out[i:i + CHUNK] for i in range(0, len(out), CHUNK)] or [b""]
    total = len(chunks)
    for i, c in enumerate(chunks):
        last = i == total - 1
        frame = {
            "id": cmd_id,
            "seq": i,
            "total": total,
            "out_b64": base64.b64encode(c).decode("ascii"),
            "done": last,
        }
        if last:
            frame.update(rc=res.get("rc", -2),
                         timed_out=bool(res.get("timed_out")),
                         elapsed_ms=int(res.get("elapsed_ms", 0)),
                         truncated=bool(res.get("truncated")))
        if not tr.publish_result(frame):
            return tr.publish_result(_fail_frame(cmd_id, i, total, res))
    return True


class StateBox:
    """采集差分基线的容器。

    心跳线程与 kind=collect 命令线程都会读-改-写同一份基线，无锁会让
    磁盘/网络速率算出尖刺甚至负值。
    """

    def __init__(self, state=None):
        self._lock = threading.Lock()
        self._state = state or {}

    @property
    def value(self):
        with self._lock:
            return dict(self._state)

    def put(self, state):
        with self._lock:
            self._state = state


def _run_collect(cmd, cfg, state_box):
    """kind=collect：不经 shell，直接调用 psutil 采集指定项并返回结构化 JSON。"""
    items = cmd.get("items") or []
    if not items:
        return {"rc": 2, "out": b"collect command requires items",
                "timed_out": False, "elapsed_ms": 0}
    data, new_state = kk_collector.collect_items(items, state_box.value, cfg)
    state_box.put(new_state)
    body = json.dumps({"items": items, "data": data}, ensure_ascii=False)
    return {"rc": 0, "out": body.encode("utf-8"), "timed_out": False, "elapsed_ms": 0}


def _run_plugin_reload(cfg, log):
    data = kk_plugins.collect_all(cfg["plugin_dir"], log, timeout=cfg["plugin_timeout"])
    body = json.dumps({"plugins": data}, ensure_ascii=False)
    return {"rc": 0, "out": body.encode("utf-8"), "timed_out": False, "elapsed_ms": 0}


def make_dispatcher(tr, runner, cfg, log, state_box):
    """构造 MQTT 命令回调。运行在 paho 网络线程，必须快速返回。"""

    def dispatch(cmd):
        cid = cmd.get("id")
        kind = cmd.get("kind") or "shell"
        if not cid:
            return
        if kind == "shell":
            runner.submit(cmd)
        elif kind == "collect":
            runner.submit_fn(cid, lambda: _run_collect(cmd, cfg, state_box))
        elif kind == "plugin_reload":
            runner.submit_fn(cid, lambda: _run_plugin_reload(cfg, log))
        elif kind == "update":
            # 服务端推送式自更新：命令载荷即版本清单，形态校验在 updater 内做
            kk_updater.spawn_apply(cfg, log, cmd)
        else:
            send_result(tr, cid, {"rc": 127, "out": b"unknown command kind: " + kind.encode(),
                                  "timed_out": False, "elapsed_ms": 0})

    return dispatch


def submit_heartbeat(tr, cfg, log, state_box, busy):
    """采集并发布一次心跳。busy 事件防止上一轮未完成时重入。"""
    if busy.is_set():
        return
    busy.set()

    def work():
        try:
            metrics, st = kk_collector.collect(cfg, state_box.value)
            state_box.put(st)
            custom = kk_plugins.collect_all(cfg["plugin_dir"], log,
                                            timeout=cfg["plugin_timeout"])
            tr.publish_hb(metrics, custom)
        except Exception:
            log.exception("heartbeat collect failed")
        finally:
            busy.clear()

    threading.Thread(target=work, daemon=True, name="kk-hb").start()


def run(stop=None, cfg=None, overrides=None):
    stop = stop or threading.Event()
    # overrides：入口位置参数（如 ./kk-agent mqtt://broker:1883）优先于环境变量。
    # 注意必须 ** 展开——load 的签名是 load(env=None, **overrides)，
    # 写成 load(overrides=...) 会把字面量 "overrides" 当配置键塞进 cfg。
    cfg = cfg or kk_config.load(**(overrides or {}))
    log = kk_logutil.get_logger(cfg["log_path"], cfg["log_level"])

    if not cfg["server"]:
        log.error("KK_SERVER 未配置（应为 mqtt:// 或 mqtts:// 地址），Agent 无法启动")
        return

    tr = None
    try:
        tr = Transport(cfg, log)
    except TransportError as e:
        log.error("%s", e)
        return

    state_box = StateBox()
    busy = threading.Event()

    def emit(cid, res):
        """executor.Runner 的结果回调；签名必须是 (cmd_id, res)。

        注意：这里曾写成 (kind, cid, res)（旧 WS 队列的元组遗留），与 Runner
        的调用不匹配 → TypeError 被线程池静默吞掉，命令结果一条都发不出去。
        """
        try:
            if not send_result(tr, cid, res):
                log.warning("result %s could not be delivered to broker", cid)
        except Exception:
            log.exception("send result failed")

    runner = kk_executor.Runner(emit, max_out=cfg["max_out_mb"] * 1024 * 1024,
                                max_workers=cfg["max_workers"],
                                allow_shell=cfg["allow_shell"], log=log)
    tr.on_cmd = make_dispatcher(tr, runner, cfg, log, state_box)

    # 优雅退出：信号处理器只能在主线程注册（测试跑在子线程时跳过）
    if threading.main_thread() is threading.current_thread():
        def _on_signal(signum, _frame):
            log.info("received signal %s, stopping", signum)
            stop.set()
        try:
            signal.signal(signal.SIGTERM, _on_signal)
            signal.signal(signal.SIGINT, _on_signal)
        except (ValueError, AttributeError, OSError):
            pass

    log.info("kk-agent %s starting: broker=%s host=%s interval=%ss",
             kk_config.AGENT_VER, cfg["server"], cfg["host"], cfg["interval"])

    tr.start()
    next_hb = 0.0
    next_update = 0.0  # 启动后尽快检查一次版本
    try:
        tr.wait_ready(timeout=10, stop=stop)
        while not stop.is_set():
            # 调度必须用单调钟：NTP 回拨会让心跳停摆、前跳会集中补发，
            # 而 500 台同时到点正是 Broker 与数据库的尖峰来源。
            now = time.monotonic()
            if now >= next_hb:
                submit_heartbeat(tr, cfg, log, state_box, busy)
                # ±10% 真随机抖动打散上报峰值
                next_hb = now + cfg["interval"] * random.uniform(0.9, 1.1)

            if not cfg["update_disabled"] and now >= next_update:
                kk_updater.spawn_check(cfg, log)
                next_update = now + cfg["update_interval"]

            stop.wait(0.5)
    finally:
        tr.stop(reason="stopping")
    log.info("kk-agent stopped")
