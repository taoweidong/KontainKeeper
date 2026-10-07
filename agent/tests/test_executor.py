"""命令执行器单测。"""
import os
import queue
import sys
import threading
import time

import pytest

from kk_agent import executor as ex
from kk_agent.transport import RC_SEND_FAILED


def test_run_shell_ok():
    r = ex.run_shell([sys.executable, "-c", "print('hi')"], timeout=10)
    assert r["rc"] == 0 and not r["timed_out"]
    assert b"hi" in r["out"]
    assert r["elapsed_ms"] >= 0


def test_run_shell_nonzero_rc():
    r = ex.run_shell([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10)
    assert r["rc"] == 3


def test_run_shell_timeout_kills_process():
    """超时必须真的把子进程杀掉（Windows 无 killpg，曾因此抛 AttributeError 泄漏进程）。"""
    t0 = time.monotonic()
    r = ex.run_shell([sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)
    assert r["timed_out"] and r["rc"] == -1
    assert time.monotonic() - t0 < 15, "杀进程树不应拖到子进程自然结束"


def test_run_shell_stderr_merged_into_out():
    r = ex.run_shell([sys.executable, "-c", "import sys; sys.stderr.write('to-stderr')"],
                     timeout=10)
    assert b"to-stderr" in r["out"]


def test_run_shell_spawn_missing():
    r = ex.run_shell(["kk-definitely-not-exist-xyz"], timeout=5)
    assert r["rc"] == 127 and b"cannot spawn" in r["out"]


def test_run_shell_truncates_and_flags():
    r = ex.run_shell([sys.executable, "-c", "import sys; sys.stdout.write('x' * 500)"],
                     timeout=20, max_out=100)
    assert len(r["out"]) == 100 and r["truncated"] is True


def test_run_shell_read_side_cap_on_large_output():
    """大输出命令：内存在读取侧即封顶，而非全量缓冲后再截断（资源评审 P1）。

    产出 64MB 输出但 max_out=4KB：若仍是 communicate() 全量缓冲，Agent 内存
    会被撑到 64MB；读取侧封顶后无论命令产出多少，驻留内存恒为 max_out 附近。
    """
    code = "import sys\nfor _ in range(1024):\n    sys.stdout.buffer.write(b'x' * 65536)\n"
    r = ex.run_shell([sys.executable, "-c", code], timeout=60, max_out=4096)
    assert r["rc"] == 0, "排水不停止读取，短命命令应正常跑完拿到准确 rc"
    assert len(r["out"]) == 4096 and r["truncated"] is True


def test_run_shell_infinite_output_bounded_and_killed():
    """`cat /dev/zero` 场景：无限输出 + 超时，输出与内存都必须有界。"""
    code = ("import sys\nwhile True:\n"
            "    sys.stdout.buffer.write(b'x' * 65536)\n    sys.stdout.buffer.flush()\n")
    t0 = time.monotonic()
    r = ex.run_shell([sys.executable, "-c", code], timeout=2, max_out=4096)
    assert r["timed_out"] is True and r["rc"] == -1
    assert len(r["out"]) == 4096 and r["truncated"] is True
    assert time.monotonic() - t0 < 15, "超时后必须立即回收进程树"


def test_runner_delivers_result_to_emit():
    """Runner 的回调契约是 emit(cmd_id, result)——签名不匹配会被线程池吞掉。"""
    got = queue.Queue()

    def emit(cid, res):
        got.put((cid, res))

    runner = ex.Runner(emit)
    runner.submit({"id": "c-t1", "argv": [sys.executable, "-c", "print('queued')"],
                   "timeout": 15})
    cid, res = got.get(timeout=20)
    assert cid == "c-t1" and res["rc"] == 0
    assert b"queued" in res["out"]


def test_runner_shell_mode_requires_allow_shell():
    got = queue.Queue()
    runner = ex.Runner(lambda cid, res: got.put(res), allow_shell=False)
    runner.submit({"id": "c-s", "argv": ["echo hi"], "use_shell": True, "timeout": 10})
    res = got.get(timeout=20)
    # allow_shell=False 时按 argv 数组直传，"echo hi" 作为单个程序名必然起不来
    assert res["rc"] in (126, 127) or b"cannot spawn" in res["out"]


def test_runner_shell_mode_when_allowed():
    got = queue.Queue()
    runner = ex.Runner(lambda cid, res: got.put(res), allow_shell=True)
    runner.submit({"id": "c-s2", "argv": ["echo shell-mode"], "use_shell": True, "timeout": 15})
    res = got.get(timeout=25)
    assert res["rc"] == 0 and b"shell-mode" in res["out"]


def test_runner_timeout_clamped():
    got = queue.Queue()
    runner = ex.Runner(lambda cid, res: got.put(res))
    t0 = time.monotonic()
    runner.submit({"id": "c-to", "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                   "timeout": 0})  # 下限夹到 1s，不能被当成 0 立刻杀或无限等
    res = got.get(timeout=20)
    assert res["timed_out"] is True
    assert time.monotonic() - t0 < 4


def test_runner_task_exception_becomes_rc125():
    got = queue.Queue()
    runner = ex.Runner(lambda cid, res: got.put(res))

    def boom():
        raise ValueError("collector exploded")
    runner.submit_fn("c-x", boom)
    res = got.get(timeout=10)
    assert res["rc"] == 125 and b"collector exploded" in res["out"]


def test_runner_pool_is_bounded():
    """并发上限必须生效：批量下发 500 台时不能无限建线程。"""
    active = {"now": 0, "max": 0}
    done = queue.Queue()

    def counted():
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        active["now"] -= 1
        done.put(1)

    runner = ex.Runner(lambda cid, res: None, max_workers=3)
    for i in range(12):
        runner.submit_fn("c-%d" % i, counted)
    for _ in range(12):
        done.get(timeout=20)
    assert active["max"] <= 3


# ---- 积压有界（QR-A12）----

def test_runner_rejects_when_pending_queue_is_full():
    """队列必须有硬上限：旧实现无界，一次批量下发的内存占用由「点击数」决定。

    溢出时当场回 rc=-3 的失败终态（同步 emit，不等 worker），让服务端那一行
    收敛成 failed；被拒的任务函数一次都不许执行。
    """
    release = threading.Event()
    started = threading.Event()
    got = queue.Queue()
    ran = []

    def blocker():
        started.set()                # 确认真的被唯一 worker 取走了再开始填队列
        release.wait(10)

    def parked():
        release.wait(10)

    def never_runs():
        ran.append("rejected task ran anyway")

    runner = ex.Runner(lambda cid, res: got.put((cid, res)),
                       max_workers=1, max_pending=2)
    runner.submit_fn("c-block", blocker)     # 被唯一 worker 取走并卡住
    assert started.wait(5), "worker 没取走首个任务，后面的排队断言不成立"
    runner.submit_fn("c-p1", parked)         # 占满队列（max_pending=2）
    runner.submit_fn("c-p2", parked)
    runner.submit_fn("c-reject", never_runs)  # 队列已满 → 当场拒绝

    cid, res = got.get(timeout=5)            # 同步返回，不依赖 worker
    assert cid == "c-reject"
    assert res["rc"] == RC_SEND_FAILED
    assert b"queue full" in res["out"], "回执要说清是被限流拒绝的，不是命令本身失败"
    assert res["timed_out"] is False and res["truncated"] is False

    release.set()
    for _ in range(3):                       # block + p1 + p2 的正常回执
        got.get(timeout=20)
    assert ran == [], "被拒的任务不能被补执行"


def test_pool_submit_reports_overflow():
    """_Pool.submit 的返回值是调用方判断「要不要自己发回执」的唯一依据。"""
    started = threading.Event()
    release = threading.Event()
    p = ex._Pool(max_workers=1, max_pending=1)
    p.submit(lambda: (started.set(), release.wait(10)))   # 被 worker 占住
    assert started.wait(5), "worker 未取走首个任务，容量断言不成立"
    assert p.submit(lambda: None) is True                 # 填满容量 1 的队列
    assert p.submit(lambda: None) is False                # 溢出：交回调用方处理
    release.set()


def test_default_pending_bound_is_explicit():
    """默认积压上限是显式预算，不能退化成「反正一般用不到」。"""
    assert 0 < ex.MAX_PENDING <= 128, \
        "MAX_PENDING=%d：过大则批量下发击穿常驻内存口径" % ex.MAX_PENDING


@pytest.mark.skipif(os.name != "posix", reason="killpg 分路仅 POSIX")
def test_kill_tree_permission_error_does_not_break_result(monkeypatch):
    """killpg 抛 PermissionError（进程已属他人/被守护）时必须吞掉，不能冒成 rc=125。

    旧实现只捕 ProcessLookupError：Windows 上抛 AttributeError、受限环境下抛
    PermissionError，都会把「超时」误报成「执行错误」并留下孤儿进程。
    """
    def deny(*a, **k):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "killpg", deny)
    r = ex.run_shell([sys.executable, "-c", "import time; time.sleep(2)"], timeout=1)
    assert r["timed_out"] is True and r["rc"] == -1, "杀不掉也要收敛成超时，不能变 rc=125"

