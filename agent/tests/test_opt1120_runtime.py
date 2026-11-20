"""QR1120-A1..A5 运行时缺陷回归。

A1：插件隔离检查必须与 transport.publish_hb 完全同口径
    （ensure_ascii=False + encode utf-8），孤立代理项与深层嵌套不得漏网。
A2：序列化/尺寸检查必须在带超时的 worker 内执行，检查本身卡死也要被隔离。
A3：单插件输出序列化后封顶 64KB，超限按坏输出同款隔离。
A4：executor timeout=Infinity/NaN/1e999 回落 30s 且正常执行有回执。
A5：collect_items 单项失败留痕——首败 warning、续败 debug、成功清零。

不依赖真实 Broker/网络/执行顺序；插件文件用 os.utime 递增 mtime
（Windows 文件时间粒度粗，与 test_plugins.py 同款手法）。
"""
import json
import os
import queue
import sys
import threading
import time

import pytest
from loguru import logger as _loguru

from kk_agent import collector as c
from kk_agent import executor as ex
from kk_agent import plugin_loader as pl

GOOD = "def collect():\n    return {'v': 1}\n"

_mtime_tick = [0]


def _write(path, text):
    # 显式 UTF-8：Python 源码按 UTF-8 读，Windows 默认 locale 编码会写坏非 ASCII 插件
    path.write_text(text, encoding="utf-8")
    # 保证各版本 mtime 严格递增（Windows 文件时间粒度较粗）
    _mtime_tick[0] += 5
    t = time.time() + _mtime_tick[0]
    os.utime(path, (t, t))


