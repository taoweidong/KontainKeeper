"""命令执行器单测。"""
import queue
import sys
import time

from kk_agent import executor as ex


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
