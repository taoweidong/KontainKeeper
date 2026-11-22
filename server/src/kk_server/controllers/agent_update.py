"""Agent 自更新接口：

- POST /api/system/agent       管理员上传新版本二进制（multipart: file + version）
- POST /api/system/agent/rollback 回滚待分发二进制到上一版（只影响重启的 Agent）
- GET  /api/system/agent/current  服务端当前待分发版本 + 落后主机数（管理员可读）
- GET  /api/system/agent/latest   Agent 查询最新版本清单（落后才 available）
- GET  /api/system/agent/download Agent 下载二进制（流式）

安全（v3）：
- 上传/回滚/批量升级需 admin 会话（QR-S42）：这三个写端点是「向全网主机分发
  可执行代码」级特权操作，router 级 current_user 只保证登录、不保证角色——
  operator 可直达等于把 RCE 入口开放给全部会话。GET /agent/current 与
  GET /updates 是只读台账，保持登录会话可读（operator 的「版本与更新」页要展示）。
- 下载/查询按请求方真实源 IP 校验 KK_AGENT_IPS 白名单
- 服务端记录 sha256，Agent 端下载后校验一致才替换，防损坏/篡改
- 二进制按平台单槽位（kk-agent），多架构需另行扩展
"""
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .deps import AgentIp, AdminUser, agent_ip_auth, current_user
from ..config import normalize_update_mode
from ..services.downlink import get_downlink
from ..logsetup import get_logger
from ..models.helpers import agent_download_url, clamp
from ..models.tables import ONLINE_GRACE
from ..models.version import count_outdated, version_lt

log = get_logger("kk.update")

# 双路由拆分（QR-S2）：同前缀下两组端点的鉴权语义不同——管理员走会话、
# Agent 走真实源 IP 白名单，router 级依赖各收各的口，新增端点默认受保护。
router = APIRouter(prefix="/api/system", dependencies=[Depends(current_user)])
agent_router = APIRouter(prefix="/api/system", dependencies=[Depends(agent_ip_auth)])

MAX_BIN_BYTES = 64 * 1024 * 1024
_BIN_NAME = "kk-agent"
_CHUNK = 256 * 1024
# QR1120-S17：版本号白名单（纯数字段点分）。旧校验只查首字符是数字，"1abc" 可入库，
# 全网 Agent 会拿到一个永远无法比较/校验的升级目标。上传是唯一写入版本号的入口
# （回滚互换的是既有清单、批量升级的目标必须等于清单版本），故收口在这一处。
_VERSION_RE = re.compile(r"\d+(\.\d+)*")
# QR1120-S14：Windows 共享冲突的短退避重试参数（测试可 monkeypatch 归零，不依赖真实时钟）
_SWAP_ATTEMPTS = 3
_SWAP_BACKOFF = 0.2

# 串行化「换 .prev / 写二进制 / 更新 KV 清单」与回滚交换（B6.5）：两个管理员并发
# 上传会互相踩 —— A 刚换完 .prev，B 又换一次，.prev 便成中间态；Windows 上换一个
# 正被下载线程读的文件还会直接失败。
# 用 asyncio.Lock 而非 threading.Lock：端点是协程，锁要跨 await（to_thread 落盘）
# 持有；阻塞式 threading.Lock 会让事件循环在第二个请求取锁时整条卡死（死锁）。
_upload_lock = asyncio.Lock()


def _bin_path(request: Request):
    return os.path.join(request.app.state.agent_bin_dir, _BIN_NAME)


