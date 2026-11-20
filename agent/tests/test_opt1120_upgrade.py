"""QR1120-A6..A12 自更新/传输健壮性批次的回归锁。

覆盖：
- A6  首拍更新检查带抖动（批量部署惊群）
- A7  良性跳过（not_newer/backoff/update_busy）回执 rc=0，真实失败仍 rc=1
- A8  下载总 wall-clock 时限 + 更新锁有限等待（busy 良性跳过）
- A9  启动清扫陈旧下载半成品（只认前缀、只清 mtime>1h）
- A10 轮询拉清单失败的 warning 节奏（首次 + 每 N 次，成功清零）
- A11 announce_update 失败留痕（不阻断 execv）
- A12 cmd 帧 64KB 上限（json.loads 前丢弃、节流 warning）+ 会话被踢提示

全部用假时钟/假 HTTP/假 MQTT Client，不依赖真实 Broker/网络/真实时钟/执行顺序。
"""
import base64
import hashlib
import os
import threading
import time

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from kk_agent import executor as kk_executor
from kk_agent import main as m
from kk_agent import transport as tp
from kk_agent import updater


# ---- 公共替身 ----

class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None

    def bind(self, *a, **k):
        return self


class _RecLog:
    """按级别记录格式化后的消息；bind 返回自身（与 loguru 适配层语义一致）。"""

    def __init__(self):
        self.records = []

    def bind(self, *a, **k):
        return self

    def _rec(self, level, msg, *args):
        self.records.append((level, msg % args if args else msg))

    def debug(self, msg, *args, **kw):
        self._rec("debug", msg, *args)

    def info(self, msg, *args, **kw):
        self._rec("info", msg, *args)

    def warning(self, msg, *args, **kw):
        self._rec("warning", msg, *args)

    def error(self, msg, *args, **kw):
        self._rec("error", msg, *args)

    def exception(self, msg, *args, **kw):
        self._rec("error", msg, *args)

    def messages(self, level):
        return [msg for lv, msg in self.records if lv == level]


class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t


@pytest.fixture(autouse=True)
def _reset_fetch_state():
    """A10 的连续失败计数是模块级状态，用例前后各清一次防串扰。"""
    updater.reset_fetch_failure()
    yield
    updater.reset_fetch_failure()


# ---- A6：首拍更新检查抖动 ----

class _RunTransport:
    def __init__(self, cfg, log):
        self.on_cmd = None
        self.stopped = None

    def start(self):
        pass

    def wait_ready(self, timeout=10, stop=None):
        return True

    def stop(self, reason=""):
        self.stopped = reason


class _FakeStop:
    """wait 推进假时钟；时钟越过 limit 即视为收到停止信号。"""

    def __init__(self, clock, limit):
        self.clock = clock
        self.limit = limit

    def is_set(self):
        return self.clock.t >= self.limit

    def wait(self, dt):
        self.clock.t += dt
        return False

    def set(self):
        self.limit = self.clock.t


RUN_CFG = {"server": "mqtt://broker.test:1883", "host": "h", "topic_prefix": "kk/v1",
           "interval": 60, "update_interval": 300, "update_disabled": False,
           "log_path": "", "log_level": "WARNING", "plugin_dir": "", "plugin_timeout": 5,
           "max_out_mb": 1, "max_workers": 2, "allow_shell": True, "agent_bin": ""}


