"""真实自更新用例（模块：UPG）。

这一模块不模拟升级的任何一步：真的 multipart 上传、服务端真的算 sha256/HMAC
并落盘、Agent 真的 HTTP 下载 → 校验 → `os.replace` 替换启动器 → `os.execv`
重启 → 新进程用新版本号重新上线，服务端再按**状态帧佐证**把台账收敛为 done。

因此这里的断言都落在「可外部核验的证据」上：
- 磁盘上那支 `kk-agent` 的 sha256 与上传清单一致（真替换，不是内存里改个数字）；
- WSL 进程加载的 `AGENT_VER` 变了（读的是进程自己的 config.py）；
- 服务端 `/api/containers` 的 `agent_ver` 与升级台账的 `status/reason` 相互对上。

顺序不能调：本模块必须在 UI 模块之后（UI-08 断言「还没有待分发版本」），
且在 resilience 模块之前（那边要拿「部分主机已升到 0.4.0」的机队做离线排队）。
"""
from __future__ import annotations

import hashlib
import json
import time

from .core import Env, api, check, hdr, http, http_bytes, trunc, wait_until
from .setup import NEW_VER, OLD_VER, Stack, launcher

MOD = "UPG"
UPGRADE_PATH = "/api/system/agent/upgrade"
LEDGER_PATH = "/api/system/updates"


# ---------------------------------------------------------------- 小工具
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, dict]:
    """手工拼 multipart（urllib 没有现成工具）：`fields` 是文本项，`files` 是文件项。"""
    boundary = "----kkE2E%d" % int(time.time() * 1000)
    out = bytearray()
    for name, value in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)).encode("utf-8")
    for name, filename, content in files:
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
                % (boundary, name, filename)).encode("utf-8")
        out += content + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), {"Content-Type": "multipart/form-data; boundary=%s" % boundary}


def _upload(env: Env, version: str, data: bytes,
            filename: str = "kk-agent", timeout: float = 120) -> tuple[int, dict]:
    body, headers = _multipart({"version": version}, [("file", filename, data)])
    st, text, _ = http(env, "POST", "/api/system/agent", data=body,
                       headers=headers, timeout=timeout)
    try:
        return st, json.loads(text)
    except Exception:  # noqa: BLE001 - 非 JSON（如代理页）也是结论之一
        return st, {"_raw": trunc(text, 200)}


def _current(env: Env) -> dict:
    st, body = api(env, "GET", "/api/system/agent/current")
    return body if st == 200 else {"_status": st, "_body": body}


def _upgrade(env: Env, hosts: list[str], version: str = "") -> tuple[int, dict]:
    return api(env, "POST", UPGRADE_PATH, json_body={"hosts": hosts, "version": version})


def _ledger(env: Env, uid: str) -> dict:
    st, body = api(env, "GET", LEDGER_PATH, params={"limit": 200})
    for row in (body.get("items") or []) if st == 200 else []:
        if row.get("id") == uid:
            return row
    return {}


def _container(env: Env, pod: str) -> dict:
    st, body = api(env, "GET", "/api/containers/%s" % pod)
    return body if st == 200 else {}


def _audit_actions(env: Env, limit: int = 400) -> list[str]:
    st, body = api(env, "GET", "/api/audit", params={"limit": limit})
    return [i.get("action") for i in (body.get("items") or [])] if st == 200 else []


# ---------------------------------------------------------------- 用例
def case_negative(env: Env, stack: Stack, host: str) -> None:
    """上传/回滚的负例：全部在真正落盘之前被拒（非法版本号、空文件、超限、无可回滚）。"""
    cur = _current(env)
    st_up, b_up = _upgrade(env, [host])
    skipped = (b_up.get("skipped") or [{}])[0] if st_up == 200 else {}
    st_ver, b_ver = _upload(env, "v1.2.3", b"not-a-real-agent")
    st_empty, b_empty = _upload(env, "1.2.3", b"")
    # 65MB：Starlette 先把 multipart 收完（1MB 起溢写到临时文件），端点再按块累计，
    # 超 64MB 立即 413 —— 所以这里能稳定拿到响应，不会因为半途被拒而连接重置
    st_big, b_big = _upload(env, "1.2.3", b"\0" * (65 * 1024 * 1024))
    st_rb, b_rb = api(env, "POST", "/api/system/agent/rollback")
    ok = (cur.get("version") == "" and cur.get("hosts_outdated") == 0
          and st_up == 200 and skipped.get("reason") == "no_binary"
          and st_ver == 400 and "version 非法" in str(b_ver.get("detail", ""))
          and st_empty == 400 and "二进制为空" in str(b_empty.get("detail", ""))
          and st_big == 413 and "二进制过大" in str(b_big.get("detail", ""))
          and st_rb == 404 and "没有可回滚的版本" in str(b_rb.get("detail", "")))
    check("UPG-01", MOD, "上传/回滚负例：无二进制不升级、非法版本/空文件/超 64MB 均被拒",
          ok, "current=%r outdated=%s skip=%r v-prefix=%s/%s empty=%s big=%s rb=%s/%s"
          % (cur.get("version"), cur.get("hosts_outdated"), skipped.get("reason"),
             st_ver, trunc(b_ver.get("detail", ""), 30), st_empty, st_big,
             st_rb, trunc(b_rb.get("detail", ""), 30)),
          precond="本次运行尚未上传任何版本（agent_assets 为空）",
          steps="GET /agent/current → POST /agent/upgrade（无二进制）→ POST /agent "
                "（version=v1.2.3 / 空文件 / 65MB）→ POST /agent/rollback",
          expect="version 为空且落后 0；skipped=no_binary；400 version 非法；"
                 "400 二进制为空；413 二进制过大；404 没有可回滚的版本")


