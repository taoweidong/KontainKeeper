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

# shell 解释器（QR-P0-2）：prog 命中且下一 token 是 -c 时，载荷才是**真正的命令**，
# 必须按 shell 语义递归校验。不处理的话 `argv=["sh","-c","rm -r -f /usr"]`
# （use_shell=false 的 argv 数组形态）结构校验整体失效——prog=sh 不命中任何
# 危险集合，参数拆写又躲过子串兜底。su -c 的载荷同理（su 不吃脚本文件形态以外
# 的选项冲突：`su - user` 的 rest[0] 不是旗标，_script_payload 自然判非内联）。
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash", "su"}
# 脚本解释器：`-c`(python) / `-e`(perl/ruby/node) 后面是**代码载荷**（QR-S25）。
# 代码不是 shell 语义，无法靠结构切分可靠还原命令，故对这类载荷额外做危险程序名扫描。
_INTERP_CODE_FLAGS = {
    "python": ("-c",), "python2": ("-c",), "python3": ("-c",), "pypy": ("-c",),
    "perl": ("-e",), "perl5": ("-e",), "ruby": ("-e",), "node": ("-e",), "nodejs": ("-e",),
}
# 组合旗标里的取命令位：sh 语义要求 `c` 是选项簇的**最后一个**字母（-xc 取命令，-cx 不取）
_COMBINED_C = re.compile(r"^-[^-]*c$")
# 递归深度上限：sh -c "sh -c ..." 的嵌套payload按层展开，超过即放弃（黑名单是
# 尽力而为的纵深之一，无界递归反而给攻击者递归炸弹）
_MAX_SHELL_DEPTH = 4

# shell 串联/命令替换分隔符：把 `a; b && c | d` 拆成多段逐段校验
# （`\|\|?` 覆盖单竖线与双竖线，漏掉单竖线会让 `cat f | dd ...` 整段逃过校验）
_SHELL_SPLIT = re.compile(r";|\|\|?|&&?|\$\(|`|\n")
_WS = re.compile(r"\s+")
_QUOTES = "\"'"

# 危险程序名词边界扫描（结构层与子串层互为冗余）：用于解释器代码载荷这种
# 结构无法解析的形态。`\b` 界定避免 "add"/"rmtree" 之类被 "dd"/"rm" 误伤。
_DANGER_WORDS = re.compile(
    r"\b(" + "|".join(sorted((re.escape(p) for p in
                              (DANGEROUS_PROGS | set(DANGEROUS_COMBOS))), key=len, reverse=True)) + r")\b"
)


def _segments(text):
    """把整条命令串按 shell 分隔符拆成若干段（去掉空段）。"""
    return [s for s in _SHELL_SPLIT.split(text) if s and s.strip()]


def _tokens(seg):
    """段内按空白分词并去引号。"""
    return [t.strip(_QUOTES) for t in _WS.split(seg.strip()) if t.strip(_QUOTES)]


def _script_payload(prog, rest):
    """若 prog 是 shell/脚本解释器且带取命令的选项，返回 (代码载荷, 是否解释器)。

    先跳过前导旗标再定位命令位：`sh -x -c CMD`、`sh -xc CMD`、`python -u -c CODE`
    都要还原出真正的载荷（QR-S25 的绕过点正是旧代码只认 `rest[0]` 恰为 -c）。
    遇到非选项 token（脚本文件名，如 `sh script.sh`）即判定不是内联载荷。
    """
    is_shell = prog in _SHELLS
    code_flags = _INTERP_CODE_FLAGS.get(prog)
    if not (is_shell or code_flags):
        return None, False
    j = 0
    while j < len(rest):
        t = rest[j].lower()
        if (is_shell and (t == "--command" or _COMBINED_C.match(t))) or \
           (code_flags and t in code_flags):
            return " ".join(rest[j + 1:]), not is_shell
        if t.startswith("-") and t != "-":
            j += 1
            continue
        break
    return None, False


# 包装器自己吃值的旗标：跳过旗标后**紧跟的一个 token 是旗标的值**，不是程序名
# （sudo -u root dd … 的 root）。刻意只收「各包装器语义一致」的少数几个：-u/-g 在
# sudo/env/doas 里都取值；-n/-p 这类在 sudo（无值）与 xargs（无值）之间语义冲突，
# 不进集合——否则 `sudo -n dd …` 反而会把 dd 当值吞掉（QR-S41）。
_WRAP_VALUE_FLAGS = {"-u", "-g", "--user", "--group", "--priority",
                     "--adjustment", "--chdir"}
# env 的 KEY=VALUE 赋值与 timeout/nice 的裸数字/时长：都是包装层参数，不是程序名
_WRAP_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAP_DUR = re.compile(r"^\d+(?:\.\d+)?[a-z]?$")


def _skip_wrapper_prefix(tokens):
    """返回程序名的下标：连续跳过包装层前缀——wrapper 本体、它的选项、已知取值
    旗标的值、KEY=VALUE 赋值、裸数字/时长。

    缺陷 QR-S41：旧实现跳过 wrapper 后**默认下一个 token 就是程序名**，wrapper
    自己的参数被当成程序名，`env FOO=1 rm -r -f /usr`（prog=foo=1）、
    `sudo -u root dd …`（prog=-u）、`timeout 10 rm -rf /`（prog=10）整条绕过
    结构层，子串兜底也接不住（连写形才命中）。误判方向刻意偏向多拦：落到
    程序名前多跳一个良性 token 的代价是偶发误拦，漏掉真程序名的代价是放行。
    裸数字/时长只在**出现过 wrapper 之后**才跳（timeout/nice 的数值参数）：
    `7z x …` 这类数字开头的真实程序名不能被吞。
    """
    i = 0
    n = len(tokens)
    take_value = False   # 上一个 token 是取值旗标：本 token 是它的值，跳过
    in_wrap = False      # 见过至少一个 wrapper：之后的裸数字按包装层参数处理
    while i < n:
        t = tokens[i].lower()
        if take_value:
            take_value = False
            i += 1
            continue
        if t in WRAPPERS:
            in_wrap = True
            i += 1
            continue
        if t.startswith("-") or _WRAP_ASSIGN.match(t) or (in_wrap and _WRAP_DUR.match(t)):
            take_value = t in _WRAP_VALUE_FLAGS
            i += 1
            continue
        break
    return i


def _check_tokens(tokens, _depth=0):
    """对一段命令做结构校验：跳过包装层前缀后取程序名 + 参数集合。"""
    i = _skip_wrapper_prefix(tokens)
    if i >= len(tokens):
        return False
    prog = os.path.basename(tokens[i]).lower()
    payload, is_interp = _script_payload(prog, tokens[i + 1:])
    if payload is not None:
        # 解释器代码结构无法解析，靠危险程序名词扫描兜底；
        # shell/解释器都再按 shell 语义递归一层（sh -c 里套命令）。
        if is_interp and _DANGER_WORDS.search(payload):
            return True
        if _depth < _MAX_SHELL_DEPTH:
            segs = _segments(payload) or ([payload] if payload.strip() else [])
            for seg in segs:
                if _check_tokens(_tokens(seg), _depth + 1):
                    return True
        # 载荷干净就放行：sh 本身不在危险集合里，落回下方常规检查
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