def test_first_update_check_is_jittered_over_first_interval(monkeypatch):
    """QR1120-A6：首拍检查必须落在 [0, update_interval) 的随机相位上。

    旧实现 next_update=0，±20% 抖动只作用后续周期——500 台同镜像批量启动
    首拍全在 t≈0 拉包（惊群）。这里用假时钟 + uniform 记录器钉住：首次抽签
    区间是 (0, interval)，首查发生在 start+interval/2（uniform 取中点替身）。
    """
    clock = _FakeClock(1000.0)
    draws = []
    spawns = []

    def fake_uniform(a, b):
        draws.append((a, b))
        return (a + b) / 2.0

    monkeypatch.setattr(m.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(m.random, "uniform", fake_uniform)
    monkeypatch.setattr(m, "Transport", _RunTransport)
    monkeypatch.setattr(m, "submit_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(m.signal, "signal", lambda *a: None)
    monkeypatch.setattr(m.kk_updater, "spawn_check",
                        lambda *a, **k: spawns.append(clock.t))

    m.run(stop=_FakeStop(clock, limit=1200.0), cfg=dict(RUN_CFG))

    assert draws[0] == (0, 300), "首拍更新检查必须在 [0, interval) 内抽签"
    assert spawns == [1150.0], "首查应落在启动后 interval/2（替身取中点），而非 t≈0"
    assert (0.8, 1.2) in draws, "后续周期的 ±20% 抖动必须保留（比例抽签，乘 interval）"


def test_run_sweeps_stale_downloads_at_startup(monkeypatch):
    """QR1120-A9：run() 启动阶段必须调用一次陈旧半成品清扫。"""
    seen = []
    monkeypatch.setattr(m, "Transport", _RunTransport)
    monkeypatch.setattr(m.signal, "signal", lambda *a: None)
    monkeypatch.setattr(m.kk_updater, "sweep_stale_downloads",
                        lambda cfg, log=None: seen.append(cfg))
    stop = threading.Event()
    stop.set()
    cfg = dict(RUN_CFG, update_disabled=True)
    m.run(stop=stop, cfg=cfg)
    assert len(seen) == 1 and seen[0] is cfg


# ---- A7：良性跳过 rc=0，真实失败仍 rc=1 ----

class _FakeTr:
    def __init__(self):
        self.frames = []

    def publish_result(self, frame, urgent=False):
        self.frames.append(frame)
        return True

    def announce_update(self):
        return True


def _update_cfg():
    return {"update_allow_unsigned": True}


@pytest.mark.parametrize("reason", ["not_newer", "backoff", "update_busy"])
def test_benign_skip_receipt_is_rc0(monkeypatch, reason):
    """QR1120-A7：良性跳过不是失败——rc=0（服务端落 done），reason 字节保留。

    QoS1 重投同一条 update 帧（execv 后新进程收到、版本已最新）或退避期重推，
    旧实现回 rc=1 → 服务端多假 failed 行 + 审计。
    """
    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt",
                        lambda cfg, log, manifest, on_before_restart=None: (False, reason))
    res = m._run_update(_FakeTr(), {"id": "c", "kind": "update"}, _update_cfg(), _NullLog())
    assert res["rc"] == 0
    assert res["out"] == reason.encode()


@pytest.mark.parametrize("reason", [
    "sha256_mismatch", "http_error", "exec_error", "no_sha256", "hmac_mismatch",
    "size_mismatch", "disk_error", "bad_target", "no_target", "no_url"])
def test_real_failure_receipt_stays_rc1(monkeypatch, reason):
    """QR1120-A7：真实失败（校验/下载/execv/形态）仍 rc=1，不许被良性映射洗白。"""
    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt",
                        lambda cfg, log, manifest, on_before_restart=None: (False, reason))
    res = m._run_update(_FakeTr(), {"id": "c", "kind": "update"}, _update_cfg(), _NullLog())
    assert res["rc"] == 1
    assert res["out"] == reason.encode()


def test_success_receipt_is_rc0(monkeypatch):
    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt",
                        lambda cfg, log, manifest, on_before_restart=None: (True, ""))
    res = m._run_update(_FakeTr(), {"id": "c", "kind": "update"}, _update_cfg(), _NullLog())
    assert res["rc"] == 0 and res["out"] == b""


def test_benign_reason_set_is_the_server_contract():
    """QR1120-A7：良性集合是跨 agent 契约（服务端 rc=0 落 done），显式钉住。

    update_busy 同时必须在 _NO_BACKOFF 里——锁 busy 跳过不算失败，
    不得污染退避账本。
    """
    assert updater.BENIGN_SKIP_REASONS == frozenset({"not_newer", "backoff", "update_busy"})
    assert updater._NO_BACKOFF == updater.BENIGN_SKIP_REASONS