def _publish_encode(data):
    """transport.publish_hb 的真实发布口径（QR1120-A1 的对照基准）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---- A1：隔离检查漏网变体 ----

def test_surrogate_output_quarantined_and_frame_still_publishable(tmp_path):
    """QR1120-A1：孤立代理项（surrogateescape 解码产物）必须被隔离。

    旧检查用默认 ensure_ascii=True，'\\ud800' 被转义放行；发布路径
    ensure_ascii=False 后 encode('utf-8') 抛 UnicodeEncodeError，整帧心跳
    （含全部采集项）每拍都丢。隔离后剩余输出必须仍能按发布口径编码。
    """
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "surr1120.py"
    # 插件源码里的 '\ud800' 转义在插件解释时成为真正的孤立代理项
    _write(f, "def collect():\n    return {'s': '\\ud800'}\n")
    _write(d / "good1120.py", GOOD)

    out = pl.collect_all(str(d))
    assert out == {"good1120": {"v": 1}}, "坏输出被丢、好插件照常上报"
    _publish_encode(out)   # 剩余输出按真实发布口径可编码（整帧不再被拖死）
    assert pl._loaded["surr1120"][2] is True, "必须进入隔离"

    # 第二轮：隔离生效，同 mtime 不再执行
    assert pl.collect_all(str(d)) == {"good1120": {"v": 1}}

    # 作者修改插件（mtime 变化）→ 重载解除隔离
    _write(f, GOOD)
    assert pl.collect_all(str(d)) == {"surr1120": {"v": 1}, "good1120": {"v": 1}}


def test_deep_nested_output_quarantined(tmp_path):
    """QR1120-A1：深层嵌套触发 RecursionError，旧 catch 元组够不着，必须隔离。"""
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "deep1120.py"
    # 深度取 10000：3.12 的 C 编码器实测 ~4000 层才触发 RecursionError，
    # 2000 层会放行（不同小版本阈值有漂移，取远超阈值的深度保证稳定触发）
    _write(f, "def collect():\n"
              "    root = cur = {}\n"
              "    for _ in range(10000):\n"
              "        cur['n'] = {}\n"
              "        cur = cur['n']\n"
              "    return root\n")
    _write(d / "gooddeep1120.py", GOOD)

    # 用成员断言而非整 dict 相等：若深嵌套对象漏进 out，pytest 的 pprint
    # 展示会再撞 RecursionError，掩盖真正的失败信息
    out = pl.collect_all(str(d))
    assert "deep1120" not in out
    assert out.get("gooddeep1120") == {"v": 1}
    assert pl._loaded["deep1120"][2] is True
    out2 = pl.collect_all(str(d))
    assert "deep1120" not in out2 and out2.get("gooddeep1120") == {"v": 1}

    _write(f, GOOD)
    assert pl.collect_all(str(d)) == {"deep1120": {"v": 1}, "gooddeep1120": {"v": 1}}


def test_normal_plugin_unaffected_by_strict_check(tmp_path):
    """QR1120-A1：口径收紧后正常插件（含非 ASCII 文本）不受影响。"""
    d = tmp_path / "plugins"
    d.mkdir()
    _write(d / "ok1120.py", "def collect():\n    return {'文本': '主机', 'n': 3}\n")
    out = pl.collect_all(str(d))
    assert out == {"ok1120": {"文本": "主机", "n": 3}}
    _publish_encode(out)
    assert pl._loaded["ok1120"][2] is False


# ---- A2：序列化检查必须在超时保护内 ----

def test_serialization_hang_is_quarantined_within_timeout(tmp_path, monkeypatch):
    """QR1120-A2：序列化检查本身卡死（超大对象图）也要被超时隔离。

    旧实现的 dumps 检查在 _collect_with_timeout 之外：检查任意耗时会让
    busy 期间后续心跳全跳过。用慢 dumps 模拟「检查卡死」，collect_all
    必须在超时窗口后放行并隔离该插件。
    """
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "slowser1120.py"
    _write(f, GOOD)

    gate = threading.Event()
    real_dumps = json.dumps

    def slow_dumps(*a, **k):
        gate.wait(3)   # 模拟序列化阶段卡死；用例收尾会 set 放行 daemon 线程
        return real_dumps(*a, **k)

    monkeypatch.setattr(pl.json, "dumps", slow_dumps)
    try:
        t0 = time.monotonic()
        out = pl.collect_all(str(d), timeout=0.3)
        assert time.monotonic() - t0 < 2.5, "序列化卡死必须在超时后放行，不能陪跑"
        assert out == {}
        assert pl._loaded["slowser1120"][2] is True

        # 第二轮：已隔离，直接跳过（不再进入慢序列化）
        t0 = time.monotonic()
        assert pl.collect_all(str(d), timeout=0.3) == {}
        assert time.monotonic() - t0 < 1
    finally:
        gate.set()
        monkeypatch.undo()

    # 作者修改插件（mtime 变化）→ 重载解除隔离，恢复正常出数
    _write(f, "def collect():\n    return {'v': 2}\n")
    assert pl.collect_all(str(d), timeout=5) == {"slowser1120": {"v": 2}}


# ---- A3：单插件输出体积封顶 ----

def test_oversize_output_quarantined(tmp_path):
    """QR1120-A3：序列化后超过 64KB 的输出按坏输出同款隔离，好插件不受影响。"""
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "big1120.py"
    over = pl.MAX_OUTPUT_BYTES + 1024
    _write(f, "def collect():\n    return {'blob': 'x' * %d}\n" % over)
    _write(d / "goodbig1120.py", GOOD)

    out = pl.collect_all(str(d))
    assert out == {"goodbig1120": {"v": 1}}
    assert pl._loaded["big1120"][2] is True
    assert pl.collect_all(str(d)) == {"goodbig1120": {"v": 1}}

    _write(f, GOOD)
    assert pl.collect_all(str(d)) == {"big1120": {"v": 1}, "goodbig1120": {"v": 1}}


def test_output_at_cap_passes(tmp_path):
    """QR1120-A3：恰好等于上限的输出放行（边界是 > 而非 >=）。"""
    d = tmp_path / "plugins"
    d.mkdir()
    pad = pl.MAX_OUTPUT_BYTES - len(json.dumps({"b": ""}).encode("utf-8"))
    _write(d / "edge1120.py", "def collect():\n    return {'b': 'x' * %d}\n" % pad)
    out = pl.collect_all(str(d))
    assert "edge1120" in out
    assert len(json.dumps(out["edge1120"], ensure_ascii=False).encode("utf-8")) \
        == pl.MAX_OUTPUT_BYTES
    assert pl._loaded["edge1120"][2] is False


# ---- A4：executor timeout 非有限数值 ----

@pytest.mark.parametrize("raw", ["Infinity", "NaN", "1e999"])
def test_runner_timeout_non_finite_falls_back_and_receipts(raw):
    """QR1120-A4：json.loads 默认接受 Infinity/NaN/1e999(→inf)。

    int(float('inf')) 抛 OverflowError，旧捕获元组 (TypeError, ValueError)
    漏了它：异常穿透 dispatch 被兜底吞掉且不发任何回执，服务端挂到超时
    清扫。修复后与畸形值同口径回落 30s，正常执行且必须有回执。
    """
    timeout_val = json.loads(raw)   # 与真实帧解析同路径
    got = queue.Queue()
    runner = ex.Runner(lambda cid, res: got.put(res))
    runner.submit({"id": "c-nf-" + raw, "argv": [sys.executable, "-c", "print('ok')"],
                   "timeout": timeout_val})
    res = got.get(timeout=20)
    assert res["rc"] == 0 and b"ok" in res["out"] and res["timed_out"] is False


# ---- A5：采集项失败留痕 ----

def _capture(name="kk-agent", level="TRACE"):
    """临时 sink 捕获日志记录（与 test_logutil.py 同款手法）。"""
    records = []
    hid = _loguru.add(lambda m: records.append(m.record),
                      level=level,
                      filter=lambda r: r["extra"].get("name") == name)
    return records, (lambda: _loguru.remove(hid))


@pytest.fixture
def item_fail_state():
    """A5 的失败计数与懒 logger 是模块级状态：用例前后各清一次。"""
    c._item_failures.clear()
    c._item_log = None
    yield
    c._item_failures.clear()
    c._item_log = None


def _boom(_state, _cfg):
    raise RuntimeError("item boom")


def _ok(_state, _cfg):
    return {"fine": 1}, None


def test_item_failure_first_warning_then_debug(item_fail_state, monkeypatch):
    """QR1120-A5：首次失败 warning 留痕，同项持续失败降 debug 防每拍刷屏。"""
    monkeypatch.setitem(c.ITEMS, "mem", _boom)
    records, done = _capture()
    try:
        for _ in range(3):
            data, _ = c.collect_items(["mem"], {}, {})
            assert data == {}, "坏项仍只跳过自己，不拖垮整帧"
    finally:
        done()
    warns = [r for r in records if r["level"].name == "WARNING"]
    debugs = [r for r in records if r["level"].name == "DEBUG"]
    assert len(warns) == 1, "首次失败必须 warning 留痕"
    assert "item boom" in warns[0]["message"]
    assert warns[0]["extra"].get("item") == "mem"
    assert len(debugs) == 2, "后续持续失败降 debug，不每拍刷屏"
    assert c._item_failures.get("mem") == 3


def test_item_success_clears_failure_streak(item_fail_state, monkeypatch):
    """QR1120-A5：成功后计数清零——再次失败重新按首败 warning 留痕。"""
    monkeypatch.setitem(c.ITEMS, "mem", _boom)
    c.collect_items(["mem"], {}, {})
    assert c._item_failures.get("mem") == 1

    monkeypatch.setitem(c.ITEMS, "mem", _ok)
    records, done = _capture()
    try:
        data, _ = c.collect_items(["mem"], {}, {})
        assert data == {"fine": 1}
    finally:
        done()
    assert "mem" not in c._item_failures, "成功必须清零连续失败计数"
    assert not [r for r in records if r["level"].name == "WARNING"]

    # 再坏：按首败重新 warning（而不是续败 debug）
    monkeypatch.setitem(c.ITEMS, "mem", _boom)
    records, done = _capture()
    try:
        c.collect_items(["mem"], {}, {})
    finally:
        done()
    assert len([r for r in records if r["level"].name == "WARNING"]) == 1


def test_item_failure_does_not_affect_other_items(item_fail_state, monkeypatch):
    """QR1120-A5：留痕不改变容错语义——坏项跳过、好项照常出数。"""
    monkeypatch.setitem(c.ITEMS, "mem", _boom)
    monkeypatch.setitem(c.ITEMS, "cpu", _ok)
    data, _ = c.collect_items(["mem", "cpu"], {}, {})
    assert data == {"fine": 1}
    assert c._item_failures.get("mem") == 1
    assert "cpu" not in c._item_failures