#!/usr/bin/env bash
# KontainKeeper 镜像级部署冒烟：起真实 Broker + 真实 kk-server 镜像 + 真实 Agent 二进制，
# 断言「部署后能用」这条主链路，而不是只看容器起没起：
#
#   健康检查(ok=true + broker=connected) → 管理员登录 → Agent 上线可见 → 指标落库 → 命令回传 → 审计留痕
#
# 为什么单独写成脚本：CI 需要在镜像构建之后、推送之前跑；本地改完 Dockerfile/镜像也能跑同一份。
#
# 用法：
#   scripts/ci_smoke.sh kk-server:latest [agent/dist/kk-agent]
#
# 环境变量：
#   SMOKE_HTTP_PORT / SMOKE_MQTT_PORT  宿主机映射端口（默认各取一个空闲端口，避免与开发中的服务撞车）
#   SMOKE_ADMIN_PASS                   冒烟管理员口令（默认随机；**不能**是默认 admin123）
#   SMOKE_TIMEOUT                      单步等待超时秒数（默认 90）
#   BROKER_IMAGE                       Broker 镜像（默认 eclipse-mosquitto:2）
#   SMOKE_KEEP=1                       保留容器与网络便于排查（默认自动清理）
#
# 退出码：0 = 全链路通过；非 0 = 有断言失败（清单已打印，容器日志已贴）。
set -euo pipefail

IMAGE="${1:-}"
AGENT_BIN="${2:-}"
[ -n "$IMAGE" ] || { echo "用法: scripts/ci_smoke.sh <镜像引用> [agent 二进制路径]" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

BROKER_IMAGE="${BROKER_IMAGE:-eclipse-mosquitto:2}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-90}"
SMOKE_ADMIN_USER="admin"
SMOKE_ADMIN_PASS="${SMOKE_ADMIN_PASS:-kk-smoke-${RANDOM}${RANDOM}}"
HOST_NAME="ci-smoke-host"
# 用镜像形态真跑一遍生产自检（KK_ENV=production 会拒绝默认口令与空白名单）
SMOKE_ENV="production"

# ---- 运行标识：带 PID + 时间戳，同机并发跑两次也不撞名 ----
TAG="$$-$(date +%s)"
NET="kk-smoke-net-${TAG}"
BROKER_CT="kk-smoke-broker-${TAG}"
SERVER_CT="kk-smoke-server-${TAG}"
AGENT_PID=""
AGENT_LOG="$(mktemp -t kk-smoke-agent.XXXXXX.log)"
TOKEN=""
CID=""

# ---- 工具自检：缺哪个直接说清，别用隐晦的 command not found 让人猜 ----
for t in docker curl python3; do
  command -v "$t" >/dev/null 2>&1 || { echo "!! 缺少命令：$t" >&2; exit 2; }
done

free_port() {
  python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
}
SMOKE_HTTP_PORT="${SMOKE_HTTP_PORT:-$(free_port)}"
SMOKE_MQTT_PORT="${SMOKE_MQTT_PORT:-$(free_port)}"
BASE="http://127.0.0.1:${SMOKE_HTTP_PORT}"

# ---- 断言与 JSON 读取 ----
FAILED=0
check() {  # check <名称> <1|0> [细节]
  if [ "$2" = "1" ]; then
    echo "PASS $1${3:+ — $3}"
  else
    echo "FAIL $1${3:+ — $3}"
    FAILED=$((FAILED + 1))
  fi
}

# jget <json> <点分路径> —— 取不到就输出空串（不抛错，让断言去报失败）
jget() {
  python3 -c '
import json, sys
try:
    cur = json.loads(sys.argv[1])
except Exception:
    print(""); sys.exit(0)
for key in sys.argv[2].split("."):
    if isinstance(cur, list):
        try:
            cur = cur[int(key)]
        except Exception:
            print(""); sys.exit(0)
    elif isinstance(cur, dict):
        cur = cur.get(key)
    else:
        print(""); sys.exit(0)
    if cur is None:
        print(""); sys.exit(0)
if isinstance(cur, bool):
    print("true" if cur else "false")
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, ensure_ascii=False))
else:
    print(cur)
' "$1" "$2"
}

api() {  # api <方法> <路径> [token] [JSON体]
  local method="$1" path="$2" token="${3:-}" body="${4:-}"
  local argv=(-sS -X "$method" -H 'Content-Type: application/json' --max-time 15)
  [ -n "$token" ] && argv+=(-H "Authorization: Bearer $token")
  [ -n "$body" ] && argv+=(-d "$body")
  curl "${argv[@]}" "${BASE}${path}"
}