@pytest.mark.parametrize("reason", ["not_newer", "backoff", "update_busy"])
def test_benign_skip_end_to_end_one_frame_rc0(monkeypatch, reason):
    """QR1120-A7 帧级回归：dispatcher → Runner → send_result 全链路恰好一帧、rc=0。"""
    monkeypatch.setattr(m.kk_updater, "apply_manifest_receipt",
                        lambda cfg, log, manifest, on_before_restart=None: (False, reason))
    tr = _FakeTr()

    def emit(cid, res):
        m.send_result(tr, cid, res)

    runner = kk_executor.Runner(emit, max_out=1024 * 1024, max_workers=2,
                                allow_shell=True, log=_NullLog())
    cfg = dict(_update_cfg(), interval=60, plugin_dir="", plugin_timeout=5,
               host="h", allow_shell=True)
    dispatch = m.make_dispatcher(tr, runner, cfg, _NullLog(), m.StateBox())
    dispatch({"id": "c-benign", "kind": "update", "version": "9.9.9", "sha256": "x"})
    for _ in range(200):
        if tr.frames and tr.frames[-1].get("done"):
            break
        threading.Event().wait(0.05)
    assert len(tr.frames) == 1, "良性跳过也必须恰好一条回执（QR-A19 口径不变）"
    assert tr.frames[0]["rc"] == 0
    out = base64.b64decode(tr.frames[0]["out_b64"])
    assert out == reason.encode()


# ---- A8：下载总时限 + 锁有限等待 ----

class _BusyLock:
    """acquire 恒失败的锁替身：记录 timeout 实参，零等待、无挂死风险。"""

    def __init__(self):
        self.timeouts = []
        self.released = 0

    def acquire(self, blocking=True, timeout=-1):
        self.timeouts.append(timeout)
        return False

    def release(self):
        self.released += 1


def _newer_manifest():
    return {"version": "99.99.99", "sha256": "00" * 32, "size": 8, "url": "/x"}


def test_lock_busy_skips_without_download_or_backoff(monkeypatch, tmp_path):
    """QR1120-A8：拿不到更新锁即跳过——不下载、不落盘、不进退避账本。

    旧实现 `with _update_lock` 无限等待：上一轮卡在慢速源时，轮询每 300s
    spawn 的新线程全在锁上堆积。
    """
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    lock = _BusyLock()
    monkeypatch.setattr(updater, "_update_lock", lock)
    dl = []
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: dl.append(1))
    cfg = {"update_url": "http://api", "agent_bin": str(target)}

    assert updater.apply_manifest_receipt(cfg, None, _newer_manifest()) == (False, "update_busy")
    assert dl == [], "锁 busy 不得发起下载"
    assert lock.timeouts == [updater.UPDATE_LOCK_WAIT_SEC], "必须按模块超时有限等待"
    assert updater.UPDATE_LOCK_WAIT_SEC == 10
    assert lock.released == 0, "没拿到锁就不能 release"
    assert updater._fail_state["count"] == 0, "busy 是良性跳过，不得记失败退避"
    assert list(tmp_path.glob(updater.DOWNLOAD_TMP_PREFIX + "*")) == []
    assert target.read_bytes() == b"old"


def test_polling_path_busy_skip_is_silent(monkeypatch, tmp_path):
    """QR1120-A8：轮询路径锁 busy 静默跳过（至多 info，不得 warning 刷屏）。"""
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    lock = _BusyLock()
    monkeypatch.setattr(updater, "_update_lock", lock)
    monkeypatch.setattr(updater, "fetch_latest", lambda *a, **k: _newer_manifest())
    dl = []
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: dl.append(1))
    rec = _RecLog()
    assert updater.check_update({"update_url": "http://api", "agent_bin": str(target)},
                                rec) is False
    assert dl == []
    assert lock.timeouts == [updater.UPDATE_LOCK_WAIT_SEC]
    assert rec.messages("warning") == [] and rec.messages("error") == []


class _FakeResp:
    """每 read 一口推进假时钟 step 秒；chunks 耗尽后返回 b""。"""

    def __init__(self, clock, chunks, step):
        self.clock = clock
        self.chunks = list(chunks)
        self.step = step

    def read(self, n):
        self.clock.t += self.step
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, resp):
        self.resp = resp

    def open(self, req, timeout=None):
        return self.resp


