"""插件热加载单测。"""
import os
import time

from kk_agent import plugin_loader as pl

GOOD = "def collect():\n    return {'v': 1}\n"
GOOD2 = "def collect():\n    return {'v': 2}\n"
BAD = "def collect():\n    raise RuntimeError('boom')\n"
NOCOLLECT = "x = 1\n"
HUNG = "import time\ndef collect():\n    time.sleep(30)\n    return {'v': 1}\n"


_mtime_tick = [0]


def _write(path, text):
    path.write_text(text)
    # 保证各版本 mtime 严格递增（Windows 文件时间粒度较粗）
    _mtime_tick[0] += 5
    t = time.time() + _mtime_tick[0]
    os.utime(path, (t, t))


def test_load_collect_and_skip(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    _write(d / "good.py", GOOD)
    _write(d / "bad.py", BAD)
    _write(d / "nocollect.py", NOCOLLECT)
    _write(d / "_skipped.py", "def collect():\n    return {'should': 'not load'}\n")

    out = pl.collect_all(str(d))
    assert out == {"good": {"v": 1}}  # 坏的被跳过、无 collect 的被忽略、下划线不加载


def test_hot_reload_on_mtime_change(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "p.py"
    _write(f, GOOD)
    assert pl.collect_all(str(d)) == {"p": {"v": 1}}
    time.sleep(0.01)
    _write(f, GOOD2)
    assert pl.collect_all(str(d)) == {"p": {"v": 2}}


def test_missing_dir(tmp_path):
    assert pl.collect_all(str(tmp_path / "nope")) == {}


def test_collect_timeout_quarantines_hung_plugin(tmp_path):
    """卡死插件（资源评审 P2）：超时即跳过且隔离，不拖停 collect_all。"""
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "hung.py"
    _write(f, HUNG)

    t0 = time.monotonic()
    out = pl.collect_all(str(d), timeout=0.5)
    assert time.monotonic() - t0 < 5, "卡死插件必须在超时后立刻放行，而不是陪跑 30s"
    assert "hung" not in out

    # 第二轮：已隔离，直接跳过（不再新开执行线程、不再等待）
    t0 = time.monotonic()
    assert pl.collect_all(str(d), timeout=0.5) == {}
    assert time.monotonic() - t0 < 2

    # 作者修改插件（mtime 变化）→ 重载解除隔离，恢复正常出数
    _write(f, GOOD)
    assert pl.collect_all(str(d), timeout=5) == {"hung": {"v": 1}}


def test_collect_exception_not_quarantined(tmp_path):
    """偶发异常只跳过本轮、不隔离：下一轮仍会尝试执行（与卡死不同）。"""
    d = tmp_path / "plugins"
    d.mkdir()
    f = d / "bad.py"
    _write(f, BAD)
    assert pl.collect_all(str(d)) == {}
    _write(f, BAD)  # mtime 变化但内容仍是坏的
    assert pl.collect_all(str(d)) == {}
    # 未被隔离的证明：换成好内容（mtime 再变）无需「解隔离」逻辑即出数
    _write(f, GOOD)
    assert pl.collect_all(str(d)) == {"bad": {"v": 1}}
