#!/usr/bin/env bash
# 非容器 Linux 主机（物理机 / VM）的 Agent 安装：把单文件二进制装进目标目录并配成
# systemd 常驻服务。与镜像叠加方案（scripts/build.sh）用的是**同一份二进制、同一套
# KK_* 变量**，只是把「supervisor 后台监管」换成「systemd Restart=always」——
# 连接模型不变：Agent 主动出站连 Broker，主机上不开任何入站端口。
#
# 用法:
#   sudo KK_SERVER=mqtt://broker:1883 bash scripts/install.sh             # 默认 /opt/kk-agent
#   sudo KK_SERVER=... bash scripts/install.sh /usr/local/kk-agent
#
# 预演（不落盘、不改服务，给 CI 与装机前自检用）:
#   KK_SERVER=mqtt://broker:1883 KK_INSTALL_DRY_RUN=1 bash scripts/install.sh
#
# 会写进 env 文件的可选变量（未设置的就不写，保持默认语义）:
#   KK_HOST_NAME KK_GROUP KK_LABELS KK_INTERVAL KK_HB_ITEMS KK_DISK_PATHS
#   KK_ADVERTISE_IP KK_TOPIC_PREFIX KK_MQTT_USERNAME KK_MQTT_PASSWORD
#   KK_TLS_CA KK_DOCKER KK_ALLOW_SHELL KK_UPDATE_URL KK_UPDATE_HMAC_KEY
#
# 关键取舍：env 文件**只在不存在时生成，绝不覆盖**。装机之后运维手工改过的那些值
# （换掉的 Broker 地址、为多网卡定的 KK_ADVERTISE_IP）比一次重装值钱得多，重装不该把它们抹掉。
# 要改配置就编辑 env 文件，然后 systemctl restart kk-agent。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${1:-/opt/kk-agent}"
BIN_SRC="${KK_INSTALL_BIN:-$REPO_ROOT/agent/dist/kk-agent}"
UNIT_NAME=kk-agent.service
SVC="${UNIT_NAME%.service}"
UNIT_SRC="$REPO_ROOT/deploy/systemd/kk-agent.service"
# 单元落盘目录。默认就是 systemd 的位置；能覆盖是为了**安全地测这个脚本**——
# 早先在 WSL 里直接验时真把 kk-agent.service 装进了 /etc/systemd/system，
# 且 Restart=always 让一个假 ExecStart 反复拉起。测试/预演请把这里指到临时目录。
UNIT_DIR="${KK_INSTALL_UNIT_DIR:-/etc/systemd/system}"
ENV_NAME=kk-agent.env
DRY_RUN="${KK_INSTALL_DRY_RUN:-}"

# 上面 16 个键逐个对过 agent/src/kk_agent/config.py 的读取清单，15 个已在读；
# 写错的键不会报错、只会静默回落默认值，所以这里只允许这份白名单进 env 文件。
# 唯一例外是 KK_DOCKER：那是 v4 的 Docker 能力开关，Agent 侧要到 P2 的 docker.py 才读它，
# 现在写进 env 无害（没人读的键就是注释），P2 合并后自动生效。
ENV_KEYS="KK_SERVER KK_HOST_NAME KK_GROUP KK_LABELS KK_INTERVAL KK_HB_ITEMS
          KK_DISK_PATHS KK_ADVERTISE_IP KK_TOPIC_PREFIX KK_MQTT_USERNAME
          KK_MQTT_PASSWORD KK_TLS_CA KK_DOCKER KK_ALLOW_SHELL KK_UPDATE_URL
          KK_UPDATE_HMAC_KEY"

die() { echo "!! $*" >&2; exit 1; }
say() { echo ">> $*"; }
run() {
  if [ -n "$DRY_RUN" ]; then echo "   [dry-run] $*"; else "$@"; fi
}

# ---- 前置检查：先把「装不上」的原因说清楚，别让 systemd 报一句含糊的 exec 失败 ----
[ -f "$BIN_SRC" ] || die "找不到 Agent 二进制：$BIN_SRC
   先在目标平台编译：cd agent && bash build/build_binary.sh   （产物 agent/dist/kk-agent）
   或指定已有二进制：KK_INSTALL_BIN=/path/to/kk-agent $0"
[ -f "$UNIT_SRC" ] || die "找不到单元模板：$UNIT_SRC"
[ -n "${KK_SERVER:-}" ] || die "需要 KK_SERVER（Broker 地址，如 mqtt://broker:1883）
   装了但连不上等于机队里多一台永远离线的主机，不如现在拒绝。"

if [ -z "$DRY_RUN" ]; then
  [ "$(id -u)" -eq 0 ] || die "需要 root：二进制目录与 env 文件必须 root 独占可写
   （能写二进制的人就在下次 Restart 时拿到 root）。用 sudo 重跑。"