def _patch_http(monkeypatch, clock, chunks, step):
    resp = _FakeResp(clock, chunks, step)
    monkeypatch.setattr(updater.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(updater, "_build_opener", lambda insecure: _FakeOpener(resp))
    return resp


def test_download_wall_clock_deadline_cleans_tmp(monkeypatch, tmp_path):
    """QR1120-A8：慢速滴答源（每口 100s）超总时限即失败，半成品删干净。

    timeout=60 只是单次 socket 读超时，滴答源能让线程长活；总时限兜底。
    """
    clock = _FakeClock(0.0)
    _patch_http(monkeypatch, clock, [b"x" * 1024] * 100, step=100.0)
    dst = tmp_path / "dl.bin"
    with pytest.raises(RuntimeError, match="deadline"):
        updater.download_binary("http://slow/bin", None, to_path=str(dst),
                                deadline_sec=300)
    assert not dst.exists(), "超限后半成品必须清理"


def test_download_deadline_also_applies_to_bytes_form(monkeypatch):
    """QR1120-A8：字节形态（旧契约）同样受总时限约束。"""
    clock = _FakeClock(0.0)
    _patch_http(monkeypatch, clock, [b"x" * 1024] * 100, step=100.0)
    with pytest.raises(RuntimeError, match="deadline"):
        updater.download_binary("http://slow/bin", None, deadline_sec=300)


def test_download_within_deadline_succeeds(monkeypatch, tmp_path):
    """QR1120-A8 正向：时限内的正常下载不受影响，(size, sha256) 契约不变。"""
    clock = _FakeClock(0.0)
    chunks = [b"a" * 1024, b"b" * 1024]
    _patch_http(monkeypatch, clock, chunks, step=1.0)
    dst = tmp_path / "dl.bin"
    size, sha = updater.download_binary("http://fast/bin", None, to_path=str(dst),
                                        deadline_sec=300)
    data = b"a" * 1024 + b"b" * 1024
    assert (size, sha) == (len(data), hashlib.sha256(data).hexdigest())
    assert dst.read_bytes() == data


def test_download_deadline_budget_is_explicit():
    assert updater.DOWNLOAD_DEADLINE_SEC == 300


def test_update_lock_released_on_success_and_failure(tmp_path, monkeypatch):
    """QR1120-A8：acquire/finally-release 改造后，成功与失败路径都必须归还锁。"""
    data = b"\x7fELF-new"
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    monkeypatch.setattr(os, "execv", lambda *a: None)

    def stub_dl(url, log, insecure=False, max_bytes=None, to_path=None):
        with open(to_path, "wb") as f:
            f.write(data)
        return len(data), hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(updater, "download_binary", stub_dl)
    cfg = {"update_url": "http://api", "agent_bin": str(target)}
    good = {"version": "99.99.99", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "url": "/x"}
    assert updater.apply_manifest(cfg, None, good) is True
    assert updater._update_lock.acquire(timeout=0) is True, "成功后锁必须已归还"
    updater._update_lock.release()

    bad = {"version": "99.99.100", "sha256": "00" * 32, "size": len(data), "url": "/x"}
    assert updater.apply_manifest_receipt(cfg, None, bad) == (False, "sha256_mismatch")
    assert updater._update_lock.acquire(timeout=0) is True, "失败后锁必须已归还"
    updater._update_lock.release()


# ---- A9：启动清扫陈旧半成品 ----

def test_sweep_removes_only_stale_prefixed_files(tmp_path):
    """QR1120-A9：只认 .kk-agent.download.* 前缀、只清 mtime>1h 的普通文件。

    在途下载（mtime 新鲜）、无关文件、同名前缀的目录都不得误删。
    """
    now = time.time()
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    stale = tmp_path / (updater.DOWNLOAD_TMP_PREFIX + "111")
    stale.write_bytes(b"z" * 1024)
    os.utime(stale, (now - 7200, now - 7200))
    fresh = tmp_path / (updater.DOWNLOAD_TMP_PREFIX + "222")
    fresh.write_bytes(b"in-flight")
    unrelated = tmp_path / "other.tmp"
    unrelated.write_bytes(b"keep")
    os.utime(unrelated, (now - 7200, now - 7200))
    decoy_dir = tmp_path / (updater.DOWNLOAD_TMP_PREFIX + "dir")
    decoy_dir.mkdir()
    os.utime(decoy_dir, (now - 7200, now - 7200))

    updater.sweep_stale_downloads({"agent_bin": str(target)}, None)

    assert not stale.exists(), "陈旧残留必须清掉"
    assert fresh.exists(), "在途下载（mtime 新鲜）不得误删"
    assert unrelated.exists(), "非前缀文件不得碰"
    assert decoy_dir.is_dir(), "目录不得删"
    assert target.read_bytes() == b"old"


def test_sweep_source_mode_is_noop(monkeypatch, tmp_path):
    """QR1120-A9：源码形态无替换目标 → 空操作，连目录都不列。"""
    monkeypatch.setattr(updater.sys, "frozen", False, raising=False)
    stale = tmp_path / (updater.DOWNLOAD_TMP_PREFIX + "111")
    stale.write_bytes(b"z")
    os.utime(stale, (0, 0))
    listed = []
    monkeypatch.setattr(updater.os, "listdir", lambda p: listed.append(p) or [])
    updater.sweep_stale_downloads({"agent_bin": ""}, None)
    assert listed == [] and stale.exists()


def test_sweep_listdir_failure_is_silent(monkeypatch, tmp_path):
    """QR1120-A9：清扫失败静默 debug——卫生动作绝不能挡启动。"""
    def boom(p):
        raise OSError("permission denied")
    monkeypatch.setattr(updater.os, "listdir", boom)
    rec = _RecLog()
    updater.sweep_stale_downloads({"agent_bin": str(tmp_path / "kk-agent")}, rec)
    assert rec.messages("warning") == [] and rec.messages("error") == []


def test_sweep_remove_failure_is_debug_only(monkeypatch, tmp_path):
    """QR1120-A9：单个文件删不掉（被占用等）只记 debug，其余照常。"""
    now = time.time()
    stale = tmp_path / (updater.DOWNLOAD_TMP_PREFIX + "111")
    stale.write_bytes(b"z")
    os.utime(stale, (now - 7200, now - 7200))

    def boom(p):
        raise OSError("file in use")
    monkeypatch.setattr(updater.os, "remove", boom)
    rec = _RecLog()
    updater.sweep_stale_downloads({"agent_bin": str(tmp_path / "kk-agent")}, rec)
    assert stale.exists()
    assert rec.messages("warning") == [] and rec.messages("error") == []
    assert rec.messages("debug"), "失败要留 debug 痕"


# ---- A10：轮询拉清单失败的 warning 节奏 ----

def test_fetch_failure_warning_cadence_and_reset(monkeypatch):
    """QR1120-A10：首次失败 warning、每 10 次再 warning，成功清零。

    旧实现只记 debug（默认 INFO 不可见），轮询坏了完全无痕。
    """
    rec = _RecLog()
    cfg = {"update_url": "http://api"}

    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(updater, "_http_get", boom)

    for _ in range(9):
        assert updater.fetch_latest(cfg, rec) is None
    assert len(rec.messages("warning")) == 1, "首次失败必须 warning，其余 debug"
    assert "1 consecutive" in rec.messages("warning")[0]

    updater.fetch_latest(cfg, rec)          # 第 10 次
    assert len(rec.messages("warning")) == 2
    assert "10 consecutive" in rec.messages("warning")[1]

    for _ in range(10):                     # 第 11..20 次
        updater.fetch_latest(cfg, rec)
    assert len(rec.messages("warning")) == 3, "第 20 次再 warning 一次"

    # 成功（即便无更新）清零：下一次失败又是「首次」
    monkeypatch.setattr(updater, "_http_get", lambda *a, **k: '{"available": false}')
    assert updater.fetch_latest(cfg, rec) is None
    monkeypatch.setattr(updater, "_http_get", boom)
    updater.fetch_latest(cfg, rec)
    assert len(rec.messages("warning")) == 4
    assert "1 consecutive" in rec.messages("warning")[3]


# ---- A11 / A12 的传输层替身 ----

class _FakeInfo:
    def __init__(self, rc, acked):
        self.rc = rc
        self._acked = acked
        self.waited = None

    def wait_for_publish(self, timeout=None):
        self.waited = timeout

    def is_published(self):
        return self._acked


class _FakeCli:
    def __init__(self, connected=False, acked=True, publish_exc=None):
        self.published = []
        self._connected = connected
        self._acked = acked
        self._exc = publish_exc

    def is_connected(self):
        return self._connected

    def publish(self, topic, payload, qos=0, retain=False):
        if self._exc is not None:
            raise self._exc
        self.published.append({"topic": topic, "payload": payload, "qos": qos,
                               "retain": retain})
        return _FakeInfo(tp.mqtt.MQTT_ERR_SUCCESS, self._acked)

    def will_set(self, *a, **k):
        pass

    def max_queued_messages_set(self, n):
        pass

    def reconnect_delay_set(self, **k):
        pass

    def subscribe(self, *a, **k):
        pass

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def _make_tr(monkeypatch, log=None, **cli_kw):
    fake = _FakeCli(**cli_kw)
    monkeypatch.setattr(tp.mqtt, "Client", lambda *a, **k: fake)
    cfg = {"server": "mqtt://broker.test:1883", "host": "web-01",
           "topic_prefix": "kk/v1", "interval": 60, "advertise_ip": "10.0.0.5"}
    return tp.Transport(cfg, log or _NullLog()), fake


# ---- A11：announce_update 失败留痕 ----

def test_announce_update_warns_when_disconnected(monkeypatch):
    """QR1120-A11：未连接不再静默 return——留 warning，但不抛、不阻断 execv。"""
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec, connected=False)
    tr.announce_update()
    assert fake.published == []
    assert any("not connected" in w for w in rec.messages("warning")), rec.records


