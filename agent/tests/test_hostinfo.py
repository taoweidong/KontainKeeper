"""主机环境探测（hostinfo，QR29-A1）的回归锁。

v4 status 帧的 env/group/caps 元信息全部出自这个模块，而它是「协议字段来自
未测代码」的典型面：/proc、cgroup、os-release 的解析全是 best-effort 字符串活。
这里用 tmp 文件替身钉住三个容器信号与 os-release 解析，psutil/platform 打桩——
全部用例与真实主机环境无关，Windows 开发机与 CI 上结果一致。
"""
import socket
import stat
import types

import pytest

from kk_agent import hostinfo


@pytest.fixture
def probes(tmp_path, monkeypatch):
    """把三个探测路径指到 tmp 文件：返回一个简单的写入器。

    cgroup / os-release 是「读文件内容」型信号，dockerenv 是「存在性」信号，
    分别用写文件 / 建文件 / 删文件控制。
    """
    cgroup = tmp_path / "cgroup"
    os_release = tmp_path / "os-release"
    dockerenv = tmp_path / "dockerenv"
    monkeypatch.setattr(hostinfo, "_CGROUP", str(cgroup))
    monkeypatch.setattr(hostinfo, "_OS_RELEASE", str(os_release))
    monkeypatch.setattr(hostinfo, "_DOCKERENV", str(dockerenv))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    class W:
        @staticmethod
        def cgroup(text):
            cgroup.write_text(text, encoding="utf-8")

        @staticmethod
        def os_release(text):
            os_release.write_text(text, encoding="utf-8")

        @staticmethod
        def dockerenv():
            dockerenv.touch()

    return W()


# ---- _clip / _safe：两个最底层的兜底 ----

def test_clip_truncates_and_strips():
    assert hostinfo._clip("  ubuntu 22.04  ", 80) == "ubuntu 22.04"
    assert hostinfo._clip("x" * 200, 78) == "x" * 78
    assert hostinfo._clip(None) == ""
    assert hostinfo._clip("") == ""


def test_safe_swallows_any_exception():
    def boom():
        raise RuntimeError("no /proc on this platform")

    assert hostinfo._safe(boom, "fallback") == "fallback"
    # 返回 None 与抛异常同型：都回落 default（调用方因此不用再判 None）
    assert hostinfo._safe(lambda: None, "d") == "d"


# ---- _in_container：三个正向信号任一中招即判容器 ----

def test_in_container_cgroup_mark(probes):
    probes.cgroup("12:pids:/docker/abc123\n")
    assert hostinfo._in_container() is True


@pytest.mark.parametrize("mark", [
    "/docker.slice/docker-abc.scope",
    "/kubepods/burstable/pod123",
    "/lxc/abc",
    "/libpod-abc",
])
def test_in_container_cgroup_marks_table(probes, mark):
    """标记清单里的每一条都要命中：新运行时加标记时这里会红，提醒同步测试。"""
    probes.cgroup("12:pids:" + mark + "\n")
    assert hostinfo._in_container() is True


def test_in_container_dockerenv(probes):
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    probes.dockerenv()
    assert hostinfo._in_container() is True


def test_in_container_kubernetes_env(probes):
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    import os
    monkey = pytest.MonkeyPatch()
    monkey.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    try:
        assert hostinfo._in_container() is True
    finally:
        monkey.undo()


def test_in_container_negative(probes):
    """物理机形态：cgroup 无标记、无 dockerenv、无 K8S 环境变量。"""
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    assert hostinfo._in_container() is False


def test_in_container_missing_files(probes):
    """/proc/cgroup 读不到（Windows 开发机、受限容器）按非容器处理，不抛异常。"""
    assert hostinfo._in_container() is False


# ---- _container_runtime：只给运维可读提示，不参与逻辑分支 ----

def test_runtime_prefers_dockerenv(probes):
    probes.cgroup("12:pids:/kubepods/pod1\n")
    probes.dockerenv()
    assert hostinfo._container_runtime() == "docker"


def test_runtime_kubernetes(probes):
    probes.cgroup("12:pids:/kubepods/pod1\n")
    monkey = pytest.MonkeyPatch()
    monkey.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    try:
        assert hostinfo._container_runtime() == "kubernetes"
    finally:
        monkey.undo()


def test_runtime_generic_and_empty(probes):
    probes.cgroup("12:pids:/lxc/abc\n")
    assert hostinfo._container_runtime() == "container"
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    assert hostinfo._container_runtime() == ""


