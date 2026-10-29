#!/usr/bin/env bash
# 质量门禁唯一入口：前后端单测 + 前端类型检查，一条命令给出可复现的红绿判定。
# 用法：scripts/quality_gate.sh [all|be|fe]   （默认 all）
#
# 为什么要有这个脚本：AGENTS.md 的协作规范要求「提交前必须跑前后端单测」，但判定口径
# 原先只存在于文档 prose 里——`| tail` 截掉失败行、skipped 数被忽略，都让「全绿」失真。
# 这里把口径固化成代码：以后端 junitxml 的 failures/errors/skipped 计数为准。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-all}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
rc=0

say() { printf '\n=== %s ===\n' "$*"; }

if [ "$MODE" != "fe" ]; then
  say "后端 pytest（agent + server 全量）"
  "$PY" -m pytest agent/tests server/tests -q --junitxml="$TMP/be.xml" >/dev/null 2>&1
  "$PY" - "$TMP/be.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
s = ET.parse(sys.argv[1]).getroot().find("testsuite")
tests, f, e, sk = (int(s.get(k) or 0) for k in ("tests", "failures", "errors", "skipped"))
print(f"tests={tests} failures={f} errors={e} skipped={sk}")
# 判定：失败/错误必须为 0；无 Broker 时允许恰好 5 条 Broker 级集成用例 skipped，
# 超过 5 说明有别的用例被悄悄跳过 —— 那是异常，不是「忽略即可」。
bad = f or e or (sk > 5)
sys.exit(1 if bad else 0)
PY
  [ $? -eq 0 ] || { echo "后端门禁：红灯（先定位失败用例，禁止跳过/注释掉测试）"; rc=1; }
fi

if [ "$MODE" != "be" ]; then
  say "前端 typecheck"
  (cd web && pnpm typecheck) || rc=1
  say "前端 vitest 单测"
  (cd web && pnpm test) || rc=1
fi

say "判定"
if [ $rc -eq 0 ]; then echo "绿：允许提交"; else echo "红：不得提交，修复后重跑本脚本"; fi
exit $rc