def case_upload(env: Env) -> dict:
    """上传真实二进制：清单可核验、下载字节一致、Agents 在 manual 策略下恒不轮询升级。"""
    data = env.new_agent_file.read_bytes()
    st, body = _upload(env, NEW_VER, data)
    sha = _sha(data)
    cur = _current(env)
    st_dl, got, headers = http_bytes(env, "/api/system/agent/download")
    st_latest, b_latest = api(env, "GET", "/api/system/agent/latest",
                              params={"ver": OLD_VER})
    acts = _audit_actions(env)
    ok = (st == 200 and body.get("version") == NEW_VER
          and body.get("sha256") == sha and body.get("size") == len(data)
          and body.get("uploaded_at", 0) > 0
          and cur.get("version") == NEW_VER
          and cur.get("hosts_outdated") == len(env.agents)
          and st_dl == 200 and _sha(got) == sha
          and "kk-agent" in (hdr(headers, "Content-Disposition") or "")
          and st_latest == 200 and b_latest.get("available") is False
          and b_latest.get("policy") == "manual"
          and "agent_upload" in acts and "agent_binary_download" in acts)
    check("UPG-02", MOD, "上传 0.4.0 二进制：清单 sha256/size 可核验、下载字节一致、"
                        "manual 策略下 Agent 轮询恒不可用",
          ok, "upload=%s version=%s sha=%s size=%s current=%s 落后=%s/%s "
              "download=%s sha一致=%s latest=%s/%s 审计=%s"
          % (st, body.get("version"), (body.get("sha256") or "")[:12],
             body.get("size"), cur.get("version"), cur.get("hosts_outdated"),
             cur.get("hosts_total"), st_dl, _sha(got) == sha,
             b_latest.get("available"), b_latest.get("policy"),
             [a for a in ("agent_upload", "agent_binary_download") if a in acts]),
          precond="WSL 侧 3 台 Agent 均为 0.3.0（真实源码树）",
          steps="POST /api/system/agent（multipart: file=启动器, version=0.4.0）→ "
                "GET /agent/current → GET /agent/download → GET /agent/latest?ver=0.3.0",
          expect="清单 sha256=本地 sha256、size 一致；current=0.4.0 且落后 3 台；"
                 "下载字节的 sha256 与清单一致；latest.available=false/policy=manual")
    return {"sha": sha, "size": len(data)}


