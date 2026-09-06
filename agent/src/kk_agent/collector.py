"""指标采集：基于 psutil，替代原先手工解析 /proc 的 233 行实现。

设计要点：
- 每个采集项是独立函数，任一失败返回 None，绝不影响整体心跳（沿用原设计的容错原则）
- 速率型指标（磁盘 IO、网络流量）依赖上一次采样的累计值做差分，状态由 collect() 的
  state 参数携带，调用方负责存回；首次采样速率为 0
- 进程 CPU 需要短间隔双采样（psutil 在 Process 对象上维护基准），采样在工作线程内完成，
  不阻塞主事件循环
"""
import os
import platform
import time

import psutil

MB = 1048576.0
TOP_N = 5
DEFAULT_SAMPLE_SEC = 0.3

# 只采集这些真实文件系统，跳过容器里的 overlay/procfs 之类噪音
_SKIP_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2",
    "devpts", "mqueue", "nsfs", "binfmt_misc", "overlay", "aufs", "ramfs",
}


def _safe(fn, default=None):
    """采集项级容错：单点异常不能拖垮整帧心跳。"""
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _mb(nbytes):
    return None if nbytes is None else round(nbytes / MB, 1)


# ---- 各采集项 ----

def cpu_metrics():
    pct = _safe(lambda: psutil.cpu_percent(interval=None), 0.0)
    per_core = _safe(lambda: psutil.cpu_percent(interval=None, percpu=True), [])
    load = _safe(psutil.getloadavg)
    return {
        "cpu": round(pct, 1) if pct is not None else None,
        "cpu_cores": _safe(psutil.cpu_count) or 0,
        "cpu_per_core": [round(x, 1) for x in per_core] if per_core else [],
        "load": " ".join("%.2f" % x for x in load) if load else None,
    }


def mem_metrics():
    vm = _safe(psutil.virtual_memory)
    sw = _safe(psutil.swap_memory)
    if vm is None:
        return {}
    out = {
        "mem_total_mb": _mb(vm.total),
        "mem_mb": _mb(vm.used),
        "mem_pct": round(vm.percent, 1),
        "mem_avail_mb": _mb(vm.available),
    }
    if sw is not None:
        out.update({
            "swap_total_mb": _mb(sw.total),
            "swap_used_mb": _mb(sw.used),
            "swap_pct": round(sw.percent, 1),
        })
    return out


def disk_metrics(paths=None):
    """挂载点使用情况。paths 为空则自动发现（跳过虚拟文件系统）。"""
    out = {}
    if paths:
        targets = [(p, None) for p in paths]
    else:
        parts = _safe(lambda: psutil.disk_partitions(all=False), []) or []
        targets = [(p.mountpoint, p.fstype) for p in parts
                   if p.fstype.lower() not in _SKIP_FSTYPES]
    for mount, fstype in targets:
        try:
            u = psutil.disk_usage(mount)
        except (OSError, ValueError):
            continue
        if u.total <= 0:
            continue
        out[mount] = {
            "total_mb": _mb(u.total),
            "used_mb": _mb(u.used),
            "pct": round(u.percent, 1),
        }
    return out


def disk_io_metrics(state):
    """磁盘 IO 速率（MB/s、IOPS）。依赖上次累计值差分。"""
    cur = _safe(psutil.disk_io_counters)
    if cur is None:
        return {}
    now = time.time()
    prev = state.get("disk_io")
    state_out = (now, cur.read_bytes, cur.write_bytes, cur.read_count, cur.write_count)
    out = {"disk_read_mb": 0.0, "disk_write_mb": 0.0, "disk_read_iops": 0.0, "disk_write_iops": 0.0}
    if prev:
        dt = now - prev[0]
        if dt > 0:
            out["disk_read_mb"] = round((cur.read_bytes - prev[1]) / MB / dt, 2)
            out["disk_write_mb"] = round((cur.write_bytes - prev[2]) / MB / dt, 2)
            out["disk_read_iops"] = round((cur.read_count - prev[3]) / dt, 1)
            out["disk_write_iops"] = round((cur.write_count - prev[4]) / dt, 1)
    return out, state_out


def net_metrics(state):
    """每网卡收发速率（MB/s、pps）。依赖上次累计值差分，自动跳过回环。"""
    cur = _safe(lambda: psutil.net_io_counters(pernic=True), {}) or {}
    now = time.time()
    prev = state.get("net_io") or {}
    out, new_state = {}, {}
    for nic, c in cur.items():
        if nic == "lo":
            continue
        new_state[nic] = (now, c.bytes_sent, c.bytes_recv, c.packets_sent, c.packets_recv)
        row = {"sent_mb": 0.0, "recv_mb": 0.0, "sent_pps": 0.0, "recv_pps": 0.0}
        p = prev.get(nic)
        if p:
            dt = now - p[0]
            if dt > 0:
                row["sent_mb"] = round(max(0, c.bytes_sent - p[1]) / MB / dt, 3)
                row["recv_mb"] = round(max(0, c.bytes_recv - p[2]) / MB / dt, 3)
                row["sent_pps"] = round(max(0, c.packets_sent - p[3]) / dt, 1)
                row["recv_pps"] = round(max(0, c.packets_recv - p[4]) / dt, 1)
        out[nic] = row
    return out, new_state


