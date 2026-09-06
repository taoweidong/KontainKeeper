"""资源基线：父进程起临时服务端，独立子进程运行 Agent（连真实 Broker），测量常驻内存。

子进程内存独立测量（父进程里不加载 agent 相关模块），避免进程内混测失真。
v3 起 Agent 基于 psutil + paho-mqtt（跨平台真实采集，不再伪造 /proc），常驻
RSS 口径约 25–35MB（见 AGENTS.md）。

前置：本机 1883 端口有 Mosquitto（docker run -d -p 1883:1883 eclipse-mosquitto:2）。

用法: python scripts/bench_agent.py [持续秒数=10]
输出: 子进程 RSS 均值/最大值、峰值、心跳条数、是否达标
"""
import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROKER_URL = os.environ.get("KK_BENCH_MQTT", "mqtt://127.0.0.1:1883")
TARGET_MB = 40.0   # psutil 口径：25–35MB 常驻 + 余量


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def broker_reachable(timeout=2):
    try:
        host = BROKER_URL.split("://", 1)[1].split(":")[0] or "127.0.0.1"
        port = int(BROKER_URL.rsplit(":", 1)[1]) if ":" in BROKER_URL.split("://", 1)[1] else 1883
        with socket.create_connection((host, port), timeout):
            return True
    except (OSError, ValueError):
        return False


def child_rss_mb(pid):
    """返回 (rss_mb, peak_mb)；获取失败返回 (None, None)。"""
    if os.name == "nt":
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaNonPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaPeakPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.POINTER(PMC), wt.DWORD)
        h = k32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
        if not h:
            return None, None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return None, None
            return pmc.WorkingSetSize / 1048576.0, pmc.PeakWorkingSetSize / 1048576.0
        finally:
            k32.CloseHandle(h)
    try:
        vals = {}
        for line in Path("/proc/%d/status" % pid).read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v, _ = line.split()
                vals[k.rstrip(":")] = int(v) / 1024.0
        return vals.get("VmRSS"), vals.get("VmHWM")
    except (OSError, ValueError):
        return None, None


def run_agent_child(prefix, fs_dir):
    """子进程模式：只加载 agent 相关模块，由父进程终止。"""
    sys.path.insert(0, str(ROOT / "agent" / "src"))
    from kk_agent import main as agent_main, config as kk_config
    cfg = kk_config.load(env={
        "KK_SERVER": BROKER_URL,
        "KK_INTERVAL": "1",
        "KK_TOPIC_PREFIX": prefix,
        "KK_LOG": "-",
        "KK_LOG_LEVEL": "ERROR",
        "KK_HOST_NAME": "bench-pod",
        "KK_UPDATE_DISABLED": "1",
    })
    agent_main.run(cfg=cfg)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    if not broker_reachable():
        print("bench: 本机无 Mosquitto（%s），跳过；见文件头说明" % BROKER_URL)
        return

    tmp = Path(tempfile.mkdtemp(prefix="kk-bench-"))
    port = free_port()
    prefix = "kk/bench%d" % port

    import uvicorn
    from kk_server.main import create_app
    app = create_app({"KK_DB_PATH": str(tmp / "bench.db"),
                      "KK_MQTT_URL": BROKER_URL,
                      "KK_TOPIC_PREFIX": prefix,
                      "KK_MQTT_CLIENT_ID": "kk-server-bench",
                      "KK_WEB_DIR": str(tmp / "no-web"),
                      "KK_ADMIN_PASS": "x" * 20,
                      "KK_LOG_LEVEL": "error"})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    child = subprocess.Popen(
        [sys.executable, __file__, "--child", prefix, str(tmp / "fs")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("bench: agent pid=%d, running %ds ..." % (child.pid, duration))

    samples = []
    end = time.time() + duration
    while time.time() < end:
        rss, peak = child_rss_mb(child.pid)
        if rss is not None:
            samples.append((rss, peak))
        time.sleep(0.5)

    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
    time.sleep(1)   # 留时间给桥接收尾最后一帧心跳
    server.should_exit = True

    rows, _ = app.state.store.metrics_series("bench-pod", 1)
    if not samples:
        print("bench: 无法读取子进程内存（权限或平台不支持）")
        return
    rss_list = [s[0] for s in samples]
    peaks = [s[1] for s in samples if s[1] is not None]
    print("bench 结果（%d 次采样 / %ds，心跳 %d 条）：" % (len(samples), duration, len(rows)))
    print("  常驻 RSS: avg=%.1f MB  max=%.1f MB" % (sum(rss_list) / len(rss_list), max(rss_list)))
    if peaks:
        print("  峰值:     %.1f MB" % max(peaks))
    print("  目标:     < %.0f MB → %s" % (TARGET_MB, "达标" if max(rss_list) < TARGET_MB else "超标"))


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--child":
        run_agent_child(sys.argv[2], sys.argv[3])
    else:
        main()
