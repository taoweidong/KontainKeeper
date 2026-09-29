"""config.load 的主机名校验回归：主机名直接拼进 MQTT 主题
（transport 的 base = prefix/host），含 +/# 时本 Agent 会订阅到通配符主题、
收到**其他主机**的命令并执行（安全评审 T2），必须启动即拒。
"""
import pytest

from kk_agent.config import load


@pytest.mark.parametrize("host", ["web-01", "WebA.prod_site", "a" * 119, "x1"])
def test_valid_hostname_passes(host):
    assert load(env={"KK_HOST_NAME": host})["host"] == host


@pytest.mark.parametrize("bad", ["a+b", "a#b", "a/b", "a b", "+lead", "#lead",
                                 "-lead", "中文名"])
def test_invalid_hostname_rejected(bad):
    with pytest.raises(ValueError):
        load(env={"KK_HOST_NAME": bad})


def test_empty_hostname_falls_back_to_system():
    """KK_HOST_NAME 为空 = 回退系统主机名（合法路径），只有显式非法值才拒绝。"""
    assert load(env={"KK_HOST_NAME": ""}, host="fallback-ok")["host"] == "fallback-ok"


def test_overrides_host_also_validated():
    """overrides 传入的 host 与环境变量同受校验（校验必须发生在合并之后）。"""
    with pytest.raises(ValueError):
        load(env={"KK_HOST_NAME": "ok"}, host="bad#name")
