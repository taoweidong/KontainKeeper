"""真实浏览器（Playwright + Chromium）页面用例。

选择器策略：仓内**没有 data-testid**，所以一律用「用户可见的中文文案 + `.kk-*`
语义类」定位，不绑定 @pureadmin/table 的内部 DOM 层级（升级底座时不至于整页用例
一起红）。每条用例都留截图，失败时靠截图与 actual 定位。

覆盖：登录（错口令拒绝 + 正确登录）→ 主机总览（读数/筛选/勾选）→ 主机详情 →
命令面板（真实下发 + 结果抽屉 + 下载输出）→ 采集面板 → 审计日志（严重度 + 导出）
→ 系统统计 → 版本与更新（上传对话框校验 + 回滚禁用态）。

不在这里做的事：真正上传/升级（upgrade_cases 用真实二进制走一遍替换与 execv），
以及离线/重连（resilience_cases）。页面用例只证明「页面能驱动同一套后端」。
"""
from __future__ import annotations

import re

from .core import Env, api, check, trunc

MOD = "UI"

PODS = ("wsl-agent-01", "wsl-agent-02", "wsl-agent-03")


# ---------------------------------------------------------------- 小工具
def _need(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _shot(page, env, cid) -> str:
    path = env.artifacts / "screenshots" / ("%s.png" % cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=True)
        return path.relative_to(env.artifacts).as_posix()
    except Exception:  # noqa: BLE001 - 截图失败不能掩掉用例结论
        return ""


def _page_text(page, limit: int = 300) -> str:
    """失败现场的可读快照：截图要人看，字符串进报告给下一次的自己看。"""
    try:
        return re.sub(r"\s+", " ", page.inner_text("body"))[:limit]
    except Exception:  # noqa: BLE001 - 页面可能已崩，诊断信息缺失不能盖掉原错误
        return "(取不到页面文本)"


def _toast(page, text: str, timeout: int = 15000) -> str:
    """等一条含指定文案的 ElMessage 浮层出现，返回它的全文。

    等不到时把**当前浮层与页面文本**带进断言：上一轮 UI-04 只留了一句
    `Timeout 10000ms exceeded`，看不出到底弹的是「请填写命令行」还是「下发失败」。
    """
    loc = page.locator(".el-message").filter(has_text=text).first
    try:
        loc.wait_for(state="visible", timeout=timeout)
        return loc.inner_text()
    except Exception:  # noqa: BLE001
        try:
            seen = [t.strip() for t in page.locator(".el-message").all_inner_texts()]
        except Exception:  # noqa: BLE001
            seen = []
        raise AssertionError("未见浮层「%s」；当前浮层=%r；页面=%r"
                             % (text, seen, _page_text(page)))


_STAMP = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {found: false};
  const reused = !!el.__kkStamp;
  el.__kkStamp = 1;
  return {found: true, reused, value: el.value};
}"""


def _fill_stable(page, selector: str, text: str, settle: int = 700,
                 tries: int = 5) -> None:
    """把文本写进输入框，直到「元素没被换掉 且 值还在」为止。

    命令面板（shell/collect）首次进入后约 0.5s，主内容会被整块重建一次
    （实测 517ms 输入框 REMOVE+ADD、705ms 复选框同上）：`shellForm` 随之重新
    初始化，先写进旧节点的值在 v-model 层已经没了——DOM 上只留一瞬，随后被
    patch 回空串。表现就是填过命令却弹「请填写命令行」且不发请求（UI-04 的坑）。
    因此以「元素身份（打标记）+ 当前值」双条件判定稳定，重试到真的写进去。
    """
    for _ in range(tries):
        page.locator(selector).fill(text)
        page.wait_for_timeout(settle)
        st = page.evaluate(_STAMP, selector)
        if st.get("found") and st.get("reused") and st.get("value") == text:
            return
    raise AssertionError(
        "输入框写入未生效（页面重建后 v-model 丢失）：selector=%s 期望=%r 现场=%r"
        % (selector, text, st))


def _settle(page, selector: str, settle: int = 700, tries: int = 4) -> None:
    """等 selector 命中的元素熬过一个观察窗还没被替换（页面重建已完成）。

    命令面板首次进入后约 0.5–0.7s 主内容会被整块重建（见 _fill_stable 注释），
    在重建前读数会读到半截 DOM。用「同一元素在两个时刻都还在」作为完成信号。
    """
    for _ in range(tries):
        if page.evaluate(_STAMP, selector).get("found"):
            page.wait_for_timeout(settle)
            if page.evaluate(_STAMP, selector).get("reused"):
                return
        else:
            page.wait_for_timeout(settle)


def _confirm(page, button: str = "确认下发", timeout: int = 8000) -> None:
    box = page.locator(".el-message-box").filter(has_text="批量下发确认").first
    box.wait_for(state="visible", timeout=timeout)
    box.get_by_role("button", name=button).click()


def _goto(page, env, path: str) -> None:
    page.goto(env.base + "/#" + path)
    page.wait_for_load_state("domcontentloaded")


def _wait_rows(page, n: int, timeout: int = 30000) -> None:
    page.wait_for_function(
        "n => document.querySelectorAll('.el-table__row').length === n",
        arg=n, timeout=timeout)


def _wait_done(page, n: int, timeout: int = 120000) -> None:
    """等到恰好 n 行、且每行都显示「已完成」（命令终态）。"""
    page.wait_for_function(
        """n => {
            const rows = [...document.querySelectorAll('.el-table__row')];
            return rows.length === n && rows.every(r => r.innerText.includes('已完成'));
        }""", arg=n, timeout=timeout)


def _case(env, page, cid, title, fn, *, precond, steps, expect,
          logs: list[str] | None = None) -> None:
    shot = ""
    try:
        actual = fn()
        shot = _shot(page, env, cid)
        check(cid, MOD, title, True, actual, precond=precond, steps=steps,
              expect=expect, screenshot=shot)
    except Exception as e:  # noqa: BLE001 - 用例失败要继续跑后面的
        shot = _shot(page, env, cid)
        # 前端报错不会自己进报告：带上控制台尾部与失败 URL，省一次「再跑一遍看看」
        tail = ""
        if logs:
            tail = "｜控制台：" + " || ".join(logs[-6:])
        check(cid, MOD, title, False, "失败：%s%s" % (e, tail), precond=precond,
              steps=steps, expect=expect, screenshot=shot)


# ---------------------------------------------------------------- 用例
def _login(page, env) -> str:
    """先错一次口令（只错一次：后端 5 次/用户名即锁定 300s），再正确登录。"""
    _goto(page, env, "/login")
    page.get_by_placeholder("账号").fill(env.admin_user)
    page.get_by_placeholder("密码").fill("wrong-password-e2e")
    page.get_by_role("button", name="登录", exact=True).click()
    bad = _toast(page, "用户名或密码错误")

    page.get_by_placeholder("密码").fill(env.admin_pass)
    page.get_by_role("button", name="登录", exact=True).click()
    _toast(page, "登录成功")
    # 登录后的落地页由菜单表头决定：`getTopMenu()` 取 wholeMenus[0].children[0]，
    # rank 0 的 Home 模块 redirect 到 /welcome（router/modules/home.ts）。断言
    # #/hosts/monitor 是把「首页」当成了「总览」，与产品实际行为不符。
    page.wait_for_function(
        "() => location.hash.startsWith('#/welcome')", timeout=20000)
    page.wait_for_selector(".welcome", timeout=20000)
    page.get_by_text("主机总数").first.wait_for(timeout=20000)
    # 错误口令留下审计（login_fail），供审计页用例断言严重度红点
    return "错误口令提示=%r；登录后落 %s" % (
        trunc(bad, 60), page.evaluate("location.hash"))


def _summary(env) -> dict:
    st, body = api(env, "GET", "/api/containers", params={"view": "summary"})
    return body if st == 200 and isinstance(body, dict) else {}


def _monitor(page, env) -> str:
    # 读数带是「服务端按同一套条件算出的台数」，断言值取接口真值而不是写死：
    # WSL 的根分区常年 >85%，磁盘告警本来就不是 0（上一版写死 0，必红）。
    api_sum = _summary(env)
    want_total = api_sum.get("total")
    want_online = api_sum.get("online")
    want_alerts = api_sum.get("alerts")
    _need(isinstance(want_total, int), "接口没给出 total：%r" % api_sum)
    _goto(page, env, "/hosts/monitor")
    _wait_rows(page, len(PODS), timeout=25000)
    band = re.sub(r"\s+", "", page.locator(".kk-band__fleet").inner_text())
    _need("%d台主机" % want_total in band,
          "读数带未报 %d 台主机：%r" % (want_total, band))
    _need("%d在线" % want_online in band,
          "读数带在线数应为 %s：%r" % (want_online, band))
    _need("%d磁盘告警" % want_alerts in band,
          "读数带磁盘告警应为 %s（接口 alerts）：%r" % (want_alerts, band))

    # 关键字筛选下推服务端：输入后只剩 1 行
    page.get_by_placeholder("主机名 / 镜像 / 系统 / 分组").fill("wsl-agent-02")
    _wait_rows(page, 1, timeout=15000)
    _need("wsl-agent-02" in page.locator(".el-table__row").first.inner_text(),
          "筛选后首行不是 wsl-agent-02")
    page.get_by_role("button", name="清空筛选").click()
    _wait_rows(page, len(PODS), timeout=15000)

    # 全选 → 读数带出已选台数；批量升级在无待分发版本时应保持禁用
    page.locator(".el-table__header .el-checkbox").first.click()
    page.wait_for_function(
        "() => document.body.innerText.includes('已选 3 台')", timeout=8000)
    disabled = page.get_by_role("button", name="批量升级").is_disabled()
    _need(disabled, "无待分发版本时「批量升级」应为禁用态")
    page.locator(".el-table__header .el-checkbox").first.click()
    page.wait_for_function(
        "() => document.body.innerText.includes('已选 0 台')", timeout=8000)
    return "读数带=%r；筛选/清空/全选/反选均生效" % trunc(band, 50)


def _detail(page, env) -> str:
    _goto(page, env, "/hosts/monitor")
    _wait_rows(page, len(PODS), timeout=25000)
    page.locator(".el-table__row").first.locator(".el-link").click()
    page.wait_for_function(
        "() => location.hash.startsWith('#/hosts/detail/')", timeout=15000)
    page.wait_for_selector(".kk-desc", timeout=20000)
    page.wait_for_selector(".kk-chart canvas", timeout=20000)
    body = page.locator(".kk-card").first.inner_text()
    _need("Agent 版本" in body and "0.3.0" in body, "详情未显示 Agent 版本 0.3.0")
    _need("最近命令" in body, "详情缺「最近命令」区块")
    _need("磁盘" in body and "网卡" in body, "详情缺磁盘/网卡区块")
    return "详情页含版本/图表/磁盘/网卡/最近命令；hash=%s" % page.evaluate("location.hash")


def _shell(page, env) -> str:
    _goto(page, env, "/command/shell?pods=" + ",".join(PODS[:2]))
    btn = page.get_by_role("button", name="下发命令（2 台）")
    btn.wait_for(timeout=20000)
    _fill_stable(page, 'input[placeholder^="与在主机上直接敲命令一致"]',
                 "echo KK_E2E_UI_OK; hostname; id -u")
    btn.click()
    # 把 POST 本身也纳入断言：上一轮「没等到 success 浮层」既可能是前端校验
    # 提前返回，也可能是接口非 200，只看浮层分不清是哪一种。
    with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/commands"),
            timeout=20000) as caught:
        _confirm(page)
    resp = caught.value
    _need(resp.status == 200,
          "POST /api/commands 返回 %s：%s" % (resp.status, trunc(resp.text(), 300)))
    _toast(page, "已下发 2 条命令")
    _wait_done(page, 2)
    page.locator(".el-table__row").first.get_by_role("button", name="查看").click()
    # 同页有两个 el-drawer（HostPicker 的「选择目标主机」常驻 DOM），按内容挑输出抽屉
    drawer = page.locator(".el-drawer").filter(has=page.locator("pre.kk-out")).first
    drawer.wait_for(state="visible", timeout=15000)
    page.wait_for_function(
        "() => (document.querySelector('.el-drawer pre.kk-out')?.innerText || '')"
        ".includes('KK_E2E_UI_OK')", timeout=20000)
    out = drawer.locator("pre.kk-out").inner_text()

    dest = env.artifacts / "downloads" / "ui-cmd-output.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with page.expect_download(timeout=20000) as dl:
        drawer.get_by_role("button", name="下载此条输出").click()
    dl.value.save_as(str(dest))
    _need(dest.read_text(encoding="utf-8", errors="replace").strip() != "",
          "下载的输出现场为空")
    page.keyboard.press("Escape")
    return "结果抽屉=%r；已下载 %s" % (trunc(out, 60), dest.name)


def _collect(page, env) -> str:
    _goto(page, env, "/command/collect?pods=" + ",".join(PODS))
    btn = page.get_by_role("button", name="下发采集（3 台）")
    btn.wait_for(timeout=20000)
    _settle(page, ".el-checkbox")
    checked = page.locator(".el-checkbox.is-checked").count()
    _need(checked >= 3, "采集项默认预勾应 ≥3 项，实际 %d" % checked)
    btn.click()
    with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/commands"),
            timeout=20000) as caught:
        _confirm(page)
    resp = caught.value
    _need(resp.status == 200,
          "POST /api/commands 返回 %s：%s" % (resp.status, trunc(resp.text(), 300)))
    _toast(page, "已下发 3 条采集命令")
    _wait_done(page, 3)
    return "默认预勾 %d 项；3 台采集全部完成" % checked


def _audit(page, env) -> str:
    _goto(page, env, "/audit/index")
    page.wait_for_function(
        "() => document.querySelectorAll('.el-table__row').length >= 4",
        timeout=25000)
    bad = page.locator(".kk-state--bad").count()
    _need(bad >= 1, "审计页应有至少一条红点事件（login_fail），实际 %d" % bad)
    text = page.locator(".kk-card, .kk-main").first.inner_text()
    _need("login_ok" in text, "审计页未见 login_ok")

    page.get_by_placeholder("按操作者 / 动作 / 明细过滤").fill("login_fail")
    page.wait_for_function(
        """() => {
            const rows = [...document.querySelectorAll('.el-table__row')];
            return rows.length > 0 && rows.every(r => r.innerText.includes('login_fail'));
        }""", timeout=15000)
    filtered = page.locator(".el-table__row").count()
    page.get_by_role("button", name="重置").click()
    page.wait_for_function(
        "() => document.querySelectorAll('.el-table__row').length > %d" % filtered,
        timeout=15000)

    dest = env.artifacts / "downloads" / "ui-audit.csv"
    with page.expect_download(timeout=20000) as dl:
        page.get_by_role("button", name="导出 CSV").first.click()
    name = dl.value.suggested_filename
    dl.value.save_as(str(dest))
    _need(name.startswith("审计日志_"), "导出文件名前缀不对：%s" % name)
    return "红点事件 %d 条；关键字筛选命中 %d 行；导出 %s" % (bad, filtered, name)


def _system(page, env) -> str:
    _goto(page, env, "/system/index")
    page.get_by_text("Broker 已连接", exact=True).wait_for(timeout=25000)
    page.wait_for_function(
        "() => document.querySelectorAll('.el-table__row').length >= 8",
        timeout=25000)
    body = page.locator(".kk-card").first.inner_text()
    _need("主机" in body, "系统统计缺主机读数")
    text = page.locator(".el-table__row").first.inner_text()
    _need("状态帧" in text or page.locator(
        ".el-table__row").filter(has_text="状态帧").count() >= 1,
        "Broker 计数器表缺「状态帧」")
    rows = page.locator(".el-table__row").count()
    return "链路=Broker 已连接；计数器 %d 行（含状态帧）" % rows


def _update_page(page, env) -> str:
    _goto(page, env, "/hosts/update")
    page.get_by_text("当前版本", exact=False).first.wait_for(timeout=20000)
    # 尚未上传过版本：主表空态应直说「先去上传」，回滚按钮禁用
    empty = page.get_by_text("还没有待分发版本").first
    empty.wait_for(timeout=20000)
    _need(page.get_by_role("button", name="回滚待分发").is_disabled(),
          "无待分发版本时「回滚待分发」应为禁用态")

    page.get_by_role("button", name="上传新版本").click()
    dlg = page.locator(".el-dialog").filter(has_text="上传新版本").first
    dlg.wait_for(state="visible", timeout=10000)
    dlg.get_by_placeholder(re.compile("首字符必须是数字")).fill("v1.2.3")
    dlg.locator(".el-dialog__title").click()  # blur → 触发校验
    dlg.get_by_text("版本号须以数字开头", exact=False).first.wait_for(timeout=8000)
    dlg.get_by_role("button", name="取消").click()
    dlg.wait_for(state="hidden", timeout=8000)
    return "空态/回滚禁用正确；上传对话框对 v1.2.3 原地报错并取消关闭"


def run(env: Env, ctx: dict | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    out: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=env.headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(20000)
        logs: list[str] = []
        page.on("console", lambda m: logs.append("console.%s %s" % (m.type, m.text)))
        page.on("pageerror", lambda e: logs.append("pageerror %s" % e))
        page.on("requestfailed",
                lambda r: logs.append("requestfailed %s %s" % (r.url, r.failure)))
        try:
            _case(env, page, "UI-01", "登录页：错口令被拒、正确登录进入总览",
                  lambda: _login(page, env),
                  precond="服务端已就绪，admin/admin123",
                  steps="输错口令提交；再输正确口令提交",
                  expect="401 文案可见；登录成功并落到 #/welcome（首页，菜单 rank 0）", logs=logs)
            _case(env, page, "UI-02", "主机总览：读数带 / 关键字筛选 / 勾选",
                  lambda: _monitor(page, env),
                  precond="3 台 Agent 在线（磁盘告警数取接口真值，WSL 根分区常超 85%）",
                  steps="打开总览；筛 wsl-agent-02；清空；全选/反选",
                  expect="读数带与 /api/containers 一致（台数/在线/磁盘告警）；筛选后 1 行；已选读数与禁用态正确", logs=logs)
            _case(env, page, "UI-03", "主机详情：曲线与指标区块",
                  lambda: _detail(page, env),
                  precond="总览有主机行",
                  steps="点主机名进入详情",
                  expect="显示 0.3.0、ECharts canvas、磁盘/网卡/最近命令", logs=logs)
            _case(env, page, "UI-04", "命令面板：批量下发 + 结果抽屉 + 下载",
                  lambda: _shell(page, env),
                  precond="2 台主机在线",
                  steps="?pods= 预选 2 台；填 echo 命令；确认下发；查看输出；下载",
                  expect="已下发 2 条；两行完成；输出含 KK_E2E_UI_OK；下载非空", logs=logs)
            _case(env, page, "UI-05", "采集面板：默认采集项 3 台下发",
                  lambda: _collect(page, env),
                  precond="3 台主机在线",
                  steps="?pods= 预选 3 台；直接下发",
                  expect="预勾 cpu/mem/disk；3 条采集命令全部完成", logs=logs)
            _case(env, page, "UI-06", "审计日志：严重度圆点 / 关键字 / 导出",
                  lambda: _audit(page, env),
                  precond="已有登录失败与成功审计",
                  steps="打开审计页；筛 login_fail；重置；导出 CSV",
                  expect="红点≥1；筛选行全为 login_fail；文件名前缀 审计日志_", logs=logs)
            _case(env, page, "UI-07", "系统统计：链路状态与计数器",
                  lambda: _system(page, env),
                  precond="服务端与 Broker 已连接",
                  steps="打开系统统计页",
                  expect="Broker 已连接；计数器表含状态帧", logs=logs)
            _case(env, page, "UI-08", "版本与更新：空态 / 上传校验 / 回滚禁用",
                  lambda: _update_page(page, env),
                  precond="尚未上传任何版本",
                  steps="打开页面；开上传框填 v1.2.3；取消",
                  expect="空态提示上传；版本号原地报错；回滚按钮禁用", logs=logs)
            out["hash"] = page.evaluate("location.hash")
        finally:
            context.close()
            browser.close()
    return out