def _replace_retry(src, dst):
    """os.replace + Windows 共享冲突短退避（QR1120-S14）。

    换 .prev / 上位 / 落 .rollback 期间，agent_download 可能正读着同一个文件：
    POSIX 读的是旧 inode，rename 天然安全（Agent 侧 sha 不符会自愈进退避）；
    Windows 上 os.replace 撞正在读的文件会 PermissionError（共享冲突），短退避
    重试给下载线程让出窗口，仍失败才报错。**不给下载路径加锁**——500 台并发
    下载不能被一次上传串行化。只重试 PermissionError：其余 OSError 是真故障。
    """
    for attempt in range(_SWAP_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt >= _SWAP_ATTEMPTS - 1:
                raise
            time.sleep(_SWAP_BACKOFF)


def _feed(f, chunk, sha, mac):
    """分块写盘并喂增量哈希（工作线程里跑，不占事件循环）。"""
    f.write(chunk)
    sha.update(chunk)
    if mac is not None:
        mac.update(chunk)


def _install(dest, tmp_path):
    """把上传临时文件原子上位，并把上一版留成 .prev。返回 (had_prev, prev_err)。

    QR1120-S15：.prev 保留失败不再静默吞掉——prev_err 交给调用方落 warning +
    审计（agent_prev_lost），否则回滚能力悄悄消失、下次 rollback 404 无从排查。
    """
    had_prev = False
    prev_err = None
    if os.path.exists(dest):  # 保留上一版，便于回滚
        try:
            # 必须用 os.replace（覆盖语义跨平台一致）：shutil.move 落到
            # os.rename，在 Windows 上遇到已存在的 .prev 会 FileExistsError。
            _replace_retry(dest, dest + ".prev")
            had_prev = True
        except OSError as e:
            prev_err = e
    # 临时文件与 dest 同目录（同文件系统）：os.replace 是原子改名，比 open("wb")
    # 直写更稳——不会留下「写了一半」的二进制，Windows 共享冲突也有重试兜底。
    _replace_retry(tmp_path, dest)
    if os.name == "posix":
        os.chmod(dest, 0o755)
    return had_prev, prev_err


@router.post("/agent")
async def upload_agent(request: Request, user: AdminUser,
                       file: UploadFile = File(...), version: str = Form(...)):
    # QR1120-S17：全匹配白名单，"1abc"/"1..2"/"1.0\n" 一律拒绝（旧校验只查首字符）
    if not version or not _VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="version 非法")

    bin_dir = request.app.state.agent_bin_dir
    dest = os.path.join(bin_dir, _BIN_NAME)
    # HMAC 签名密钥（QR-P0-1）：配了它清单才带 sig，Agent 推送路径强制验签，
    # 与 Agent 侧 KK_UPDATE_HMAC_KEY 保持一致
    key = (getattr(request.app.state.settings, "update_hmac_key", "") or "").strip()

    # QR1120-S13：流式落盘到二进制目录的临时文件 + 增量 sha256/HMAC——整包不再
    # 驻留内存（旧实现 bytearray 累到 64MB 后又 bytes() 复制一份算哈希，峰值 ~128MB）。
    # 64MB 上限在流式过程中强制：超限即断，finally 清理临时文件。
    await asyncio.to_thread(os.makedirs, bin_dir, exist_ok=True)
    fd, tmp_path = await asyncio.to_thread(
        tempfile.mkstemp, prefix=".kk-upload-", suffix=".tmp", dir=bin_dir)
    sha = hashlib.sha256()
    mac = hmac.new(key.encode(), digestmod=hashlib.sha256) if key else None
    total = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BIN_BYTES:
                    raise HTTPException(status_code=413, detail="二进制过大")
                # 写盘与哈希都丢工作线程：上传期间心跳与其他请求不被卡住（QR-S10）
                await asyncio.to_thread(_feed, f, chunk, sha, mac)
        if total < 1:
            raise HTTPException(status_code=400, detail="二进制为空")

        # 上一版的**清单**要和二进制一起留下来（B2）：回滚时无从反推旧版本号，
        # 而 sha256 必须对得上 .prev 文件，否则 Agent 下载后会校验失败。
        store = request.app.state.store
        async with _upload_lock:
            # 读旧清单 → 换 .prev 并落盘 → 写新清单，三步必须整体原子（B6.5）
            prev_info = await store.get_agent_latest()
            had_prev, prev_err = await asyncio.to_thread(_install, dest, tmp_path)
            info = {"version": version, "sha256": sha.hexdigest(), "size": total,
                    # uploaded_at 随清单一起留痕（D1.2）：管理员要能回答「这个版本是什么时候传的」。
                    # 回滚时它随清单一起互换，语义是「这个版本被上传的时刻」，正确。
                    "uploaded_at": int(time.time())}
            if mac is not None:
                # sig 随清单走：推送（bridge.publish_update）与轮询（/agent/latest）都带上，
                # Agent 侧用二进制本体验 HMAC（QR-P0-1）
                info["sig"] = mac.hexdigest()
            # 顺序要紧：先落盘成功，再写 KV 清单 —— 否则清单可能指向尚未写完的字节
            await store.set_agent_latest(info)
            if had_prev and prev_info:
                await store.set_agent_prev(prev_info)
            elif not had_prev:
                await store.kv_set("agent_prev", "")   # 首次上传：清掉可能的历史残留
            if prev_err is not None:
                # QR1120-S15：回滚能力受损必须留痕（日志 + 审计），不许静默
                log.warning("保留上一版二进制失败，回滚能力受损：%s", prev_err)
                await store.add_audit(user, "agent_prev_lost", {
                    "version": version, "path": dest + ".prev",
                    "reason": str(prev_err)})
            await store.add_audit(user, "agent_upload", info)
        return {"ok": True, **info}
    finally:
        # 成功路径临时文件已被 os.replace 改名走；拒绝/异常路径必须清理残留
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.post("/agent/rollback")
async def rollback_agent(request: Request, user: AdminUser):
    """把服务端**待分发**的 Agent 二进制回滚到上一版（B2 / P2-7）。

    语义边界（不说清就会被当成「一键回滚全网」）：
    - 只换服务端待分发的二进制与版本清单 —— **新上线或重启的 Agent** 才会拿到旧版；
    - 已在跑的 Agent **不会**因此降级：`version_lt` 只升不降，桥接的升级推送
      不会反向触发；真要让在跑的实例回退，只能让它们重启后重新走 latest 判定；
    - 本接口是「当前 ↔ 另一版」的**互换**：再调一次即撤销回滚（换回回滚前的
      那个版本）。`agent_prev` 恒记「另一版」的清单，文件路径则看它从哪来 ——
      上传留下的是 `.prev`，回滚留下的是 `.rollback`，两者都被认作候选。
    """
    store = request.app.state.store
    dest = _bin_path(request)

    async with _upload_lock:   # 与上传共用一把锁：并发上传/回滚同样会踩 .prev/.rollback
        return await _do_rollback(user, store, dest)


