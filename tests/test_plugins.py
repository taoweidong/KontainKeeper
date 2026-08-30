"""插件热加载单测。"""
import os
import time

from kk_agent import plugin_loader as pl

GOOD = "def collect():\n    return {'v': 1}\n"
GOOD2 = "def collect():\n    return {'v': 2}\n"
BAD = "def collect():\n    raise RuntimeError('boom')\n"
NOCOLLECT = "x = 1\n"


_mtime_tick = [0]


def _write(path, text):
    path.write_text(text)
    # 保证各版本 mtime 严格递增（Windows 文件时间粒度较粗）
    _mtime_tick[0] += 5
    t = time.time() + _mtime_tick[0]
    os.utime(path, (t, t))


def test_load_collect_and_skip(fake_fs, tmp_path):
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
