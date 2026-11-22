"""D1 版本信息硬化：单一真源与「默认上报」契约。

版本号曾有两个真源且互相矛盾（`config.AGENT_VER = "0.3.0"` vs
`__init__.__version__ = "0.2.0"`），而只有前者被上行帧消费 —— 后者是纯误导。
"""
from importlib.metadata import version

from kk_agent import __version__
from kk_agent.config import AGENT_VER


def test_version_single_source():
    """`kk_agent.__version__` 必须等于 `config.AGENT_VER`（D1.1）。

    锁的是「两个真源」这件事本身：任何把版本号写回 `__init__.py` 的改动都会被这条拦下。
    """
    assert __version__ == AGENT_VER


def test_wheel_metadata_version_matches_agent_ver():
    """QR1128-A9：wheel 元数据版本必须等于 config.AGENT_VER。

    pyproject 的静态 version 曾停在 0.1.0（第三个真源，落后上报版本两个版本）。
    现改 hatchling dynamic 直读 config.py——这条锁住「装出来的包元数据」与
    「真实上报版本」一致，pattern 失配或回退静态写法都会红。
    """
    assert version("kk-agent") == AGENT_VER
