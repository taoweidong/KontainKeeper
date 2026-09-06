"""主循环协作单测：结果分块、失败收敛、命令分派。

其中 dispatcher + Runner + send_result 的串联刻意复刻 run() 里的接法：
`emit` 签名曾写成三参数而与 Runner 的两参数调用不匹配，TypeError 被线程池
静默吞掉，导致**一条命令结果都发不出去**。这个用例就是那条路径的回归锁。
"""
import base64
import queue
import sys
import threading

from kk_agent import main as m
from kk_agent import executor as kk_executor


CFG = {"interval": 60, "plugin_dir": "", "top_n": 5, "disk_paths": [],
       "host": "web-01", "allow_shell": True, "plugin_timeout": 5}


class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeTransport:
    """fail_calls 按*调用次序*模拟 Broker 拒收（真实队列不看帧内 seq）。"""

    def __init__(self, fail_calls=()):
        self.frames = []
        self.fail_calls = set(fail_calls)
        self.calls = 0

    def publish_result(self, frame):
        i = self.calls
        self.calls += 1
        if i in self.fail_calls:
            return False
        self.frames.append(frame)
        return True

    def publish_hb(self, metrics, custom=None):
        self.frames.append({"hb": metrics, "custom": custom})
        return True


def decode(frames):
    return b"".join(base64.b64decode(f["out_b64"]) for f in frames if "out_b64" in f)


# ---- 结果分块与失败收敛 ----

def test_send_result_single_chunk_carries_terminal_fields():
    tr = FakeTransport()
    ok = m.send_result(tr, "c-1", {"rc": 0, "out": b"hello", "timed_out": False,
                                   "elapsed_ms": 12, "truncated": False})
    assert ok is True
    assert len(tr.frames) == 1
    f = tr.frames[0]
    assert f["done"] is True and f["seq"] == 0 and f["total"] == 1
    assert f["rc"] == 0 and f["elapsed_ms"] == 12 and decode([f]) == b"hello"


def test_send_result_empty_output_still_emits_done():
    tr = FakeTransport()
    m.send_result(tr, "c-2", {"rc": 0, "out": b"", "timed_out": False, "elapsed_ms": 1})
    assert tr.frames[-1]["done"] is True and tr.frames[-1]["out_b64"] == ""


def test_send_result_chunks_are_reassemblable_in_order():
    tr = FakeTransport()
    payload = bytes(range(256)) * 600  # 153,600 字节 → 4 块
    m.send_result(tr, "c-3", {"rc": 0, "out": payload, "timed_out": False,
                              "elapsed_ms": 5, "truncated": False})
    assert [f["seq"] for f in tr.frames] == [0, 1, 2, 3]
    assert tr.frames[-1]["done"] is True
    assert decode(tr.frames) == payload


def test_send_result_emits_failure_terminal_when_chunk_dropped():
    """out-queue 挤爆时宁可回一条失败终态，也不能让命令永远停在 running。"""
    tr = FakeTransport(fail_calls=[1])  # 第 2 块被拒，终态帧仍可送达
    ok = m.send_result(tr, "c-4", {"rc": 0, "out": b"z" * (m.CHUNK * 3),
                                   "timed_out": False, "elapsed_ms": 30})
    assert ok is True
    assert decode(tr.frames) == b"z" * m.CHUNK, "已送达的第一块输出要保留"
    last = tr.frames[-1]
    assert last["done"] is True
    assert last["rc"] == m.RC_SEND_FAILED
    assert last["truncated"] is True and last["out_b64"] == ""


def test_send_result_reports_give_up_when_terminal_also_fails():
    tr = FakeTransport(fail_calls=[0, 1])
    assert m.send_result(tr, "c-5", {"rc": 0, "out": b"x"}) is False


# ---- 命令分派 ----

def build_runner(tr, allow_shell=True):
    """与 run() 内同样的接法搭好 Runner + dispatcher。"""
    def emit(cid, res):
        m.send_result(tr, cid, res)

    runner = kk_executor.Runner(emit, max_out=4 * 1024 * 1024, max_workers=2,
                                allow_shell=allow_shell, log=_NullLog())
    return m.make_dispatcher(tr, runner, dict(CFG), _NullLog(), m.StateBox())


def test_dispatch_shell_command_returns_result_end_to_end():
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-ok", "kind": "shell",
              "argv": [sys.executable, "-c", "print('kk-ok')"], "timeout": 20})
    # 命令在池里异步跑，等一下末帧
    for _ in range(200):
        if tr.frames and tr.frames[-1].get("done"):
            break
        threading.Event().wait(0.05)
    assert tr.frames and tr.frames[-1]["done"] is True
    assert b"kk-ok" in decode(tr.frames)
    assert tr.frames[-1]["rc"] == 0


