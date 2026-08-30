"""极简日志：stderr + 可选 1MB 轮转文件，避免长驻吃磁盘。"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s %(levelname)s %(message)s"


def get_logger(path="", level="INFO", name="kk-agent"):
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    if logger.handlers:  # 已初始化（reload 场景）
        return logger
    fmt = logging.Formatter(_FMT)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if path and path != "-":
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            fh = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=1)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            logger.warning("cannot open log file %s, stderr only", path)
    return logger
