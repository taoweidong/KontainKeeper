"""E2E 报告：同一个 CASES 列表同时落成 JSON（机器读）与 Markdown（人读）。

为什么两份都要：
- JSON 给后续门禁/看板/对比两次运行用，字段必须稳定（cases 里原样保留 detail）；
- Markdown 是「任务完成后的完整流程步骤」载体，表格列固定为
  编号 / 用例 / 前置 / 步骤 / 预期 / 实际 / 结论，截图以相对链接挂进产物目录。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .core import CASES, Env


def _md_cell(text, limit: int = 180) -> str:
    """表格单元格：竖线与换行会把 Markdown 表格撑坏，先规整再截断。"""
    s = ("" if text is None else str(text)).replace("|", "\\|")
    s = s.replace("\r", " ").replace("\n", "<br>")
    return s if len(s) <= limit else s[:limit] + "…"


def summarize(cases=None) -> dict:
    rows = CASES if cases is None else cases
    by_module: dict[str, dict[str, int]] = {}
    for c in rows:
        m = by_module.setdefault(c["module"], {"total": 0, "pass": 0, "fail": 0})
        m["total"] += 1
        m["pass" if c["result"] == "PASS" else "fail"] += 1
    total = len(rows)
    passed = sum(1 for c in rows if c["result"] == "PASS")
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(100.0 * passed / total, 1) if total else 0.0,
        "by_module": by_module,
    }


def write(env: Env, *, started_at: float, meta: dict | None = None) -> tuple[Path, Path]:
    """写出 artifacts/<run_id>/report.{json,md}，返回两个路径。

    meta 由 runner 传入（服务端版本、Agent 版本、隔离端口等）——报告必须能自证
    「这次跑的是哪套代码、哪个库」，否则翻出旧报告时无从判断是否还成立。
    """
    env.artifacts.mkdir(parents=True, exist_ok=True)
    summary = summarize()
    finished_at = time.time()
    payload = {
        "run_id": env.run_id,
        "started_at": int(started_at),
        "finished_at": int(finished_at),
        "elapsed_sec": round(finished_at - started_at, 1),
        "env": {
            "base": env.base,
            "public_url": env.public_url,
            "broker_port": env.broker_port,
            "topic_prefix": env.prefix,
            "distro": env.distro,
            "db_path": str(env.db_path),
            "artifacts": str(env.artifacts),
            **(meta or {}),
        },
        "summary": summary,
        "cases": CASES,
    }
    json_path = env.artifacts / "report.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    md_path = env.artifacts / "report.md"
    md_path.write_text(_render_md(env, payload), encoding="utf-8")
    return json_path, md_path


def _render_md(env: Env, payload: dict) -> str:
    s = payload["summary"]
    e = payload["env"]
    out = [
        "# KontainKeeper 全流程 E2E 报告",
        "",
        "运行编号 `%s` · 开始 %s · 耗时 %.1fs" % (
            payload["run_id"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["started_at"])),
            payload["elapsed_sec"]),
        "",
        "## 结论",
        "",
        "| 总数 | 通过 | 失败 | 通过率 |",
        "| ---: | ---: | ---: | ---: |",
        "| %d | %d | %d | %s%% |" % (
            s["total"], s["passed"], s["failed"], s["pass_rate"]),
        "",
        "| 模块 | 总数 | 通过 | 失败 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for mod, m in s["by_module"].items():
        out.append("| %s | %d | %d | %d |" % (mod, m["total"], m["pass"], m["fail"]))
    out += [
        "",
        "## 环境",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 页面/接口 | `%s` |" % e.get("base"),
        "| Agent 可达基址 | `%s` |" % e.get("public_url"),
        "| Broker | `127.0.0.1:%s`（%s） |" % (e.get("broker_port"), e.get("topic_prefix")),
        "| 数据库 | `%s` |" % e.get("db_path"),
        "| WSL 发行版 | `%s` |" % e.get("distro"),
        "| 产物目录 | `%s` |" % e.get("artifacts"),
    ]
    for k, v in (payload.get("env") or {}).items():
        if k in ("base", "public_url", "broker_port", "topic_prefix", "db_path",
                 "distro", "artifacts"):
            continue
        out.append("| %s | `%s` |" % (k, v))

    fails = [c for c in payload["cases"] if c["result"] != "PASS"]
    out += ["", "## 失败明细" if fails else "## 失败明细", ""]
    if not fails:
        out.append("无失败用例。")
    for c in fails:
        out += [
            "### %s %s" % (c["id"], c["title"]),
            "",
            "- 前置：%s" % (c["precond"] or "-"),
            "- 步骤：%s" % (c["steps"] or "-"),
            "- 预期：%s" % (c["expect"] or "-"),
            "- 实际：`%s`" % (c["actual"] or "-"),
        ]
        if c.get("detail"):
            out.append("- 细节：\n\n```\n%s\n```" % c["detail"])
        if c.get("screenshot"):
            out.append("- 截图：![%s](%s)" % (c["id"], c["screenshot"]))
        out.append("")

    out += ["", "## 用例明细", ""]
    mods: dict[str, list[dict]] = {}
    for c in payload["cases"]:
        mods.setdefault(c["module"], []).append(c)
    for mod, rows in mods.items():
        out += [
            "### %s（%d 条）" % (mod, len(rows)),
            "",
            "| 编号 | 用例 | 前置 | 步骤 | 预期 | 实际 | 结论 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for c in rows:
            mark = "✅ PASS" if c["result"] == "PASS" else "❌ FAIL"
            out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                c["id"], _md_cell(c["title"], 60), _md_cell(c["precond"], 120),
                _md_cell(c["steps"], 160), _md_cell(c["expect"], 120),
                _md_cell(c["actual"], 140), mark))
        out.append("")

    shots = sorted(p.name for p in (env.artifacts / "screenshots").glob("*.png"))
    if shots:
        out += ["## 截图", ""]
        out += ["- [%s](screenshots/%s)" % (n, n) for n in shots]
        out.append("")
    dl = sorted(p.name for p in (env.artifacts / "downloads").iterdir()
                if p.is_file()) if (env.artifacts / "downloads").is_dir() else []
    if dl:
        out += ["## 下载产物（页面导出 / 文件提取）", ""]
        out += ["- [%s](downloads/%s)" % (n, n) for n in dl]
        out.append("")
    out += ["", "日志：`server.log`、`web-build.log`、`agents/<host>.log`（WSL 内）。", ""]
    return "\n".join(out)