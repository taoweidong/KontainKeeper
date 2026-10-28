"""管理员登录/登出/身份。"""
import threading
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import CurrentUser

router = APIRouter(prefix="/api")

# 登录失败限流（P1-5 / B3）：按**两个维度**各自计数，任一维度达阈值即锁 ——
# 用户名（"u:<name>"）挡定向爆破，客户端 IP（"i:<ip>"）挡用户名枚举。
# 只按用户名计数时，攻击者拿一个 IP 换着用户名试，永远到不了 5 次。
# 内存态，重启即清零——足以拖慢在线爆破，不依赖外部存储。
_LOGIN_FAILS: dict[str, int] = defaultdict(int)
# 每个失败计数键的最近一次失败时刻：_reap 靠它回收「未达阈值且已冷却」的键
# （QR29-S7，与 docstring「只增不减就是内存泄漏」的承诺配套）
_LOGIN_FAIL_AT: dict[str, float] = {}
# 锁定期内 login_locked 审计的最近落库时刻（QR29-S8：未鉴权路径不可逐请求灌审计表）
_LOCKED_AUDIT_TS: dict[str, float] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}
_LOGIN_LIMIT_LOCK = threading.Lock()
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300
# 失败计数的冷却 TTL：最近一次失败超过它，枚举攻击已不活跃，计数回收归零。
# 与锁定时长同档——爆破者每 5 分钟最多再试 5 次，防御强度不变
FAIL_COUNT_TTL = LOGIN_LOCK_SECONDS
# 锁定期审计的最小落库间隔（对齐桥接侧 AUDIT_THROTTLE 的语义）
LOCKED_AUDIT_INTERVAL = 60


class LoginBody(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str:
    """限流用的客户端 IP：**只认直连的 request.client.host**。

    刻意不读 X-Forwarded-For：它可任意伪造，反而给攻击者「换个头就换个锁对象」
    的便利，比不限流更糟。经反代时源 IP 会退化成代理地址 —— 那时应由网关限流
    （见 docs/deployment.md §6），服务端这一层会退化为「全局 5 次」。
    """
    client = request.client
    return (client.host if client else "") or "unknown"


def _reap(now: float):
    """剔除已过期的锁定与计数键（须持锁调用）。

    键无上限，只增不减就是内存泄漏（QR29-S7）——一次扫描式的用户名枚举即可把
    字典撑大。锁定键看锁定到期时间；失败计数键看最近一次失败是否已冷却
    （活跃攻击期间计数被不断刷新，不会被误清）。放在既有临界区里，成本只有
    几次遍历。
    """
    for k in [k for k, until in _LOGIN_LOCKED_UNTIL.items() if until <= now]:
        _LOGIN_LOCKED_UNTIL.pop(k, None)
        _LOGIN_FAILS.pop(k, None)
        _LOGIN_FAIL_AT.pop(k, None)
    for k in [k for k, at in _LOGIN_FAIL_AT.items() if now - at > FAIL_COUNT_TTL]:
        _LOGIN_FAILS.pop(k, None)
        _LOGIN_FAIL_AT.pop(k, None)
    # 节流时间戳同型回收：留 4 个节流窗口的余量，别掐掉还在窗口内的键
    for k in [k for k, at in _LOCKED_AUDIT_TS.items()
              if now - at > LOCKED_AUDIT_INTERVAL * 4]:
        _LOCKED_AUDIT_TS.pop(k, None)


def _locked_audit_allowed(ukey: str, ikey: str, now: float) -> bool:
    """锁定期审计节流（QR29-S8）：ukey/ikey 任一维度距上次落库不足一个
    节流窗口则跳过。锁定期内的每个请求本都会走到这里，不限速等于把
    审计表交给未鉴权路径灌（audit 只增不减，靠 cleanup 保留期兜底）。"""
    last = max(_LOCKED_AUDIT_TS.get(ukey, 0), _LOCKED_AUDIT_TS.get(ikey, 0))
    if now - last < LOCKED_AUDIT_INTERVAL:
        return False
    _LOCKED_AUDIT_TS[ukey] = now
    _LOCKED_AUDIT_TS[ikey] = now
    return True


@router.post("/login")
async def login(body: LoginBody, request: Request):
    store = request.app.state.store
    username = body.username
    ip = _client_ip(request)
    ukey, ikey = "u:" + username, "i:" + ip
    now = time.time()
    with _LOGIN_LIMIT_LOCK:
        _reap(now)
        until = max(_LOGIN_LOCKED_UNTIL.get(ukey, 0), _LOGIN_LOCKED_UNTIL.get(ikey, 0))
        locked = until > now
    if locked:
        if _locked_audit_allowed(ukey, ikey, now):
            await store.add_audit(username, "login_locked",
                                  {"retry_after": int(until - now), "ip": ip})
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    ok = await store.verify_admin(username, body.password)
    locked_now = False
    with _LOGIN_LIMIT_LOCK:
        if not ok:
            _LOGIN_FAILS[ukey] += 1
            _LOGIN_FAILS[ikey] += 1
            _LOGIN_FAIL_AT[ukey] = now
            _LOGIN_FAIL_AT[ikey] = now
            locked_now = (_LOGIN_FAILS[ukey] >= MAX_LOGIN_FAILS
                          or _LOGIN_FAILS[ikey] >= MAX_LOGIN_FAILS)
            if locked_now:
                # 达到阈值：立即锁定，本次失败直接回 429（而非等下一次）
                for k in (ukey, ikey):
                    _LOGIN_LOCKED_UNTIL[k] = now + LOGIN_LOCK_SECONDS
                    _LOGIN_FAILS[k] = 0
                    _LOGIN_FAIL_AT.pop(k, None)
        else:
            # 成功：清零该用户与该 IP 的失败计数与锁定
            for k in (ukey, ikey):
                _LOGIN_FAILS.pop(k, None)
                _LOGIN_FAIL_AT.pop(k, None)
                _LOGIN_LOCKED_UNTIL.pop(k, None)
    if not ok:
        if locked_now:
            await store.add_audit(username, "login_locked",
                                  {"retry_after": LOGIN_LOCK_SECONDS, "ip": ip})
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
        await store.add_audit(username, "login_fail",
                              {"fails": _LOGIN_FAILS.get(ukey, 0), "ip": ip})
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = await store.create_session(username)
    await store.add_audit(username, "login_ok", {})
    return {"token": token, "username": username}


@router.post("/logout")
async def logout(request: Request):
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.startswith("Bearer ") else ""
    actor = None
    if token:
        # 先解析出用户名再删会话，便于审计登出主体
        actor = await request.app.state.store.get_session(token)
        await request.app.state.store.delete_session(token)
    # P2 审计：登出动作留痕，便于安全事件追溯。
    # 仅有效会话才写审计：本接口无鉴权，无条件落库会让匿名请求刷审计表
    # （audit 表只增不减，见 cleanup 的保留策略）。
    if actor:
        await request.app.state.store.add_audit(actor, "logout", {})
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser):
    """会话身份：鉴权走 CurrentUser 依赖（QR-S2），漏配路由级依赖也会被参数兜住。"""
    return {"username": user}
