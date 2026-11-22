"""SSE 下行连接表（QR1125 Phase 0）：Agent 出站 GET 长连接，主机不开入站端口。

设计见 docs/design-transport.md §5.2。单进程内存连接表（host -> asyncio.Queue），
生命周期仿 mqtt_bridge._result_locks + _result_lock_ts + sweep 回收：SSE 生成器
connect 注册、ping 循环 touch 续活、finally disconnect；_janitor 周期 sweep_dead
回收停止 touch 的死连接（生成器异常退出未走 finally 的兜底）。

多实例：Phase 0 单进程先行；命令存 DB queued，Agent 重连任意实例 flush 补投保正确性，
跨实例实时推（sticky / pub-sub）留后续。flush_queued_commands 泛化 flush_queued_upgrades
（mqtt_bridge.py:655）：先 mark_queued_commands_sent 刷计时再推，防重启重投窗口。
"""
import asyncio
import time

from .downlink import cmd_payload, flush_queued


class SseHub:

    def __init__(self):
        # host -> {"queue": asyncio.Queue, "ts": 最近 touch}
        self._conns = {}

    def connect(self, host):
        """注册连接，返回该 host 的命令队列（SSE 生成器从中读）。须在事件循环内调。"""
        queue = asyncio.Queue()
        self._conns[host] = {"queue": queue, "ts": int(time.time())}
        return queue

    def touch(self, host):
        conn = self._conns.get(host)
        if conn is not None:
            conn["ts"] = int(time.time())

    def disconnect(self, host, queue=None):
        """注销连接；返回是否真的摘除了条目。

        传 queue 时按身份注销（QR1128-S7）：同 host 重连的新 connect 会覆盖
        旧条目，旧生成器的 finally 若按 host 无条件 pop，删掉的是**新**连接——
        活跃主机被误判离线、后续命令推进死队列。仅当条目仍是自己那份队列才摘。
        不传 queue 保持无条件注销（sweep 与既有测试调用点兼容）。
        """
        conn = self._conns.get(host)
        if conn is None:
            return False
        if queue is not None and conn["queue"] is not queue:
            return False
        self._conns.pop(host, None)
        return True

    def is_connected(self, host):
        return host in self._conns

    def push_command(self, host, payload):
        """连接在 → 入队 True；不在 → False（调用方据此回落 MQTT 或 queued）。"""
        conn = self._conns.get(host)
        if conn is None:
            return False
        conn["queue"].put_nowait(payload)
        conn["ts"] = int(time.time())
        return True

    def sweep_dead(self, timeout=90):
        """回收停止 touch 的死连接（健康生成器每 15s ping touch 一次）。"""
        now = int(time.time())
        dead = [h for h, c in self._conns.items() if now - c["ts"] > timeout]
        for h in dead:
            self._conns.pop(h, None)
        return len(dead)

    async def flush_queued_commands(self, host, store):
        """SSE 连接建立触发点：补投 queued 命令进本连接队列。

        骨架收口在 downlink.flush_queued（QR1128-S11）：先转 sent 刷计时再推，
        与 status online 触发点共用同一份「先 mark 后推」语义。
        """
        return await flush_queued(
            host, store, lambda r: self.push_command(host, cmd_payload(r)))