authed() {  # authed <路径> —— 带 token 的 GET，失败输出空串
  curl -fsS --max-time 8 -H "Authorization: Bearer ${TOKEN}" "${BASE}$1" 2>/dev/null || true
}

# ---- 轮询探针：命中输出非空值，未命中输出空串（由 wait_for 决定重试）----
# 注意：探针必须是本 shell 可见的函数，不能塞进 bash -c（子 shell 里拿不到这些函数与变量）
poll_broker() {
  python3 -c "import socket;socket.create_connection(('127.0.0.1',${SMOKE_MQTT_PORT}),2);print('up')" 2>/dev/null || true
}
poll_health() {
  curl -fsS --max-time 5 "${BASE}/api/health" 2>/dev/null || true
}
poll_online() {
  printf '%s' "$(authed '/api/containers?view=summary')" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if any(i.get("pod") == sys.argv[1] and i.get("online") for i in d.get("items", [])):
    print("online")
' "$HOST_NAME"
}
poll_first_hb() {
  printf '%s' "$(authed "/api/containers/${HOST_NAME}")" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
mem = (d.get("metrics") or {}).get("mem_mb")
if mem:
    print("mem_mb=%s" % mem)
'
}
poll_terminal() {
  printf '%s' "$(authed "/api/commands/${CID}")" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get("status") in ("done", "failed", "timeout"):
    print(json.dumps(d))
'
}

wait_for() {  # wait_for <超时秒> <探针函数> [参数...] —— 命中则把探针输出打到 stdout 并返回 0
  local deadline=$((SECONDS + $1)); shift
  local fn="$1"; shift
  local out=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    if out="$("$fn" "$@")" && [ -n "$out" ]; then
      printf '%s' "$out"
      return 0
    fi
    sleep 2
  done
  return 1
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ] || [ "${SMOKE_KEEP:-0}" = "1" ]; then
    echo "---- 诊断：容器日志尾部 ----"
    docker logs --tail 40 "$SERVER_CT" 2>&1 | sed 's/^/  [server] /' || true
    docker logs --tail 20 "$BROKER_CT" 2>&1 | sed 's/^/  [broker] /' || true
    if [ -s "$AGENT_LOG" ]; then
      echo "---- 诊断：Agent 日志尾部 ----"
      tail -n 30 "$AGENT_LOG" | sed 's/^/  [agent] /' || true
    fi
  fi
  if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
  rm -f "$AGENT_LOG"
  if [ "${SMOKE_KEEP:-0}" = "1" ]; then
    echo ">> SMOKE_KEEP=1：保留容器 $BROKER_CT / $SERVER_CT 与网络 $NET 供排查"
  else
    docker rm -fv "$SERVER_CT" "$BROKER_CT" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

echo ">> 冒烟目标镜像：$IMAGE"
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "!! 镜像不存在：$IMAGE" >&2; exit 2; }
echo ">> 端口：HTTP=$SMOKE_HTTP_PORT MQTT=$SMOKE_MQTT_PORT（均只绑 127.0.0.1）"

# ---- 1. 网络 + Broker ----
docker network create "$NET" >/dev/null
docker run -d --name "$BROKER_CT" --network "$NET" \
  -p "127.0.0.1:${SMOKE_MQTT_PORT}:1883" \
  -v "$REPO_ROOT/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  "$BROKER_IMAGE" >/dev/null
if wait_for 60 poll_broker >/dev/null; then
  check "Broker 容器就绪（匿名 1883，生产同源配置）" 1
else
  check "Broker 容器就绪（匿名 1883，生产同源配置）" 0 "60s 内端口不可达"
  exit 1
fi

# ---- 2. 服务端镜像（真跑生产自检）----
# KK_AGENT_IPS 必须含 127.0.0.1：Agent 在宿主机跑、连的是回环上的 Broker，自报 IP 即回环。
docker run -d --name "$SERVER_CT" --network "$NET" \
  -p "127.0.0.1:${SMOKE_HTTP_PORT}:8443" \
  -e "KK_MQTT_URL=mqtt://${BROKER_CT}:1883" \
  -e "KK_AGENT_IPS=127.0.0.1/32" \
  -e "KK_ADMIN_USER=${SMOKE_ADMIN_USER}" \
  -e "KK_ADMIN_PASS=${SMOKE_ADMIN_PASS}" \
  -e "KK_ENV=${SMOKE_ENV}" \
  -e "KK_LOG_LEVEL=info" \
  "$IMAGE" >/dev/null

if health="$(wait_for "$SMOKE_TIMEOUT" poll_health)"; then
  check "服务端健康接口可用" 1
