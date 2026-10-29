"""真实故障路径用例（模块：RES）。

这一模块验证的不是「功能能用」，而是「出事以后能不能自己回来」——四条真故障：

1. **掉线判定（LWT）**：`kill -9` 掉一台 Agent，Broker 按遗嘱发布 offline，服务端
   在线数立刻少一台；这不是 Agent 自己发的帧，验证的是「Broker 代发 + 服务端落库」。
2. **离线排队补投**：主机离线时下发升级，台账记 `queued` 且**不发帧**；主机重新连上
   后由桥接补投，最终仍靠状态帧佐证收敛为 `done`。
3. **Broker 重启**：整个消息总线断一次。REST 必须不受影响（数据在库里），Agent 靠
   paho 自动重连，同一批进程不需要重启，心跳与在线状态自己恢复。
4. **服务端重启**：会话落在 SQLite 里，重启后旧 token 仍有效、数据不丢、心跳续传；
   伪造 token 仍然 401。
5. **优雅退出的 reason 可辨**：SIGTERM 走 Agent 自己的 stop 路径（先发 offline 再断），
   与 LWT 的空 reason 区分开 —— 运维据此判断「正常停机」还是「异常掉线」。

顺序依赖：必须在 upgrade_cases 之后（那边把待分发版本钉回 0.4.0，这里才有可排队的
目标；且 host_b 仍是 0.3.0，正好用来演离线排队）。
"""
from __future__ import annotations

import time

from .core import Env, api, check, login, wait_until, wsl_stdin
from .setup import NEW_VER, OLD_VER, Stack
from .upgrade_cases import _container, _ledger, _upgrade

MOD = "RES"


def _summary(env: Env, **params) -> dict:
    st, body = api(env, "GET", "/api/containers", params=dict(view="summary", **params))
    return body if st == 200 else {}


def _health(env: Env) -> dict:
    st, body = api(env, "GET", "/api/health", token="")
    return body if st == 200 else {}


def _broker_stats(env: Env) -> dict:
    st, body = api(env, "GET", "/api/system/stats")
    return ((body.get("broker") or {}).get("stats") or {}) if st == 200 else {}


def _fleet_online(env: Env) -> bool:
    """三台都在线（只判 online，不要求 mem_mb：这里只验证「掉线前的基线」）。"""
    st, body = api(env, "GET", "/api/containers", params={"view": "summary"})
    items = body.get("items") or []
    return (st == 200 and len(items) == len(env.agents)
            and all(i.get("online") for i in items))


# ---------------------------------------------------------------- 用例
def case_lwt_offline(env: Env, stack: Stack, host: str) -> None:
    """kill -9 → 遗嘱 offline → 在线数、离线筛选、健康检查三处同时反映。"""
    before = _container(env, host)
    stack.stop_agent(host)
    gone = bool(wait_until(lambda: _container(env, host).get("online") is False, 60, 1.0))
    row = _container(env, host)
    tot = _summary(env)
    off = _summary(env, online="false")
    h = _health(env)
    pid = stack.agent_pid(host)
    ok = (before.get("online") is True and gone and row.get("online") is False
          and row.get("status_reason") in ("", None)      # LWT 不带 reason
          and tot.get("total") == len(env.agents)
          and tot.get("online") == len(env.agents) - 1
          and off.get("total") == 1
          and [i.get("pod") for i in off.get("items") or []] == [host]
          and h.get("agents_online") == len(env.agents) - 1
          and not pid)
    check("RES-01", MOD, "掉线判定：kill -9 后遗嘱 offline 生效，在线数/筛选/健康检查同步",
          ok, "掉线前 online=%s → 掉线后 online=%s reason=%r 在线数=%s/%s 离线筛选=%s "
              "health=%s pid=%r"
          % (before.get("online"), row.get("online"), row.get("status_reason"),
             tot.get("online"), tot.get("total"), off.get("total"),
             h.get("agents_online"), pid),
          precond="%s 在线且为 0.3.0；服务端待分发版本为 %s" % (host, NEW_VER),
          steps="kill -9 Agent（不走优雅退出）→ 轮询 /api/containers/{pod}.online → "
                "查 summary / online=false 筛选 / /api/health",
          expect="online=false 且 status_reason 为空；在线数 3→2；离线筛选命中该主机；"
                 "health.agents_online=2；进程已不存在")


