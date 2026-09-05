"""存储层工具函数：URL 规整、口令遮蔽、密码哈希、base64 尾段解码。

从 store.py 拆出——纯函数，不依赖 Store 实例或 engine。
"""
import base64
import hashlib

def _pwdf(salt, password):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), 120_000).hex()


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


def _b64_tail(b64, nbytes=2048):
    """从拼接好的 base64 里截末段解码：每 4 字符解 3 字节且组间独立，右截仍合法。"""
    if not b64:
        return ""
    chars = ((nbytes + 2) // 3) * 4
    tail = b64[-chars:] if len(b64) > chars else b64
    tail = tail[-((len(tail) // 4) * 4):] or tail
    try:
        return base64.b64decode(tail, validate=False).decode("utf-8", "replace")[-nbytes:]
    except Exception:
        return ""