else
  check "服务端健康接口可用" 0 "等 ${SMOKE_TIMEOUT}s 未返回"
  exit 1
fi
check "健康接口 ok=true" "$([ "$(jget "$health" ok)" = "true" ] && echo 1 || echo 0)" \
  "version=$(jget "$health" version) proto_ver=$(jget "$health" proto_ver)"
check "服务端已连上 Broker" "$([ "$(jget "$health" broker)" = "connected" ] && echo 1 || echo 0)" \
  "broker=$(jget "$health" broker)"
check "生产自检通过（KK_ENV=${SMOKE_ENV} + 非默认口令 + 非空白名单）" 1

# ---- 3. 登录 ----
login="$(api POST /api/login '' "{\"username\":\"${SMOKE_ADMIN_USER}\",\"password\":\"${SMOKE_ADMIN_PASS}\"}")"
TOKEN="$(jget "$login" token)"
check "管理员登录拿到 token" "$([ -n "$TOKEN" ] && echo 1 || echo 0)" "$(printf '%s' "$login" | head -c 120)"
[ -n "$TOKEN" ] || exit 1

# ---- 4. Agent 二进制（可选）上线 + 指标 + 命令回传 ----
if [ -z "$AGENT_BIN" ]; then
  echo ">> 未提供 Agent 二进制，跳过「上线 / 指标 / 命令 / 审计」四段（传入第二个参数即可启用）"
else
  [ -x "$AGENT_BIN" ] || { echo "!! Agent 二进制不可执行：$AGENT_BIN" >&2; exit 2; }
  # KK_LOG=- 表示只写 stderr（不与宿主机日志文件双写）；自报 IP 固定回环以匹配白名单
  KK_SERVER="mqtt://127.0.0.1:${SMOKE_MQTT_PORT}" \
  KK_HOST_NAME="$HOST_NAME" \
  KK_INTERVAL=1 \
  KK_ADVERTISE_IP=127.0.0.1 \
  KK_UPDATE_DISABLED=1 \
  KK_LOG_LEVEL=WARNING \
  KK_LOG=- \
    "$AGENT_BIN" >"$AGENT_LOG" 2>&1 &
  AGENT_PID=$!

  if wait_for "$SMOKE_TIMEOUT" poll_online >/dev/null; then
    check "Agent 上线并在主机列表可见（online=true）" 1
  else
    check "Agent 上线并在主机列表可见（online=true）" 0
  fi

  # 指标要等首帧心跳（KK_INTERVAL=1），别把「已上线」当成「有指标」
  if hb="$(wait_for "$SMOKE_TIMEOUT" poll_first_hb)"; then
    check "首帧心跳指标已落库" 1 "$hb"
  else
    check "首帧心跳指标已落库" 0 "mem_mb 一直为空"
  fi

  created="$(api POST /api/commands "$TOKEN" \
    "{\"pods\":[\"${HOST_NAME}\"],\"kind\":\"shell\",\"argv\":[\"echo\",\"ci-smoke-ok\"],\"timeout\":30}")"
  CID="$(jget "$created" items.0.id)"
  check "命令下发被受理（拿到 id）" "$([ -n "$CID" ] && echo 1 || echo 0)" "$(printf '%s' "$created" | head -c 160)"

  if [ -n "$CID" ]; then
    if detail="$(wait_for "$SMOKE_TIMEOUT" poll_terminal)"; then
      check "命令进入终态" 1 "status=$(jget "$detail" status)"
      check "命令执行成功（rc=0）" "$([ "$(jget "$detail" rc)" = "0" ] && echo 1 || echo 0)" "rc=$(jget "$detail" rc)"
    else
      check "命令进入终态" 0 "${SMOKE_TIMEOUT}s 内未终结"
    fi

    out="$(api GET "/api/commands/${CID}/out?format=text" "$TOKEN")"
    case "$out" in
      *ci-smoke-ok*) check "结果回传内容正确" 1 "out=$(printf '%s' "$out" | tr -d '\n' | head -c 40)" ;;
      *) check "结果回传内容正确" 0 "out=$(printf '%s' "$out" | head -c 80)" ;;
    esac

    case "$(authed '/api/audit?limit=50')" in
      *command_create*) check "命令下发已留审计" 1 ;;
      *) check "命令下发已留审计" 0 ;;
    esac
  fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo ">> 冒烟通过：部署主链路（健康 / 登录 / 上线 / 指标 / 命令 / 结果 / 审计）全部正常"
  exit 0
fi
echo ">> 冒烟失败：$FAILED 项未通过（详见上方 FAIL 行与容器日志）"
exit 1