def test_announce_update_warns_when_unacked(monkeypatch):
    """QR1120-A11：PUBACK 等不到也要留痕（B6.1 宽限可能失效，运维要能看见）。"""
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec, connected=True, acked=False)
    tr.announce_update()
    assert fake.published, "仍要尝试发 updating 帧"
    assert any("not acknowledged" in w for w in rec.messages("warning")), rec.records


def test_announce_update_quiet_when_acked(monkeypatch):
    """QR1120-A11 正向：正常送达不得虚报 warning。"""
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec, connected=True, acked=True)
    tr.announce_update()
    assert rec.messages("warning") == []
    assert fake.published[-1]["retain"] is True


def test_announce_update_warns_on_publish_exception(monkeypatch):
    """QR1120-A11：publish 抛错同样留痕且不把异常外溢给更新主流程。"""
    rec = _RecLog()
    tr, _fake = _make_tr(monkeypatch, log=rec, connected=True,
                         publish_exc=RuntimeError("broker went away"))
    tr.announce_update()          # 不抛即通过
    assert any("announce_update failed" in w for w in rec.messages("warning"))


# ---- A12：cmd 帧尺寸上限 ----

def _padded_cmd(size):
    head = b'{"id":"c-big","kind":"shell","pad":"'
    tail = b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def _frame(payload):
    import types
    return types.SimpleNamespace(payload=payload)