async def _do_rollback(user, store, dest):
    """回滚的临界区（调用方须持 _upload_lock）。"""
    prev_info = await store.get_agent_prev()
    alt = dest + ".prev" if os.path.isfile(dest + ".prev") else dest + ".rollback"
    if not os.path.isfile(alt) or not prev_info:
        raise HTTPException(status_code=404, detail="没有可回滚的版本")
    cur_info = await store.get_agent_latest()

    def _swap():
        # 顺序要紧：当前版先暂存到 .swap，再让另一版上位，最后把暂存的当前版
        # 落为 .rollback（新备用）。若直接 replace(dest, ".rollback")，而备用
        # 恰好就叫 .rollback，会在搬走之前把它覆盖掉 —— 回滚一次后就没得撤了。
        # os.replace 的覆盖语义跨平台一致，Windows 上不会因目标已存在而失败；
        # 但会因目标正被下载线程读而 PermissionError —— _replace_retry 短退避
        # 重试兜底（QR1120-S14），下载路径不加锁。
        if os.path.exists(dest):
            tmp = dest + ".swap"
            _replace_retry(dest, tmp)
            _replace_retry(alt, dest)
            _replace_retry(tmp, dest + ".rollback")
        else:
            _replace_retry(alt, dest)
        if os.name == "posix":
            os.chmod(dest, 0o755)

    await asyncio.to_thread(_swap)
    await store.set_agent_latest(prev_info)
    if cur_info:
        await store.set_agent_prev(cur_info)
    await store.add_audit(user, "agent_rollback", {
        "from_version": (cur_info or {}).get("version", ""),
        "to_version": prev_info.get("version", ""),
    })
    return {"ok": True, **prev_info}


def _download_url(request: Request) -> str:
    """下发给 Agent 的下载地址（拼接收口在 helpers.agent_download_url，QR1128-S4）。"""
    return agent_download_url(getattr(request.app.state.settings, "public_url", ""))


@router.get("/updates")
async def list_updates(request: Request, limit: int = 50):
    """自更新台账（A6.2）：回答「这次上传的新版本，500 台升了多少、失败多少」。

    各状态计数 + 最近明细（含失败原因），逐台可核验。
    """
    store = request.app.state.store
    limit = clamp(int(limit or 50), 1, 500)
    items, summary = await asyncio.gather(
        store.list_updates(limit=limit), store.updates_summary())
    return {"items": items, "summary": summary, "limit": limit}


