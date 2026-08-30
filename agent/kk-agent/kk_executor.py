"""命令执行：exec 数组（不经 shell）、限时、输出封顶，工作线程内阻塞执行。

空闲时零线程零开销；命令到达才起一个 daemon 线程，结果投递回主循环队列，
由主循环统一分块发帧（保证 socket 只在主线程访问）。
"""
import queue
import subprocess
import threading
import time

SPAWN_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError)


def run_shell(argv, timeout=30, max_out=4 * 1024 * 1024):
    """执行 argv，返回 {rc, out(bytes), timed_out, elapsed_ms}。"""
    t0 = time.monotonic()
    timed_out = False
    out = b""
    rc = 126
    try:
        p = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except SPAWN_ERRORS as e:
        return {"rc": 127 if isinstance(e, FileNotFoundError) else 126,
                "out": ("kk-agent: cannot spawn: %s" % e).encode("utf-8", "replace"),
                "timed_out": False, "elapsed_ms": 0}
    except OSError as e:
        return {"rc": 126, "out": ("kk-agent: spawn error: %s" % e).encode("utf-8", "replace"),
                "timed_out": False, "elapsed_ms": 0}
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        p.kill()
        try:
            out, _ = p.communicate(timeout=5)
        except Exception:
            pass
    except Exception as e:  # 通信异常按失败处理
        p.kill()
        return {"rc": 125, "out": ("kk-agent: exec error: %s" % e).encode("utf-8", "replace"),
                "timed_out": False, "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    return {
        "rc": -1 if timed_out else (p.returncode if p.returncode is not None else -2),
        "out": (out or b"")[:max_out],
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


class Runner:
    """把命令派发到一次性工作线程，结果 (kind, cmd_id, result) 投递到 outq。"""

    def __init__(self, outq, max_out=4 * 1024 * 1024):
        self.q = outq  # type: queue.Queue
        self.max_out = max_out

    def submit(self, cmd):
        argv = list(cmd.get("argv") or [])
        try:
            timeout = min(max(int(cmd.get("timeout", 30)), 1), 600)
        except (TypeError, ValueError):
            timeout = 30

        def work():
            res = run_shell(argv, timeout, self.max_out)
            self.q.put(("cmd_result", cmd.get("id"), res))

        threading.Thread(target=work, daemon=True, name="kk-cmd").start()

    def submit_fn(self, kind, cmd_id, fn):
        """通用后台任务（如插件即时采集），fn() 返回 result dict。"""

        def work():
            try:
                res = fn()
            except Exception as e:
                res = {"rc": 125, "out": ("kk-agent: task error: %s" % e).encode("utf-8", "replace"),
                       "timed_out": False, "elapsed_ms": 0}
            self.q.put((kind, cmd_id, res))

        threading.Thread(target=work, daemon=True, name="kk-task").start()
