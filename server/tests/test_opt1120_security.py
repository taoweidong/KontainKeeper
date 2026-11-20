"""QR1120-S18：命令黑名单 wrapper 前缀绕过的回归锁（含 API 边界审计）。

缺陷形态：_check_tokens 跳过 wrapper 后默认下一 token 即程序名——
  env FOO=1 rm -rf /        （prog=foo=1）
  sudo -u root dd if=/dev/zero  （prog=-u）
  timeout 10 rm -rf /       （prog=10）
结构层整体失效，子串兜底也接不住（拆写形不命中）。

裁决口径（与 QR-S41 同源，此处按 QR1120-S18 复核钉死）：跳过 wrapper 的取值
附属 token（KEY=VALUE 赋值、-/-- 旗标、已知取值旗标的值、裸数字/时长）再定位
程序名。**只修解析，不新增黑名单词**——curl|sh / truncate / find -delete 等
覆盖面放行现状由 test_security.py 的 QR29-S6 锁守着，本文件不动那些断言。

不依赖真实 Broker / 网络 / 真实时钟：API 用例走 ASGITransport + FakePublish。
"""
import json

import pytest

from kk_server.services.security import is_blacklisted

from test_controlled_upgrade import bridge_app  # noqa: F401

# 三类 wrapper 后命中黑名单词：结构层独立拦住（patterns=[]，不靠子串兜底）
BYPASS_FORMS = [
    ["env", "FOO=1", "rm", "-rf", "/"],
    ["env", "A=1", "B=2", "rm", "-r", "-f", "/usr"],
    ["env", "-u", "HOME", "wipefs", "-a", "/dev/sda"],
    ["sudo", "-u", "root", "dd", "if=/dev/zero", "of=/dev/sda"],
    ["sudo", "-g", "wheel", "rm", "-rf", "/data"],
    ["sudo", "--user", "root", "mkfs", "/dev/sdb"],
    ["timeout", "10", "rm", "-rf", "/"],
    ["timeout", "5m", "dd", "if=/dev/zero", "of=/dev/sda"],
    ["timeout", "--preserve-status", "10", "rm", "-rf", "/"],
    ["nice", "-n", "10", "rm", "-rf", "/"],
]

# 正当包装层用法不许误杀（合法 env 赋值、sudo 非黑名单程序、timeout 时长）
BENIGN_FORMS = [
    ["env", "FOO=1", "python", "app.py"],
    ["env", "A=1", "B=2", "ls", "-la"],
    ["sudo", "-u", "postgres", "pg_dump", "mydb"],
    ["sudo", "-n", "systemctl", "status", "nginx"],
    ["timeout", "30", "curl", "http://example.com"],
    ["nice", "-n", "5", "make", "build"],
    ["7z", "x", "archive.zip"],   # 数字开头的真实程序名不是 wrapper 数值参数
]


@pytest.mark.parametrize("argv", BYPASS_FORMS)
def test_wrapper_bypass_blocked_in_both_forms(argv):
    """QR1120-S18：env/sudo/timeout 等 wrapper 的附属 token 不是程序名，
    跳过后必须命中其后的黑名单词——argv 数组与 shell 单串两种形态都拦。"""
    assert is_blacklisted(argv, []) is True
    assert is_blacklisted(" ".join(argv), [], use_shell=True) is True


@pytest.mark.parametrize("argv", BENIGN_FORMS)
def test_wrapper_benign_not_false_positive(argv):
    """QR1120-S18：加固只修解析、不新增黑名单词——正当包装用法照常放行。"""
    assert is_blacklisted(argv, []) is False, argv
    assert is_blacklisted(" ".join(argv), [], use_shell=True) is False, argv


@pytest.mark.parametrize("argv", [
    ["env", "FOO=1", "rm", "-rf", "/"],
    ["sudo", "-u", "root", "dd", "if=/dev/zero", "of=/dev/sda"],
    ["timeout", "10", "rm", "-rf", "/"],
])
async def test_wrapper_bypass_blocked_with_audit_at_api(bridge_app, argv):  # noqa: F811
    """QR1120-S18：API 边界（/api/commands）拒绝 + command_blocked 审计 +
    零命令帧——安全红线在控制器层同样不可绕过（审计 store.add_audit）。"""
    await bridge_app.store.upsert_container("h1", "img", "0.1.0", 60)
    r = await bridge_app.client.post("/api/commands", json={
        "pods": ["h1"], "kind": "shell", "argv": argv})
    assert r.status_code == 400 and "黑名单" in r.json()["detail"]
    audit = await bridge_app.store.list_audit(limit=20)
    blocked = [a for a in audit if a["action"] == "command_blocked"]
    assert len(blocked) == 1
    assert json.loads(blocked[0]["detail"])["argv"] == argv
    assert not [m for m in bridge_app.bridge.cli.msgs
                if m["topic"].endswith("/h1/cmd")]


async def test_wrapper_bypass_shell_cmdline_blocked_with_audit(bridge_app):  # noqa: F811
    """QR1120-S18：use_shell 单串形态（前端 cmdline 恒 use_shell=true）同样
    拒绝 + 审计——wrapper 绕过不因提交形态而异。"""
    await bridge_app.store.upsert_container("h1", "img", "0.1.0", 60)
    r = await bridge_app.client.post("/api/commands", json={
        "pods": ["h1"], "kind": "shell",
        "cmdline": "env FOO=1 rm -rf /", "use_shell": True})
    assert r.status_code == 400 and "黑名单" in r.json()["detail"]
    audit = await bridge_app.store.list_audit(limit=20)
    assert [a for a in audit if a["action"] == "command_blocked"]
    assert not [m for m in bridge_app.bridge.cli.msgs
                if m["topic"].endswith("/h1/cmd")]


async def test_wrapper_benign_command_accepted_at_api(bridge_app):  # noqa: F811
    """QR1120-S18：sudo 非黑名单程序在 API 边界正常受理（不误杀），
    命令帧真发出去、command_create 审计在。"""
    await bridge_app.store.upsert_container("h1", "img", "0.1.0", 60)
    r = await bridge_app.client.post("/api/commands", json={
        "pods": ["h1"], "kind": "shell",
        "argv": ["sudo", "-u", "postgres", "pg_dump", "mydb"]})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
    frames = [m for m in bridge_app.bridge.cli.msgs
              if m["topic"].endswith("/h1/cmd")]
    assert len(frames) == 1
    audit = await bridge_app.store.list_audit(limit=20)
    assert [a for a in audit if a["action"] == "command_create"]
    assert not [a for a in audit if a["action"] == "command_blocked"]