"""指标采集：直接读 /proc 与 statvfs，不依赖 psutil，采样即读即停。

所有文件系统访问都基于 fs_root（默认 "/"），便于测试注入伪造的 /proc。
"""
import os
import time

try:
    import resource
    PAGE_KB = resource.getpagesize() // 1024
except ImportError:  # 非 POSIX（仅开发/测试环境会走到这里）
    PAGE_KB = 4

try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, AttributeError, OSError):
    CLK_TCK = 100

TOP_N = 5
MAX_USERS = 20


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _join(root, path):
    root = str(root)
    if root == "/":
        return path
    return root + path


# ---- CPU ----
def read_cpu_total(fs_root):
    """/proc/stat 首行 → (total_ticks, idle_ticks)；不可读返回 None。"""
    text = _read_text(_join(fs_root, "/proc/stat"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = [float(x) for x in line.split()[1:] if _isnum(x)]
            if not vals:
                return None
            idle = (vals[3] if len(vals) > 3 else 0.0) + (vals[4] if len(vals) > 4 else 0.0)
            return sum(vals), idle
    return None


def _isnum(x):
    try:
        float(x)
        return True
    except ValueError:
        return False


def cpu_percent(prev, cur):
    """两次 /proc/stat 采样差分 → 区间平均 CPU%；信息不足返回 None。"""
    if not prev or not cur:
        return None
    d_total = cur[0] - prev[0]
    d_idle = cur[1] - prev[1]
    if d_total <= 0:
        return None
    pct = (d_total - d_idle) * 100.0 / d_total
    return round(max(0.0, min(100.0, pct)), 1)


# ---- 内存 ----
def read_meminfo(fs_root):
    text = _read_text(_join(fs_root, "/proc/meminfo"))
    vals = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        vals[k] = float(v.strip().split()[0]) if v.strip() else 0.0  # kB
    total = vals.get("MemTotal", 0.0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0.0))
    if total <= 0:
        return None
    used = total - avail
    return {
        "mem_total_mb": round(total / 1024, 1),
        "mem_mb": round(used / 1024, 1),
        "mem_pct": round(used * 100.0 / total, 1),
    }


# ---- 磁盘 ----
def disk_usage(paths):
    out = {}
    if not hasattr(os, "statvfs"):
        return out
    for p in paths:
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        if st.f_blocks <= 0:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        out[p] = {
            "total_mb": round(total / 1048576, 1),
            "used_mb": round(used / 1048576, 1),
            "pct": round(used * 100.0 / total, 1) if total else 0.0,
        }
    return out


# ---- 进程 ----
def _list_pids(fs_root):
    proc = _join(fs_root, "/proc")
    try:
        names = os.listdir(proc)
    except OSError:
        return []
    return [n for n in names if n.isdigit()]


def read_procs(fs_root):
    """{pid: (comm, utime+stime, rss_pages)}；单次遍历 /proc/<pid>/stat。"""
    out = {}
    for pid in _list_pids(fs_root):
        text = _read_text(_join(fs_root, "/proc/%s/stat" % pid))
        if not text:
            continue
        rp = text.rpartition(")")  # comm 可能含空格
        if not rp[1]:
            continue
        fields = rp[2].split()
        if len(fields) < 22:
            continue
        try:
            ticks = float(fields[11]) + float(fields[12])
            rss_pages = float(fields[21])
        except ValueError:
            continue
        out[pid] = (rp[0].partition("(")[2], ticks, rss_pages)
    return out


def procs_top(cur, prev, wall_sec):
    """按区间 CPU% 排序取前 N；首次采样（无 prev）cpu 记 0。"""
    rows = []
    for pid, (comm, ticks, rss) in cur.items():
        cpu = 0.0
        if prev and pid in prev and wall_sec > 0:
            d = ticks - prev[pid][1]
            cpu = round(d / CLK_TCK * 100.0 / wall_sec, 1) if d > 0 else 0.0
        rows.append({"pid": int(pid), "name": comm[:32], "cpu": cpu,
                     "mem_mb": round(rss * PAGE_KB / 1024, 1)})
    rows.sort(key=lambda r: (-r["cpu"], -r["mem_mb"]))
    return rows[:TOP_N]


# ---- 用户 ----
def read_passwd(fs_root):
    users = []
    text = _read_text(_join(fs_root, "/etc/passwd"))
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            uid = int(parts[2])
        except ValueError:
            continue
        users.append((parts[0], uid, parts[5]))
    return users


def count_uids(fs_root):
    """{uid: 进程数}，来自 /proc/<pid>/status 的 Uid 行。"""
    counts = {}
    for pid in _list_pids(fs_root):
        text = _read_text(_join(fs_root, "/proc/%s/status" % pid))
        if not text:
            continue
        for line in text.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                    uid = int(parts[1])
                    counts[uid] = counts.get(uid, 0) + 1
                break
    return counts


def collect_users(fs_root):
    uid_counts = count_uids(fs_root)
    out = []
    for name, uid, home in read_passwd(fs_root):
        home_path = _join(fs_root, home) if home.startswith("/") else None
        home_exists = bool(home_path) and os.path.isdir(home_path)
        vscode = bool(home_path) and os.path.isdir(os.path.join(home_path, ".vscode-server"))
        procs = uid_counts.get(uid, 0)
        if not (home_exists or uid >= 1000 or vscode):
            continue  # 过滤无家目录的系统账户
        out.append({"name": name, "uid": uid, "procs": procs, "vscode": vscode})
    out.sort(key=lambda u: (-u["procs"], u["name"]))
    return out[:MAX_USERS]


# ---- 汇总 ----
def collect(cfg, state):
    """一次完整采集。state 携带上次的 /proc 采样用于差分。

    返回 (metrics, new_state)；任何子项失败都不影响整体。
    """
    fs_root = cfg.get("fs_root", "/")
    now = time.time()

    cpu = read_cpu_total(fs_root)
    cur_procs = read_procs(fs_root)
    wall = max(0.001, now - state.get("ts", now))
    loadavg = _read_text(_join(fs_root, "/proc/loadavg")).split()[:3]
    metrics = {
        "cpu": cpu_percent(state.get("cpu"), cpu),
        "load": " ".join(loadavg) if loadavg else None,
    }
    metrics.update(read_meminfo(fs_root) or {})
    metrics["disks"] = disk_usage(cfg.get("disk_paths", []))
    metrics["procs_top"] = procs_top(cur_procs, state.get("procs", {}), wall)
    metrics["users"] = collect_users(fs_root)

    new_state = {"cpu": cpu, "procs": cur_procs, "ts": now}
    return metrics, new_state
