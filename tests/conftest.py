import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "server" / "src"), str(ROOT / "agent" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest


def make_fake_fs(base: Path) -> Path:
    """构造伪造的 /proc + /etc 树，Windows 上也能跑采集逻辑。"""
    proc = base / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "2").mkdir()
    (proc / "meminfo").write_text("MemTotal:       2048000 kB\nMemAvailable:   1536000 kB\n")
    (proc / "stat").write_text(
        "cpu  100 0 100 700 0 0 0 0 0 0\n"
        "cpu0 50 0 50 350 0 0 0 0 0 0\n"
        "intr 12345 0 0 0\n")
    (proc / "loadavg").write_text("0.10 0.20 0.30 1/50 123\n")
    # 字段（去掉 "pid (comm)" 后，0 起）：3=state 11=utime 12=stime 20=vsize 21=rss
    (proc / "1" / "stat").write_text(
        "1 (systemd) S 0 1 1 0 -1 4194560 100 0 0 0 200 100 0 0 0 0 1 0 100 16777216 5000\n")
    (proc / "1" / "status").write_text("Name:\tsystemd\nUid:\t0\t0\t0\t0\nVmRSS:\t500 kB\n")
    (proc / "2" / "stat").write_text(
        "2 (node) S 1 1 1 0 -1 4194560 900 0 0 0 500 300 0 0 0 0 1 0 100 8388608 30000\n")
    (proc / "2" / "status").write_text("Name:\tnode\nUid:\t1000\t1000\t1000\t1000\nVmRSS:\t30000 kB\n")

    etc = base / "etc"
    etc.mkdir()
    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\ndev:x:1000:1000::/home/dev:/bin/bash\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n")
    (base / "root").mkdir()
    (base / "home" / "dev" / ".vscode-server").mkdir(parents=True)
    return base


@pytest.fixture
def fake_fs(tmp_path):
    return make_fake_fs(tmp_path / "fs")
