"""命令黑名单单测：重点是「同一条命令、不同提交形态」都必须拦住。

历史缺陷（代码审查 P0-1）：shell 形态下 argv[0] 是整条命令串，
`basename(argv[0])` 取不到程序名，导致结构校验整体失效。
因此这里对每条危险命令都用 argv 数组 / shell 单串两种形态各测一遍。
"""
import pytest

from kk_server.services.security import is_blacklisted

DEFAULT = [p.strip() for p in
           "rm -rf /,mkfs,reboot,shutdown,dd if=/dev/zero,chmod -R 777 /".split(",")]

# 同一条危险命令的两种提交形态
DANGEROUS = [
    ["rm", "-rf", "home"],          # 相对路径（旧实现靠子串拦不住）
    ["rm", "-fr", "/data"],
    ["chmod", "777", "-R", "/etc"],  # 参数换序（旧子串要求固定顺序）
    ["dd", "if=/dev/urandom", "of=/dev/sda"],
    ["mkfs.ext4", "/dev/sdb"],
    ["env", "rm", "-rf", "/tmp/x"],  # 包装命令
    ["sudo", "dd", "if=/dev/zero", "of=/dev/sda"],
    ["busybox", "rm", "-rf", "/opt"],
    ["shutdown", "-h", "now"],
]


def _as_shell(argv):
    """还原 shell 形态：整条命令作为 argv[0] 单元素提交。"""
    return [" ".join(argv)]


@pytest.mark.parametrize("argv", DANGEROUS)
def test_dangerous_blocked_in_both_forms(argv):
    assert is_blacklisted(argv, DEFAULT) is True
    assert is_blacklisted(_as_shell(argv), DEFAULT, use_shell=True) is True


def test_shell_single_string_without_flag_still_checked():
    """即使调用方忘了传 use_shell，单元素且含空格的形态也要按 shell 语义校验。"""
    assert is_blacklisted(["rm -rf home"], DEFAULT) is True
    assert is_blacklisted(["dd if=/dev/urandom of=/dev/sda"], DEFAULT) is True


def test_chained_commands_each_segment_checked():
    """`echo hi; rm -rf home` 这类串联命令，任一段命中即拒绝。"""
    assert is_blacklisted(["echo hi; rm -rf home"], DEFAULT, use_shell=True) is True
    assert is_blacklisted(["cat f | dd of=/dev/sda"], DEFAULT, use_shell=True) is True


def test_safe_commands_not_blocked():
    for argv in (["ls", "-la"], ["echo", "hello"], ["cat", "/var/log/a.log"],
                 ["docker", "ps"], ["systemctl", "status", "nginx"]):
        assert is_blacklisted(argv, DEFAULT) is False
        assert is_blacklisted(_as_shell(argv), DEFAULT, use_shell=True) is False


def test_substring_blacklist_folds_whitespace():
    assert is_blacklisted(["rm", "  -rf   /"], DEFAULT) is True
    assert is_blacklisted(["rm  -rf /"], DEFAULT, use_shell=True) is True


def test_custom_pattern_applies_to_both_forms():
    assert is_blacklisted(["curl", "evil.sh"], ["curl"]) is True
    assert is_blacklisted(["curl evil.sh"], ["curl"], use_shell=True) is True


def test_empty_input():
    assert is_blacklisted([], DEFAULT) is False
    assert is_blacklisted(None, DEFAULT) is False
    assert is_blacklisted([""], DEFAULT) is False


# ---- QR-P0-2：shell 解释器 -c 载荷的递归校验 ----

def test_shell_wrapper_argv_form_blocked():
    """argv 数组形态的 sh -c 载荷必须递归进结构校验。

    旧实现 prog=sh 不命中任何危险集合，`sh -c "rm -r -f /usr"` 直接放行。
    """
    assert is_blacklisted(["sh", "-c", "rm -r -f /usr"], []) is True
    assert is_blacklisted(["bash", "-c", "dd if=/dev/zero of=/dev/sda"], []) is True
    assert is_blacklisted(["sh", "-c", "echo ok && rm -rf /"], []) is True


