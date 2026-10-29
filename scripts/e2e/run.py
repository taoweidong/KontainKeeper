"""全流程 E2E 入口：一条命令把「真实栈 + 四个模块 + 报告」跑完。

    .venv/Scripts/python.exe -m scripts.e2e.run                 # 全量
    .venv/Scripts/python.exe -m scripts.e2e.run --skip-build    # 复用 web/dist
    .venv/Scripts/python.exe -m scripts.e2e.run --only API,UPG  # 指定模块（调试用）

只能用 `-m` 形式：包内是相对导入，`python scripts/e2e/run.py` 直接跑会在
`from . import ...` 上 ImportError（这条注释就是给下次照着敲的人看的）。

编排顺序有硬约束，不是随手排的：

1. **先起 Broker 再起服务端**：服务端启动即连 Broker，顺序反了要等重连。
2. **服务端先以空 `KK_PUBLIC_URL` 起来，探明 WSL 可达基址后重启一次**：Agent 的
   下载地址由服务端在下发帧里给（`bridge._download_url()`），它读的是启动时的配置；
   而「WSL 能不能访问到 Windows 的这个端口」只能实测（mirrored 与 NAT 两种形态）。
   实测不到就连不上升级下载——此时报告里如实留空，不假装通过。
3. **先部署 Agent 再探测就绪**：上线看 status 帧、指标要等首帧心跳，两个条件都满足
   才算「机队就绪」（AGENTS.md 明示：只看 status 会在 mem_mb 为空时误判）。
4. **模块顺序 API → UI → UPG → RES**：UI-08 断言「还没有待分发版本」，UPG 上传后
   这条前提就没了；RES 又要拿 UPG 钉回的 0.4.0 与尚未升级的主机演离线排队。

失败语义：单个用例失败**不中断**（报告要一次看全）；环境级故障（preflight 硬项、
栈起不来）才中止，并把原因写进报告而不是抛栈。
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

from . import api_cases, resilience_cases, ui_cases, upgrade_cases
from .core import Env, CASES, check, login, wait_until
from .report import write
from .setup import Stack, ensure_artifacts, pick_port

MODULES = {
    "API": api_cases.run,
    "UI": ui_cases.run,
    "UPG": upgrade_cases.run,
    "RES": resilience_cases.run,
}


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KontainKeeper 真实环境全流程 E2E")
    p.add_argument("--distro", default="Ubuntu-22.04", help="WSL 发行版")
    p.add_argument("--port", type=int, default=18443, help="服务端端口（默认 18443）")
    p.add_argument("--broker-port", type=int, default=18831,
                   help="专用 Broker 端口（默认 18831，避开开发机的 1883）")
    p.add_argument("--skip-build", action="store_true", help="复用现有 web/dist")
    p.add_argument("--headed", action="store_true", help="浏览器带界面（默认 headless）")
    p.add_argument("--keep", action="store_true",
                   help="结束后保留 Broker/服务端/Agent（排查用；下次运行前会清）")
    p.add_argument("--only", default="", help="只跑指定模块，逗号分隔：API,UI,UPG,RES")
    p.add_argument("--root-wsl", default="", help="WSL 侧根目录（默认 $HOME/kk-e2e）")
    return p.parse_args(argv)


def _meta(env: Env, stack: Stack, notes: list[str]) -> dict:
    """写进报告的环境自证信息：跑的是哪套代码、哪个库、哪次构建。"""
    from .core import api
    meta: dict = {"notes": " / ".join(notes) if notes else "-"}
    try:
        st, body = api(env, "GET", "/api/health", token="")
        meta["server_version"] = body.get("version", "") if st == 200 else "?"
        meta["proto_ver"] = body.get("proto_ver", "?")
    except Exception:  # noqa: BLE001 - 报告字段缺失不影响结论
        meta["server_version"] = "?"
    try:
        meta["agent_version"] = stack.agent_version(env.agents[0]) or "?"
    except Exception:  # noqa: BLE001
        meta["agent_version"] = "?"
    return meta


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    started_at = time.time()
    notes: list[str] = []
    env = Env(distro=args.distro, skip_build=args.skip_build,
              headless=not args.headed, root_wsl=args.root_wsl)
    # 端口冲突就顺延：开发机上这些端口常年被占，硬失败会让整轮白跑
    env.port = pick_port(args.port)
    env.broker_port = pick_port(args.broker_port)
    # base 必须在任何栈操作之前定稿：Env.__post_init__ 里算的是**默认**端口，
    # 端口顺延后若不覆盖，start_server 会去轮询一个没人监听的地址直到超时。
    env.base = "http://127.0.0.1:%d" % env.port
    if env.port != args.port or env.broker_port != args.broker_port:
        notes.append("端口顺延 %d→%d / %d→%d" % (args.port, env.port,
                                                 args.broker_port, env.broker_port))
    stack = Stack(env)
    ensure_artifacts(env)

    want = [m.strip().upper() for m in args.only.split(",") if m.strip()] or list(MODULES)
    unknown = [m for m in want if m not in MODULES]
    if unknown:
        print("未知模块：%s（可选 %s）" % (unknown, list(MODULES)))
        return 2

    ctx: dict = {"stack": stack}
    fatal = ""
    try:
        # 第一件事就是钉住 WSL 实例：preflight 里的几次 wsl.exe 调用之间也有空窗，
        # 实例一旦被回收，后面 systemd 起的 Broker/Agent 全部作废（见 start_keeper）。
        stack.start_keeper()

        print("== preflight ==")
        checks = stack.preflight()
        hard = {".venv Python", "WSL 发行版", "WSL mosquitto", "WSL Agent 依赖",
                "Playwright Chromium", "web/mock 目录"}
        if not env.skip_build:
            hard.add("pnpm")
        # preflight 的发行版条目自带版本号（"WSL 发行版 Ubuntu-22.04"），按前缀匹配；
        # 早先用全等匹配导致「发行版缺失」被判成非硬项，整轮白跑。
        bad = [c for c in checks
               if not c[1] and any(c[0].startswith(h) for h in hard)]
        for name, ok, detail in checks:
            print("  [%s] %s — %s" % ("ok" if ok else "XX", name, detail))
        if bad:
            raise RuntimeError("preflight 未通过：%s" % [c[0] for c in bad])

        print("== 构建前端 ==")
        notes.append(stack.prepare_web())

        print("== 起 Broker ==")
        stack.start_broker()

        print("== 起服务端（空 KK_PUBLIC_URL）==")
        stack.start_server(90)
        env.public_url = stack.detect_public_url()
        notes.append("Agent 可达基址=%s" % (env.public_url or "探测失败"))
        if env.public_url:
            print("== 重启服务端以注入 KK_PUBLIC_URL=%s ==" % env.public_url)
            stack.restart_server(90)
        else:
            print("!! WSL 探测不到服务端：自更新下载会失败，报告将如实记录")

        print("== 登录 ==")
        if not login(env):
            raise RuntimeError("admin 登录失败（%s）" % env.base)

        print("== 部署并启动 3 台 Agent ==")
        stack.deploy_agents()
        stack.start_agents()

        def _ready() -> bool:
            from .core import api
            st, body = api(env, "GET", "/api/containers", params={"view": "full"})
            items = body.get("items") or []
            return (st == 200 and len(items) == len(env.agents)
                    and all(i.get("online") for i in items)
                    and all((i.get("metrics") or {}).get("mem_mb") for i in items))

        if not wait_until(_ready, 120, 2.0):
            raise RuntimeError("机队 120s 内未就绪（status 或首帧心跳缺失）")
        notes.append("机队就绪 %d 台" % len(env.agents))

        for name in want:
            print("\n== 模块 %s ==" % name)
            out = MODULES[name](env, ctx)
            if isinstance(out, dict):
                ctx[name] = out
                ctx.update({k: v for k, v in out.items() if k not in ctx})
    except KeyboardInterrupt:
        fatal = "用户中断"
        check("ENV-00", "ENV", "运行被用户中断", False, fatal)
    except Exception as e:  # noqa: BLE001 - 环境级故障也要落报告，而不是只留栈
        fatal = "%s: %s" % (type(e).__name__, e)
        print("\n!! 环境级故障：%s" % fatal)
        traceback.print_exc()
        check("ENV-01", "ENV", "环境编排未完成（后续用例未执行）", False,
              fatal, precond="栈编排阶段", steps="preflight → 构建 → Broker → 服务端 → Agent",
              expect="全部就绪后进入用例模块",
              detail=traceback.format_exc()[-2000:])
    finally:
        json_path, md_path = write(env, started_at=started_at,
                                   meta=_meta(env, stack, notes))
        if not args.keep:
            print("== 收尾：停 Agent / 服务端 / Broker ==")
            stack.stop_all()
        else:
            # 保活会话也留着：放掉它 = 发行版被回收 = Broker/Agent 一起没
            print("== --keep：栈保留在 %s（Broker %d / 服务端 %d）=="
                  % (env.base, env.broker_port, env.port))
            print("   释放实例：wsl --terminate %s（或关掉那个 sleep 会话）" % env.distro)

    passed = sum(1 for c in CASES if c["result"] == "PASS")
    print("\n用例 %d 条：通过 %d，失败 %d" % (len(CASES), passed, len(CASES) - passed))
    print("报告：%s" % json_path)
    print("      %s" % md_path)
    if fatal:
        print("环境级故障：%s" % fatal)
    return 0 if passed == len(CASES) and CASES and not fatal else 1


if __name__ == "__main__":
    sys.exit(main())