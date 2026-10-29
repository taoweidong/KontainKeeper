"""Store 的 auth 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import asyncio
import secrets
import time

from sqlalchemy import delete, insert, select, update

from .helpers import _PWDF_ITERS_LEGACY, _pwdf
from .tables import admins, sessions

# verify_admin 时序拉平用的假盐（QR-S8）：值无意义，只为让不存在用户也吃满一次 PBKDF2
_DUMMY_SALT = "0" * 32


class _StoreAuthMixin:

    # ---- 管理员与会话 ----
    async def ensure_admin(self, username, password, force=False):
        """不存在则创建；口令与 env 不一致则轮换（改 KK_ADMIN_PASS 后重启即生效）。"""
        row = await self._one(select(admins).where(admins.c.username == username))
        if row is None:
            salt = secrets.token_hex(16)
            await self._run(insert(admins).values(username=username, salt=salt,
                                                  pw_hash=_pwdf(salt, password),
                                                  created=int(time.time())))
            return True
        if force or not secrets.compare_digest(row["pw_hash"], _pwdf(row["salt"], password)):
            salt = secrets.token_hex(16)
            await self._run(update(admins).where(admins.c.username == username)
                            .values(salt=salt, pw_hash=_pwdf(salt, password)))
            return True
        return False

    async def verify_admin(self, username, password):
        row = await self._one(select(admins).where(admins.c.username == username))
        if not row:
            # 时序侧信道（QR-S8）：用户不存在也跑一次同代价 PBKDF2，让「存在/不存在」
            # 的响应时间不可分辨；登录限流挡枚举，这里抹掉剩余的时间差。
            await asyncio.to_thread(_pwdf, _DUMMY_SALT, password)
            return False
        # PBKDF2（310k 迭代）是同步 CPU 活，必须挪进线程池：本方法跑在事件循环上，
        # 而 MQTT 桥接的协程处理器在同一条循环里——同步哈希一次 login 就能让心跳
        # 落库与命令下发停摆数十毫秒（QR29-S1）。三路返回值与旧实现逐字一致。
        cand = await asyncio.to_thread(_pwdf, row["salt"], password)
        if secrets.compare_digest(row["pw_hash"], cand):
            return True
        # 兼容旧迭代数的存量哈希：首次成功登录时原地升级到新迭代数
        legacy = await asyncio.to_thread(_pwdf, row["salt"], password,
                                         _PWDF_ITERS_LEGACY)
        if secrets.compare_digest(row["pw_hash"], legacy):
            await self._run(update(admins).where(admins.c.username == username)
                            .values(salt=row["salt"], pw_hash=cand))
            return True
        return False

    async def create_session(self, username, ttl=12 * 3600):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        await self._run(insert(sessions).values(token=token, username=username,
                                                created=now, expires=now + ttl))
        return token

    async def get_session(self, token):
        if not token:
            return None
        row = await self._one(select(sessions).where(sessions.c.token == token))
        if not row or row["expires"] < int(time.time()):
            return None
        return row["username"]

    async def delete_session(self, token):
        return await self._run(delete(sessions).where(sessions.c.token == token))

    async def default_admin_exists(self):
        return await self._one(select(admins.c.username)
                               .where(admins.c.username == "admin")) is not None

    # ---- KV / Agent 版本清单 ----
