"""命令执行：限时、输出封顶、进程组清理，在有限线程池内阻塞执行。

安全与稳定性要点：
- 默认 argv 数组直传 exec（不经 shell 拼接），天然免疫 shell 注入；
  需要管道/重定向时由服务端显式下发 `use_shell=true`，且 Agent 侧可用
  KK_ALLOW_SHELL=0 彻底关闭该能力
- 用 start_new_session 创建进程组，超时或异常时 killpg 清理整棵子进程树，
  避免 `sh -c "long &"` 之类的命令在超时后留下孤儿进程
- 输出在**读取侧**即封顶 max_out：达到上限后继续排水（丢弃）但不再保留，
  Agent 内存被硬性限制在 max_out 附近——旧实现用 communicate() 全量缓冲，
  `cat /dev/zero` 类大输出命令在超时窗口内可把 Agent 吃到 OOM（资源评审 P1）
"""
import os
import queue
import signal
import subprocess
import threading
import time

from .transport import RC_SEND_FAILED

SPAWN_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError, ValueError)

_READ_CHUNK = 65536  # 读取线程的单次排水块大小
# 工作线程之外允许积压的任务数（QR-A12）。旧实现队列无界：服务端一次点 500 台
# ×大输出命令，内存占用就由「点击数」决定，与 25–35MB 的常驻口径完全脱钩。
# 溢出当场回 rc=-3 的失败终态，让服务端那一行收敛成 failed。
MAX_PENDING = 64

# 子进程回收与读取线程的等待上限（QR29-A10：魔法数命名，调参有处可查）。
# TERM 与 KILL 之间留出体面退出的时间窗；reader.join 只兜「孙进程占住管道
# 写端」的极端情况，不该阻塞结果回传。
TERM_GRACE_SEC = 3
KILL_REAP_SEC = 5
READER_JOIN_SEC = 2
TASKKILL_SEC = 10


def mk_result(rc, out, elapsed_ms=0, timed_out=False, truncated=False):
    """shell 执行结果帧的唯一构造点（QR29-A8）：这五个字段是协议契约，
    手写散落多处迟早漂移（历史上 elapsed 单位就差点写错）。"""
    return {"rc": rc, "out": out, "timed_out": timed_out,
            "elapsed_ms": int(elapsed_ms), "truncated": truncated}


def run_shell(argv, timeout=30, max_out=4 * 1024 * 1024, use_shell=False):
    """执行 argv（或 shell 字符串），返回 {rc, out, timed_out, elapsed_ms, truncated}。"""
    t0 = time.monotonic()
    timed_out = False
    buf = bytearray()
    state = {"overflow": False}

    try:
        p = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            shell=bool(use_shell),
            start_new_session=True,  # 独立进程组，便于整组回收
        )
    except SPAWN_ERRORS as e:
        return mk_result(127 if isinstance(e, FileNotFoundError) else 126,
                         ("kk-agent: cannot spawn: %s" % e).encode("utf-8", "replace"))
    except OSError as e:
        return mk_result(126, ("kk-agent: spawn error: %s" % e).encode("utf-8", "replace"))

    def _kill_tree():
        """先 TERM 给进程组一个体面退出的机会，再 KILL 兜底。

        Windows 上没有 killpg/getpgid：AttributeError 会冒泡成 rc=125 且子进程泄漏，
        故按平台分路——POSIX 杀整个进程组，Windows 用 taskkill /T 杀进程树。
        """
        if os.name != "posix":
            _kill_tree_windows()
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            p.wait(timeout=TERM_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _kill_tree_windows():
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=TASKKILL_SEC)
        except Exception:
            pass
        try:
            p.wait(timeout=TERM_GRACE_SEC)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _drain():
        """读取线程：入内存直到 max_out，之后只丢弃。

        必须持续排水而不是停止读取——停读会让子进程阻塞在写管道上挂到超时，
        短命命令拿不到准确 rc；持续丢弃排水则两者兼得：内存有界 + rc 准确。
        """
        stream = p.stdout
        try:
            while True:
                chunk = stream.read1(_READ_CHUNK)
                if not chunk:
                    return
                room = max_out - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                    if len(chunk) > room:
                        state["overflow"] = True
                else:
                    state["overflow"] = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    reader = threading.Thread(target=_drain, daemon=True, name="kk-read")
    reader.start()

    rc = None
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree()
        try:
            p.wait(timeout=KILL_REAP_SEC)
        except subprocess.TimeoutExpired:
            pass
        rc = -1
    except Exception as e:
        _kill_tree()
        return mk_result(125, ("kk-agent: exec error: %s" % e).encode("utf-8", "replace"),
                         elapsed_ms=(time.monotonic() - t0) * 1000,
                         truncated=bool(state["overflow"]))

    # 读取线程通常随管道 EOF 结束；孙进程占住管道写端的极端情况留给
    # daemon 线程自行消亡，主流程最多等 READER_JOIN_SEC，不阻塞结果回传。
    reader.join(timeout=READER_JOIN_SEC)
    # truncated 只看 overflow 标记：_drain 保证 buf 恒不超过 max_out
    # （超限部分只丢弃不保留），此前同句里的 `len(buf) > max_out` 恒 False（QR29-A11）
    return mk_result(rc if rc is not None else -2, bytes(buf),
                     elapsed_ms=(time.monotonic() - t0) * 1000,
                     timed_out=timed_out, truncated=state["overflow"])