fi
case "$(uname -s)" in
  Linux) ;;
  *) die "只支持 Linux（当前 $(uname -s)）；Windows/macOS 上直接跑 python -m kk_agent 即可" ;;
esac
if [ -z "$DRY_RUN" ] && [ ! -d /run/systemd/system ]; then
  die "这台机器没有运行中的 systemd，本脚本只覆盖 systemd 形态
   容器里请改用 scripts/build.sh 的镜像叠加方案（supervisor 入口）。"
fi
# 架构不匹配在这台机器上只会变成 systemd 那句 cryptic 的 Exec format error，所以装之前
# 自己核一遍。刻意不依赖 `file`：裸装的最小镜像里它常常没装，而这个脚本正是给裸机第一步用的。
elf_arch() {
  [ "$(head -c 4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "7f454c46" ] || return 1
  case "$(dd if="$1" bs=1 skip=18 count=2 2>/dev/null | od -An -tx1 | tr -d ' \n')" in
    3e00) echo x86_64 ;;
    b700) echo aarch64 ;;
    *) echo unknown ;;
  esac
}
# 架构检查在 dry-run 里也照做：它只读二进制，而「装错了平台的包」正是装机前最想提前
# 发现的那件事——root 与 /run/systemd 的检查才是只属于真装的。
got_arch="$(elf_arch "$BIN_SRC" || true)"
case "$got_arch" in
  "") die "$BIN_SRC 不是 Linux ELF 可执行文件（比如误拿了 Windows 的 agent/dist/kk-agent.exe）。
   在 Linux 上重新编译：cd agent && bash build/build_binary.sh" ;;
  unknown) say "警告：认不出二进制的 e_machine，跳过架构匹配检查" ;;
esac
if [ -n "$got_arch" ] && [ "$got_arch" != "unknown" ] && [ "$got_arch" != "$(uname -m)" ]; then
  die "二进制是给 $got_arch 的，本机是 $(uname -m)——别硬装，systemd 只会报 Exec format error。
   在目标架构上重新编译：cd agent && bash build/build_binary.sh"
fi

say "目标目录 $TARGET_DIR，二进制来自 $BIN_SRC"

# docs/deployment.md §7.3 的手工流程把二进制**直接放在** /opt/kk-agent（那是个文件），
# 而本脚本装的是目录；撞上时 mkdir 只会甩一句 "Not a directory"，所以先把话说清楚。
if [ -e "$TARGET_DIR" ] && [ ! -d "$TARGET_DIR" ]; then
  die "$TARGET_DIR 已存在，而且不是目录（多半就是 §7.3 手工放的那个二进制文件）。
   二选一：先把它移走（mv $TARGET_DIR ${TARGET_DIR}.old）再重跑；
   或者换个目标：bash scripts/install.sh ${TARGET_DIR}-agent"
fi

# 中途失败也别把暂存二进制和临时单元留在目标目录里
trap 'for f in "${STAGED:-}" "${UNIT_TMP:-}"; do if [ -n "$f" ]; then rm -f "$f"; fi; done' EXIT

# ---- 1) 目录与二进制：同盘 rename 原子替换，运行中的进程不受影响 ----
run mkdir -p "$TARGET_DIR"
run chown root:root "$TARGET_DIR"
run chmod 0755 "$TARGET_DIR"

STAGED="$TARGET_DIR/kk-agent.new"
run cp -f "$BIN_SRC" "$STAGED"
run chown root:root "$STAGED"
run chmod 0755 "$STAGED"

# 内容没变就别重启：重装不该打断正在跑的心跳与在途命令（一次重启 = 一段离线窗口
# + 一次 LWT，总览页会闪一下「离线」）。
CHANGED=1
if [ -z "$DRY_RUN" ] && [ -f "$TARGET_DIR/kk-agent" ]; then
  new_sum="$(sha256sum "$STAGED" | cut -d' ' -f1)"
  old_sum="$(sha256sum "$TARGET_DIR/kk-agent" | cut -d' ' -f1)"
  [ "$new_sum" = "$old_sum" ] && CHANGED=0
fi
if [ "$CHANGED" -eq 1 ]; then
  run mv -f "$STAGED" "$TARGET_DIR/kk-agent"
  say "二进制已就位（新构建或首次安装）"
else
  run rm -f "$STAGED"
  say "二进制与现有文件一致，跳过替换"
fi

