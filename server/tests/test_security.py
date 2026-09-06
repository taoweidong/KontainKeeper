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
