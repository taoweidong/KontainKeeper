"""命令黑名单校验（安全红线，禁止绕过；纯逻辑，便于独立单测）。

支持两种输入形态：

1. **argv 数组**（默认，推荐）：`argv[0]` 为程序名，逐项校验参数。
2. **单串形态**（`use_shell=True`，或 argv 只有一项且含空格）：此时
   `argv[0]` 就是**整条命令串**，`os.path.basename(argv[0])` 拿到的不是
   程序名——旧实现会让「程序名 + 高危参数组合」与「高危程序集合」两层
   结构校验**整体失效**，只剩子串匹配，可被相对路径、参数换序、
   `dd if=/dev/urandom` 等写法轻松绕过（代码审查 P0-1）。
   现按 shell 语义切分整串、还原 token 后走同一套结构校验。
"""
import os
import re

# 程序名 + 高危参数组合：命中程序且参数集合有交集即拒绝。
# 注意：参数一律按小写比较（调用前会 lower），故这里必须全小写，
# 否则 `-R`（大写）永远匹配不上归一化后的 `-r`。
DANGEROUS_COMBOS = {
    "rm": {"-r", "-rf", "-fr", "--recursive", "-f", "--force"},
    "mv": {"-r", "-rf", "-fr", "--recursive", "-f", "--force"},
    "chmod": {"-r", "--recursive"},
    "chown": {"-r", "--recursive"},
}

# 无条件拒绝的高危程序
DANGEROUS_PROGS = {
    "dd", "mkfs", "reboot", "shutdown", "poweroff", "halt",
    "init", "fdisk", "parted", "wipefs", "lvremove", "pvremove", "vgremove",
}

# 提权/包装类前缀：跳过它们才能看到真正的程序名（env rm -rf / 必须拦住）
WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "timeout", "xargs", "command",
            "busybox"}

# shell 串联/命令替换分隔符：把 `a; b && c | d` 拆成多段逐段校验
# （`\|\|?` 覆盖单竖线与双竖线，漏掉单竖线会让 `cat f | dd ...` 整段逃过校验）
_SHELL_SPLIT = re.compile(r";|\|\|?|&&?|\$\(|`|\n")
_WS = re.compile(r"\s+")
_QUOTES = "\"'"


def _segments(text):
    """把整条命令串按 shell 分隔符拆成若干段（去掉空段）。"""
    return [s for s in _SHELL_SPLIT.split(text) if s and s.strip()]


def _tokens(seg):
    """段内按空白分词并去引号。"""
    return [t.strip(_QUOTES) for t in _WS.split(seg.strip()) if t.strip(_QUOTES)]


def _check_tokens(tokens):
    """对一段命令做结构校验：跳过包装前缀后取程序名 + 参数集合。"""
    i = 0
    while i < len(tokens) and tokens[i].lower() in WRAPPERS:
        i += 1
    if i >= len(tokens):
        return False
    prog = os.path.basename(tokens[i]).lower()
    args = {a.lower() for a in tokens[i + 1:]}
    if prog in DANGEROUS_COMBOS and (args & DANGEROUS_COMBOS[prog]):
        return True
    return prog in DANGEROUS_PROGS


def _hits_substring(text, patterns):
    """配置型子串黑名单：折叠多余空白，避免 "rm  -rf /" 双空格绕过。"""
    norm = _WS.sub(" ", text.lower())
    for p in patterns or []:
        if _WS.sub(" ", str(p).lower()) in norm:
            return True
    return False


def is_blacklisted(argv, patterns, use_shell=False):
    """判断命令是否命中黑名单。

    :param argv: 命令参数数组；单串形态传 `["整条命令"]` 或直接传字符串
    :param patterns: 管理员配置的子串黑名单（KK_CMD_BLACKLIST）
    :param use_shell: 是否以 shell 单串形态执行（此时必须按 shell 语义切分）
    """
    if not argv:
        return False
    if isinstance(argv, str):
        argv = [argv]
    argv = [str(a) for a in argv if a is not None]
    if not argv:
        return False

    joined = " ".join(argv)
    single = use_shell or (len(argv) == 1 and " " in argv[0])

    if single:
        # 单串形态：逐段还原 token 后结构校验；无段可分时退回整串校验
        segs = _segments(joined) or [joined]
        for seg in segs:
            if _check_tokens(_tokens(seg)):
                return True
    elif _check_tokens(argv):
        return True

    return _hits_substring(joined, patterns)