def test_oversized_cmd_frame_dropped_before_parse(monkeypatch):
    """QR1120-A12：超过 64KB 的 cmd 帧在 json.loads 前丢弃 + warning。"""
    rec = _RecLog()
    tr, _fake = _make_tr(monkeypatch, log=rec)
    received = []
    tr.on_cmd = received.append
    tr._on_message(None, None, _frame(_padded_cmd(tp.MAX_CMD_BYTES + 1)))
    assert received == [], "超大帧不得派发"
    assert any("oversized" in w for w in rec.messages("warning"))
    assert tp.MAX_CMD_BYTES == 64 * 1024


def test_cmd_frame_at_cap_is_dispatched(monkeypatch):
    """QR1120-A12 边界：恰好 64KB 的合法帧必须照常派发。"""
    tr, _fake = _make_tr(monkeypatch)
    received = []
    tr.on_cmd = received.append
    payload = _padded_cmd(tp.MAX_CMD_BYTES)
    assert len(payload) == tp.MAX_CMD_BYTES
    tr._on_message(None, None, _frame(payload))
    assert len(received) == 1 and received[0]["id"] == "c-big"


def test_oversize_warning_is_throttled(monkeypatch):
    """QR1120-A12：刷屏防护——节流窗口内只 warning 一次，计数持续累计。"""
    clock = _FakeClock(0.0)
    monkeypatch.setattr(tp.time, "monotonic", clock.monotonic)
    rec = _RecLog()
    tr, _fake = _make_tr(monkeypatch, log=rec)
    tr.on_cmd = lambda p: None
    big = _frame(_padded_cmd(tp.MAX_CMD_BYTES + 1))
    tr._on_message(None, None, big)
    tr._on_message(None, None, big)
    assert len(rec.messages("warning")) == 1, "节流窗口内不得重复 warning"
    assert tr._oversize_drops == 2
    clock.t += tp.WARN_THROTTLE_SEC
    tr._on_message(None, None, big)
    assert len(rec.messages("warning")) == 2
    assert "3 dropped" in rec.messages("warning")[1]