def proc_metrics(top_n=TOP_N, sample_sec=DEFAULT_SAMPLE_SEC):
    """Top N 进程（按 CPU 降序，其次内存）。

    psutil 的进程 cpu_percent 基准挂在 Process 对象上，需两次采样才有值；
    采样间隔默认 0.3s，在 60s 心跳周期里开销可忽略。
    """
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name", "username"]):
        try:
            p.cpu_percent(None)  # 初始化基准
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if sample_sec > 0:
        time.sleep(sample_sec)

    rows = []
    for p in procs:
        try:
            with p.oneshot():
                info = p.as_dict(attrs=["pid", "name", "username", "memory_info"])
                cpu = p.cpu_percent(None) or 0.0
            rss = info.get("memory_info")
            rows.append({
                "pid": info["pid"],
                "name": (info.get("name") or "?")[:32],
                "user": (info.get("username") or "?")[:32],
                "cpu": round(cpu, 1),
                "mem_mb": _mb(rss.rss) if rss else 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: (-r["cpu"], -r["mem_mb"]))
    return rows[:top_n]


def user_metrics():
    """当前登录会话（Linux 服务器场景下的「谁在用这台机器」）。"""
    rows = []
    for u in _safe(psutil.users, []) or []:
        rows.append({
            "name": u.name,
            "terminal": u.terminal or "",
            "host": u.host or "",
            "started": int(u.started) if u.started else 0,
        })
    return rows


def sys_metrics():
    boot = _safe(psutil.boot_time)
    return {
        "os": _safe(platform.platform, ""),
        "kernel": _safe(platform.release, ""),
        "arch": _safe(platform.machine, ""),
        "uptime_sec": int(time.time() - boot) if boot else None,
        "boot_ts": int(boot) if boot else None,
    }


# ---- 按需采集（供 kind=collect 命令使用，不经 shell）----
# 每项返回 (指标字典, 需要回写的差分状态片段或 None)

def _item_cpu(_state, _cfg):
    return cpu_metrics(), None


def _item_mem(_state, _cfg):
    return mem_metrics(), None


def _item_disk(_state, cfg):
    return {"disks": disk_metrics(cfg.get("disk_paths") or None)}, None


def _item_disk_io(state, _cfg):
    out, st = disk_io_metrics(state)
    return out, {"disk_io": st}


def _item_net(state, _cfg):
    out, st = net_metrics(state)
    return {"net": out}, {"net_io": st}


def _item_proc(_state, cfg):
    return {"procs_top": proc_metrics(top_n=int(cfg.get("top_n") or TOP_N))}, None


def _item_user(_state, _cfg):
    return {"users": user_metrics()}, None


def _item_sys(_state, _cfg):
    return sys_metrics(), None


ITEMS = {
    "cpu": _item_cpu,
    "mem": _item_mem,
    "disk": _item_disk,
    "disk_io": _item_disk_io,
    "net": _item_net,
    "proc": _item_proc,
    "user": _item_user,
    "sys": _item_sys,
}

ITEM_NAMES = sorted(ITEMS)


def collect_items(items, state, cfg=None):
    """按名称采集指定项，返回 (指标字典, 新状态)。未知项忽略。"""
    cfg = cfg or {}
    out, next_state = {}, dict(state or {})
    for name in items or []:
        fn = ITEMS.get(name)
        if fn is None:
            continue
        try:
            part, st = fn(next_state, cfg)
        except Exception:
            continue
        out.update(part or {})
        if st:
            next_state.update(st)
    return out, next_state


# ---- 完整心跳采集 ----

# 心跳默认全采 8 项；KK_HB_ITEMS 可精简（如千进程主机去掉 proc 项——
# 全进程遍历 + 双采样是心跳里最贵的一段，资源评审 P3）
HB_DEFAULT_ITEMS = ["cpu", "mem", "disk", "disk_io", "net", "proc", "user", "sys"]


def collect(cfg, state):
    """一次完整采集。state 携带上次采样用于差分，返回 (metrics, new_state)。

    cfg["hb_items"]（KK_HB_ITEMS）缺省或为空时全采；项名白名单与
    kind=collect 命令一致（ITEM_NAMES），未知项静默忽略。
    """
    cfg = cfg or {}
    items = cfg.get("hb_items") or HB_DEFAULT_ITEMS
    metrics, new_state = collect_items(items, state or {}, cfg)
    metrics["ts"] = int(time.time())
    new_state["ts"] = metrics["ts"]
    return metrics, new_state