def case_queued_upgrade(env: Env, stack: Stack, host: str, sha: str) -> dict:
    """离线主机照样受理升级：先 queued、重连补投、状态帧佐证后 done。"""
    st, body = _upgrade(env, [host])
    acc = (body.get("accepted") or [{}])[0] if st == 200 else {}
    uid = acc.get("ledger_id", "")
    row0 = _ledger(env, uid) if uid else {}
    flushed0 = _broker_stats(env).get("upgrade_flushed", 0)

    # 离线窗口内不该有任何进展：台账 queued、版本不动
    time.sleep(4)
    row1 = _ledger(env, uid) if uid else {}
    ver_offline = _container(env, host).get("agent_ver")
    disk_offline = stack.agent_binary_sha(host)

    stack.start_agent(host)

    def _reached():
        row = _ledger(env, uid) if uid else {}
        return (row.get("status") == "done"
                and _container(env, host).get("agent_ver") == NEW_VER
                and stack.agent_version(host) == NEW_VER)

    t0 = time.time()
    reached = bool(wait_until(_reached, 180, 2.0))
    secs = round(time.time() - t0, 1)
    row2 = _ledger(env, uid) if uid else {}
    disk = stack.agent_binary_sha(host)
    flushed1 = _broker_stats(env).get("upgrade_flushed", 0)
    ok = (st == 200 and acc.get("queued") is True
          and row0.get("status") == "queued"
          and row1.get("status") == "queued"
          and ver_offline == OLD_VER and disk_offline != sha
          and reached and disk == sha
          and row2.get("from_version") == OLD_VER
          and row2.get("to_version") == NEW_VER
          and flushed1 > flushed0)
    check("RES-02", MOD, "离线排队补投：%s 离线时下发→queued 不发帧，重连后补投并升到 %s"
          % (host, NEW_VER), ok,
          "accepted.queued=%s 台账 %s→%s 离线期版本=%s 重连后=%s(%.1fs) 磁盘sha对齐=%s "
          "upgrade_flushed %s→%s"
          % (acc.get("queued"), row0.get("status"), row1.get("status"), ver_offline,
             row2.get("status"), secs, disk == sha, flushed0, flushed1),
          precond="%s 已被 kill -9；服务端最新版本 %s" % (host, NEW_VER),
          steps="POST /agent/upgrade {hosts:[离线主机]} → 观察台账与版本 4s → "
                "启动 Agent → 轮询台账/版本/进程 AGENT_VER",
          expect="accepted.queued=true；台账 queued 且离线期版本不变；重连后补投，"
                 "台账 done、版本 0.4.0、磁盘 sha256 与清单一致；upgrade_flushed 增长")
    return {"secs": secs, "uid": uid, "flushed": flushed1 - flushed0}


def case_broker_restart(env: Env, stack: Stack) -> None:
    """Broker 整个断一次：REST 不受影响，Agent 自动重连，读数自己恢复。"""
    pods = list(env.agents)
    pids = {h: stack.agent_pid(h) for h in pods}
    seen = {h: _container(env, h).get("last_seen", 0) for h in pods}
    stack.stop_broker()
    down = bool(wait_until(lambda: _health(env).get("broker") == "disconnected", 40, 1.0))
    # 总线断了，REST 必须照常可用（数据在库里，不依赖 MQTT）
    st_l, b_l = api(env, "GET", "/api/containers", params={"view": "summary"})
    stack.start_broker()
    up = bool(wait_until(
        lambda: (lambda h: h.get("broker") == "connected"
                 and h.get("agents_online") == len(pods))(_health(env)), 180, 2.0))
    adv = bool(wait_until(
        lambda: all(_container(env, h).get("last_seen", 0) > seen[h] for h in pods),
        90, 2.0))
    pids_after = {h: stack.agent_pid(h) for h in pods}
    ok = (down and up and adv
          and st_l == 200 and b_l.get("total") == len(pods)
          and pids_after == pids and all(pids.values()))
    check("RES-03", MOD, "Broker 重启：REST 不受影响，3 台 Agent 自动重连（进程未重启）",
          ok, "断连感知=%s 停机期REST=%s/%s 恢复=%s 心跳前进=%s pid不变=%s"
          % (down, st_l, b_l.get("total"), up, adv, pids_after == pids),
          precond="3 台 Agent 在线且持续心跳（KK_INTERVAL=3）",
          steps="停 Broker → 等 /api/health.broker=disconnected → 期间读 /api/containers → "
                "起 Broker → 等 connected 且 3 台在线 → 比较 last_seen 与 pid",
          expect="断连可感知；停机期 REST 仍返回 3 台；恢复后 agents_online=3、"
                 "last_seen 前进、pid 完全相同（靠重连而非重启）")


