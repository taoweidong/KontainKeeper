"""kk-agent 配置：全部来自环境变量，镜像构建期注入。"""
import os
import socket

AGENT_VER = "0.1.0"
PROTO_VER = 1


def load(env=None, **overrides):
    env = dict(os.environ if env is None else env)

    def _int(key, default):
        try:
            return int(env.get(key, default))
        except (TypeError, ValueError):
            return default

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = {
        "server": env.get("KK_SERVER", "").strip(),
        "token": env.get("KK_TOKEN", "").strip(),
        "interval": max(1, _int("KK_INTERVAL", 60)),
        "disk_paths": [p.strip() for p in env.get("KK_DISK_PATHS", "/,/workspace").split(",") if p.strip()],
        "plugin_dir": env.get("KK_PLUGIN_DIR", "") or os.path.join(here, "plugins"),
        "fs_root": env.get("KK_FS_ROOT", "/").rstrip("/") or "/",
        "log_path": env.get("KK_LOG", ""),
        "log_level": env.get("KK_LOG_LEVEL", "INFO").upper(),
        "image": env.get("KK_IMAGE", ""),
        "hostname": env.get("KK_POD_NAME", "") or socket.gethostname(),
        "max_out_mb": max(1, _int("KK_MAX_OUT_MB", 4)),
    }
    cfg.update(overrides)
    return cfg