# ---- 2) env 文件：只创建、不覆盖，权限 0600（里面可能有 Broker 口令）----
ENV_PATH="$TARGET_DIR/$ENV_NAME"
if [ -f "$ENV_PATH" ]; then
  say "保留既有配置 $ENV_PATH（要改用编辑器，然后 systemctl restart ${UNIT_NAME%.service}）"
  # 本次传入的 KK_SERVER 不会写进已存在的 env 文件，但运维大概会以为它生效了——
  # 「换了 Broker 地址、Agent 还在连旧的」是静默失效，必须当场说破。
  file_server="$(sed -n 's/^KK_SERVER=//p' "$ENV_PATH" | tail -n 1)"
  if [ "$file_server" != "$KK_SERVER" ]; then
    echo "!! 注意：本次传入 KK_SERVER=$KK_SERVER，但 $ENV_PATH 里记的是 ${file_server:-（没有这一行）}。" >&2
    echo "   env 文件只创建不覆盖，所以新地址不会生效，Agent 仍连旧 Broker。" >&2
    echo "   要换 Broker：编辑 $ENV_PATH 改掉 KK_SERVER 那一行，再 systemctl restart ${SVC}。" >&2
  fi
elif [ -n "$DRY_RUN" ]; then
  echo "   [dry-run] 生成 $ENV_PATH，内容如下："
  for k in $ENV_KEYS; do
    [ -n "${!k:-}" ] && echo "     $k=${!k}"
  done
else
  : >"$ENV_PATH"
  for k in $ENV_KEYS; do
    [ -n "${!k:-}" ] && printf '%s=%s\n' "$k" "${!k}" >>"$ENV_PATH"
  done
  # 值原样落盘：systemd 的 EnvironmentFile 不做变量展开，未加引号的值取到行尾
  # （空格也算在内，实测 `KK_MQTT_PASSWORD=p@ss with space` 原样进环境）。
  # 里面有 Broker 口令，所以权限面靠上面的 0600 + root 属主兜住。
  chown root:root "$ENV_PATH"
  chmod 0600 "$ENV_PATH"
  say "已生成 $ENV_PATH（0600，只写了显式传入的键）"
fi

# ---- 3) systemd 单元：模板替换目标目录，内容变了才 daemon-reload ----
UNIT_TMP="$(mktemp)"
sed "s|@TARGET_DIR@|$TARGET_DIR|g" "$UNIT_SRC" >"$UNIT_TMP"
if grep -q '@TARGET_DIR@' "$UNIT_TMP"; then
  die "单元模板里的 @TARGET_DIR@ 没被替换干净（目标目录里含 & 或换行？）：$TARGET_DIR"
fi
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
run mkdir -p "$UNIT_DIR"   # 真机上 /etc/systemd/system 已存在；只有测试钩子指到临时目录时才需要
UNIT_CHANGED=1
if [ -f "$UNIT_PATH" ] && cmp -s "$UNIT_TMP" "$UNIT_PATH"; then
  UNIT_CHANGED=0
fi
if [ "$UNIT_CHANGED" -eq 1 ]; then
  run cp -f "$UNIT_TMP" "$UNIT_PATH"
else
  say "单元与现有文件一致，跳过写入"
fi
# 内容一致也归一权限：单元得让非 root 的 systemctl status 读得到。
run chmod 0644 "$UNIT_PATH"

# ---- 4) 启用并（仅在必要时）重启，然后确认它真的活着 ----
if [ "$UNIT_CHANGED" -eq 1 ]; then
  run systemctl daemon-reload
fi
run systemctl enable "$SVC"          # 只登记开机自启，不等于「现在就跑」
if [ "$CHANGED" -eq 1 ] || [ "$UNIT_CHANGED" -eq 1 ]; then
  run systemctl restart "$SVC"
else
  run systemctl start "$SVC" || true  # 已在跑就什么都不做
fi

if [ -n "$DRY_RUN" ]; then
  say "dry-run 结束：什么都没改动。去掉 KK_INSTALL_DRY_RUN 即真正安装。"
  exit 0
fi

# 给进程一点起步时间（连 Broker 是异步的，active 不等于已上线）
for _ in 1 2 3 4 5 6 7 8 9 10; do
  systemctl is-active --quiet "$SVC" && break
  sleep 1
done
if ! systemctl is-active --quiet "$SVC"; then
  echo "!! 服务没有起来，最近日志：" >&2
  journalctl -u "$SVC" -n 30 --no-pager >&2 || true
  die "看上面日志定位（多数是 KK_SERVER 不可达或二进制与本机架构不匹配）"
fi

say "已安装并运行：$SVC（开机自启已开）"
echo
echo "还差两步才算接入机队——这两步都在服务端，不在本机："
echo "  1. 把这台机器对外可见的 IP 加进服务端 KK_AGENT_IPS 白名单，"
echo "     否则上报会被判 ip_rejected 并整帧丢弃（多网卡/NAT 时用 KK_ADVERTISE_IP 显式指定）。"
echo "  2. 在总览页确认它出现且首帧心跳已到位（指标列要有值，光在线只说明 status 帧到了）。"
echo
echo "常用排查： systemctl status $SVC / journalctl -u $SVC -f"
