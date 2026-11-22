"""指标采集：基于 psutil，替代原先手工解析 /proc 的 233 行实现。

设计要点：
- 每个采集项是独立函数，任一失败返回 None，绝不影响整体心跳（沿用原设计的容错原则）
- 速率型指标（磁盘 IO、网络流量）依赖上一次采样的累计值做差分，状态由 collect() 的
  state 参数携带，调用方负责存回；首次采样速率为 0
- 进程 CPU 需要短间隔双采样（psutil 在 Process 对象上维护基准），采样在工作线程内完成，
  不阻塞主事件循环
"""
import platform
import threading
import time

import psutil

from ._util import safe as _safe   # 单点容错收口在 _util（QR1128-A6）
from .logutil import get_logger

MB = 1048576.0
TOP_N = 5
DEFAULT_SAMPLE_SEC = 0.3

# 只采集这些真实文件系统，跳过容器里的 overlay/procfs 之类噪音
_SKIP_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2",
    "devpts", "mqueue", "nsfs", "binfmt_misc", "overlay", "aufs", "ramfs",
}


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


def _deltas(prev, now, cur):
    """累计计数器五元组 (t, v0..v3) 的差分：dt<=0 回 None；计数器重置/回绕
    （磁盘重插、容器迁移）算出的负速率钳 0（QR-A13）。

    disk_io 与 net 两侧共用同一公式（QR29-A7）——负速率钳制历史上就是
    两边各修一次，同类修复不能再漏一边。
    """
    if not prev:
        return None
    dt = now - prev[0]
    if dt <= 0:
        return None
    return tuple(max(0, c - p) / dt for c, p in zip(cur[1:], prev[1:]))


def disk_io_metrics(state):
    """磁盘 IO 速率（MB/s、IOPS）。依赖上次累计值差分。

    返回恒为 (out, state) 二元组：psutil 拿不到计数器（Windows 性能计数器
    不可用等）时返回 ({}, None)——此前这里 `return {}` 会击穿解包契约，
    异常被 collect_items 吞掉后 disk_io 永久静默缺失（QR-A4）。
    """
    cur = _safe(psutil.disk_io_counters)
    if cur is None:
        return {}, None
    now = time.time()
    prev = state.get("disk_io")
    state_out = (now, cur.read_bytes, cur.write_bytes, cur.read_count, cur.write_count)
    out = {"disk_read_mb": 0.0, "disk_write_mb": 0.0, "disk_read_iops": 0.0, "disk_write_iops": 0.0}
    d = _deltas(prev, now, state_out)
    if d:
        out["disk_read_mb"] = round(d[0] / MB, 2)
        out["disk_write_mb"] = round(d[1] / MB, 2)
        out["disk_read_iops"] = round(d[2], 1)
        out["disk_write_iops"] = round(d[3], 1)
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
        d = _deltas(prev.get(nic), now, new_state[nic])
        if d:
            row["sent_mb"] = round(d[0] / MB, 3)
            row["recv_mb"] = round(d[1] / MB, 3)
            row["sent_pps"] = round(d[2], 1)
            row["recv_pps"] = round(d[3], 1)
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
    return out, ({"disk_io": st} if st else None)


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


# QR1120-A5：采集项失败留痕。此前 except 静默 continue，某项永久坏掉时
# 静默缺项无从排障。首次失败 warning、同项持续失败降 debug（防每拍刷屏）、
# 成功清零。collect_items 会被心跳 worker 与命令池 worker（kind=collect）
# 并发调用，状态读写加锁守卫。
_item_failures = {}          # name -> 连续失败次数
_item_fail_lock = threading.Lock()
_item_log = None             # 懒初始化：避免 import 期装配日志 sink


def _get_item_log():
    global _item_log
    if _item_log is None:
        # 与主日志同 name：复用 main 已装配的级别/sink（get_logger 按 name 幂等）
        _item_log = get_logger()
    return _item_log


def _note_item_failure(name, exc):
    with _item_fail_lock:
        n = _item_failures.get(name, 0) + 1
        _item_failures[name] = n
    log = _get_item_log().bind(item=name)
    if n == 1:
        log.warning("collect item failed: %s", exc)
    else:
        log.debug("collect item still failing (%d consecutive): %s", n, exc)


def _note_item_success(name):
    with _item_fail_lock:
        _item_failures.pop(name, None)


def collect_items(items, state, cfg=None):
    """按名称采集指定项，返回 (指标字典, 差分状态片段)。未知项忽略。

    回写只含本次实际更新的差分项（QR1128-A1），不抄带入参快照：调用方
    （StateBox.put）按 key 合并进共享基线，心跳与 collect worker 并发时
    各自只覆盖自己采过的 key。若整体抄回快照，后写者会用自己的旧基线
    抹掉对方刚更新的 key——磁盘/网络速率差分丢基线、摊到更长 dt 上失真。

    单项异常仍只跳过该项（不拖垮整帧），但会留痕（QR1120-A5）：
    首次失败 warning，同项持续失败降 debug，成功清零。
    """
    cfg = cfg or {}
    base = dict(state or {})
    out, diff = {}, {}
    for name in items or []:
        fn = ITEMS.get(name)
        if fn is None:
            continue
        try:
            part, st = fn(base, cfg)
        except Exception as e:
            _note_item_failure(name, e)
            continue
        _note_item_success(name)
        out.update(part or {})
        if st:
            diff.update(st)
            base.update(st)   # 同一次调用内后续项仍读到最新基线
    return out, diff


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