class _Pool:
    """固定数量 daemon 工作线程池。

    并发数有界，防止服务端批量下发大量命令时无界创建线程；空闲时线程阻塞在队列上。
    任务异常必须记日志而不是静默吞掉——回调签名不匹配这类 bug 曾因此隐身数个版本。
    """

    def __init__(self, max_workers=8, log=None, max_pending=MAX_PENDING):
        self._q = queue.Queue(maxsize=max_pending)
        self._log = log
        for _ in range(max(1, max_workers)):
            threading.Thread(target=self._loop, daemon=True, name="kk-task").start()

    def _loop(self):
        while True:
            fn = self._q.get()
            try:
                fn()
            except Exception:
                if self._log:
                    self._log.exception("task raised in worker thread")

    def submit(self, fn):
        """入队成功返回 True；队列满返回 False，由调用方负责回执。"""
        try:
            self._q.put_nowait(fn)
            return True
        except queue.Full:
            return False


class Runner:
    """把命令派发到后台线程池，结果经 emit(cmd_id, result) 回调送出。"""

    def __init__(self, emit, max_out=4 * 1024 * 1024, max_workers=8, allow_shell=True, log=None,
                 max_pending=MAX_PENDING):
        self.emit = emit
        self.max_out = max_out
        self.allow_shell = allow_shell
        self._log = log
        self._pool = None
        self._max_workers = max_workers
        self._max_pending = max_pending

    def _get_pool(self):
        if self._pool is None:
            self._pool = _Pool(self._max_workers, self._log, self._max_pending)
        return self._pool

    def submit(self, cmd):
        """shell 命令帧 {"id","argv","timeout","use_shell"} → 后台执行。"""
        argv = cmd.get("argv") or []
        try:
            timeout = min(max(int(cmd.get("timeout", 30)), 1), 600)
        except (TypeError, ValueError):
            timeout = 30
        use_shell = bool(cmd.get("use_shell")) and self.allow_shell
        if use_shell:
            argv = argv[0] if isinstance(argv, list) and argv else str(argv)
        self.submit_fn(cmd.get("id"),
                       lambda: run_shell(argv, timeout, self.max_out, use_shell))

    def submit_fn(self, cmd_id, fn):
        """通用后台任务（shell 命令、collect 采集、插件重载）。

        结果帧的**唯一**出口是这里的 `emit`：任务函数只返回结果，不要再自己发帧
        （QR-A19 就是 update 分支两处都发导致台账计数翻倍）。
        """

        def work():
            try:
                res = fn()
            except Exception as e:
                res = mk_result(125, ("kk-agent: task error: %s" % e).encode("utf-8", "replace"))
            self.emit(cmd_id, res)

        if self._get_pool().submit(work):
            return
        # 积压满：当场拒绝并回失败终态，比让命令在内存里排队更诚实（QR-A12）
        if self._log:
            self._log.bind(cmd=cmd_id).warning(
                "task queue full (%d pending), command rejected", self._max_pending)
        self.emit(cmd_id, mk_result(RC_SEND_FAILED,
                                    b"kk-agent: task queue full, try again later"))
