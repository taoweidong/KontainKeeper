"""kk-agent 配置：全部来自环境变量（沿用项目约定：配置只走 KK_* 环境变量）。"""
import os
import re
import socket
import sys

AGENT_VER = "0.3.0"
# v4 = 上报对象从「K8S 容器 IDE」扩为任意 Linux 主机：status 帧新增 env/group/labels/caps，
#     服务端接受 v3/v4 双版本窗口（见 kk_server.ACCEPT_PROTO_VERS）
PROTO_VER = 4

DEFAULT_TOPIC_PREFIX = "kk/v1"

# 主机名直接拼进 MQTT 主题（transport 的 base = prefix/host），必须排除主题
# 通配符与层级分隔符：含 +/# 会让本 Agent 订阅到通配符主题、收到**其他主机**
# 的命令并执行；含 / 会破坏主题层级（安全评审 T2）。首字符限字母数字。
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,118}$")


def _env_bool(env, key, default=False):
    return env.get(key, "").strip().lower() in ("1", "true", "yes", "on") if env.get(key) else default


def _parse_labels(raw):
    """`KK_LABELS` → dict：`k=v,k2=v2`。

    标签是机队筛选的主入口（500 台只按名字搜等于没有筛选），因此解析要宽容：
    无 `=` 的片段按 `key: ""` 收下、重复 key 后者覆盖、非法字符不影响其余项。
    """
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, val = part.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key[:64]] = val.strip()[:128]
    return out


def load(env=None, **overrides):
    env = dict(os.environ if env is None else env)

    def _int(key, default):
        try:
            return int(env.get(key, default))
        except (TypeError, ValueError):
            return default

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = {
        # ---- MQTT 连接（默认匿名 Broker；启用 Broker 鉴权时配用户名/口令，
        #      与服务端 KK_MQTT_USERNAME 同一套账目，见 docs/deployment.md）----
        "server": env.get("KK_SERVER", "").strip(),
        "mqtt_username": env.get("KK_MQTT_USERNAME", "").strip(),
        "mqtt_password": env.get("KK_MQTT_PASSWORD", ""),
        "topic_prefix": env.get("KK_TOPIC_PREFIX", DEFAULT_TOPIC_PREFIX).strip(),
        "keepalive": max(10, _int("KK_KEEPALIVE", 60)),
        "tls_ca": env.get("KK_TLS_CA", "").strip(),
        "tls_insecure": _env_bool(env, "KK_TLS_INSECURE"),
        "client_id": env.get("KK_CLIENT_ID", "kk").strip(),
        # 出口 IP 自报值（写入 status/hb/result 帧供服务端白名单校验）：
        # 缺省自动探测（UDP connect 到 Broker 选出可达网卡的本地地址），
        # 多网卡/NAT 下探测不准时可用 KK_ADVERTISE_IP 显式覆盖
        "advertise_ip": env.get("KK_ADVERTISE_IP", "").strip(),

        # ---- 身份 ----
        # 主机名即 Agent 在管理平台上的唯一标识，克隆机请显式设置避免重复
        "host": (env.get("KK_HOST_NAME", "").strip()
                 or env.get("KK_POD_NAME", "").strip()  # 兼容旧变量名
                 or socket.gethostname()),
        "image": env.get("KK_IMAGE", "").strip(),
        # 机队分组与标签（v4）：只来自环境变量，不引入配置文件（项目约定）。
        # group 是单值长串维度（一个组），labels 是 k=v 多值维度（可多维筛选）
        "group": env.get("KK_GROUP", "").strip()[:64],
        "labels": _parse_labels(env.get("KK_LABELS", "")),

        # ---- 采集 ----
        "interval": max(1, _int("KK_INTERVAL", 60)),
        "disk_paths": [p.strip() for p in env.get("KK_DISK_PATHS", "").split(",") if p.strip()],
        "top_n": max(1, min(_int("KK_TOP_N", 5), 50)),
        # 心跳采集项（KK_HB_ITEMS）：逗号分隔，取值同 kind=collect 白名单
        # （cpu,mem,disk,disk_io,net,proc,user,sys）；空=全采。千进程主机
        # 可去掉 proc 项以削掉全进程遍历开销（资源评审 P3）
        "hb_items": [s.strip() for s in env.get("KK_HB_ITEMS", "").split(",") if s.strip()],
        "plugin_dir": env.get("KK_PLUGIN_DIR", "") or os.path.join(here, "plugins"),
        # 插件 collect() 超时（秒）：卡死的插件被隔离到 mtime 变化重载为止，
        # 不再逐心跳泄漏执行线程（资源评审 P2）
        "plugin_timeout": max(1, _int("KK_PLUGIN_TIMEOUT", 5)),

        # ---- 命令执行 ----
        "max_out_mb": max(1, _int("KK_MAX_OUT_MB", 4)),
        "max_workers": max(1, min(_int("KK_MAX_WORKERS", 8), 64)),
        # 断线期间 paho out-queue 的消息上限（预算口径见 transport.MAX_QUEUED 注释）：
        # 一条 4MB 输出约 86 块，默认 128 可缓约 1.5 条大命令 ≈8MB，够装下一轮
        # 应急回执；超量回 rc=-3 失败终态而非静默丢弃，也不把 RSS 顶出 25–35MB 口径。
        "max_queued": max(16, _int("KK_MAX_QUEUED", 128)),

        # ---- 自更新（独立二进制形态下生效）----
        "update_url": env.get("KK_UPDATE_URL", "").strip(),
        "update_interval": max(30, _int("KK_UPDATE_INTERVAL", 300)),
        "update_disabled": _env_bool(env, "KK_UPDATE_DISABLED"),
        "update_insecure": _env_bool(env, "KK_UPDATE_INSECURE"),
        # 可选 HMAC-SHA256 签名校验（纯标准库实现，防伪造更新）
        "update_hmac_key": env.get("KK_UPDATE_HMAC_KEY", ""),
        "update_require_sig": _env_bool(env, "KK_UPDATE_REQUIRE_SIG"),
        # 推送更新的未签名清单是否放行（QR-P0-1）：配了 KK_UPDATE_HMAC_KEY 时
        # 本键无意义（签名强制）；未配 key 时推送路径默认拒绝未签名清单，
        # 内网可信部署显式置 1 才放行。HTTP 轮询路径不受此键影响。
        "update_allow_unsigned": _env_bool(env, "KK_UPDATE_ALLOW_UNSIGNED"),
        "agent_bin": env.get("KK_AGENT_BIN", "").strip(),
        # shell 模式（允许管道/重定向）开关，服务器运维场景常用；置 0 可彻底关闭
        "allow_shell": env.get("KK_ALLOW_SHELL", "1").strip().lower() not in ("0", "false", "no", "off"),

        # ---- 日志 ----
        "log_path": env.get("KK_LOG", ""),
        "log_level": env.get("KK_LOG_LEVEL", "INFO").upper(),
    }
    # agent_bin 缺省时不在配置里填 sys.executable——那会让自更新误把 Python 解释器
    # 当成待替换的二进制（见 updater.apply_manifest）。缺省即表示「不自替换」。
    cfg.update(overrides)
    # 校验放在 overrides 合并之后：测试与嵌入式调用可能经 overrides 传入主机名
    if not _HOST_RE.match(cfg["host"]):
        raise ValueError(
            "主机名 %r 含 MQTT 主题非法字符（只允许字母数字与 . _ -，且以字母数字开头）；"
            "请设置 KK_HOST_NAME 为合法值" % cfg["host"])
    return cfg
