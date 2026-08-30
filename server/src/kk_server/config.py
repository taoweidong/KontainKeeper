"""KK_* 环境变量解析：服务端全部配置集中于此（项目约定：配置只走环境变量，不引入配置文件）。"""
import os
from dataclasses import dataclass

DEFAULT_BLACKLIST = "rm -rf /,mkfs,reboot,shutdown,dd if=/dev/zero,chmod -R 777 /"


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


def load_settings(env=None) -> Settings:
    env = dict(os.environ if env is None else env)
    db_path = env.get("KK_DB_PATH", "kk-server.db")
    agent_tokens = [t.strip() for t in env.get("KK_AGENT_TOKENS", "dev-token").split(",") if t.strip()]
    admin_user = env.get("KK_ADMIN_USER", "admin")
    admin_pass = env.get("KK_ADMIN_PASS", "admin")
    cmd_blacklist = [p.strip().lower() for p in env.get("KK_CMD_BLACKLIST", DEFAULT_BLACKLIST).split(",") if p.strip()]
    enforced_raw = env.get("KK_ENFORCED_INTERVAL", "").strip()
    enforced_interval = int(enforced_raw) if enforced_raw.isdigit() else None
    agent_bin_dir = env.get("KK_AGENT_BIN_DIR", "agent_assets")
    web_dir = env.get("KK_WEB_DIR") or None

    if admin_pass == "admin" and os.environ.get("KK_ENV", "").lower() == "production":
        raise RuntimeError(
            "KK_ENV=production 但仍在用默认口令 admin，存在严重安全风险；"
            "请先通过 KK_ADMIN_PASS 设置强口令再启动")
    return Settings(db_path, agent_tokens, admin_user, admin_pass, cmd_blacklist,
                    enforced_interval, agent_bin_dir, web_dir)