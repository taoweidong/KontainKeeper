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


def test_run_shell_timeout():
    t0 = time.monotonic()
    r = ex.run_shell([sys.executable, "-c", "import time; time.sleep(10)"], timeout=1)
    assert r["timed_out"] and r["rc"] == -1
    assert time.monotonic() - t0 < 8


def test_run_shell_spawn_missing():
    r = ex.run_shell(["kk-definitely-not-exist-xyz"], timeout=5)
    assert r["rc"] == 127 and b"cannot spawn" in r["out"]


def test_runner_queue_delivery():
    q = queue.Queue()
    runner = ex.Runner(q)
    runner.submit({"id": "c-t1", "argv": [sys.executable, "-c", "print('queued')"], "timeout": 10})
    kind, ref, res = q.get(timeout=10)
    assert kind == "cmd_result" and ref == "c-t1" and res["rc"] == 0
    assert b"queued" in res["out"]