def case_server_restart(env: Env, stack: Stack) -> None:
    """服务端重启：会话与数据都在库里，旧 token 仍有效，心跳续传。"""
    pod = env.agents[0]
    tok = env.token
    st_me1, me1 = api(env, "GET", "/api/me")
    st_c1, c1 = api(env, "GET", "/api/containers", params={"view": "summary"})
    st_a1, a1 = api(env, "GET", "/api/audit", params={"limit": 1})
    ver1 = {h: _container(env, h).get("agent_ver") for h in env.agents}
    seen1 = _container(env, pod).get("last_seen", 0)

    stack.restart_server()

    st_me2, me2 = api(env, "GET", "/api/me")      # 刻意沿用在重启前的 token
    st_c2, c2 = api(env, "GET", "/api/containers", params={"view": "summary"})
    st_a2, a2 = api(env, "GET", "/api/audit", params={"limit": 1})
    st_bad, _ = api(env, "GET", "/api/me", token="forged-token-after-restart")
    ver2 = {h: _container(env, h).get("agent_ver") for h in env.agents}
    resumed = bool(wait_until(
        lambda: _container(env, pod).get("last_seen", 0) > seen1, 60, 2.0))
    relog = login(env)

    ok = (st_me1 == 200 and me1.get("username") == env.admin_user
          and st_me2 == 200 and me2.get("username") == me1.get("username")
          and st_bad == 401
          and st_c1 == st_c2 == 200 and c1.get("total") == c2.get("total") == len(env.agents)
          and ver1 == ver2
          and st_a1 == st_a2 == 200 and a2.get("total", 0) >= a1.get("total", 0) > 0
          and resumed and bool(relog))
    check("RES-04", MOD, "服务端重启：旧会话仍有效、数据不丢、心跳续传、伪造 token 仍 401",
          ok, "me=%s/%s→%s/%s 伪造=%s 主机 %s→%s 版本一致=%s 审计 %s→%s 心跳续传=%s 重登=%s"
          % (st_me1, me1.get("username"), st_me2, me2.get("username"), st_bad,
             c1.get("total"), c2.get("total"), ver1 == ver2,
             a1.get("total"), a2.get("total"), resumed, bool(relog)),
          precond="已有会话、3 台主机、审计与心跳数据",
          steps="记录 token/主机数/审计数/版本 → 重启服务端进程 → 用旧 token 读 /api/me、"
                "/api/containers、/api/audit → 伪造 token → 等心跳续传 → 重新登录",
          expect="旧 token 200 且身份不变；伪造 token 401；主机数与版本不变；审计只增不减；"
                 "last_seen 继续前进；重新登录成功")


def case_graceful_stop(env: Env, stack: Stack, host: str) -> None:
    """SIGTERM 走 Agent 自己的收尾：reason=stopping，与 LWT 的空 reason 区分开。"""
    pid = stack.agent_pid(host)
    wsl_stdin(env, 'kill -TERM "%s" 2>/dev/null; echo sent' % pid, timeout=30)
    gone = bool(wait_until(lambda: not stack.agent_pid(host), 60, 1.0))
    row = _container(env, host)
    stack.start_agent(host)
    back = bool(wait_until(lambda: _container(env, host).get("online") is True, 60, 1.0))
    ok = (bool(pid) and gone and row.get("online") is False
          and row.get("status_reason") == "stopping" and back)
    check("RES-05", MOD, "优雅退出：SIGTERM 由 Agent 自报 reason=stopping（区别于 LWT 空 reason）",
          ok, "pid=%s 已退出=%s online=%s reason=%r 重启后在线=%s"
          % (pid, gone, row.get("online"), row.get("status_reason"), back),
          precond="%s 在线" % host,
          steps="kill -TERM Agent → 等进程退出 → 读 /api/containers/{pod}.status_reason → "
                "重新启动 Agent",
          expect="进程退出；online=false 且 status_reason=stopping；重启后重新在线")


def run(env: Env, ctx: dict | None = None) -> dict:
    stack: Stack = (ctx or {}).get("stack")
    if stack is None:
        raise RuntimeError("resilience_cases 需要 ctx['stack']")
    sha = (ctx or {}).get("upload_sha", "")
    host_b, host_c = env.agents[1], env.agents[2]

    # 前置：机队全在线。UPG-06 刚重启过 host_c，若不在这里收敛，「在线数 3→2」
    # 的基线本身不成立，红灯会指向掉线判定而不是上一模块的收尾。
    wait_until(lambda: _fleet_online(env), 60, 1.0)

    case_lwt_offline(env, stack, host_b)
    queued = case_queued_upgrade(env, stack, host_b, sha)
    case_broker_restart(env, stack)
    case_server_restart(env, stack)
    case_graceful_stop(env, stack, host_c)
    return {"queued_secs": queued.get("secs", 0), "sha": sha,
            "versions": {h: _container(env, h).get("agent_ver") for h in env.agents}}