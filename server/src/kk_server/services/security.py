"""命令黑名单校验（安全红线，禁止绕过；纯逻辑，便于独立单测）。"""
import os
import re


def is_blacklisted(argv, patterns):
    """命令黑名单：重点防护破坏性指令。

    比单纯子串匹配更抗绕过：
    1) 程序名 + 高危参数组合：rm/mv 带递归或强制；chmod/chown -R；dd；
       mkfs/reboot/shutdown/poweroff/halt/... 等直接拒绝。
    2) 管理员可配置的 KK_CMD_BLACKLIST 子串（折叠多余空白后匹配），兼容旧用法，
       并修复 "rm  -rf /" 这类双空格绕过。
    """
    if not argv:
        return False
    prog = os.path.basename(str(argv[0]).strip().lower())
    args = [str(a).lower() for a in argv[1:]]
    dangerous_combos = {
        "rm": {"-r", "-rf", "-fr", "-R", "--recursive", "-f", "--force"},
        "mv": {"-r", "-rf", "-fr", "-R", "--recursive", "-f", "--force"},
        "chmod": {"-R", "--recursive"},
        "chown": {"-R", "--recursive"},
    }
    if prog in dangerous_combos and (set(args) & dangerous_combos[prog]):
        return True
    if prog in {"dd", "mkfs", "reboot", "shutdown", "poweroff", "halt",
                "init", "fdisk", "parted", "wipefs", "lvremove", "pvremove", "vgremove"}:
        return True
    # 配置型子串黑名单（折叠多余空白，避免双空格绕过）
    norm = re.sub(r"\s+", " ", " ".join(str(a) for a in argv).lower())
    for p in patterns:
        if re.sub(r"\s+", " ", p.lower()) in norm:
            return True
    return False