def case_push(env: Env, stack: Stack, host: str, sha: str) -> dict:
    """推送式升级一台主机：真下载/替换/execv，最终以「状态帧上报新版本」收敛。"""
    st, body = _upgrade(env, [host])
    acc = (body.get("accepted") or [{}])[0] if st == 200 else {}
    uid = acc.get("ledger_id", "")

    def _reached():
        c = _container(env, host)
        row = _ledger(env, uid) if uid else {}
        return (c.get("agent_ver") == NEW_VER and row.get("status") == "done"
                and stack.agent_version(host) == NEW_VER)

    t0 = time.time()
    reached = bool(wait_until(_reached, 120, 1.5))
    secs = round(time.time() - t0, 1)
    row = _ledger(env, uid) if uid else {}
    disk_sha = stack.agent_binary_sha(host)
    c = _container(env, host)
    acts = _audit_actions(env)
    ok = (st == 200 and acc.get("from_version") == OLD_VER
          and acc.get("to_version") == NEW_VER and acc.get("queued") is False
          and reached and disk_sha == sha
          and c.get("agent_ver") == NEW_VER
          and row.get("from_version") == OLD_VER and row.get("to_version") == NEW_VER
          and "agent_upgrade_batch" in acts)
    check("UPG-03", MOD, "推送升级 %s：%s→%s（下载→sha256/HMAC→原子替换→execv），"
                        "服务端以状态帧佐证判定完成" % (host, OLD_VER, NEW_VER),
          ok, "accepted=%s 台账=%s(%.1fs) 磁盘sha==清单:%s 容器版本=%s 台账=%s/%s"
          % (trunc(acc, 160), row.get("status"), secs, disk_sha == sha,
             c.get("agent_ver"), row.get("status"), row.get("reason") or "-"),
          precond="已上传 0.4.0；%s 在线且为 0.3.0" % host,
          steps="POST /agent/upgrade {hosts:[%s]} → 轮询 /updates 台账 + /containers 版本 + "
                "WSL 进程 AGENT_VER" % host,
          expect="accepted 1 台 queued=false；台账 done；容器 agent_ver=0.4.0；"
                 "主机上 kk-agent 的 sha256 等于上传清单")
    return {"uid": uid, "secs": secs}


def case_skip(env: Env, done_host: str, host: str) -> None:
    """幂等与跳过原因：已最新 / 不存在 / 版本号写错 / 空名单，一次点击里各归各的。"""
    st1, b1 = _upgrade(env, [done_host])
    st2, b2 = _upgrade(env, ["ghost-host"])
    st3, b3 = _upgrade(env, [host], version="9.9.9")
    st4, b4 = _upgrade(env, ["  ", ""])
    r1 = (b1.get("skipped") or [{}])[0] if st1 == 200 else {}
    r2 = (b2.get("skipped") or [{}])[0] if st2 == 200 else {}
    r3 = (b3.get("skipped") or [{}])[0] if st3 == 200 else {}
    ok = (st1 == 200 and r1.get("reason") == "already_latest"
          and st2 == 200 and r2.get("reason") == "not_found"
          and st3 == 200 and r3.get("reason") == "bad_version"
          and st4 == 400 and "hosts 不能为空" in str(b4.get("detail", "")))
    check("UPG-04", MOD, "升级幂等与跳过原因：已最新 / 主机不存在 / 版本号非当前 / 空名单",
          ok, "latest=%s ghost=%s badver=%s empty=%s/%s"
          % (r1.get("reason"), r2.get("reason"), r3.get("reason"), st4,
             trunc(b4.get("detail", ""), 30)),
          precond="%s 已升到 0.4.0；服务端单槽位存储" % done_host,
          steps="POST /agent/upgrade 依次传已升级主机 / ghost-host / version=9.9.9 / 空名单",
          expect="already_latest / not_found / bad_version；空名单 400 hosts 不能为空")


def case_rollback(env: Env, stack: Stack, sha_new: str) -> dict:
    """回滚是「当前 ↔ 另一版」互换：换一次、再换一次即撤销，二进制也真跟着换。"""
    var_b = launcher(env, "0.4.1").encode("utf-8")
    st_up2, b_up2 = _upload(env, "0.4.1", var_b)
    cur1 = _current(env)
    st_rb1, b_rb1 = api(env, "POST", "/api/system/agent/rollback")
    st_dl1, got1, _ = http_bytes(env, "/api/system/agent/download")
    cur2 = _current(env)
    st_rb2, b_rb2 = api(env, "POST", "/api/system/agent/rollback")
    st_dl2, got2, _ = http_bytes(env, "/api/system/agent/download")
    cur3 = _current(env)
    acts = _audit_actions(env)
    ok = (st_up2 == 200 and cur1.get("version") == "0.4.1"
          and st_rb1 == 200 and b_rb1.get("version") == NEW_VER
          and st_dl1 == 200 and _sha(got1) == sha_new
          and cur2.get("version") == NEW_VER
          and st_rb2 == 200 and b_rb2.get("version") == "0.4.1"
          and st_dl2 == 200 and _sha(got2) == _sha(var_b)
          and cur3.get("version") == "0.4.1"
          and acts.count("agent_rollback") >= 2)
    check("UPG-05", MOD, "回滚待分发版本：两次互换回到原点，下载内容随版本一起切换",
          ok, "upload2=%s→%s rollback→%s(sha==0.4.0:%s) rollback→%s(sha==0.4.1:%s) 审计x%d"
          % (st_up2, cur1.get("version"), cur2.get("version"), _sha(got1) == sha_new,
             cur3.get("version"), _sha(got2) == _sha(var_b),
             acts.count("agent_rollback")),
          precond="当前待分发 0.4.0（有 .prev，可回滚）",
          steps="再上传 0.4.1 → POST rollback（→0.4.0）→ GET download → POST rollback"
                "（→0.4.1）→ GET download",
          expect="两次回滚版本互换正确；下载字节 sha256 分别对上 0.4.0 / 0.4.1；"
                 "审计 agent_rollback≥2 条")
    # 收尾：把待分发版本固定回 0.4.0（韧性用例要按这个目标给离线主机排队）
    data = env.new_agent_file.read_bytes()
    st_back, b_back = _upload(env, NEW_VER, data)
    return {"restored": st_back == 200 and b_back.get("version") == NEW_VER}


