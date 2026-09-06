"""采集器单测：一律 mock psutil。

真实系统指标不可控、且 Windows/Linux 差异大，故不给 psutil 打真数据；
FakePsutil 对未预置的调用直接抛错，防止用例偷偷摸到真实内核。
"""
import time

import pytest

from kk_agent import collector as c

MB = 1048576


class _Ns:
    """把关键字参数变成带属性访问的简单命名空间（模拟 psutil 的具名元组）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class FakePsutil:
    """未预置的 psutil 调用在*调用时*抛错（而不是取属性时），以贴近真实模块语义：
    属性总存在、取用无害，采集器的 _safe 才因此能把单项失败兜成 None。"""

    NoSuchProcess = NoSuchProcess
    AccessDenied = AccessDenied

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        def _unexpected(*_a, **_k):
            raise AssertionError("unexpected psutil.%s call in test" % name)
        return _unexpected


@pytest.fixture
def ps(monkeypatch):
    def _apply(**kw):
        fake = FakePsutil(**kw)
        monkeypatch.setattr(c, "psutil", fake)
        return fake
    return _apply


# ---- 单项采集 ----

def test_mem_metrics(ps):
    vm = _Ns(total=2048 * MB, used=1024 * MB, percent=50.0, available=1024 * MB)
    sw = _Ns(total=1024 * MB, used=256 * MB, percent=25.0)
    ps(virtual_memory=lambda: vm, swap_memory=lambda: sw)
    m = c.mem_metrics()
    assert m["mem_total_mb"] == 2048.0
    assert m["mem_mb"] == 1024.0
    assert m["mem_pct"] == 50.0
    assert m["swap_used_mb"] == 256.0


def test_mem_metrics_failure_isolated(ps):
    """单项炸掉只影响该项，绝不拖垮整帧心跳。"""
    def boom():
        raise RuntimeError("kernel denied")
    ps(virtual_memory=boom)
    assert c.mem_metrics() == {}


def test_cpu_metrics(ps):
    def cpu_percent(interval=None, percpu=False):
        return [10.0, 20.0] if percpu else 15.0
    ps(cpu_percent=cpu_percent, cpu_count=lambda: 2, getloadavg=lambda: (0.1, 0.2, 0.3))
    m = c.cpu_metrics()
    assert m["cpu"] == 15.0
    assert m["cpu_cores"] == 2
    assert m["cpu_per_core"] == [10.0, 20.0]
    assert m["load"] == "0.10 0.20 0.30"


def test_disk_metrics_skips_virtual_fstypes(ps):
    parts = [_Ns(mountpoint="/", fstype="ext4"),
             _Ns(mountpoint="/proc", fstype="proc"),
             _Ns(mountpoint="/var/lib", fstype="overlay")]
    usage = {"/": _Ns(total=2048 * MB, used=512 * MB, percent=25.0)}
    ps(disk_partitions=lambda all=False: parts, disk_usage=lambda p: usage[p])
    d = c.disk_metrics()
    assert list(d) == ["/"]
    assert d["/"]["used_mb"] == 512.0


def test_disk_metrics_explicit_paths_and_unusable_mount(ps):
    def usage(p):
        if p == "/missing":
            raise OSError("no such mount")
        return _Ns(total=1024 * MB, used=100 * MB, percent=9.8)
    ps(disk_usage=usage)
    d = c.disk_metrics(["/data", "/missing"])
    assert list(d) == ["/data"]


def test_disk_io_rate_from_diff(ps):
    cur = lambda: _Ns(read_bytes=10 * MB, write_bytes=20 * MB, read_count=100, write_count=200)
    ps(disk_io_counters=cur)
    out, baseline = c.disk_io_metrics({})
    assert out["disk_read_mb"] == 0.0, "首次无基线，速率必须为 0 而非猜测"
    assert baseline[1] == 10 * MB, "回写的基线要带上本次累计读字节"

    prev = time.time() - 10
    out2, _ = c.disk_io_metrics({"disk_io": (prev, 0, 0, 0, 0)})
    assert out2["disk_read_mb"] == pytest.approx(1.0)
    assert out2["disk_write_mb"] == pytest.approx(2.0)
    assert out2["disk_write_iops"] == pytest.approx(20.0)


def test_net_metrics_skips_loopback_and_diffs(ps):
    nics = {"lo": _Ns(bytes_sent=1, bytes_recv=1, packets_sent=1, packets_recv=1),
            "eth0": _Ns(bytes_sent=5 * MB, bytes_recv=10 * MB,
                        packets_sent=1000, packets_recv=2000)}
    ps(net_io_counters=lambda pernic=False: nics)
    out, state = c.net_metrics({})
    assert "lo" not in out and "eth0" in out
    assert out["eth0"]["sent_mb"] == 0.0

    prev = time.time() - 5
    out2, _ = c.net_metrics({"net_io": {"eth0": (prev, 0, 0, 0, 0)}})
    assert out2["eth0"]["sent_mb"] == pytest.approx(1.0)
    assert out2["eth0"]["recv_mb"] == pytest.approx(2.0)
    assert out2["eth0"]["recv_pps"] == pytest.approx(400.0)


class _FakeProc:
    def __init__(self, pid, name, user, cpu, rss, raise_later=False):
        self.pid, self._name, self._user = pid, name, user
        self._cpu, self._rss = cpu, rss
        self._raise = raise_later
        self._opened = False

    def cpu_percent(self, _interval):
        if self._raise and self._opened:
            raise NoSuchProcess()
        return self._cpu

    def oneshot(self):
        self._opened = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def as_dict(self, attrs=None):
        return {"pid": self.pid, "name": self._name, "username": self._user,
                "memory_info": _Ns(rss=self._rss)}


def test_proc_metrics_sorts_by_cpu(ps):
    procs = [_FakeProc(11, "nginx", "root", 10.0, 50 * MB),
             _FakeProc(22, "node", "dev", 80.0, 100 * MB),
             _FakeProc(33, "sleep", "dev", 0.0, 5 * MB)]
    ps(process_iter=lambda attrs=None: iter(procs))
    rows = c.proc_metrics(top_n=3, sample_sec=0)
    assert [r["name"] for r in rows] == ["node", "nginx", "sleep"]
    assert rows[0]["mem_mb"] == 100.0
    assert rows[0]["user"] == "dev"


def test_proc_metrics_drops_vanished_process(ps):
    """进程在两次采样之间退出：跳过它，不能让整帧心跳失败。"""
    procs = [_FakeProc(1, "ok", "root", 5.0, MB),
             _FakeProc(2, "gone", "root", 5.0, MB, raise_later=True)]
    ps(process_iter=lambda attrs=None: iter(procs))
    rows = c.proc_metrics(top_n=5, sample_sec=0)
    assert [r["name"] for r in rows] == ["ok"]


def test_user_metrics(ps):
    ps(users=lambda: [_Ns(name="dev", terminal="pts/0", host="10.0.0.7", started=1700.9)])
    assert c.user_metrics() == [
        {"name": "dev", "terminal": "pts/0", "host": "10.0.0.7", "started": 1700}]


def test_sys_metrics_uses_stdlib_platform(monkeypatch, ps):
    """sys_metrics 的 os/kernel/arch 取自标准库 platform，不是 psutil——别补错地方。"""
    ps(boot_time=lambda: 1000)
    monkeypatch.setattr(c, "platform", _Ns(platform=lambda: "Linux-6.6",
                                           release=lambda: "6.6.0",
                                           machine=lambda: "x86_64"))
    s = c.sys_metrics()
    assert s["os"] == "Linux-6.6" and s["kernel"] == "6.6.0" and s["arch"] == "x86_64"
    now = int(time.time())
    assert now - 1000 - 2 <= s["uptime_sec"] <= now - 1000 + 2


# ---- 按项采集（供 kind=collect 命令，服务端白名单据此对齐）----

def test_item_names_are_the_public_contract():
    """这组名字会进协议与前端勾选项，改名等于破坏兼容。"""
    assert set(c.ITEM_NAMES) == {"cpu", "mem", "disk", "disk_io", "net", "proc", "user", "sys"}


def test_collect_items_partial_and_unknown_ignored(ps):
    vm = _Ns(total=2048 * MB, used=1024 * MB, percent=50.0, available=1024 * MB)
    ps(virtual_memory=lambda: vm, swap_memory=lambda: None)
    data, state = c.collect_items(["mem", "no_such_item"], {}, {})
    assert set(data) >= {"mem_total_mb", "mem_mb"}
    assert state == {}, "未请求差分项时不应回写基线"


def test_collect_items_tolerates_single_item_failure(ps):
    """disk 项失败不影响 mem 项出数。"""
    vm = _Ns(total=2048 * MB, used=1024 * MB, percent=50.0, available=1024 * MB)
    ps(virtual_memory=lambda: vm, swap_memory=lambda: None,
       disk_partitions=lambda all=False: (_ for _ in ()).throw(OSError("denied")))
    data, _ = c.collect_items(["disk", "mem"], {}, {})
    assert data.get("disks") == {}, "挂载枚举失败时应给空字典而非崩溃"
    assert data["mem_mb"] == 1024.0


def test_collect_items_carries_diff_state_back(ps):
    cur = lambda: _Ns(read_bytes=MB, write_bytes=MB, read_count=1, write_count=1)
    ps(disk_io_counters=cur)
    _, state = c.collect_items(["disk_io"], {}, {})
    assert "disk_io" in state
    data, _ = c.collect_items(["disk_io"], state, {})
    assert "disk_read_mb" in data


# ---- 完整心跳 ----

def test_collect_full_frame_shape(ps, monkeypatch):
    """服务端按固定字段读指标（metrics.mem_mb 等），帧形不能漂。"""
    vm = _Ns(total=2048 * MB, used=500 * MB, percent=25.0, available=1548 * MB)

    def cpu_percent(interval=None, percpu=False):
        return [12.5, 12.5, 12.5, 12.5] if percpu else 12.5

    monkeypatch.setattr(c, "platform", _Ns(platform=lambda: "Linux", release=lambda: "6.6",
                                           machine=lambda: "x86_64"))
    ps(virtual_memory=lambda: vm, swap_memory=lambda: None,
       cpu_percent=cpu_percent, cpu_count=lambda: 4,
       getloadavg=lambda: (0.1, 0.2, 0.3),
       disk_partitions=lambda all=False: [_Ns(mountpoint="/", fstype="ext4")],
       disk_usage=lambda p: _Ns(total=2048 * MB, used=812 * MB, percent=39.6),
       disk_io_counters=lambda: _Ns(read_bytes=0, write_bytes=0, read_count=0, write_count=0),
       net_io_counters=lambda pernic=False: {},
       process_iter=lambda attrs=None: iter([]),
       users=lambda: [], boot_time=lambda: int(time.time()) - 60)
    cfg = {"disk_paths": [], "top_n": 5}
    metrics, state = c.collect(cfg, {})
    for key in ("cpu", "mem_mb", "mem_total_mb", "disks", "net", "procs_top",
                "users", "uptime_sec", "ts"):
        assert key in metrics, key
    assert metrics["mem_mb"] == 500.0
    assert metrics["cpu_per_core"] == [12.5] * 4
    assert metrics["disks"]["/"]["pct"] == 39.6
    assert set(state) == {"disk_io", "net_io", "ts"}

    # 第二轮带基线再采一次：差分路径不报错、ts 前进
    metrics2, _ = c.collect(cfg, state)
    assert metrics2["ts"] >= metrics["ts"]


def test_collect_hb_items_subset(ps, monkeypatch):
    """KK_HB_ITEMS 精简心跳项（资源评审 P3）：只采指定项，proc 遍历不再发生。"""
    vm = _Ns(total=2048 * MB, used=500 * MB, percent=25.0, available=1548 * MB)

    def _boom(attrs=None):
        raise AssertionError("proc 项未请求，不应发生全进程遍历")

    monkeypatch.setattr(c, "platform", _Ns(platform=lambda: "Linux", release=lambda: "6.6",
                                           machine=lambda: "x86_64"))
    ps(virtual_memory=lambda: vm, swap_memory=lambda: None,
       cpu_percent=lambda interval=None, percpu=False: ([5.0] if percpu else 5.0),
       process_iter=_boom,  # 若 proc 项仍被采集，这里立刻失败
       boot_time=lambda: int(time.time()) - 60)
    metrics, _ = c.collect({"hb_items": ["cpu", "mem", "sys"]}, {})
    assert "cpu" in metrics and metrics["mem_mb"] == 500.0
    assert "procs_top" not in metrics and "disks" not in metrics and "net" not in metrics
    assert "ts" in metrics


def test_collect_hb_items_empty_means_all(ps, monkeypatch):
    """hb_items 为空/缺省 = 全采 8 项（帧形与旧版完全一致）。"""
    vm = _Ns(total=2048 * MB, used=500 * MB, percent=25.0, available=1548 * MB)
    monkeypatch.setattr(c, "platform", _Ns(platform=lambda: "Linux", release=lambda: "6.6",
                                           machine=lambda: "x86_64"))
    ps(virtual_memory=lambda: vm, swap_memory=lambda: None,
       cpu_percent=lambda interval=None, percpu=False: ([5.0] if percpu else 5.0),
       getloadavg=lambda: (0.1, 0.2, 0.3),
       disk_partitions=lambda all=False: [_Ns(mountpoint="/", fstype="ext4")],
       disk_usage=lambda p: _Ns(total=2048 * MB, used=812 * MB, percent=39.6),
       disk_io_counters=lambda: _Ns(read_bytes=0, write_bytes=0, read_count=0, write_count=0),
       net_io_counters=lambda pernic=False: {},
       process_iter=lambda attrs=None: iter([]),
       users=lambda: [], boot_time=lambda: int(time.time()) - 60)
    for cfg in ({}, {"hb_items": []}):
        metrics, state = c.collect(dict(cfg, disk_paths=[], top_n=5), {})
        for key in ("cpu", "mem_mb", "disks", "net", "procs_top", "users", "ts"):
            assert key in metrics, (cfg, key)
        assert set(state) == {"disk_io", "net_io", "ts"}