# ---- _virt：container > vm > metal ----

def test_virt_container_wins(probes, monkeypatch):
    probes.cgroup("12:pids:/docker/x\n")
    # 即便 cpuinfo 报了 hypervisor，容器判定优先级更高
    monkeypatch.setattr(hostinfo, "_cpuinfo_flags", lambda: {"hypervisor"})
    assert hostinfo._virt() == "container"


def test_virt_vm_by_hypervisor_flag(probes, monkeypatch):
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    monkeypatch.setattr(hostinfo, "_cpuinfo_flags", lambda: {"fpu", "hypervisor"})
    assert hostinfo._virt() == "vm"


def test_virt_metal_fallback(probes, monkeypatch):
    probes.cgroup("12:pids:/system.slice/sshd.service\n")
    monkeypatch.setattr(hostinfo, "_cpuinfo_flags", lambda: set())
    assert hostinfo._virt() == "metal"


# ---- _pretty_name：os-release 解析与退化路径 ----

def test_pretty_name_from_os_release(probes):
    probes.os_release('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\n')
    assert hostinfo._pretty_name() == "Ubuntu 22.04.3 LTS"


def test_pretty_name_missing_file_falls_back(probes, monkeypatch):
    monkeypatch.setattr(hostinfo.platform, "platform", lambda: "test-platform")
    assert hostinfo._pretty_name() == "test-platform"


def test_pretty_name_clipped(probes):
    probes.os_release('PRETTY_NAME="%s"\n' % ("L" * 300))
    assert len(hostinfo._pretty_name()) == 78


# ---- _ips：去环回、去重、8 上限 ----

class _Addr(types.SimpleNamespace):
    pass


def _addr(family, address):
    return _Addr(family=family, address=address)


def test_ips_filters_loopback_and_dedups(monkeypatch):
    monkeypatch.setattr(hostinfo.psutil, "net_if_addrs", lambda: {
        "eth0": [_addr(socket.AF_INET, "10.0.0.5"),
                 _addr(socket.AF_INET, "10.0.0.5"),   # 同址重复（别名网卡）
                 _addr(socket.AF_INET6, "fe80::1")],  # 非 IPv4 忽略
        "lo": [_addr(socket.AF_INET, "127.0.0.1")],   # 环回忽略
        "eth1": [_addr(socket.AF_INET, "10.0.0.6")],
    })
    assert hostinfo._ips() == ["10.0.0.5", "10.0.0.6"]


def test_ips_caps_at_eight(monkeypatch):
    addrs = [_addr(socket.AF_INET, "10.0.0.%d" % i) for i in range(20)]
    monkeypatch.setattr(hostinfo.psutil, "net_if_addrs", lambda: {
        "bond0": addrs})
    assert len(hostinfo._ips()) == 8


def test_ips_swallows_psutil_errors(monkeypatch):
    def boom():
        raise RuntimeError("no /proc/net")

    monkeypatch.setattr(hostinfo.psutil, "net_if_addrs", boom)
    assert hostinfo._ips() == []


# ---- host_env：聚合形状（协议契约面） ----

def test_host_env_shape(probes, monkeypatch):
    """status 帧 env 字段的键集与取值类型是协议契约（proto/messages.md §3.1）：
    多键少键都会让服务端/前端的兼容处理落空。"""
    probes.cgroup("12:pids:/docker/x\n")
    probes.dockerenv()
    probes.os_release('PRETTY_NAME="Debian GNU/Linux 12"\n')
    monkeypatch.setattr(hostinfo.platform, "release", lambda: "6.1.0-0-amd64")
    monkeypatch.setattr(hostinfo.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hostinfo.socket, "gethostname", lambda: "web-01")
    monkeypatch.setattr(hostinfo.psutil, "net_if_addrs",
                        lambda: {"eth0": [_addr(socket.AF_INET, "10.0.0.5")]})

    env = hostinfo.host_env()
    assert set(env) == {"os", "kernel", "arch", "virt", "in_container",
                        "runtime", "hostname", "ips"}
    assert env["os"] == "Debian GNU/Linux 12"
    assert env["kernel"] == "6.1.0-0-amd64"
    assert env["arch"] == "x86_64"
    assert env["virt"] == "container"
    assert env["in_container"] is True
    assert env["runtime"] == "docker"
    assert env["hostname"] == "web-01"
    assert env["ips"] == ["10.0.0.5"]
