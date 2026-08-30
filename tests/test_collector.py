"""采集器单测：基于伪造 /proc 树。"""
from kk_agent import collector as c


CFG = {"fs_root": None, "disk_paths": []}


def test_meminfo(fake_fs):
    m = c.read_meminfo(fake_fs)
    assert m == {"mem_total_mb": 2000.0, "mem_mb": 500.0, "mem_pct": 25.0}


def test_cpu_total_and_percent(fake_fs):
    cur = c.read_cpu_total(fake_fs)
    assert cur == (900.0, 700.0)
    assert c.cpu_percent((400.0, 300.0), cur) == 20.0
    assert c.cpu_percent(None, cur) is None
    assert c.cpu_percent(cur, cur) is None  # 无差分信息


def test_procs_and_top(fake_fs):
    procs = c.read_procs(fake_fs)
    assert set(procs) == {"1", "2"}
    assert procs["2"][0] == "node"
    # 上一轮 tick=0，本轮 node=800：10s 间隔 → 800/100/10*100 = 800%（多核累加）
    top = c.procs_top(procs, {"2": ("node", 0.0, 0.0), "1": ("systemd", 0.0, 0.0)}, 10.0)
    assert top[0]["name"] == "node" and top[0]["cpu"] > 0
    assert top[0]["mem_mb"] > 0


def test_users(fake_fs):
    users = {u["name"]: u for u in c.collect_users(fake_fs)}
    assert users["dev"]["vscode"] is True
    assert users["dev"]["procs"] == 1
    assert users["root"]["procs"] == 1
    assert "daemon" not in users  # 无家目录的系统账户被过滤


def test_collect_full(fake_fs):
    cfg = dict(CFG, fs_root=str(fake_fs))
    m1, state = c.collect(cfg, {"cpu": None, "procs": {}, "ts": 0})
    assert m1["mem_mb"] == 500.0
    assert m1["load"] == "0.10 0.20 0.30"
    assert m1["cpu"] is None  # 首帧无差分
    assert isinstance(m1["users"], list) and len(m1["users"]) == 2
    # 伪造第二次采样（修改 stat 使 total/idle 变化）
    (fake_fs / "proc" / "stat").write_text("cpu  200 0 100 1400 0 0 0 0 0 0\n")
    m2, _ = c.collect(cfg, state)
    assert m2["cpu"] == 12.5  # d_total=800, d_idle=700 → (800-700)/800
