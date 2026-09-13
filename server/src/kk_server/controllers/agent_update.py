"""Agent 自更新接口：

- POST /api/system/agent       管理员上传新版本二进制（multipart: file + version）
- POST /api/system/agent/rollback 回滚待分发二进制到上一版（只影响重启的 Agent）
- GET  /api/system/agent/current  服务端当前待分发版本 + 落后主机数（管理员可读）
- GET  /api/system/agent/latest   Agent 查询最新版本清单（落后才 available）
- GET  /api/system/agent/download Agent 下载二进制（流式）

安全（v3）：
- 上传需管理员会话；下载/查询按请求方真实源 IP 校验 KK_AGENT_IPS 白名单
- 服务端记录 sha256，Agent 端下载后校验一致才替换，防损坏/篡改
- 二进制按平台单槽位（kk-agent），多架构需另行扩展
"""
import asyncio
import hashlib
import json
import os
import time
from typing import List

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .deps import agent_ip_auth, current_user
from ..models.tables import ONLINE_GRACE
from ..models.version import count_outdated, version_lt

router = APIRouter(prefix="/api/system")

MAX_BIN_BYTES = 64 * 1024 * 1024
_BIN_NAME = "kk-agent"
_CHUNK = 256 * 1024

# 串行化「换 .prev / 写二进制 / 更新 KV 清单」与回滚交换（B6.5）：两个管理员并发
# 上传会互相踩 —— A 刚换完 .prev，B 又换一次，.prev 便成中间态；Windows 上换一个
# 正被下载线程读的文件还会直接失败。
# 用 asyncio.Lock 而非 threading.Lock：端点是协程，锁要跨 await（to_thread 落盘）
# 持有；阻塞式 threading.Lock 会让事件循环在第二个请求取锁时整条卡死（死锁）。
_upload_lock = asyncio.Lock()


def _bin_path(request: Request):
    return os.path.join(request.app.state.agent_bin_dir, _BIN_NAME)


@router.post("/agent")
async def upload_agent(request: Request, file: UploadFile = File(...), version: str = Form(...)):
    user = await current_user(request)
    if not version or not version[0].isdigit():
        raise HTTPException(status_code=400, detail="version 非法")

    # 边读边累计，超限即断：避免先 `await file.read()` 把整包（最大 64MB）一次性读入内存
    data = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_BIN_BYTES:
            raise HTTPException(status_code=413, detail="二进制过大")
    if len(data) < 1:
        raise HTTPException(status_code=400, detail="二进制为空")

    bin_dir = request.app.state.agent_bin_dir
    dest = os.path.join(bin_dir, _BIN_NAME)

    def _write():
        """最大 64MB 的同步写不要占住事件循环——否则上传时所有心跳与请求都被卡住。"""
        os.makedirs(bin_dir, exist_ok=True)
        had_prev = False
        if os.path.exists(dest):  # 保留上一版，便于回滚
            try:
                # 必须用 os.replace（覆盖语义跨平台一致）：shutil.move 落到
                # os.rename，在 Windows 上遇到已存在的 .prev 会 FileExistsError，
                # 被下面的 except 吞掉 → 第二次上传起「上一版」静默不保留。
                os.replace(dest, dest + ".prev")
                had_prev = True
            except OSError:
                pass
        with open(dest, "wb") as f:
            f.write(data)
        if os.name == "posix":
            os.chmod(dest, 0o755)
        return had_prev

    # 上一版的**清单**要和二进制一起留下来（B2）：回滚时无从反推旧版本号，
    # 而 sha256 必须对得上 .prev 文件，否则 Agent 下载后会校验失败。
    store = request.app.state.store
    async with _upload_lock:
        # 读旧清单 → 换 .prev 并落盘 → 写新清单，三步必须整体原子（B6.5）
        prev_info = await store.get_agent_latest()
        had_prev = await asyncio.to_thread(_write)
        sha = hashlib.sha256(data).hexdigest()
        info = {"version": version, "sha256": sha, "size": len(data),
                # uploaded_at 随清单一起留痕（D1.2）：管理员要能回答「这个版本是什么时候传的」。
                # 回滚时它随清单一起互换，语义是「这个版本被上传的时刻」，正确。
                "uploaded_at": int(time.time())}
        # 顺序要紧：先落盘成功，再写 KV 清单 —— 否则清单可能指向尚未写完的字节
        await store.set_agent_latest(info)
        if had_prev and prev_info:
            await store.set_agent_prev(prev_info)
        elif not had_prev:
            await store.kv_set("agent_prev", "")   # 首次上传：清掉可能的历史残留
        await store.add_audit(user, "agent_upload", info)
    return {"ok": True, **info}


