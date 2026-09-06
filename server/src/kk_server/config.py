"""KK_* 环境变量解析：服务端全部配置集中于此（项目约定：配置只走环境变量，不引入配置文件）。"""
import ipaddress
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
    admin_user: str
    admin_pass: str
    cmd_blacklist: list
    enforced_interval: int | None
    agent_bin_dir: str
    web_dir: str | None
    # Agent 接入白名单（v3：替代原 token 认证）。ipaddress.ip_network 对象列表，
    # 空列表 = 不设限（开发/测试模式，允许所有上报）
    agent_ips: list
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


def parse_ip_whitelist(raw):
    """解析 KK_AGENT_IPS：逗号分隔的 IP / CIDR 混合列表 → ip_network 对象列表。

    单个 IP 自动按 /32（IPv6 按 /128）处理；格式非法直接抛 ValueError——
    白名单写错时启动即失败，好过静默放行或全拒。
    """
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def ip_in_whitelist(networks, ip_str):
    """校验自报 ip 是否命中白名单。

    MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，白名单基于 Agent 自报值
    （适合内网可信环境）；格式非法的 ip 一律视为不在白名单。
    """
    if not networks:
        return True   # 未配置白名单 = 不设限（开发/测试模式）
    try:
        addr = ipaddress.ip_address(str(ip_str or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def load_settings(env=None) -> Settings:
    env = dict(os.environ if env is None else env)
    db_path = env.get("KK_DB_PATH", "kk-server.db")
    # KK_DB_URL 选库：sqlite+aiosqlite:///x.db | postgresql+asyncpg://... | mysql+aiomysql://...
    # 省略 async 驱动后缀也可以；不配则回落到 KK_DB_PATH 的 SQLite。
    db_url = env.get("KK_DB_URL", "").strip()
    admin_user = env.get("KK_ADMIN_USER", "admin")
    admin_pass = env.get("KK_ADMIN_PASS", "admin123")
    cmd_blacklist = [p.strip().lower() for p in env.get("KK_CMD_BLACKLIST", DEFAULT_BLACKLIST).split(",") if p.strip()]
    enforced_raw = env.get("KK_ENFORCED_INTERVAL", "").strip()
    enforced_interval = int(enforced_raw) if enforced_raw.isdigit() else None
    agent_bin_dir = env.get("KK_AGENT_BIN_DIR", "agent_assets")
    web_dir = env.get("KK_WEB_DIR") or None
    # Agent 接入白名单（v3：替代原 KK_AGENT_TOKENS token 池）
    agent_ips = parse_ip_whitelist(env.get("KK_AGENT_IPS", ""))

    if admin_pass == "admin123" and env.get("KK_ENV", "").strip().lower() == "production":
        raise RuntimeError(
            "KK_ENV=production 但仍在用默认口令 admin123，存在严重安全风险；"
            "请先通过 KK_ADMIN_PASS 设置强口令再启动")
    # 生产必须显式配置 IP 白名单：不配等于对所有上报放行，与内网可信前提
    # 之外的生产环境不匹配（替代原 dev-token 自检）
    if not agent_ips and env.get("KK_ENV", "").strip().lower() == "production":
        raise RuntimeError(
            "KK_ENV=production 但 KK_AGENT_IPS 未配置（白名单为空 = 放行所有上报）；"
            "请设置 KK_AGENT_IPS（逗号分隔 IP/CIDR，如 10.0.0.0/24,192.168.1.5）再启动")
    return Settings(db_path, admin_user, admin_pass, cmd_blacklist,
                    enforced_interval, agent_bin_dir, web_dir, agent_ips,
                    db_url=db_url,
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
