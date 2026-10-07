"""主机环境探测：把「这台机器是什么」变成 status 帧里的元信息（v4）。

v4 起 Agent 的上报对象从「K8S 下的 vscode-server 容器 IDE」扩为**任意 Linux 主机**
（物理机 / 虚拟机 / 容器），管理面需要 OS、内核、架构、虚拟化类型才能给出一屏
可读的机队视图；分组与标签则来自运维配置（KK_GROUP / KK_LABELS），放 config.py。

探测全部走 psutil / platform / /proc，**零新增依赖**——单文件二进制形态下每个
第三方包都是体积与攻击面。任一项探测失败只丢那一项（`_safe`），绝不影响整帧
status：元信息是「锦上添花」，在线状态才是主链路。
"""
import os
import platform
import socket

import psutil

_DOCKERENV = "/.dockerenv"
_OS_RELEASE = "/etc/os-release"
_CGROUP = "/proc/1/cgroup"
# cgroup 路径里出现这些片段即视为「跑在容器里」：docker 与 K8S（kubepods/containerd）
_CONTAINER_CGROUP_MARKS = ("/docker/", "/docker.slice/", "/kubepods", "/actions_job/",
                           "/lxc/", "/libpod-")


def _safe(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _clip(text, n=78):
    """收敛长度：os/kernel 字符串可能非常长（自编译内核带一串补丁号），
    落库列是定长 VARCHAR，超长会在 MySQL 严格模式下直接报错。"""
    s = str(text or "").strip()
    return s[:n] if s else ""


def _in_container():
    """判定本进程是否跑在容器里。

    三个正向信号任一中招即判容器：/proc/1/cgroup 路径含容器标记、/.dockerenv 存在、
    KUBERNETES_SERVICE_HOST 存在。都是主机用户态可见的事实，不需要额外权限，
    也不依赖某个具体运行时。
    """
    try:
        with open(_CGROUP, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        if any(mark in body for mark in _CONTAINER_CGROUP_MARKS):
            return True
    except OSError:
        pass
    if _safe(lambda: os.path.exists(_DOCKERENV), False):
        return True
    return bool(_safe(lambda: os.environ.get("KUBERNETES_SERVICE_HOST"), ""))


def _container_runtime():
    """容器运行时粗判：只用于给运维一个可读提示，不参与任何逻辑分支。"""
    if not _in_container():
        return ""
    if _safe(lambda: os.path.exists(_DOCKERENV), False):
        return "docker"
    if _safe(lambda: os.environ.get("KUBERNETES_SERVICE_HOST"), ""):
        return "kubernetes"
    return "container"


def _virt():
    """virtualization：container > vm > metal。

    CPU hypervisor flag 判虚拟机是用户态可读的最可靠信号之一（/proc/cpuinfo 无需
    权限）；拿不到就保守判 metal。systemd-detect-virt 要起子进程，对每 60s 一次的
    上线帧是纯浪费——只在 status 帧探测一次，但也不值得为此 fork。
    """
    if _in_container():
        return "container"
    flags = _safe(lambda: _cpuinfo_flags(), set())
    if "hypervisor" in flags:
        return "vm"
    return "metal"


def _cpuinfo_flags():
    out = set()
    with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
            if not out and line.startswith("Features"):
                out = set(line.split(":", 1)[1].split())
    return out


def _pretty_name():
    """发行版可读名：/etc/os-release 的 PRETTY_NAME，退化到 platform.platform()。"""
    try:
        with open(_OS_RELEASE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return _clip(line.split("=", 1)[1].strip().strip('"'), 78)
    except OSError:
        pass
    return _clip(platform.platform(), 78)


def _ips():
    """本机全部 IPv4 地址（去环回与 link-local），用于总览页副行展示。

    只读网卡地址、不发包；多网卡环境这里给的是全集，真正用于服务端白名单校验的
    自报出口 IP 仍是 `transport.detect_outbound_ip`（连 Broker 选路那一个）。
    """
    out = []
    try:
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    if a.address not in out:
                        out.append(a.address)
    except Exception:
        return []
    return out[:8]   # 上限保护：容器里几十块网卡时不要把帧撑爆


def host_env():
    """status 帧的 `env` 字段（v4）。任一项缺失即缺省，调用方按可缺省处理。"""
    return {
        "os": _pretty_name(),
        "kernel": _clip(_safe(platform.release, ""), 78),
        "arch": _clip(_safe(platform.machine, ""), 20),
        "virt": _virt(),
        "in_container": _in_container(),
        "runtime": _container_runtime(),
        "hostname": _clip(_safe(socket.gethostname, ""), 78),
        "ips": _ips(),
    }