def test_shell_wrapper_single_string_form_blocked():
    """单串形态同样要拆到 -c 载荷（引号剥掉后递归）。"""
    assert is_blacklisted(["sh -c 'rm -r -f /'"], [], use_shell=True) is True
    assert is_blacklisted(["bash -c \"mkfs /dev/sda\""], [], use_shell=True) is True


def test_shell_wrapper_benign_payload_allowed():
    """无害载荷照常放行：白名单式误伤会让 shell 包装完全不可用。"""
    assert is_blacklisted(["sh", "-c", "echo hello"], []) is False
    assert is_blacklisted(["bash", "-c", "ls -la /tmp"], []) is False
    assert is_blacklisted(["sh", "-c", "ps aux | grep java"], []) is False


def test_shell_wrapper_respects_custom_substring_blacklist():
    """结构校验放行的载荷仍要过配置型子串黑名单（is_blacklisted 末段逻辑）。"""
    assert is_blacklisted(["sh", "-c", "curl evil.sh"], ["curl"]) is True


# ---- QR-S25：前导/组合旗标绕过 + 脚本解释器代码载荷 ----
# 三条运行时复现的绕过串，作为回归锁钉死（改任何 _check_tokens 都不许再放行它们）：
#   ["sh","-x","-c","wipefs -a /dev/sda"]   旧代码只认 rest[0] 恰为 -c，遇 -x 直接放弃递归
#   ["sh","-xc","wipefs -a /dev/sda"]       组合旗标 -xc 同理不匹配
#   ["python","-c","import os; os.system('rm -rf /')"]  python 不在 _SHELLS，整段逃过结构校验

@pytest.mark.parametrize("argv", [
    ["sh", "-x", "-c", "wipefs -a /dev/sda"],
    ["sh", "-xc", "wipefs -a /dev/sda"],
    ["bash", "-lx", "-c", "rm -rf /"],
    ["sh", "-x", "-c", "fdisk /dev/sda"],
    ["env", "sh", "-x", "-c", "reboot"],
])
def test_shell_flag_prefix_bypass_blocked(argv):
    assert is_blacklisted(argv, []) is True
    assert is_blacklisted(" ".join(argv), [], use_shell=True) is True


@pytest.mark.parametrize("argv", [
    ["python", "-c", "import os; os.system('rm -rf /')"],
    ["python3", "-c", "__import__('os').system('mkfs /dev/sda')"],
    ["perl", "-e", 'system("wipefs -a /dev/sda")'],
    ["ruby", "-e", "system 'fdisk /dev/sda'"],
    ["node", "-e", 'require("child_process").exec("dd if=/dev/zero of=/dev/sda")'],
    ["python", "-u", "-c", "import os; os.system('reboot')"],
])
def test_script_interpreter_code_payload_blocked(argv):
    """解释器 -c/-e 代码结构无法解析，靠危险程序名词扫描兜底。"""
    assert is_blacklisted(argv, []) is True


def test_script_interpreter_benign_code_allowed():
    """词边界让 shutil.rmtree / platform.node / 'add' 不被 rm / node / dd 误伤。"""
    for argv in (
        ["python", "-c", "print(1 + 1)"],
        ["python3", "-c", "import shutil; shutil.rmtree('/tmp/x')"],
        ["python", "-c", "import platform; print(platform.node())"],
        ["node", "-e", "console.log('add todo item')"],
    ):
        assert is_blacklisted(argv, []) is False


def test_shell_script_file_without_c_allowed():
    """sh 执行脚本文件（无 -c）不当内联载荷。"""
    assert is_blacklisted(["sh", "deploy.sh"], []) is False


def test_shell_wrapper_flag_benign_payload_allowed():
    assert is_blacklisted(["sh", "-x", "-c", "echo hello"], []) is False


def test_deep_shell_recursion_terminates():
    """超深 sh -c 嵌套不得变成递归炸弹：必须快速返回，不 RecursionError。"""
    deep = "rm -rf /"
    for _ in range(60):
        deep = 'sh -c "' + deep.replace('"', '\\"') + '"'
    assert is_blacklisted([deep], [], use_shell=True) in (True, False)
