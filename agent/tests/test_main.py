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
       "host": "web-01", "allow_shell": True, "plugin_timeout": 5,
       # QR-P0-1：推送更新默认拒绝未签名清单；本文件的更新用例只关心回执链路，
       # 显式放行（未签名拒绝与 KK_UPDATE_DISABLED 门控各有专属用例）
       "update_allow_unsigned": True}


class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None

    def bind(self, *a, **k):
        # 与 loguru 语义一致：bind 返回可继续调用的 logger
        return self


class FakeTransport:
    """fail_calls 按*调用次序*模拟 Broker 拒收（真实队列不看帧内 seq）。"""

    def __init__(self, fail_calls=()):
        self.frames = []
        self.fail_calls = set(fail_calls)
        self.calls = 0
        self.urgent = []        # 每次 publish_result 的 urgent 标记（QR-A6）

    def publish_result(self, frame, urgent=False):
        i = self.calls
        self.calls += 1
        self.urgent.append(urgent)
        if i in self.fail_calls:
            return False
        self.frames.append(frame)
        return True

    def publish_hb(self, metrics, custom=None):
        self.frames.append({"hb": metrics, "custom": custom})
        return True

    def announce_update(self):
        # B6.1：真实 Transport 会发 reason=updating 的离线帧并等 PUBACK
        self.frames.append({"announce": "updating"})
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


def test_send_result_terminal_retry_is_urgent():
    """QR-A6：补发的失败终态走 QoS0——QoS1 会被同一个积压一起挡在门外。

    挡住的后果不是「少一段输出」而是「服务端那一行永远停在 running」，
    于是拿「终态帧可能丢一次」去换「不被自己的积压堵住」，真丢了有服务端超时清扫兜底。
    """
    tr = FakeTransport(fail_calls=[0])
    assert m.send_result(tr, "c-urgent", {"rc": 0, "out": b"x",
                                          "timed_out": False, "elapsed_ms": 0}) is True
    assert tr.urgent == [False, True], "首块 QoS1，失败终态必须 urgent=True"
    assert tr.frames[-1]["rc"] == m.RC_SEND_FAILED


def test_send_result_reports_give_up_when_terminal_also_fails():
    tr = FakeTransport(fail_calls=[0, 1])
    assert m.send_result(tr, "c-5", {"rc": 0, "out": b"x"}) is False


# ---- 命令分派 ----

def build_runner(tr, allow_shell=True, cfg_overrides=None):
    """与 run() 内同样的接法搭好 Runner + dispatcher。"""
    def emit(cid, res):
        m.send_result(tr, cid, res)

    runner = kk_executor.Runner(emit, max_out=4 * 1024 * 1024, max_workers=2,
                                allow_shell=allow_shell, log=_NullLog())
    cfg = dict(CFG)
    cfg.update(cfg_overrides or {})
    return m.make_dispatcher(tr, runner, cfg, _NullLog(), m.StateBox())


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
    """推送式自更新此前在 dispatcher 里没有分支，落到 unknown kind → 永远不更新。

    A6.2 起改为走 runner 并回传回执：更新曾经是全平台唯一没有结果的操作，
    服务端无从回答「500 台升了多少、失败多少」，故这里连回执一起锁。
    """
    seen = {}

    def spy(cfg, log, manifest, on_before_restart=None):
        seen["manifest"] = manifest
        return True, None

    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt", spy)
    tr = FakeTransport()
    dispatch = build_runner(tr)
    manifest = {"id": "c-up", "kind": "update", "version": "9.9.9", "sha256": "abc"}
    dispatch(manifest)
    for _ in range(200):
        if tr.frames and tr.frames[-1].get("done"):
            break
        threading.Event().wait(0.05)
    assert seen.get("manifest") == manifest
    assert tr.frames and tr.frames[-1]["rc"] == 0, "更新成功也要回执，否则台账永远停在 pending"


def test_dispatch_update_failure_receipt_carries_reason(monkeypatch):
    """失败回执带原因码：台账要显示 sha256_mismatch，而不是一句「失败」。"""
    monkeypatch.setattr(
        m.kk_updater, "apply_manifest_receipt",
        lambda cfg, log, manifest, on_before_restart=None: (False, "sha256_mismatch"))
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-up-bad", "kind": "update", "version": "9.9.9", "sha256": "x"})
    for _ in range(200):
        if tr.frames and tr.frames[-1].get("done"):
            break
        threading.Event().wait(0.05)
    assert tr.frames[-1]["rc"] == 1
    assert b"sha256_mismatch" in decode(tr.frames)


def test_dispatch_update_announces_offline_before_restart(monkeypatch):
    """B6.1：execv 前必须宣告 reason=updating。

    否则服务端只看到 Broker 补发的 LWT（reason 为空），运维分不清「正在自更新」
    与「容器停了」。
    """
    seen = {}

    def spy(cfg, log, manifest, on_before_restart=None):
        seen["hook"] = on_before_restart
        if on_before_restart:
            on_before_restart()          # 真实 apply 在 execv 前调用钩子
        return True, ""

    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt", spy)
    tr = FakeTransport()
    dispatch = build_runner(tr)
    dispatch({"id": "c-up-ann", "kind": "update", "version": "9.9.9", "sha256": "x"})
    for _ in range(200):
        if any(f.get("done") for f in tr.frames):
            break
        threading.Event().wait(0.05)
    assert seen.get("hook") is not None, "更新必须带 on_before_restart 钩子"
    assert any(f.get("announce") == "updating" for f in tr.frames), "缺少 reason=updating 宣告"


