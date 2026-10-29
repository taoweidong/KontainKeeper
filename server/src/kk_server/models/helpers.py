"""存储层工具函数：URL 规整、口令遮蔽、密码哈希、base64 尾段解码。

从 store.py 拆出——纯函数，不依赖 Store 实例或 engine。
"""
import base64
import hashlib

# PBKDF2 迭代数（P2 安全评审：120k 偏低，提到 310k 与现代 Broker 口令强度对齐）。
# 旧哈希用 120k 的存量管理员在「首次成功登录」时原地升级到新迭代数，无需手工迁移。
_PWDF_ITERS = 310_000
_PWDF_ITERS_LEGACY = 120_000


def _pwdf(salt, password, iters=_PWDF_ITERS):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), iters).hex()


def normalize_url(url=None, db_path=None):
    """把用户写的连接串规整成 async engine 可用的形式。

    - 没有 scheme → 当 SQLite 文件路径（旧 `KK_DB_PATH=kk-server.db` 用法）
    - 有 scheme → 只换驱动，路径原样保留（写 `postgresql://` 自动补 `+asyncpg`）
    """
    u = (url or db_path or "").strip()
    if "://" not in u:
        return "sqlite+aiosqlite:///" + u.replace("\\", "/") if u else                "sqlite+aiosqlite:///kk-server.db"
    scheme, _, rest = u.partition("://")
    base = scheme.split("+")[0]          # 用户显式写了别的驱动也一并纠正过来
    return {"sqlite": "sqlite+aiosqlite", "postgresql": "postgresql+asyncpg",
            "postgres": "postgresql+asyncpg", "mysql": "mysql+aiomysql",
            "mariadb": "mysql+aiomysql"}.get(base, scheme) + "://" + rest


def mask_url(url):
    """抹掉连接串里的账号口令，用于日志与错误回显。"""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return "%s://***@%s" % (scheme, rest.rsplit("@", 1)[1])


def _num(value):
    """标量指标转 float；坏值回 None 而不是抛异常让整帧心跳落不了库。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 列表路径保留的输出尾段（base64 字符数）：_b64_tail 与 commands.out_tail 列共用
# 同一算术——ceil(2048/3)*4 = 2732，右截到 4 的倍数仍是合法 base64。
TAIL_B64_CHARS = ((2048 + 2) // 3) * 4


def _b64_tail(b64, nbytes=2048):
    """从拼接好的 base64 里截末段解码：每 4 字符解 3 字节且组间独立，右截仍合法。"""
    if not b64:
        return ""
    chars = TAIL_B64_CHARS if nbytes == 2048 else ((nbytes + 2) // 3) * 4
    tail = b64[-chars:] if len(b64) > chars else b64
    tail = tail[-((len(tail) // 4) * 4):] or tail
    try:
        return base64.b64decode(tail, validate=False).decode("utf-8", "replace")[-nbytes:]
    except Exception:
        return ""


# IN 列表的分片尺寸：三库的语句变量数上限各不相同（SQLite 默认 999 起），
# 500 台规模一次 IN 不触顶，但常量只在这里定义一份，改动才不会漏站（QR29-S17）。
SHARD_SIZE = 400


def shards(seq, size=SHARD_SIZE):
    """把序列切成 size 片的生成器：`for shard in shards(pods): ... in_(shard)`。

    逐条 UPDATE 在 SQLite 上约数十毫秒，但 500 条就是 500 次事务往返——
    分片批处理是「一次点击」路径的标准形态，原先四处手写同构循环。
    """
    seq = list(seq or [])
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def clamp(v, lo, hi):
    """把用户给的数值参数钳进 [lo, hi]：负 limit/超大量程在入口一次挡掉。

    原先六处手写 `min(max(v, lo), hi)`，量程各不相同——量程留在调用点，
    这里只收「钳」这个动作本身（QR29-S17）。
    """
    return min(max(v, lo), hi)
