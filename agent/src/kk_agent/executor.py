"""命令执行：exec 数组（不经 shell）、限时、输出封顶，一次性工作线程内阻塞执行。

空闲时零线程零开销；任务到达才起 daemon 线程，结果 (kind, cmd_id, result)
投递回主循环队列，由主循环统一分块发帧（保证 socket 只在主线程访问）。
"""
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


class _Pool:
    """固定数量 daemon 工作线程的线程池。

    并发数有界（默认 8），防止恶意/故障服务端下发大量命令时无界创建线程导致 DoS；
    任务队列吸收瞬时峰值，空闲时线程阻塞在队列上、零 CPU 开销。线程为 daemon，
    进程退出时不会挂起。
    """

    def __init__(self, max_workers=8):
        import queue as _q
        self._q = _q.Queue()
        self._workers = []
        for _ in range(max(1, max_workers)):
            t = threading.Thread(target=self._loop, daemon=True, name="kk-task")
            t.start()
            self._workers.append(t)

    def _loop(self):
        while True:
            fn = self._q.get()
            if fn is None:
                self._q.put(None)  # 广播退出信号，让其余 worker 也能结束
                break
            try:
                fn()
            except Exception:
                pass

    def submit(self, fn):
        self._q.put(fn)


class Runner:
    """把任务派发到后台线程池，结果 (kind, cmd_id, result) 投递到 outq。"""

    def __init__(self, outq, max_out=4 * 1024 * 1024, max_workers=8):
        self.q = outq  # type: queue.Queue
        self.max_out = max_out
        self._pool = None
        self._max_workers = max_workers

    def _get_pool(self):
        if self._pool is None:
            self._pool = _Pool(self._max_workers)
        return self._pool

    def submit(self, cmd):
        """shell 命令帧（{"id","argv","timeout"}）→ 后台执行 run_shell。"""
        argv = list(cmd.get("argv") or [])
        try:
            timeout = min(max(int(cmd.get("timeout", 30)), 1), 600)
        except (TypeError, ValueError):
            timeout = 30
        self.submit_fn("cmd_result", cmd.get("id"),
                       lambda: run_shell(argv, timeout, self.max_out))

    def submit_fn(self, kind, cmd_id, fn):
        """通用后台任务（shell 命令、插件即时采集），fn() 返回 result dict。"""

        def work():
            try:
                res = fn()
            except Exception as e:
                res = {"rc": 125, "out": ("kk-agent: task error: %s" % e).encode("utf-8", "replace"),
                       "timed_out": False, "elapsed_ms": 0}
            self.q.put((kind, cmd_id, res))

        self._get_pool().submit(work)