def case_failure(env: Env, stack: Stack, host: str) -> None:
    """失败也必须有回执：把替换目标改成 `.py`，Agent 在下载前拒绝并回传原因码。"""
    good = "%s/kk-agent" % env.agent_dir(host)
    broken = "%s/kk-broken.py" % env.agent_dir(host)
    before = _container(env, host).get("agent_ver")
    row: dict = {}
    hit: list = []
    acc: dict = {}
    st = -1
    try:
        stack.set_agent_env(host, "KK_AGENT_BIN", broken)
        stack.restart_agent(host)
        wait_until(lambda: _container(env, host).get("agent_ver") == OLD_VER, 40, 1.0)
        st, body = _upgrade(env, [host])
        acc = (body.get("accepted") or [{}])[0] if st == 200 else {}
        uid = acc.get("ledger_id", "")
        if uid:
            wait_until(lambda: (_ledger(env, uid) or {}).get("status") in ("failed", "done"),
                       90, 1.5)
            row = _ledger(env, uid)
        st_a, b_a = api(env, "GET", "/api/audit", params={"action": "agent_update_failed"})
        hit = [i for i in (b_a.get("items") or [])
               if host in str(i.get("detail", ""))] if st_a == 200 else []
        after = _container(env, host).get("agent_ver")
    finally:
        # 无论断言结果如何都要把主机恢复成可升级状态，后面的韧性用例还要用它
        stack.set_agent_env(host, "KK_AGENT_BIN", good)
        stack.restart_agent(host)
        # 还要等它**真的重新上线**再交给 RES：restart_agent 只等到 MainPID 出现，
        # 状态帧落地还有几秒；不等的话 RES-01 采样到的在线数会少一台，症状看起来
        # 像「掉线判定坏了」（实测踩过：在线数 1/3、离线筛选 2）。
        wait_until(lambda: _container(env, host).get("online") is True, 60, 1.0)
    ok = (st == 200 and acc.get("queued") is False
          and row.get("status") == "failed" and row.get("reason") == "bad_target"
          and bool(hit) and after == before == OLD_VER
          and _container(env, host).get("agent_ver") == OLD_VER)
    check("UPG-06", MOD, "升级失败留痕：替换目标不是独立二进制 → 台账 failed/bad_target + 审计",
          ok, "accepted=%s 台账=%s/%s 审计命中=%d 版本未变=%s 还原后=%s"
          % (acc.get("queued"), row.get("status"), row.get("reason"), len(hit), after,
             _container(env, host).get("agent_ver")),
          precond="%s 的 KK_AGENT_BIN 指向 kk-broken.py（形态校验必拒）" % host,
          steps="改该主机 KK_AGENT_BIN 并重启 → POST /agent/upgrade → 轮询台账与审计 → 还原",
          expect="台账 status=failed、reason=bad_target；审计 agent_update_failed 含该主机；"
                 "主机版本仍为 0.3.0")


def run(env: Env, ctx: dict | None = None) -> dict:
    stack: Stack = (ctx or {}).get("stack")
    if stack is None:
        raise RuntimeError("upgrade_cases 需要 ctx['stack']（Agent 生命周期由 setup.Stack 管）")
    host_a, host_b, host_c = env.agents[0], env.agents[1], env.agents[2]

    case_negative(env, stack, host_a)
    up = case_upload(env)
    pushed = case_push(env, stack, host_a, up.get("sha", ""))
    case_skip(env, host_a, host_b)
    rb = case_rollback(env, stack, up.get("sha", ""))
    case_failure(env, stack, host_c)
    return {"upload_sha": up.get("sha", ""), "upload_size": up.get("size", 0),
            "ledger_id": pushed.get("uid", ""),
            "upgrade_secs": pushed.get("secs", 0),
            "rollback_restored": rb.get("restored")}