def test_dispatch_rejects_unknown_kind():
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-bad", "kind": "wat"})
    assert tr.frames[-1]["rc"] == 127 and b"unknown command kind" in decode(tr.frames)


def test_dispatch_ignores_frame_without_id():
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"kind": "shell", "argv": ["echo"]})
    assert tr.frames == []


def test_dispatch_update_kind_triggers_self_update(monkeypatch):
    """推送式自更新此前在 dispatcher 里没有分支，落到 unknown kind → 永远不更新。"""
    seen = {}

    def spy(cfg, log, manifest):
        seen["manifest"] = manifest
    monkeypatch.setattr(m.kk_updater, "spawn_apply", spy)
    tr = FakeTransport()
    dispatch = build_runner(tr)
    manifest = {"id": "c-up", "kind": "update", "version": "9.9.9", "sha256": "abc"}
    dispatch(manifest)
    assert seen["manifest"] == manifest
    assert tr.frames == [], "update 不该回命令结果"


def test_dispatch_collect_requires_items(monkeypatch):
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-c", "kind": "collect", "items": []})
    for _ in range(100):
        if tr.frames:
            break
        threading.Event().wait(0.05)
    assert tr.frames[-1]["rc"] == 2


def test_dispatch_collect_passes_items_through(monkeypatch):
    calls = {}

    def fake_items(items, state, cfg=None):
        calls["items"] = items
        return {"cpu": 1.5}, state
    monkeypatch.setattr(m.kk_collector, "collect_items", fake_items)
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-c2", "kind": "collect", "items": ["cpu", "net"]})
    for _ in range(100):
        if tr.frames:
            break
        threading.Event().wait(0.05)
    assert calls["items"] == ["cpu", "net"]
    assert b'"cpu"' in decode(tr.frames)


# ---- 入口位置参数 → 配置 ----

def test_run_positional_overrides_reach_config(monkeypatch):
    """入口位置参数（./kk-agent mqtt://broker:1883）必须覆盖到 cfg["server"]。

    回归锁：run 曾写成 load(overrides=...)，字面量 "overrides" 被当成配置键
    塞进 cfg，server 覆盖从未生效——二进制一参拉起直接报「KK_SERVER 未配置」
    退出（2026-09-06 WSL 端到端验证抓到）。
    """
    monkeypatch.delenv("KK_SERVER", raising=False)
    made = {}

    class FakeTransport:
        def __init__(self, cfg, log):
            made["server"] = cfg["server"]
            made["cfg_keys"] = set(cfg)
            self.on_cmd = None

        def start(self):
            pass

        def wait_ready(self, timeout=10, stop=None):
            return True

        def stop(self, reason=""):
            pass

    monkeypatch.setattr(m, "Transport", FakeTransport)
    stop = threading.Event()
    stop.set()
    m.run(stop=stop, overrides={"server": "mqtt://broker.test:1883"})
    assert made.get("server") == "mqtt://broker.test:1883"
    assert "overrides" not in made.get("cfg_keys", {"overrides"}), \
        "overrides 不得作为配置键残留"


# ---- 差分基线容器 ----

def test_statebox_returns_copy_so_callers_cannot_mutate():
    box = m.StateBox({"disk_io": (1, 2)})
    got = box.value
    got["disk_io"] = "clobbered"
    assert box.value["disk_io"] == (1, 2)


def test_submit_heartbeat_skips_when_busy_and_clears_after(monkeypatch):
    """上一轮采集没跑完就跳过本轮，避免慢采集堆积成线程洪水。"""
    started = queue.Queue()

    def fake_collect(cfg, state):
        started.put("go")
        return {"cpu": 1.0}, {}
    monkeypatch.setattr(m.kk_collector, "collect", fake_collect)
    monkeypatch.setattr(m.kk_plugins, "collect_all", lambda d, log, timeout=5: {})
    tr = FakeTransport()
    busy = threading.Event()
    busy.set()
    m.submit_heartbeat(tr, dict(CFG), _NullLog(), m.StateBox(), busy)
    assert started.empty(), "busy 时不得再起线程"
    busy.clear()
    m.submit_heartbeat(tr, dict(CFG), _NullLog(), m.StateBox(), busy)
    assert started.get(timeout=5) == "go"
    for _ in range(100):
        if tr.frames:
            break
        threading.Event().wait(0.05)
    assert not busy.is_set()
    assert tr.frames and "hb" in tr.frames[0]