@router.get("/agent/current")
async def agent_current(request: Request):
    """服务端当前待分发版本 + 落后主机数（D1.2，**管理员会话**）。

    与 Agent 用的 `/agent/latest` 刻意分成两个端点，不复用：
    - 鉴权语义不同（这里是管理员会话，那边是 `KK_AGENT_IPS` 源 IP 白名单）；
    - 回答的问题不同（这里答「最新是什么、有多少台落后」，那边答「**我这个版本**
      要不要升」）。

    500 台规模下「最新是什么版本」原先对运维完全不可见：`/agent/latest` 只回
    `{available}`，客户端无从得知版本号，也就无法表达「升级到最新版本」。
    """
    store = request.app.state.store
    latest = await store.get_agent_latest() or {}
    hosts_total, versions = await asyncio.gather(store.count_containers(),
                                                store.agent_version_counts())
    ver = latest.get("version", "")
    uploaded_at = int(latest.get("uploaded_at") or 0)
    if not uploaded_at:
        # 加字段之前上传的版本没记时刻：退回二进制的 mtime（拿不到就 0，不抛）
        try:
            uploaded_at = int(os.path.getmtime(_bin_path(request)))
        except OSError:
            uploaded_at = 0
    return {
        "version": ver,
        "sha256": latest.get("sha256", ""),
        "size": latest.get("size", 0),
        "uploaded_at": uploaded_at,
        "hosts_total": hosts_total,
        "hosts_outdated": count_outdated(versions, ver),
    }


@agent_router.get("/agent/latest")
async def agent_latest(request: Request, ver: str = ""):
    """Agent 轮询路径：「**现在**该不该升级」。

    响应恒带 `policy` 自描述（D2.1）：`manual` 模式下答案是「否」，这是策略而非故障 ——
    没有这个字段，排障时会把「自动升级被策略关闭」误读成端点坏了。

    关轮询**不需要改 Agent 一行**：老版本 Agent 读到 `available=false` 就什么都不做，
    于是服务端单侧改动就建立起一个全网点，避免「要改行为先得升级全网」的鸡生蛋困境。
    """
    store = request.app.state.store
    # 归一收口在 config.normalize_update_mode（QR1128-S4）：与 settings 层、
    # 桥接 _update_mode 共用同一份「写错回落 manual」的安全裁决。
    policy = normalize_update_mode(getattr(request.app.state.settings, "update_mode", "manual"))
    if policy != "auto":
        return JSONResponse({"available": False, "policy": policy})
    latest = await store.get_agent_latest()
    if not latest:
        return JSONResponse({"available": False, "policy": policy})
    if not version_lt(ver or "", latest.get("version", "")):
        return JSONResponse({"available": False, "policy": policy})
    return {
        "available": True,
        "policy": policy,
        "version": latest["version"],
        "sha256": latest.get("sha256", ""),
        "size": latest.get("size", 0),
        "sig": latest.get("sig", ""),
        "url": _download_url(request),
    }


class UpgradeBody(BaseModel):
    hosts: list[str]
    version: str = ""      # 省略/空 = 用当前最新；服务端单槽位，指定别的版本即 bad_version