@router.post("/agent/rollback")
async def rollback_agent(request: Request):
    """把服务端**待分发**的 Agent 二进制回滚到上一版（B2 / P2-7）。

    语义边界（不说清就会被当成「一键回滚全网」）：
    - 只换服务端待分发的二进制与版本清单 —— **新上线或重启的 Agent** 才会拿到旧版；
    - 已在跑的 Agent **不会**因此降级：`version_lt` 只升不降，桥接的升级推送
      不会反向触发；真要让在跑的实例回退，只能让它们重启后重新走 latest 判定；
    - 本接口是「当前 ↔ 另一版」的**互换**：再调一次即撤销回滚（换回回滚前的
      那个版本）。`agent_prev` 恒记「另一版」的清单，文件路径则看它从哪来 ——
      上传留下的是 `.prev`，回滚留下的是 `.rollback`，两者都被认作候选。
    """
    user = await current_user(request)
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
        # os.replace 的覆盖语义跨平台一致，Windows 上不会因目标已存在而失败。
        if os.path.exists(dest):
            tmp = dest + ".swap"
            os.replace(dest, tmp)
            os.replace(alt, dest)
            os.replace(tmp, dest + ".rollback")
        else:
            os.replace(alt, dest)
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
    """下发给 Agent 的下载地址：配了 KK_PUBLIC_URL 就给绝对地址（A6.1）。

    MQTT 化之后 Agent 无法从 broker 地址推导 HTTP API 地址，而镜像构建脚本不烧入
    KK_UPDATE_URL —— 相对地址会让推送式更新静默失效（Agent 侧只记一条 info 日志）。
    绝对地址让镜像侧零配置即可升级。
    """
    base = getattr(request.app.state.settings, "public_url", "") or ""
    path = "/api/system/agent/download"
    return base + path if base else path


@router.get("/updates")
async def list_updates(request: Request, limit: int = 50):
    """自更新台账（A6.2）：回答「这次上传的新版本，500 台升了多少、失败多少」。

    各状态计数 + 最近明细（含失败原因），逐台可核验。
    """
    await current_user(request)
    store = request.app.state.store
    limit = min(max(int(limit or 50), 1), 500)
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
    await current_user(request)
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


@router.get("/agent/latest")
async def agent_latest(request: Request, ver: str = ""):
    """Agent 轮询路径：「**现在**该不该升级」。

    响应恒带 `policy` 自描述（D2.1）：`manual` 模式下答案是「否」，这是策略而非故障 ——
    没有这个字段，排障时会把「自动升级被策略关闭」误读成端点坏了。

    关轮询**不需要改 Agent 一行**：老版本 Agent 读到 `available=false` 就什么都不做，
    于是服务端单侧改动就建立起一个全网点，避免「要改行为先得升级全网」的鸡生蛋困境。
    """
    await agent_ip_auth(request)
    store = request.app.state.store
    policy = (getattr(request.app.state.settings, "update_mode", "manual")
              or "manual").strip().lower()
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
        "url": _download_url(request),
    }


class UpgradeBody(BaseModel):
    hosts: List[str]
    version: str = ""      # 省略/空 = 用当前最新；服务端单槽位，指定别的版本即 bad_version


@router.post("/agent/upgrade")
async def upgrade_hosts(body: UpgradeBody, request: Request):
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
    user = await current_user(request)
    store = request.app.state.store
    bridge = request.app.state.bridge

    # 同一批里重复传同一台按一次算（保持传入顺序，便于前端逐条对齐）
    hosts = list(dict.fromkeys(h.strip() for h in (body.hosts or []) if h and h.strip()))
    if not hosts:
        raise HTTPException(status_code=400, detail="hosts 不能为空")
    batch_id = "ug-%d-%d" % (int(time.time()), len(hosts))

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
        is_online = host in online
        uid = await bridge.dispatch_upgrade(host, from_ver, latest, online=is_online)
        accepted.append({"host": host, "from_version": from_ver, "to_version": target,
                         "ledger_id": uid, "queued": not is_online})

    if accepted:
        await store.add_audit(user, "agent_upgrade_batch", {
            "batch_id": batch_id, "to_version": target,
            "accepted": [a["host"] for a in accepted], "skipped": skipped})
    return {"ok": True, "batch_id": batch_id, "accepted": accepted, "skipped": skipped}


@router.get("/agent/download")
async def agent_download(request: Request):
    await agent_ip_auth(request)
    latest = await request.app.state.store.get_agent_latest()
    dest = _bin_path(request)
    if not latest or not os.path.isfile(dest):
        raise HTTPException(status_code=404, detail="no agent binary")

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