# ---- A12：会话被踢 / 异常断开提示 ----

def test_session_takeover_code_gives_targeted_hint(monkeypatch):
    """QR1120-A12：MQTT5 的 142（Session taken over）给定向提示 KK_HOST_NAME。

    paho 2.1.0 的 ReasonCode 对象与裸 int 两种形态都要认得。
    """
    for rc in (142, ReasonCode(PacketTypes.DISCONNECT, "Session taken over")):
        rec = _RecLog()
        tr, fake = _make_tr(monkeypatch, log=rec)
        tr._on_disconnect(fake, None, None, rc)
        hints = [w for w in rec.messages("warning") if "KK_HOST_NAME" in w]
        assert hints, "被踢会话必须提示设置 KK_HOST_NAME"
        assert any("taken over" in w for w in hints)
        assert tr.connected.is_set() is False


def test_v311_kick_gives_generic_hint_not_guess(monkeypatch):
    """QR1120-A12：v311 没有断开原因码——被踢表现为 TCP 关闭，paho 2.1.0 转成
    "Unspecified error"(128)。无法与普通断网区分，只给通用提示，不瞎猜码值。
    """
    for rc in (ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"), 7):
        rec = _RecLog()
        tr, fake = _make_tr(monkeypatch, log=rec)
        tr._on_disconnect(fake, None, None, rc)
        hints = [w for w in rec.messages("warning") if "KK_HOST_NAME" in w]
        assert hints, "非正常断开要给通用排查提示"
        assert not any("taken over" in w for w in hints), "不得把普通断开误报为被踢"
        assert any("paho will retry" in w for w in rec.messages("warning")), \
            "既有重连提示保留"


def test_normal_disconnect_gives_no_hint(monkeypatch):
    """QR1120-A12：rc=0（正常断开）不触发任何 KK_HOST_NAME 提示。"""
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec)
    tr._on_disconnect(fake, None, None, 0)
    assert not any("KK_HOST_NAME" in w for w in rec.messages("warning"))


def test_stopping_suppresses_disconnect_hint(monkeypatch):
    """QR1120-A12：优雅停机路径不打断开提示（与既有 warning 抑制口径一致）。"""
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec)
    tr._stopping.set()
    tr._on_disconnect(fake, None, None, 142)
    assert rec.messages("warning") == []


def test_disconnect_hint_is_throttled(monkeypatch):
    """QR1120-A12：克隆机互踢会反复断开——提示按窗口节流，不能刷屏。"""
    clock = _FakeClock(0.0)
    monkeypatch.setattr(tp.time, "monotonic", clock.monotonic)
    rec = _RecLog()
    tr, fake = _make_tr(monkeypatch, log=rec)
    tr._on_disconnect(fake, None, None, 142)
    tr._on_disconnect(fake, None, None, 142)
    hints = [w for w in rec.messages("warning") if "KK_HOST_NAME" in w]
    assert len(hints) == 1, "节流窗口内只提示一次"
    clock.t += tp.WARN_THROTTLE_SEC
    tr._on_disconnect(fake, None, None, 142)
    hints = [w for w in rec.messages("warning") if "KK_HOST_NAME" in w]
    assert len(hints) == 2