@router.post("/agent/upgrade")
async def upgrade_hosts(body: UpgradeBody, request: Request, user: AdminUser):
    """受控批量升级（D2.2）：把选中的主机升到服务端当前最新版本。

    需求②的落地：原先只有「Agent 自己轮询」与「服务端在 status 帧无差别推送」两条路，
    两条都**选不了机器**（status 只在连接时发一次，上传新版本后已在线的主机根本收不到）。
    这里把决策权显式收归服务端 + 人工选机。

    每台受理的主机在 `updates` 台账写一行，并用**台账主键**作为 cmd 帧的 id（A6.2），
    于是「升级了多少、谁失败了、为什么」全都能逐台核验。

    离线主机**照常受理**：Broker 的持久会话会为它排队（QoS1），台账记 `queued`，重连时
    由桥接补投。对运维的说法应是「已排队，重连即升」，UI 也必须如实说明 —— 把离线记成
    失败会让人以为要重试，实际上什么都不用做。

    查询次数与主机数无关：版本、在途台账、在线集合各一次（500 台不留 N+1）。
    """
    store = request.app.state.store
    bridge = request.app.state.bridge

    # 同一批里重复传同一台按一次算（保持传入顺序，便于前端逐条对齐）
    hosts = list(dict.fromkeys(h.strip() for h in (body.hosts or []) if h and h.strip()))
    if not hosts:
        raise HTTPException(status_code=400, detail="hosts 不能为空")
    # 随机尾缀防同秒同数量批次撞 id（QR1128-S13，与 upgrades uid 的 QR-S46 同型）
    batch_id = "ug-%d-%d-%s" % (int(time.time()), len(hosts), secrets.token_hex(3))

    latest = await store.get_agent_latest()
    if not latest:
        return {"ok": True, "batch_id": batch_id, "accepted": [],
                "skipped": [{"host": h, "reason": "no_binary"} for h in hosts]}
    target = (body.version or "").strip() or latest.get("version", "")
    if target != latest.get("version"):
        # 单槽位存储：不存在的版本号只能是运维敲错了
        return {"ok": True, "batch_id": batch_id, "accepted": [],
                "skipped": [{"host": h, "reason": "bad_version"} for h in hosts]}

    versions, in_flight, online = await asyncio.gather(
        store.agent_versions(hosts), store.in_flight_pods(hosts),
        store.online_set(ONLINE_GRACE))

    accepted, skipped = [], []
    chosen = []   # (host, from_ver, is_online)：通过全部分流的主机，保持传入顺序
    for host in hosts:
        if host not in versions:
            skipped.append({"host": host, "reason": "not_found"})
            continue
        from_ver = versions[host]
        if not version_lt(from_ver, target):
            skipped.append({"host": host, "reason": "already_latest"})
            continue
        if host in in_flight:
            # 没有台账就只能靠版本比较猜，必然重复下发与重复下载（8–12MB/台）
            skipped.append({"host": host, "reason": "in_flight"})
            continue
        if bridge is None:
            # 未配 Broker 的只读部署：明说而不是静默受理
            skipped.append({"host": host, "reason": "no_broker"})
            continue
        chosen.append((host, from_ver, host in online))

    # QR1120-S16：台账一次建完，不再逐台 dispatch_upgrade（每台一次 create_update
    # 事务，500 台 = 500 次串行 DB 往返挤在一个 HTTP 请求里）。create_updates_batch
    # 只吃单一 from_version/status，故按 (from_version, 在线态) 分组——组数 = 版本数×2，
    # 与主机数无关。publish 循环保留逐台：MQTT 发布是 paho 非阻塞入队，本身廉价，
    # 且 publish_update 自带「失败落 failed + 日志 + 统计」语义，与 dispatch_upgrade
    # 完全一致（离线只写 queued 不投帧，重连由桥接补投）。
    if chosen:
        groups = {}
        for host, from_ver, is_online in chosen:
            groups.setdefault((from_ver, is_online), []).append(host)
        uid_by_host = {}
        for (from_ver, is_online), group in groups.items():
            ids = await store.create_updates_batch(
                group, from_ver, target, status="pending" if is_online else "queued")
            uid_by_host.update(zip(group, ids))
        # 下行通道一次请求内取一次即可（QR1128-S5）：逐台 new RoutingDownlink
        # 是纯浪费；get_downlink 的「不缓存」纪律针对的是跨请求/夹具换 bridge。
        downlink = get_downlink(request)
        for host, from_ver, is_online in chosen:
            uid = uid_by_host[host]
            if is_online:
                # QR1125：经 get_downlink（Phase 0 升级帧仍走 MQTT，SSE 升级留 Phase 1+）
                await downlink.publish_update(host, uid, latest)
            else:
                log.bind(host=host, cmd=uid).info(
                    "upgrade queued（主机离线，重连后补投）-> %s", target)
            accepted.append({"host": host, "from_version": from_ver, "to_version": target,
                             "ledger_id": uid, "queued": not is_online})

    if accepted:
        await store.add_audit(user, "agent_upgrade_batch", {
            "batch_id": batch_id, "to_version": target,
            "accepted": [a["host"] for a in accepted], "skipped": skipped})
    return {"ok": True, "batch_id": batch_id, "accepted": accepted, "skipped": skipped}


@agent_router.get("/agent/download")
async def agent_download(request: Request, ip: AgentIp):
    latest = await request.app.state.store.get_agent_latest()
    dest = _bin_path(request)
    if not latest or not os.path.isfile(dest):
        raise HTTPException(status_code=404, detail="no agent binary")
    # 二进制属批量资产外带（8~12MB），下载留痕可追溯（QR-S7）
    await request.app.state.store.add_audit(
        "agent", "agent_binary_download", {"ip": ip, "version": latest.get("version", "")})

    def gen():
        with open(dest, "rb") as f:
            while True:
                b = f.read(_CHUNK)
                if not b:
                    break
                yield b

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="%s"' % _BIN_NAME},
    )
