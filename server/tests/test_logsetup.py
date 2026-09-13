"""A7 服务端日志装配用例：幂等、stdlib 拦截、sink 归属与 KK_LOG* 环境变量。

这几条是最容易「静默失效」的契约，所以单独锁住：
- **幂等**：`create_app` 在整套测试里被调用数百次（function 级 fixture）。若每次
  `logger.add()`，sink 会线性增长 → 一条日志打几百遍、输出爆炸，且真实故障被淹没。
- **拦截**：不接 `InterceptHandler`，且不把 uvicorn 的 `log_config` 关掉，uvicorn
  自带的 dictConfig 会盖掉我们的格式，「全项目日志统一」静默失效。
- **归属**：`KK_LOG` 留空时服务端只写 stdout（容器采集），不落盘、不与 Agent 抢文件。
"""
import json
import logging
import types
from pathlib import Path

import pytest
from loguru import logger as _logger

from kk_server import logsetup
from kk_server.config import load_settings
from kk_server.main import create_app


def _stub(**kw):
    """setup_logging 只按属性取配置，用轻量替身即可，避免构造完整 Settings。"""
    opts = {"log_level": "INFO", "log_path": "", "log_json": False}
    opts.update(kw)
    return types.SimpleNamespace(**opts)


@pytest.fixture
def fresh_logsetup():
    """把模块级配置态还原成「未配置」，并在收尾卸载本次加的 sink。

    不能只清 `_CONFIGURED`：现有 sink 若不卸载会一直留在 loguru 的全局单例里，
    后续用例的每一条日志都会多打一份。
    """
    for sid in list(logsetup._SINK_IDS):
        try:
            _logger.remove(sid)
        except ValueError:
            pass
    logsetup._SINK_IDS.clear()
    logsetup._CONFIGURED = False
    yield logsetup
    for sid in list(logsetup._SINK_IDS):
        try:
            _logger.remove(sid)
        except ValueError:
            pass
    logsetup._SINK_IDS.clear()
    logsetup._CONFIGURED = False


def _capture(level="TRACE"):
    """挂内存 sink 收集记录（带 patcher，与生产配置同构）。

    注意 patcher 走 `Logger.patch()`——它是 Logger 级配置，`add()` 不接受 patch 参数。
    """
    records = []
    sid = _logger.patch(logsetup._patch).add(
        lambda m: records.append(m.record), level=level, format="{message}")
    return records, sid


def test_setup_logging_idempotent_across_create_app(fresh_logsetup, tmp_path):
    """create_app 被反复调用，sink 数不得增长（否则日志重复 N 遍）。"""
    env = {"KK_DB_PATH": str(tmp_path / "idem.db")}
    create_app(env)
    first = list(fresh_logsetup._SINK_IDS)
    assert first, "stdout sink 必须挂上"
    create_app(env)
    assert fresh_logsetup._SINK_IDS == first

    # 直接调 setup_logging 同样幂等（两条入口都走同一道门）
    fresh_logsetup.setup_logging(_stub())
    assert fresh_logsetup._SINK_IDS == first


def test_stdlib_logs_are_intercepted(fresh_logsetup):
    """uvicorn / paho 等 stdlib 记录必须转投 loguru，且 component 取 logger 名。"""
    fresh_logsetup.setup_logging(_stub())
    records, sid = _capture()
    try:
        logging.getLogger("uvicorn.error").warning("boom %s", "x")
        logging.getLogger("uvicorn.access").info("access line")
    finally:
        _logger.remove(sid)

    hit = [r for r in records if r["message"] == "boom x"]
    assert hit, "uvicorn 的记录没有转发到 loguru（InterceptHandler 未生效）"
    assert hit[0]["level"].name == "WARNING"
    # component 取 stdlib 的 logger 名；若落成 "logging"，日志里分不清来源
    assert hit[0]["extra"]["component"] == "uvicorn.error"
    assert any(r["message"] == "access line"
               and r["extra"]["component"] == "uvicorn.access" for r in records)


def test_plain_file_sink_renders_bound_context(fresh_logsetup, tmp_path):
    """bind() 的上下文要在**纯文本** sink 里可见。

    loguru 的 extra 只在 serialize（JSON）模式下自然可见；不显式渲染 ctx 的话，
    `log.bind(host=...)` 在容器日志里等于没绑。
    """
    path = tmp_path / "plain.log"
    fresh_logsetup.setup_logging(_stub(log_path=str(path)))
    logsetup.get_logger("kk.bridge").bind(host="web01", cmd="c1").info("命令入队失败")
    line = path.read_text("utf-8").strip()
    # ctx 按 key 排序拼装：与调用点传参顺序无关，日志才能稳定 grep/比对
    assert "kk.bridge cmd=c1 host=web01 命令入队失败" in line


def test_server_json_sink_when_kk_log_json(fresh_logsetup, tmp_path):
    """KK_LOG_JSON=1 → JSON Lines，且 bind 的上下文进 record.extra（供日志平台取）。"""
    path = tmp_path / "kk.json.log"
    fresh_logsetup.setup_logging(_stub(log_path=str(path), log_json=True))
    logsetup.get_logger("kk.test").bind(host="web01").info("hello %s", "json")
    _logger.complete()

    lines = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    assert lines, "JSON sink 没有写入任何行"
    rec = lines[-1]["record"]
    assert rec["message"] == "hello json"        # 适配层已做 %s 格式化
    assert rec["extra"]["component"] == "kk.test"
    assert rec["extra"]["host"] == "web01"
    assert rec["extra"]["ctx"] == " host=web01"


def test_log_env_vars_are_parsed():
    """KK_LOG / KK_LOG_LEVEL / KK_LOG_JSON 必须落到 Settings（配置只走环境变量）。"""
    s = load_settings({"KK_LOG_LEVEL": "debug", "KK_LOG": " /tmp/kk.log ",
                       "KK_LOG_JSON": "1"})
    assert (s.log_level, s.log_path, s.log_json) == ("DEBUG", "/tmp/kk.log", True)

    d = load_settings({})
    assert (d.log_level, d.log_path, d.log_json) == ("INFO", "", False)


def test_no_stdlib_logging_outside_logsetup():
    """静态回归锁（A7.6）：除 logsetup.py 外不得再 import logging / getLogger。

    没有这道锁，新代码很容易顺手写回 `logging.getLogger`，于是同一进程里跑着
    两套后端：一半日志有 component、一半没有，格式与轮转策略各说各话。
    """
    src = Path(logsetup.__file__).resolve().parent
    offenders = sorted(p.name for p in src.rglob("*.py")
                       if p.name != "logsetup.py"
                       and ("import logging" in p.read_text("utf-8")
                            or "logging.getLogger" in p.read_text("utf-8")))
    assert not offenders, (
        "A7 约定：仅 logsetup.py 可 import logging（InterceptHandler 需要）；"
        "其余模块改用 logsetup.get_logger()。违规文件：%s" % offenders)
