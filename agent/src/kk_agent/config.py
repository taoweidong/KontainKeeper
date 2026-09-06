"""kk-agent 配置：全部来自环境变量（沿用项目约定：配置只走 KK_* 环境变量）。"""
import os
import socket
import sys

AGENT_VER = "0.2.0"
PROTO_VER = 2  # MQTT 主题布局与帧格式（v1 为旧的自研 WebSocket 协议）

DEFAULT_TOPIC_PREFIX = "kk/v1"


def _env_bool(env, key, default=False):
    return env.get(key, "").strip().lower() in ("1", "true", "yes", "on") if env.get(key) else default


def load(env=None, **overrides):
    env = dict(os.environ if env is None else env)

    def _int(key, default):
        try:
            return int(env.get(key, default))
        except (TypeError, ValueError):
            return default

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = {
        # ---- MQTT 连接 ----
        "server": env.get("KK_SERVER", "").strip(),
        "token": env.get("KK_TOKEN", "").strip(),
        "topic_prefix": env.get("KK_TOPIC_PREFIX", DEFAULT_TOPIC_PREFIX).strip(),
        "keepalive": max(10, _int("KK_KEEPALIVE", 60)),
        "tls_ca": env.get("KK_TLS_CA", "").strip(),
        "tls_insecure": _env_bool(env, "KK_TLS_INSECURE"),
        # Broker 凭据：缺省取 (KK_HOST_NAME, KK_TOKEN)——生产 ACL 的
        # `pattern kk/v1/%u/#` 要求用户名 = 主机名（代码审查 P1-2）
        "mqtt_username": env.get("KK_MQTT_USERNAME", "").strip(),
        "mqtt_password": env.get("KK_MQTT_PASSWORD", "").strip(),
        "client_id": env.get("KK_CLIENT_ID", "kk").strip(),

        # ---- 身份 ----
        # 主机名即 Agent 在管理平台上的唯一标识，克隆机请显式设置避免重复
        "host": (env.get("KK_HOST_NAME", "").strip()
                 or env.get("KK_POD_NAME", "").strip()  # 兼容旧变量名
                 or socket.gethostname()),
        "image": env.get("KK_IMAGE", "").strip(),

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
        # 断线期间 paho out-queue 的消息上限。一条 4MB 输出约 86 块，
        # 默认 512 可缓约 6 条大命令；超量回 rc=-3 失败终态而非静默丢弃。
        "max_queued": max(16, _int("KK_MAX_QUEUED", 512)),

        # ---- 自更新（独立二进制形态下生效）----
        "update_url": env.get("KK_UPDATE_URL", "").strip(),
        "update_interval": max(30, _int("KK_UPDATE_INTERVAL", 300)),
        "update_disabled": _env_bool(env, "KK_UPDATE_DISABLED"),
        "update_insecure": _env_bool(env, "KK_UPDATE_INSECURE"),
        # 可选 HMAC-SHA256 签名校验（纯标准库实现，防伪造更新）
        "update_hmac_key": env.get("KK_UPDATE_HMAC_KEY", ""),
        "update_require_sig": _env_bool(env, "KK_UPDATE_REQUIRE_SIG"),
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
    return cfg
