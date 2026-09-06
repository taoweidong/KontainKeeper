"""KK_* 环境变量解析：服务端全部配置集中于此（项目约定：配置只走环境变量，不引入配置文件）。"""
import os
from dataclasses import dataclass

DEFAULT_BLACKLIST = "rm -rf /,mkfs,reboot,shutdown,dd if=/dev/zero,chmod -R 777 /"

# Agent 可请求的采集项白名单，须与 agent 侧 kk_agent.collector.ITEM_NAMES 一致。
# 两端各留一份常量而不是向 Agent 询问：省一次协议往返，漂移由测试抓。
COLLECT_ITEMS = ["cpu", "mem", "disk", "disk_io", "net", "proc", "user", "sys"]
COMMAND_KINDS = ["shell", "collect", "plugin_reload"]


@dataclass
class Settings:
    db_path: str
    agent_tokens: list
    admin_user: str
    admin_pass: str
    cmd_blacklist: list
    enforced_interval: int | None
    agent_bin_dir: str
    web_dir: str | None
    # ---- MQTT Broker ----
    mqtt_url: str = ""
    mqtt_prefix: str = "kk/v1"
    mqtt_client_id: str = "kk-server"
    mqtt_keepalive: int = 60
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls_ca: str = ""
    mqtt_tls_insecure: bool = False
    db_url: str = ""      # 放在末尾：不破坏既有按位置构造 Settings 的调用方


def _env_int(env, key, default):
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(env, key):
    return env.get(key, "").strip().lower() in ("1", "true", "yes", "on")


def load_settings(env=None) -> Settings:
    env = dict(os.environ if env is None else env)
    db_path = env.get("KK_DB_PATH", "kk-server.db")
    # KK_DB_URL 选库：sqlite+aiosqlite:///x.db | postgresql+asyncpg://... | mysql+aiomysql://...
    # 省略 async 驱动后缀也可以；不配则回落到 KK_DB_PATH 的 SQLite。
    db_url = env.get("KK_DB_URL", "").strip()
    agent_tokens = [t.strip() for t in env.get("KK_AGENT_TOKENS", "dev-token").split(",") if t.strip()]
    admin_user = env.get("KK_ADMIN_USER", "admin")
    admin_pass = env.get("KK_ADMIN_PASS", "admin123")
    cmd_blacklist = [p.strip().lower() for p in env.get("KK_CMD_BLACKLIST", DEFAULT_BLACKLIST).split(",") if p.strip()]
    enforced_raw = env.get("KK_ENFORCED_INTERVAL", "").strip()
    enforced_interval = int(enforced_raw) if enforced_raw.isdigit() else None
    agent_bin_dir = env.get("KK_AGENT_BIN_DIR", "agent_assets")
    web_dir = env.get("KK_WEB_DIR") or None

    if admin_pass == "admin123" and env.get("KK_ENV", "").strip().lower() == "production":
        raise RuntimeError(
            "KK_ENV=production 但仍在用默认口令 admin123，存在严重安全风险；"
            "请先通过 KK_ADMIN_PASS 设置强口令再启动")
    # 默认 Agent token 同样是公开值（仓库里写死过），生产必须换掉——
    # 否则任何人都能用已知 token 注册伪主机、读写自己的主题（代码审查 P0-2）
    if "dev-token" in agent_tokens and env.get("KK_ENV", "").strip().lower() == "production":
        raise RuntimeError(
            "KK_ENV=production 但 KK_AGENT_TOKENS 仍含默认公开值 dev-token；"
            "请为每台主机下发独立 token 再启动")
    return Settings(db_path, agent_tokens, admin_user, admin_pass, cmd_blacklist,
                    enforced_interval, agent_bin_dir, web_dir, db_url=db_url,
                    mqtt_url=env.get("KK_MQTT_URL", "").strip(),
                    # 与 Agent 的 KK_TOPIC_PREFIX 同名，双端配一个键不容易写错
                    mqtt_prefix=(env.get("KK_TOPIC_PREFIX") or "kk/v1").strip("/"),
                    # 多实例部署时此值必须逐实例唯一：MQTT 里 client_id 就是会话标识，
                    # 两个实例共用会被 Broker 判为重复连接而互相踢下线。
                    mqtt_client_id=env.get("KK_MQTT_CLIENT_ID", "kk-server").strip(),
                    mqtt_keepalive=max(10, _env_int(env, "KK_MQTT_KEEPALIVE", 60)),
                    mqtt_username=env.get("KK_MQTT_USERNAME", "").strip(),
                    mqtt_password=env.get("KK_MQTT_PASSWORD", "").strip(),
                    mqtt_tls_ca=env.get("KK_MQTT_TLS_CA", "").strip(),
                    mqtt_tls_insecure=_env_bool(env, "KK_MQTT_TLS_INSECURE"))