def test_each_kind_emits_exactly_one_result(monkeypatch):
    """QR-A19 回归锁：kind=update 的结果帧曾发两遍。

    `_run_update` 自己 `send_result` 之后又 `return res`，`submit_fn` 按返回值再
    `emit` 一次 → 服务端多写一条 `agent_update_failed` 审计、`upgrade_failed`
    计数翻倍、全网 QoS1 流量 ×2。旧用例只断言「末帧内容正确」，看不见多发，
    所以这里数的是 `publish_result` 的调用次数：每个 kind 都必须恰好一条回执。
    """
    monkeypatch.setattr(
        m.kk_updater, "apply_manifest_receipt",
        lambda cfg, log, manifest, on_before_restart=None: (False, "boom"))
    monkeypatch.setattr(m.kk_collector, "collect_items",
                        lambda items, state, cfg=None: ({"cpu": 1.5}, state))
    cases = [
        {"id": "c-1", "kind": "shell",
         "argv": [sys.executable, "-c", "print(1)"], "timeout": 20},
        {"id": "c-2", "kind": "collect", "items": ["cpu"]},
        {"id": "c-3", "kind": "plugin_reload"},
        {"id": "c-4", "kind": "update", "version": "9.9.9", "sha256": "x"},
    ]
    for cmd in cases:
        tr = FakeTransport()
        dispatch = build_runner(tr)
        dispatch(cmd)
        for _ in range(200):
            if tr.frames and tr.frames[-1].get("done"):
                break
            threading.Event().wait(0.05)
        assert tr.frames and tr.frames[-1]["done"] is True, cmd["kind"]
        assert tr.calls == 1, "%s 发了 %d 帧结果，回执必须恰好一条" % (cmd["kind"], tr.calls)


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


# ---- QR-P0-1 / QR-A1：更新门控、未签名拒绝与 QoS1 去重 ----

def wait_done(tr, limit=200):
    for _ in range(limit):
        if tr.frames and tr.frames[-1].get("done"):
            return True
        threading.Event().wait(0.05)
    return False


def test_dispatch_update_respects_update_disabled():
    """QR-P0-1：KK_UPDATE_DISABLED 必须同时关掉推送路径，回执带 update_disabled。

    此前该开关只挡轮询（main 的调度循环与 check_update），kind=update 命令
    照常执行——运维显式关了更新，推送式升级仍能把 Agent 整个换掉。
    """
    tr = FakeTransport()
    dispatch = build_runner(tr, cfg_overrides={"update_disabled": True})
    dispatch({"id": "c-dis", "kind": "update", "version": "9.9.9", "sha256": "abc"})
    assert wait_done(tr)
    assert tr.frames[-1]["rc"] == 1
    assert b"update_disabled" in decode(tr.frames)


def test_dispatch_update_rejects_unsigned_by_default():
    """QR-P0-1：未配 HMAC key 且未显式 KK_UPDATE_ALLOW_UNSIGNED=1 时拒绝未签名推送。

    匿名 Broker 下未签名推送等价于任何能连 Broker 的客户端都能远程替换二进制。
    """
    tr = FakeTransport()
    dispatch = build_runner(tr, cfg_overrides={"update_allow_unsigned": False})
    dispatch({"id": "c-uns", "kind": "update", "version": "9.9.9", "sha256": "abc"})
    assert wait_done(tr)
    assert tr.frames[-1]["rc"] == 1
    assert b"unsigned_push_rejected" in decode(tr.frames)


def test_dispatch_update_accepts_signed_when_key_configured(monkeypatch):
    """配了 KK_UPDATE_HMAC_KEY 时推送放行（签名本体校验在 updater 内，另有用例）。"""
    seen = {}

    def spy(cfg, log, manifest, on_before_restart=None):
        seen["ok"] = True
        return True, ""

    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt", spy)
    tr = FakeTransport()
    dispatch = build_runner(tr, cfg_overrides={"update_hmac_key": "s3cret",
                                               "update_allow_unsigned": False})
    dispatch({"id": "c-key", "kind": "update", "version": "9.9.9", "sha256": "abc"})
    assert wait_done(tr)
    assert seen.get("ok") is True
    assert tr.frames[-1]["rc"] == 0


def test_dispatcher_dedups_redelivered_command():
    """QR-A1：QoS1 持久会话重发同 id 命令只执行一次（shell 副作用不可幂等）。"""
    tr = FakeTransport()
    dispatch = build_runner(tr)
    cmd = {"id": "c-dup", "kind": "shell",
           "argv": [sys.executable, "-c", "print('kk-dedup')"], "timeout": 20}
    dispatch(cmd)
    dispatch(cmd)   # Broker 重发：必须被丢弃，不进执行池
    assert wait_done(tr)
    threading.Event().wait(0.3)   # 给「去重失效时的第二跑」留完成窗口
    done_frames = [f for f in tr.frames if f.get("done")]
    assert len(done_frames) == 1, "同一命令 id 只允许一条终态帧"
